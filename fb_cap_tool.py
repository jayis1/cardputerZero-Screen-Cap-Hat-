#!/usr/bin/env python3
"""
Pure-Python framebuffer helper for the Cardputer Zero secondary CAP display
(/dev/fb_cap or /dev/fb1). Zero pip dependencies required.

Subcommands:
  clear   Fill framebuffer with black (used on clean shutdown)
  test    Draw RGB bars, white border, corner markers, and FB geometry text
  status  Live lightweight telemetry dashboard (CPU, RAM, Temp, IP, Battery, Uptime)
"""

import argparse
import fcntl
import mmap
import os
import signal
import socket
import struct
import sys
import time

# Linux framebuffer ioctls
FBIOGET_VSCREENINFO = 0x4600
FBIOPUT_VSCREENINFO = 0x4601
FBIOGET_FSCREENINFO = 0x4602
FBIOBLANK = 0x4611
FB_BLANK_UNBLANK = 0

# Minimal 5x7 uppercase/digits/symbol bitmap font (column-major or row-major: 7 rows of 5 bits)
FONT_5X7 = {
    " ": [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    "0": [0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E],
    "1": [0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E],
    "2": [0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F],
    "3": [0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E],
    "4": [0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02],
    "5": [0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E],
    "6": [0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E],
    "7": [0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08],
    "8": [0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E],
    "9": [0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C],
    "A": [0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    "B": [0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E],
    "C": [0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E],
    "D": [0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C],
    "E": [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F],
    "F": [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10],
    "G": [0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F],
    "H": [0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
    "I": [0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
    "J": [0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C],
    "K": [0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11],
    "L": [0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F],
    "M": [0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11],
    "N": [0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11],
    "O": [0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    "P": [0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10],
    "Q": [0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D],
    "R": [0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11],
    "S": [0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E],
    "T": [0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
    "U": [0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
    "V": [0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04],
    "W": [0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11],
    "X": [0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11],
    "Y": [0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04],
    "Z": [0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F],
    ":": [0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x00],
    ".": [0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C],
    "-": [0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00],
    "/": [0x01, 0x02, 0x02, 0x04, 0x08, 0x08, 0x10],
    "%": [0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03],
    "(": [0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02],
    ")": [0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08],
    "_": [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F],
    "+": [0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00],
    "=": [0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00],
    "!": [0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04],
    "?": [0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04],
}


def resolve_fb_path(explicit=None):
    if explicit:
        return explicit
    for candidate in ("/dev/fb_cap", "/dev/fb1"):
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("Neither /dev/fb_cap nor /dev/fb1 found")


class Framebuffer:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)

        # fb_var_screeninfo is 160 bytes; first 7 uint32 fields include xres, yres, ..., bpp
        vinfo = bytearray(160)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, vinfo)
        self.width, self.height, _, _, _, _, self.bpp = struct.unpack_from("7I", vinfo, 0)

        # fb_fix_screeninfo: line_length is read cleanly from sysfs fallback if struct padding varies
        fb_name = os.path.basename(os.path.realpath(path))
        stride_path = f"/sys/class/graphics/{fb_name}/stride"
        if os.path.exists(stride_path):
            self.stride = int(open(stride_path, encoding="utf-8").read().strip())
        else:
            self.stride = self.width * (self.bpp // 8)

        self.bytes_pp = self.bpp // 8
        self.size = self.stride * self.height
        self.buf = bytearray(self.size)

        # Wake / unblank DRM pipeline so panel-mipi-dbi runs init even after a live rebind
        try:
            fcntl.ioctl(self.fd, FBIOBLANK, FB_BLANK_UNBLANK)
            # Set activate = FB_ACTIVATE_NOW | FB_ACTIVATE_FORCE (0x80) at offset 128 in fb_var_screeninfo
            struct.pack_into("I", vinfo, 128, 0x80)
            fcntl.ioctl(self.fd, FBIOPUT_VSCREENINFO, vinfo)
        except OSError:
            pass

    def pack_color(self, r, g, b):
        if self.bpp == 16:
            # Little-endian RGB565 (Linux fbdev standard on ARM)
            val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            return struct.pack("<H", val)
        elif self.bpp == 32:
            # Little-endian XRGB8888 (B, G, R, X)
            return bytes((b, g, r, 0xFF))
        elif self.bpp == 24:
            return bytes((b, g, r))
        raise ValueError(f"Unsupported bpp: {self.bpp}")

    def fill(self, r, g, b):
        px = self.pack_color(r, g, b)
        row = px * self.width
        if len(row) < self.stride:
            row += b"\x00" * (self.stride - len(row))
        for y in range(self.height):
            off = y * self.stride
            self.buf[off : off + self.stride] = row

    def fill_rect(self, x, y, w, h, r, g, b):
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(self.width, x + w)
        y1 = min(self.height, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        px = self.pack_color(r, g, b)
        span = px * (x1 - x0)
        bpp = self.bytes_pp
        for row_y in range(y0, y1):
            off = row_y * self.stride + x0 * bpp
            self.buf[off : off + len(span)] = span

    def draw_text(self, x, y, text, r, g, b, scale=2):
        cx = x
        for ch in text.upper():
            glyph = FONT_5X7.get(ch, FONT_5X7["?"])
            for row_i, bits in enumerate(glyph):
                for col_i in range(5):
                    if bits & (1 << (4 - col_i)):
                        self.fill_rect(cx + col_i * scale, y + row_i * scale, scale, scale, r, g, b)
            cx += 6 * scale

    def flush(self):
        # Use pwrite() so drm_fbdev_generic immediately flushes damage over SPI
        off = 0
        mv = memoryview(self.buf)
        while off < self.size:
            n = os.pwrite(self.fd, mv[off:], off)
            if n <= 0:
                break
            off += n

    def close(self):
        os.close(self.fd)


def read_cpu_percent(prev):
    parts = open("/proc/stat", encoding="utf-8").readline().split()
    nums = [int(x) for x in parts[1:8]]
    idle = nums[3] + nums[4]
    total = sum(nums)
    if prev is None:
        return 0.0, (idle, total)
    d_idle = idle - prev[0]
    d_total = total - prev[1]
    pct = 0.0 if d_total <= 0 else (1.0 - d_idle / d_total) * 100.0
    return pct, (idle, total)


def read_mem():
    info = {}
    for line in open("/proc/meminfo", encoding="utf-8"):
        k, v = line.split(":", 1)
        info[k] = int(v.strip().split()[0])
    total_mb = info.get("MemTotal", 0) / 1024.0
    avail_mb = info.get("MemAvailable", 0) / 1024.0
    used_mb = max(0.0, total_mb - avail_mb)
    pct = 0.0 if total_mb <= 0 else (used_mb / total_mb) * 100.0
    return used_mb, total_mb, pct


def read_temp():
    try:
        milli = int(open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8").read().strip())
        return f"{milli / 1000.0:.1f}C"
    except OSError:
        return "N/A"


def read_battery():
    for base in ("/sys/class/power_supply/bq27220-0", "/sys/class/power_supply/battery"):
        cap = os.path.join(base, "capacity")
        st = os.path.join(base, "status")
        if os.path.exists(cap):
            try:
                c = open(cap, encoding="utf-8").read().strip()
                s = open(st, encoding="utf-8").read().strip() if os.path.exists(st) else ""
                tag = " CHG" if "Charg" in s and "Dis" not in s else ""
                return f"{c}%{tag}"
            except OSError:
                pass
    # Generic scan
    ps_dir = "/sys/class/power_supply"
    if os.path.isdir(ps_dir):
        for entry in sorted(os.listdir(ps_dir)):
            cap = os.path.join(ps_dir, entry, "capacity")
            if os.path.exists(cap):
                try:
                    return f"{open(cap, encoding='utf-8').read().strip()}%"
                except OSError:
                    pass
    return "N/A"


def read_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "NO NET"


def read_uptime():
    secs = int(float(open("/proc/uptime", encoding="utf-8").read().split()[0]))
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}H {m:02d}M"


def draw_bar(fb, x, y, w, h, pct, fg):
    fb.fill_rect(x, y, w, h, 40, 48, 60)
    fill_w = int(max(0.0, min(100.0, pct)) / 100.0 * (w - 4))
    if fill_w > 0:
        fb.fill_rect(x + 2, y + 2, fill_w, h - 4, *fg)


def cmd_clear(fb):
    fb.fill(0, 0, 0)
    fb.flush()


def cmd_test(fb):
    w, h = fb.width, fb.height
    w1, w2 = w // 3, (2 * w) // 3
    fb.fill_rect(0, 0, w1, h, 220, 30, 30)
    fb.fill_rect(w1, 0, w2 - w1, h, 30, 200, 40)
    fb.fill_rect(w2, 0, w - w2, h, 30, 60, 230)
    # 3px white border
    fb.fill_rect(0, 0, w, 3, 255, 255, 255)
    fb.fill_rect(0, h - 3, w, 3, 255, 255, 255)
    fb.fill_rect(0, 0, 3, h, 255, 255, 255)
    fb.fill_rect(w - 3, 0, 3, h, 255, 255, 255)
    # Corner markers
    fb.fill_rect(6, 6, 24, 24, 255, 255, 0)
    fb.fill_rect(w - 30, h - 30, 24, 24, 0, 255, 255)
    # Label box
    fb.fill_rect(20, h // 2 - 22, w - 40, 44, 10, 12, 18)
    fb.draw_text(28, h // 2 - 14, f"CAP FB {w}X{h} {fb.bpp}BPP", 255, 255, 255, scale=2)
    fb.draw_text(28, h // 2 + 4, os.path.basename(fb.path), 140, 220, 255, scale=2)
    fb.flush()


def cmd_status(fb, interval):
    running = True

    def stop_handler(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    prev_cpu = None
    while running:
        cpu_pct, prev_cpu = read_cpu_percent(prev_cpu)
        mem_used, mem_tot, mem_pct = read_mem()
        temp = read_temp()
        batt = read_battery()
        ip = read_ip()
        up = read_uptime()
        now = time.strftime("%H:%M:%S")

        fb.fill(12, 16, 24)
        # Header bar
        fb.fill_rect(0, 0, fb.width, 28, 24, 72, 130)
        fb.draw_text(10, 7, "CARDPUTER ZERO", 255, 255, 255, scale=2)
        fb.draw_text(fb.width - 104, 7, now, 180, 235, 255, scale=2)

        y = 40
        fb.draw_text(12, y, f"CPU  {cpu_pct:5.1f}%   TEMP {temp}", 230, 240, 250, scale=2)
        draw_bar(fb, 12, y + 18, fb.width - 24, 14, cpu_pct, (60, 200, 120))

        y += 44
        fb.draw_text(12, y, f"RAM  {mem_used:3.0f}/{mem_tot:3.0f}M  ({mem_pct:2.0f}%)", 230, 240, 250, scale=2)
        draw_bar(fb, 12, y + 18, fb.width - 24, 14, mem_pct, (70, 160, 255))

        y += 46
        fb.draw_text(12, y, f"IP   {ip}", 255, 220, 100, scale=2)
        y += 26
        fb.draw_text(12, y, f"BAT  {batt}", 140, 255, 160, scale=2)
        y += 26
        fb.draw_text(12, y, f"UP   {up}", 180, 195, 215, scale=2)

        fb.flush()
        for _ in range(int(interval * 10)):
            if not running:
                break
            time.sleep(0.1)

    cmd_clear(fb)


def main():
    parser = argparse.ArgumentParser(description="Cardputer Zero secondary framebuffer helper")
    parser.add_argument("action", choices=["clear", "test", "status"])
    parser.add_argument("--fb", default=None, help="Framebuffer path (default: /dev/fb_cap or /dev/fb1)")
    parser.add_argument("--interval", type=float, default=2.0, help="Status refresh interval in seconds")
    args = parser.parse_args()

    fb_path = resolve_fb_path(args.fb)
    fb = Framebuffer(fb_path)
    try:
        if args.action == "clear":
            cmd_clear(fb)
        elif args.action == "test":
            cmd_test(fb)
        elif args.action == "status":
            cmd_status(fb, args.interval)
    finally:
        fb.close()


if __name__ == "__main__":
    main()
