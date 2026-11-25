#!/usr/bin/env python3
"""
Generate Linux kernel `panel-mipi-dbi` binary firmware files (.bin)
for secondary SPI displays on the M5Stack Cardputer Zero EXT header.

Binary format (drivers/gpu/drm/tiny/panel-mipi-dbi.c):
  - Bytes  0..14 : Magic "MIPI DBI" padded with NUL bytes (15 bytes total)
  - Byte      15 : File format version = 1
  - Bytes 16..   : Sequence of [cmd, num_params, param_0, ..., param_{n-1}]
                   Special delay command: [0x00, 0x01, delay_ms]
"""

import argparse
import os
import sys

MAGIC = b"MIPI DBI" + b"\x00" * 7
FORMAT_VERSION = 1


def madctl_for(rotation, bgr=False):
    rot_map = {
        0: 0x00,
        90: 0x60,   # MX | MV (landscape)
        180: 0xC0,  # MY | MX
        270: 0xA0,  # MY | MV (landscape flipped)
    }
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
    },
    "st7789-320x240-noinv": {
        "desc": "ST7789V 320x240 landscape (INVOFF)",
        "controller": "st7789",
        "width": 320,
        "height": 240,
        "rotation": 90,
        "bgr": False,
        "invert": False,
    },
    "term512-ili9341": {
        "desc": "TERM512 / CapTFT V2 ILI9341 320x240 landscape (verified upright MADCTL=0x28, BGR, INVOFF)",
        "controller": "ili9341",
        "width": 320,
        "height": 240,
        "rotation": 90,
        "madctl": 0x28,
        "bgr": True,
        "invert": False,
    },
    "ili9341-320x240": {
        "desc": "ILI9341 2.2\"/2.4\"/2.8\" 320x240 landscape (BGR, INVOFF)",
        "controller": "ili9341",
        "width": 320,
        "height": 240,
        "rotation": 90,
        "bgr": True,
        "invert": False,
    },
    "st7789-240x240": {
        "desc": "ST7789V 1.3\"/1.54\" 240x240 square IPS (INVON)",
        "controller": "st7789",
        "width": 240,
        "height": 240,
        "rotation": 0,
        "bgr": False,
        "invert": True,
    },
    "st7735-160x128": {
        "desc": "ST7735S 1.8\" 160x128 landscape",
        "controller": "st7735",
        "width": 160,
        "height": 128,
        "rotation": 90,
        "bgr": False,
        "invert": False,
    },
}


def encode_commands(entries):
    out = bytearray(MAGIC)
    out.append(FORMAT_VERSION)
    for kind, *rest in entries:
        if kind == "delay":
            ms = int(rest[0])
            while ms > 0:
                step = min(ms, 255)
                out.extend([0x00, 0x01, step])
                ms -= step
        elif kind == "cmd":
            opcode = int(rest[0]) & 0xFF
            params = [int(p) & 0xFF for p in (rest[1] if len(rest) > 1 else [])]
            if len(params) > 255:
                raise ValueError("Too many parameters for a single MIPI DBI command")
            out.append(opcode)
            out.append(len(params))
            out.extend(params)
        else:
            raise ValueError(f"Unknown entry kind: {kind}")
    return bytes(out)


def build_sequence(controller, madctl, invert):
    # Note: panel-mipi-dbi calls mipi_dbi_hw_reset() before running commands.
    seq = [
        ("cmd", 0x01),          # SWRESET
        ("delay", 150),
        ("cmd", 0x11),          # SLPOUT
        ("delay", 120),
    ]

    if controller == "st7789":
        seq += [
            ("cmd", 0xB2, [0x0C, 0x0C, 0x00, 0x33, 0x33]),  # Porch setting
            ("cmd", 0xB7, [0x35]),                          # Gate control
            ("cmd", 0xBB, [0x19]),                          # VCOM setting
            ("cmd", 0xC0, [0x2C]),                          # LCM control
            ("cmd", 0xC2, [0x01]),                          # VDV and VRH enable
            ("cmd", 0xC3, [0x12]),                          # VRH set
            ("cmd", 0xC4, [0x20]),                          # VDV set
            ("cmd", 0xC6, [0x0F]),                          # Frame rate control (60Hz)
            ("cmd", 0xD0, [0xA4, 0xA1]),                    # Power control 1
        ]
    elif controller == "ili9341":
        seq += [
            ("cmd", 0xCF, [0x00, 0xC1, 0x30]),
            ("cmd", 0xED, [0x64, 0x03, 0x12, 0x81]),
            ("cmd", 0xE8, [0x85, 0x00, 0x78]),
            ("cmd", 0xCB, [0x39, 0x2C, 0x00, 0x34, 0x02]),
            ("cmd", 0xF7, [0x20]),
            ("cmd", 0xEA, [0x00, 0x00]),
            ("cmd", 0xC0, [0x23]),
            ("cmd", 0xC1, [0x10]),
            ("cmd", 0xC5, [0x3E, 0x28]),
            ("cmd", 0xC7, [0x86]),
            ("cmd", 0xB1, [0x00, 0x18]),
            ("cmd", 0xB6, [0x08, 0x82, 0x27]),
            ("cmd", 0xF2, [0x00]),
            ("cmd", 0x26, [0x01]),
        ]
    elif controller == "st7735":
        seq += [
            ("cmd", 0xB1, [0x01, 0x2C, 0x2D]),
            ("cmd", 0xB2, [0x01, 0x2C, 0x2D]),
            ("cmd", 0xB3, [0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D]),
            ("cmd", 0xB4, [0x07]),
            ("cmd", 0xC0, [0xA2, 0x02, 0x84]),
            ("cmd", 0xC1, [0xC5]),
            ("cmd", 0xC2, [0x0A, 0x00]),
            ("cmd", 0xC3, [0x8A, 0x2A]),
            ("cmd", 0xC4, [0x8A, 0xEE]),
            ("cmd", 0xC5, [0x0E]),
        ]
    else:
        raise ValueError(f"Unsupported controller: {controller}")

    seq += [
        ("cmd", 0x3A, [0x55]),              # COLMOD: 16-bit RGB565
        ("cmd", 0x36, [madctl]),            # MADCTL: orientation + RGB/BGR
        ("cmd", 0x21 if invert else 0x20),  # INVON / INVOFF
        ("cmd", 0x13),                      # NORON
        ("delay", 10),
        ("cmd", 0x29),                      # DISPON
        ("delay", 20),
    ]
    return seq


def verify_bin(path):
    data = open(path, "rb").read()
    if len(data) < 16:
        raise ValueError("File shorter than 16-byte header")
    if data[:15] != MAGIC:
        raise ValueError(f"Bad magic: {data[:15]!r}")
    if data[15] != FORMAT_VERSION:
        raise ValueError(f"Unsupported version: {data[15]}")

    i = 16
    count = 0
    while i < len(data):
        if i + 2 > len(data):
            raise ValueError(f"Truncated command header at offset {i}")
        cmd = data[i]
        n = data[i + 1]
        i += 2
        if i + n > len(data):
            raise ValueError(f"Truncated params for cmd 0x{cmd:02X} at offset {i}")
        params = list(data[i : i + n])
        i += n
        count += 1
        if cmd == 0x00 and n == 1:
            print(f"  delay {params[0]} ms")
        else:
            phex = " ".join(f"0x{b:02X}" for b in params)
            print(f"  cmd   0x{cmd:02X}" + (f"  {phex}" if phex else ""))
    print(f"[OK] Valid panel-mipi-dbi v1 firmware: {path} ({len(data)} bytes, {count} entries)")


def main():
    parser = argparse.ArgumentParser(description="Build/verify panel-mipi-dbi .bin firmware")
    parser.add_argument("--preset", choices=sorted(PRESETS.keys()), default="st7789-320x240")
    parser.add_argument("--rotation", type=int, choices=[0, 90, 180, 270], default=None)
    parser.add_argument("--bgr", action="store_true", default=None)
    parser.add_argument("--rgb", action="store_true")
    parser.add_argument("--invert", action="store_true", default=None)
    parser.add_argument("--no-invert", action="store_true")
    parser.add_argument("--madctl", type=lambda x: int(x, 0), default=None,
                        help="Override raw MADCTL byte (e.g. 0x68, 0xA8, 0x28, 0xE8)")
    parser.add_argument("-o", "--output", default="cardputer-cap-lcd.bin")
    parser.add_argument("--verify", metavar="FILE", help="Parse and verify an existing .bin file")
    args = parser.parse_args()

    if args.verify:
        verify_bin(args.verify)
        return

    cfg = PRESETS[args.preset]
    rotation = cfg["rotation"] if args.rotation is None else args.rotation
    bgr = True if args.bgr else (False if args.rgb else cfg["bgr"])
    invert = False if args.no_invert else (True if args.invert else cfg["invert"])
    if args.madctl is not None:
        madctl = args.madctl
    elif "madctl" in cfg and args.rotation is None and not args.bgr and not args.rgb:
        madctl = cfg["madctl"]
    else:
        madctl = madctl_for(rotation, bgr=bgr)

    seq = build_sequence(cfg["controller"], madctl=madctl, invert=invert)
    blob = encode_commands(seq)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "wb") as f:
        f.write(blob)

    print(f"[OK] Wrote {args.output} ({len(blob)} bytes)")
    print(f"     preset={args.preset}, controller={cfg['controller']}, rotation={rotation}, bgr={bgr}, invert={invert}, madctl=0x{madctl:02X}")


if __name__ == "__main__":
    main()
