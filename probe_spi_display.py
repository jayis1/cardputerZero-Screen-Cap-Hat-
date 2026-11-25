#!/usr/bin/env python3
"""
Live zero-reboot SPI display probe for M5Stack Cardputer Zero (EXT 2.54-14P).

Drives the secondary display over /dev/spidev0.1 using pure Python stdlib
(no pip packages required) so you can verify wiring, backlight, chipset,
RGB/BGR order, color inversion, and orientation BEFORE installing a DT overlay.

Default pin mapping (BCM):
  SPI0 CE1 : /dev/spidev0.1 (GPIO7)
  SPI0 CLK : GPIO11
  SPI0 MOSI: GPIO10
  DC       : GPIO22
  RST      : GPIO26
  BL       : GPIO23
  5V VCC   : Pin 6 (M5IOE1 ext_5v_out)
"""

import argparse
import array
import fcntl
import glob
import os
import struct
import sys
import time

# Linux spidev ioctl constants (aarch64 / arm / x86_64)
SPI_IOC_MAGIC = ord("k")
SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04

EXT_5V_LED = "/sys/class/leds/ext_5v_out/brightness"
EXT_USB_MUX = "/sys/class/leds/ext_usb_gpio_fun/brightness"


def read_int(path):
    with open(path, "r", encoding="utf-8") as f:
        return int(f.read().strip())


def write_text(path, value):
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(value))


def find_bcm_gpio_base():
    """Find the Linux sysfs GPIO base for the SoC BCM2835 GPIO controller."""
    for chip in sorted(glob.glob("/sys/class/gpio/gpiochip*")):
        try:
            dev_link = os.path.realpath(os.path.join(chip, "device"))
            label = open(os.path.join(chip, "label"), encoding="utf-8").read().strip()
            if "pinctrl-bcm" in label or "3f200000.gpio" in dev_link or "fe200000.gpio" in dev_link:
                return int(open(os.path.join(chip, "base"), encoding="utf-8").read().strip())
        except OSError:
            continue
    return 512


class SysfsGpio:
    """Minimal GPIO output controller using /sys/class/gpio (no external deps)."""

    def __init__(self, bcm_pins):
        self.base = find_bcm_gpio_base()
        self.exported = []
        self.paths = {}
        for bcm in bcm_pins:
            num = self.base + bcm
            gpio_dir = f"/sys/class/gpio/gpio{num}"
            if not os.path.exists(gpio_dir):
                write_text("/sys/class/gpio/export", num)
                self.exported.append(num)
                for _ in range(50):
                    if os.path.exists(os.path.join(gpio_dir, "direction")):
                        break
                    time.sleep(0.01)
            write_text(os.path.join(gpio_dir, "direction"), "out")
            self.paths[bcm] = os.path.join(gpio_dir, "value")

    def write(self, bcm, value):
        write_text(self.paths[bcm], "1" if value else "0")

    def close(self):
        for num in self.exported:
            try:
                write_text("/sys/class/gpio/unexport", num)
            except OSError:
                pass


class RawSpiDev:
    """Minimal /dev/spidevX.Y writer using fcntl.ioctl."""

    def __init__(self, path="/dev/spidev0.1", mode=0, speed_hz=24000000):
        self.fd = os.open(path, os.O_RDWR)
        fcntl.ioctl(self.fd, SPI_IOC_WR_MODE, struct.pack("B", mode))
        fcntl.ioctl(self.fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
        fcntl.ioctl(self.fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", speed_hz))

    def write(self, data, chunk_size=4096):
        mv = memoryview(data)
        for i in range(0, len(mv), chunk_size):
            os.write(self.fd, mv[i : i + chunk_size])

    def close(self):
        os.close(self.fd)


def madctl_for(rotation, bgr=False):
    # MADCTL bits: MY=0x80, MX=0x40, MV=0x20, BGR=0x08
    rot_map = {
        0: 0x00,
        90: 0x60,   # MX | MV (landscape)
        180: 0xC0,  # MY | MX
        270: 0xA0,  # MY | MV (landscape flipped)
    }
    if rotation not in rot_map:
        raise ValueError("rotation must be 0, 90, 180, or 270")
    return rot_map[rotation] | (0x08 if bgr else 0x00)


PRESETS = {
    "st7789-320x240": {
        "desc": "ST7789V 2.0\"/2.4\" IPS 320x240 landscape (GMT020-02 style, INVON)",
        "controller": "st7789",
        "width": 320,
        "height": 240,
        "rotation": 90,
        "bgr": False,
        "invert": True,
        "x_offset": 0,
        "y_offset": 0,
    },
    "st7789-320x240-noinv": {
        "desc": "ST7789V 320x240 landscape without color inversion (INVOFF)",
        "controller": "st7789",
        "width": 320,
        "height": 240,
        "rotation": 90,
        "bgr": False,
        "invert": False,
        "x_offset": 0,
        "y_offset": 0,
    },
    "ili9341-320x240": {
        "desc": "ILI9341 2.2\"/2.4\"/2.8\" 320x240 landscape (BGR, INVOFF)",
        "controller": "ili9341",
        "width": 320,
        "height": 240,
        "rotation": 90,
        "bgr": True,
        "invert": False,
        "x_offset": 0,
        "y_offset": 0,
    },
    "st7789-240x240": {
        "desc": "ST7789V 1.3\"/1.54\" 240x240 square IPS (INVON)",
        "controller": "st7789",
        "width": 240,
        "height": 240,
        "rotation": 0,
        "bgr": False,
        "invert": True,
        "x_offset": 0,
        "y_offset": 0,
    },
    "st7735-160x128": {
        "desc": "ST7735S 1.8\" 160x128 landscape",
        "controller": "st7735",
        "width": 160,
        "height": 128,
        "rotation": 90,
        "bgr": False,
        "invert": False,
        "x_offset": 0,
        "y_offset": 0,
    },
}


class SpiDisplay:
    def __init__(self, spi, gpio, dc_pin, rst_pin, bl_pin):
        self.spi = spi
        self.gpio = gpio
        self.dc = dc_pin
        self.rst = rst_pin
        self.bl = bl_pin

    def cmd(self, opcode, data=b""):
        self.gpio.write(self.dc, 0)
        self.spi.write(bytes([opcode]))
        if data:
            self.gpio.write(self.dc, 1)
            self.spi.write(bytes(data))

    def hw_reset(self):
        self.gpio.write(self.rst, 1)
        time.sleep(0.02)
        self.gpio.write(self.rst, 0)
        time.sleep(0.05)
        self.gpio.write(self.rst, 1)
        time.sleep(0.15)

    def backlight(self, on):
        self.gpio.write(self.bl, 1 if on else 0)

    def init_panel(self, controller, madctl, invert):
        self.hw_reset()
        self.cmd(0x01)  # SWRESET
        time.sleep(0.15)
        self.cmd(0x11)  # SLPOUT
        time.sleep(0.12)

        if controller == "ili9341":
            self.cmd(0xCF, [0x00, 0xC1, 0x30])
            self.cmd(0xED, [0x64, 0x03, 0x12, 0x81])
            self.cmd(0xE8, [0x85, 0x00, 0x78])
            self.cmd(0xCB, [0x39, 0x2C, 0x00, 0x34, 0x02])
            self.cmd(0xF7, [0x20])
            self.cmd(0xEA, [0x00, 0x00])
            self.cmd(0xC0, [0x23])
            self.cmd(0xC1, [0x10])
            self.cmd(0xC5, [0x3E, 0x28])
            self.cmd(0xC7, [0x86])
            self.cmd(0xB1, [0x00, 0x18])
            self.cmd(0xB6, [0x08, 0x82, 0x27])
            self.cmd(0xF2, [0x00])
            self.cmd(0x26, [0x01])
        elif controller == "st7789":
            self.cmd(0xB2, [0x0C, 0x0C, 0x00, 0x33, 0x33])
            self.cmd(0xB7, [0x35])
            self.cmd(0xBB, [0x19])
            self.cmd(0xC0, [0x2C])
            self.cmd(0xC2, [0x01])
            self.cmd(0xC3, [0x12])
            self.cmd(0xC4, [0x20])
            self.cmd(0xC6, [0x0F])
            self.cmd(0xD0, [0xA4, 0xA1])
        elif controller == "st7735":
            self.cmd(0xB1, [0x01, 0x2C, 0x2D])
            self.cmd(0xB2, [0x01, 0x2C, 0x2D])
            self.cmd(0xB3, [0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D])
            self.cmd(0xB4, [0x07])
            self.cmd(0xC0, [0xA2, 0x02, 0x84])
            self.cmd(0xC1, [0xC5])
            self.cmd(0xC2, [0x0A, 0x00])
            self.cmd(0xC3, [0x8A, 0x2A])
            self.cmd(0xC4, [0x8A, 0xEE])
            self.cmd(0xC5, [0x0E])

        self.cmd(0x3A, [0x55])     # RGB565 (16-bit)
        self.cmd(0x36, [madctl])   # MADCTL
        self.cmd(0x21 if invert else 0x20)
        self.cmd(0x13)             # NORON
        time.sleep(0.01)
        self.cmd(0x29)             # DISPON
        time.sleep(0.05)

    def push_rgb565(self, width, height, buf, x_off=0, y_off=0):
        x0, x1 = x_off, x_off + width - 1
        y0, y1 = y_off, y_off + height - 1
        self.cmd(0x2A, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self.cmd(0x2B, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self.cmd(0x2C)
        self.gpio.write(self.dc, 1)
        self.spi.write(buf)


def rgb565_be(r, g, b):
    val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return struct.pack(">H", val)


def build_test_pattern(width, height):
    """
    Draws:
      - 3px white border around exact screen bounds (checks resolution/offsets)
      - 3 vertical bands: RED (left), GREEN (middle), BLUE (right) (checks RGB/BGR + inversion)
      - 28x28 YELLOW square in TOP-LEFT corner (checks rotation/flip)
      - 28x28 CYAN square in BOTTOM-RIGHT corner
    """
    red = rgb565_be(255, 0, 0)
    green = rgb565_be(0, 255, 0)
    blue = rgb565_be(0, 0, 255)
    white = rgb565_be(255, 255, 255)
    yellow = rgb565_be(255, 255, 0)
    cyan = rgb565_be(0, 255, 255)
    black = rgb565_be(0, 0, 0)

    w1 = width // 3
    w2 = (2 * width) // 3
    rows = []
    for y in range(height):
        if y < 3 or y >= height - 3:
            rows.append(white * width)
            continue
        row = bytearray()
        for x in range(width):
            if x < 3 or x >= width - 3:
                px = white
            elif x < 31 and y < 31:
                px = yellow
            elif x >= width - 31 and y >= height - 31:
                px = cyan
            elif abs(y - height // 2) <= 1:
                px = black
            elif x < w1:
                px = red
            elif x < w2:
                px = green
            else:
                px = blue
            row.extend(px)
        rows.append(bytes(row))
    return b"".join(rows)


def check_power_and_mux():
    if os.path.exists(EXT_USB_MUX):
        mux = read_int(EXT_USB_MUX)
        if mux != 0:
            print(f"[WARN] {EXT_USB_MUX}={mux} (USB4 mode active; GPIO23/GPIO26 may be muxed away!)")
            print(f"       Run: echo 0 | sudo tee {EXT_USB_MUX}")
        else:
            print("[OK]   ext_usb_gpio_fun=0 (GPIO23/GPIO26 in GPIO mode)")

    if os.path.exists(EXT_5V_LED):
        val = read_int(EXT_5V_LED)
        if val == 0:
            print("[INFO] Enabling Pin 6 5V rail via ext_5v_out...")
            write_text(EXT_5V_LED, 255)
            time.sleep(0.2)
        print(f"[OK]   ext_5v_out={read_int(EXT_5V_LED)} (Pin 6 5V OUT active)")


def main():
    parser = argparse.ArgumentParser(description="Live SPI display probe for Cardputer Zero EXT header")
    parser.add_argument("--preset", choices=sorted(PRESETS.keys()), default="st7789-320x240")
    parser.add_argument("--list-presets", action="store_true", help="List available display presets")
    parser.add_argument("--spidev", default="/dev/spidev0.1")
    parser.add_argument("--speed", type=int, default=24000000, help="SPI clock in Hz (default: 24000000)")
    parser.add_argument("--spi-mode", type=int, choices=[0, 1, 2, 3], default=0)
    parser.add_argument("--dc", type=int, default=22, help="BCM GPIO for DC (default: 22)")
    parser.add_argument("--rst", type=int, default=26, help="BCM GPIO for RST (default: 26)")
    parser.add_argument("--bl", type=int, default=23, help="BCM GPIO for BL (default: 23)")
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--rotation", type=int, choices=[0, 90, 180, 270], default=None)
    parser.add_argument("--bgr", action="store_true", default=None, help="Force BGR color order")
    parser.add_argument("--rgb", action="store_true", help="Force RGB color order")
    parser.add_argument("--invert", action="store_true", default=None, help="Force display inversion ON")
    parser.add_argument("--no-invert", action="store_true", help="Force display inversion OFF")
    parser.add_argument("--x-offset", type=int, default=None)
    parser.add_argument("--y-offset", type=int, default=None)
    parser.add_argument("--blink-bl", action="store_true", help="Blink backlight twice to verify GPIO23")
    args = parser.parse_args()

    if args.list_presets:
        for name, cfg in PRESETS.items():
            print(f"  {name:22s} : {cfg['desc']}")
        return

    if not os.path.exists(args.spidev):
        print(f"[ERR] {args.spidev} not found. (If Option B overlay is already loaded, /dev/fb_cap owns spi0.1.)")
        sys.exit(1)

    cfg = dict(PRESETS[args.preset])
    width = args.width or cfg["width"]
    height = args.height or cfg["height"]
    rotation = cfg["rotation"] if args.rotation is None else args.rotation
    bgr = True if args.bgr else (False if args.rgb else cfg["bgr"])
    invert = False if args.no_invert else (True if args.invert else cfg["invert"])
    x_off = cfg["x_offset"] if args.x_offset is None else args.x_offset
    y_off = cfg["y_offset"] if args.y_offset is None else args.y_offset
    madctl = madctl_for(rotation, bgr=bgr)

    print(f"=== Cardputer Zero EXT Display Probe ===")
    print(f"Preset    : {args.preset} ({cfg['controller'].upper()} {width}x{height})")
    print(f"SPI       : {args.spidev} @ {args.speed // 1_000_000} MHz, mode={args.spi_mode}")
    print(f"GPIOs     : DC={args.dc}, RST={args.rst}, BL={args.bl}")
    print(f"Options   : rotation={rotation}, bgr={bgr}, invert={invert}, madctl=0x{madctl:02X}, offset=({x_off},{y_off})")

    check_power_and_mux()

    gpio = SysfsGpio([args.dc, args.rst, args.bl])
    spi = RawSpiDev(args.spidev, mode=args.spi_mode, speed_hz=args.speed)
    try:
        disp = SpiDisplay(spi, gpio, args.dc, args.rst, args.bl)

        if args.blink_bl:
            print("[TEST] Blinking backlight on GPIO23...")
            for _ in range(2):
                disp.backlight(False)
                time.sleep(0.35)
                disp.backlight(True)
                time.sleep(0.35)
        else:
            disp.backlight(True)

        print("[INIT] Sending controller reset + init sequence...")
        disp.init_panel(cfg["controller"], madctl=madctl, invert=invert)

        print("[DRAW] Pushing RGB + border + corner orientation test pattern...")
        frame = build_test_pattern(width, height)
        t0 = time.monotonic()
        disp.push_rgb565(width, height, frame, x_off=x_off, y_off=y_off)
        dt = (time.monotonic() - t0) * 1000
        print(f"[DONE] Frame sent in {dt:.1f} ms!")
        print()
        print("Check the top screen:")
        print("  - White border on all 4 edges?  -> Resolution & offsets are exact!")
        print("  - Left=RED, Mid=GREEN, Right=BLUE? -> If Red/Blue swapped, pass --bgr / --rgb.")
        print("  - Colors look inverted (cyan/magenta/yellow instead of R/G/B)? -> Pass --no-invert / --invert.")
        print("  - YELLOW square in TOP-LEFT, CYAN in BOTTOM-RIGHT? -> If rotated, try --rotation 270 / 0 / 180.")
        print("  - Still white? -> Try --preset ili9341-320x240 or --spi-mode 3.")
    finally:
        spi.close()
        gpio.close()


if __name__ == "__main__":
    main()
