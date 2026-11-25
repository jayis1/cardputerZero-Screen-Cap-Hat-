#!/usr/bin/env python3
"""
cap_hud.py — Advanced Multi-Page Cyberdeck HUD & Renderer for Cardputer Zero
Secondary CAP Display (/dev/fb_cap -> /dev/fb1, 320x240 RGB565).

Pure Python stdlib (zero pip dependencies). Uses pwrite() + FBIOBLANK unblank
for instant, tear-free updates on drm_fbdev_generic / panel-mipi-dbi without
touching the primary 1.9" display (/dev/fb_lcd).

Modes:
  status  : System telemetry + live 60-sample CPU sparkline + Battery/Temp/IP
  net     : Network interfaces, Wi-Fi RSSI/SSID, live RX/TX throughput sparkline
  proc    : Live top processes (CPU/RAM) & systemd service health
  clock   : Giant digital cyberdeck clock + date + battery + IP standby view
  log     : Live scrolling journalctl / dmesg tail on top screen
  run     : Live streaming output of any shell command onto top screen
  watch   : Periodically refreshed command output on top screen
  menu    : Render interactive mode selector screen (used by cap_hotkeys)
  osd     : Flash a transient notification toast banner on top screen
  clear   : Fill screen black
  test    : Geometry & color test bars
"""

import argparse
import collections
import fcntl
import glob
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time

FBIOGET_VSCREENINFO = 0x4600
FBIOPUT_VSCREENINFO = 0x4601
FBIOBLANK = 0x4611
FB_BLANK_UNBLANK = 0

# Complete printable ASCII 5x7 bitmap font (rows of 5 bits, MSB=left)
FONT_5X7 = {
    " ": [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    "!": [0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04],
    '"': [0x0A, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x00],
    "#": [0x0A, 0x1F, 0x0A, 0x0A, 0x1F, 0x0A, 0x00],
    "$": [0x04, 0x0F, 0x14, 0x0E, 0x05, 0x1E, 0x04],
    "%": [0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03],
    "&": [0x0C, 0x12, 0x14, 0x08, 0x15, 0x12, 0x0D],
    "'": [0x04, 0x04, 0x08, 0x00, 0x00, 0x00, 0x00],
    "(": [0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02],
    ")": [0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08],
    "*": [0x00, 0x0A, 0x04, 0x1F, 0x04, 0x0A, 0x00],
    "+": [0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00],
    ",": [0x00, 0x00, 0x00, 0x00, 0x04, 0x04, 0x08],
    "-": [0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00],
    ".": [0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C],
    "/": [0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10],
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
    ":": [0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x00],
    ";": [0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x08],
    "<": [0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02],
    "=": [0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00],
    ">": [0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08],
    "?": [0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04],
    "@": [0x0E, 0x11, 0x17, 0x15, 0x17, 0x10, 0x0E],
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
    "[": [0x0E, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0E],
    "\\": [0x10, 0x10, 0x08, 0x04, 0x02, 0x01, 0x01],
    "]": [0x0E, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0E],
    "^": [0x04, 0x0A, 0x11, 0x00, 0x00, 0x00, 0x00],
    "_": [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F],
    "`": [0x08, 0x04, 0x02, 0x00, 0x00, 0x00, 0x00],
    "a": [0x00, 0x00, 0x0E, 0x01, 0x0F, 0x11, 0x0F],
    "b": [0x10, 0x10, 0x16, 0x19, 0x11, 0x11, 0x1E],
    "c": [0x00, 0x00, 0x0E, 0x10, 0x10, 0x11, 0x0E],
    "d": [0x01, 0x01, 0x0D, 0x13, 0x11, 0x11, 0x0F],
    "e": [0x00, 0x00, 0x0E, 0x11, 0x1F, 0x10, 0x0E],
    "f": [0x06, 0x09, 0x08, 0x1C, 0x08, 0x08, 0x08],
    "g": [0x00, 0x0F, 0x11, 0x11, 0x0F, 0x01, 0x0E],
    "h": [0x10, 0x10, 0x16, 0x19, 0x11, 0x11, 0x11],
    "i": [0x04, 0x00, 0x0C, 0x04, 0x04, 0x04, 0x0E],
    "j": [0x02, 0x00, 0x06, 0x02, 0x02, 0x12, 0x0C],
    "k": [0x10, 0x10, 0x12, 0x14, 0x18, 0x14, 0x12],
    "l": [0x0C, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
    "m": [0x00, 0x00, 0x1A, 0x15, 0x15, 0x15, 0x15],
    "n": [0x00, 0x00, 0x16, 0x19, 0x11, 0x11, 0x11],
    "o": [0x00, 0x00, 0x0E, 0x11, 0x11, 0x11, 0x0E],
    "p": [0x00, 0x00, 0x1E, 0x11, 0x1E, 0x10, 0x10],
    "q": [0x00, 0x00, 0x0D, 0x13, 0x0F, 0x01, 0x01],
    "r": [0x00, 0x00, 0x16, 0x19, 0x10, 0x10, 0x10],
    "s": [0x00, 0x00, 0x0F, 0x10, 0x0E, 0x01, 0x1E],
    "t": [0x08, 0x08, 0x1C, 0x08, 0x08, 0x09, 0x06],
    "u": [0x00, 0x00, 0x11, 0x11, 0x11, 0x13, 0x0D],
    "v": [0x00, 0x00, 0x11, 0x11, 0x11, 0x0A, 0x04],
    "w": [0x00, 0x00, 0x11, 0x11, 0x15, 0x15, 0x0A],
    "x": [0x00, 0x00, 0x11, 0x0A, 0x04, 0x0A, 0x11],
    "y": [0x00, 0x00, 0x11, 0x11, 0x0F, 0x01, 0x0E],
    "z": [0x00, 0x00, 0x1F, 0x02, 0x04, 0x08, 0x1F],
    "{": [0x02, 0x04, 0x04, 0x08, 0x04, 0x04, 0x02],
    "|": [0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
    "}": [0x08, 0x04, 0x04, 0x02, 0x04, 0x04, 0x08],
    "~": [0x00, 0x00, 0x08, 0x15, 0x02, 0x00, 0x00],
}

# Theme palette (Kali Cyberdeck Dark)
BG = (10, 14, 22)
CARD = (18, 25, 38)
BORDER = (40, 56, 82)
ACCENT = (0, 200, 255)
ACCENT2 = (60, 230, 140)
WARN = (255, 195, 60)
DANGER = (255, 85, 95)
TEXT = (232, 240, 252)
MUTED = (130, 150, 180)
HEADER_BG = (16, 46, 86)


def resolve_fb(explicit=None):
    if explicit:
        return explicit
    for p in ("/dev/fb_cap", "/dev/fb1"):
        if os.path.exists(p):
            return p
    raise FileNotFoundError("Neither /dev/fb_cap nor /dev/fb1 found")


class Canvas:
    def __init__(self, path=None):
        self.path = resolve_fb(path)
        self.fd = os.open(self.path, os.O_RDWR)
        vinfo = bytearray(160)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, vinfo)
        self.w, self.h, _, _, _, _, self.bpp = struct.unpack_from("7I", vinfo, 0)
        fb_name = os.path.basename(os.path.realpath(self.path))
        stride_file = f"/sys/class/graphics/{fb_name}/stride"
        self.stride = int(open(stride_file).read().strip()) if os.path.exists(stride_file) else self.w * (self.bpp // 8)
        self.bytes_pp = self.bpp // 8
        self.size = self.stride * self.h
        self.buf = bytearray(self.size)

        # Wake DRM CRTC so panel-mipi-dbi is always active
        try:
            fcntl.ioctl(self.fd, FBIOBLANK, FB_BLANK_UNBLANK)
            struct.pack_into("I", vinfo, 128, 0x80)
            fcntl.ioctl(self.fd, FBIOPUT_VSCREENINFO, vinfo)
        except OSError:
            pass

    def pack(self, color):
        r, g, b = color
        if self.bpp == 16:
            return struct.pack("<H", ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3))
        if self.bpp == 32:
            return bytes((b, g, r, 0xFF))
        return bytes((b, g, r))

    def fill(self, color):
        px = self.pack(color)
        row = px * self.w
        if len(row) < self.stride:
            row += b"\x00" * (self.stride - len(row))
        for y in range(self.h):
            off = y * self.stride
            self.buf[off : off + self.stride] = row

    def rect(self, x, y, w, h, color):
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.w, int(x + w)), min(self.h, int(y + h))
        if x0 >= x1 or y0 >= y1:
            return
        span = self.pack(color) * (x1 - x0)
        bpp = self.bytes_pp
        for ry in range(y0, y1):
            off = ry * self.stride + x0 * bpp
            self.buf[off : off + len(span)] = span

    def box(self, x, y, w, h, fill_c=CARD, border_c=BORDER):
        self.rect(x, y, w, h, fill_c)
        self.rect(x, y, w, 1, border_c)
        self.rect(x, y + h - 1, w, 1, border_c)
        self.rect(x, y, 1, h, border_c)
        self.rect(x + w - 1, y, 1, h, border_c)

    def hbar(self, x, y, w, h, pct, fg=ACCENT2, bg=(30, 40, 56)):
        self.rect(x, y, w, h, bg)
        fw = int(max(0.0, min(100.0, pct)) / 100.0 * max(0, w - 2))
        if fw > 0:
            self.rect(x + 1, y + 1, fw, max(1, h - 2), fg)

    def sparkline(self, x, y, w, h, values, max_val=100.0, fg=ACCENT, bg=(14, 20, 32)):
        self.box(x, y, w, h, fill_c=bg, border_c=BORDER)
        if not values:
            return
        inner_w = w - 4
        inner_h = h - 4
        n = len(values)
        bar_w = max(1, inner_w // max(n, 1))
        mv = max(max_val, 1e-6)
        for i, v in enumerate(values):
            bx = x + 2 + i * bar_w
            if bx >= x + w - 2:
                break
            bh = int(max(0.0, min(1.0, v / mv)) * inner_h)
            if bh > 0:
                self.rect(bx, y + h - 2 - bh, max(1, bar_w - 1), bh, fg)

    def text(self, x, y, s, color=TEXT, scale=1, max_chars=None):
        if max_chars is not None and len(s) > max_chars:
            s = s[: max(0, max_chars - 1)] + "~"
        cx = int(x)
        cy = int(y)
        px = self.pack(color)
        bpp = self.bytes_pp
        step_x = 6 * scale
        for ch in s:
            if cx + 5 * scale > self.w:
                break
            glyph = FONT_5X7.get(ch, FONT_5X7.get(ch.upper(), FONT_5X7["?"]))
            for row_i, bits in enumerate(glyph):
                if not bits:
                    continue
                for col_i in range(5):
                    if bits & (1 << (4 - col_i)):
                        if scale == 1:
                            py = cy + row_i
                            px_x = cx + col_i
                            if 0 <= py < self.h and 0 <= px_x < self.w:
                                off = py * self.stride + px_x * bpp
                                self.buf[off : off + bpp] = px
                        else:
                            self.rect(cx + col_i * scale, cy + row_i * scale, scale, scale, color)
            cx += step_x

    def header(self, title, right_text=None):
        self.rect(0, 0, self.w, 22, HEADER_BG)
        self.rect(0, 22, self.w, 1, ACCENT)
        self.text(8, 5, title, TEXT, scale=2)
        rt = right_text if right_text is not None else time.strftime("%H:%M:%S")
        rx = self.w - len(rt) * 6 - 8
        self.text(rx, 8, rt, ACCENT, scale=1)

    def flush(self):
        off = 0
        mv = memoryview(self.buf)
        while off < self.size:
            n = os.pwrite(self.fd, mv[off:], off)
            if n <= 0:
                break
            off += n

    def close(self):
        os.close(self.fd)


# ---------- System Probes (all pure /proc & /sys) ----------

def read_cpu_pct(prev):
    parts = open("/proc/stat").readline().split()
    nums = [int(x) for x in parts[1:8]]
    idle = nums[3] + nums[4]
    total = sum(nums)
    if prev is None:
        return 0.0, (idle, total)
    di = idle - prev[0]
    dt = total - prev[1]
    return (0.0 if dt <= 0 else (1.0 - di / dt) * 100.0), (idle, total)


def read_cpu_freq_mhz():
    try:
        khz = int(open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read().strip())
        return f"{khz // 1000}MHz"
    except OSError:
        return ""


def read_temp_c():
    try:
        return int(open("/sys/class/thermal/thermal_zone0/temp").read().strip()) / 1000.0
    except OSError:
        return 0.0


def read_mem():
    info = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        info[k] = int(v.strip().split()[0])
    tot = info.get("MemTotal", 1) / 1024.0
    avail = info.get("MemAvailable", 0) / 1024.0
    used = max(0.0, tot - avail)
    return used, tot, (used / tot) * 100.0


def read_disk_root():
    st = os.statvfs("/")
    tot = (st.f_blocks * st.f_frsize) / (1024 ** 3)
    free = (st.f_bavail * st.f_frsize) / (1024 ** 3)
    used = max(0.0, tot - free)
    pct = 0.0 if tot <= 0 else (used / tot) * 100.0
    return used, tot, pct


def read_battery():
    for base in sorted(glob.glob("/sys/class/power_supply/*")):
        cap = os.path.join(base, "capacity")
        if os.path.exists(cap):
            try:
                pct = int(open(cap).read().strip())
                st = open(os.path.join(base, "status")).read().strip() if os.path.exists(os.path.join(base, "status")) else ""
                volt = ""
                vp = os.path.join(base, "voltage_now")
                if os.path.exists(vp):
                    volt = f" {int(open(vp).read().strip()) / 1e6:.2f}V"
                chg = " CHG" if ("Charg" in st and "Dis" not in st) else ""
                return pct, f"{pct}%{chg}{volt}"
            except OSError:
                continue
    return None, "N/A"


def read_uptime():
    s = int(float(open("/proc/uptime").read().split()[0]))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{d}d {h:02d}h{m:02d}m" if d else f"{h}h {m:02d}m"


def read_primary_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "NO NET"


def read_net_bytes():
    rx = tx = 0
    for line in open("/proc/net/dev").readlines()[2:]:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        if iface.strip() == "lo":
            continue
        cols = rest.split()
        rx += int(cols[0])
        tx += int(cols[8])
    return rx, tx


def read_interfaces():
    out = []
    try:
        res = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=1.0)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] != "lo":
                out.append((parts[1], parts[3]))
    except Exception:
        pass
    return out


def read_wifi_info():
    ssid = "disconnected"
    signal_dbm = ""
    try:
        res = subprocess.run(["iw", "dev"], capture_output=True, text=True, timeout=1.0)
        for line in res.stdout.splitlines():
            ls = line.strip()
            if ls.startswith("ssid "):
                ssid = ls[5:].strip()
    except Exception:
        pass
    try:
        for line in open("/proc/net/wireless").readlines()[2:]:
            cols = line.split()
            if len(cols) >= 4:
                signal_dbm = f"{cols[3].rstrip('.')} dBm"
                break
    except OSError:
        pass
    return ssid, signal_dbm


def read_top_procs(prev_ticks, elapsed_ticks, limit=8):
    cur = {}
    rows = []
    mem_tot_kb = 1
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                mem_tot_kb = int(line.split()[1])
                break
    except OSError:
        pass

    page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            raw = open(f"/proc/{pid}/stat").read()
            lpar = raw.find("(")
            rpar = raw.rfind(")")
            comm = raw[lpar + 1 : rpar]
            fields = raw[rpar + 2 :].split()
            ticks = int(fields[11]) + int(fields[12])
            rss_kb = int(fields[21]) * page_kb
            cur[pid] = ticks
            prev_t = prev_ticks.get(pid)
            cpu_p = 0.0 if (prev_t is None or elapsed_ticks <= 0) else ((ticks - prev_t) / elapsed_ticks) * 100.0
            mem_p = (rss_kb / mem_tot_kb) * 100.0
            rows.append((cpu_p, mem_p, pid, comm))
        except OSError:
            continue

    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return rows[:limit], cur


# ---------- HUD Pages ----------

def make_stop_flag():
    flag = {"run": True}
    def _h(_s, _f):
        flag["run"] = False
    signal.signal(signal.SIGTERM, _h)
    signal.signal(signal.SIGINT, _h)
    return flag


def sleep_interruptible(flag, seconds):
    end = time.monotonic() + seconds
    while flag["run"] and time.monotonic() < end:
        time.sleep(min(0.1, max(0.01, end - time.monotonic())))


def page_status(cv, interval):
    flag = make_stop_flag()
    prev_cpu = None
    cpu_hist = collections.deque([0.0] * 50, maxlen=50)

    while flag["run"]:
        cpu, prev_cpu = read_cpu_pct(prev_cpu)
        cpu_hist.append(cpu)
        temp = read_temp_c()
        freq = read_cpu_freq_mhz()
        m_used, m_tot, m_pct = read_mem()
        d_used, d_tot, d_pct = read_disk_root()
        bat_pct, bat_str = read_battery()
        ip = read_primary_ip()
        ssid, rssi = read_wifi_info()
        up = read_uptime()
        load1 = open("/proc/loadavg").read().split()[0]

        cv.fill(BG)
        cv.header("SYS HUD", f"{time.strftime('%H:%M:%S')}")

        # CPU + Sparkline card
        cv.box(6, 28, 308, 66)
        c_col = DANGER if cpu > 85 else (WARN if cpu > 60 else ACCENT2)
        cv.text(12, 34, f"CPU {cpu:5.1f}%  {freq}", c_col, scale=2)
        t_col = DANGER if temp > 70 else (WARN if temp > 58 else TEXT)
        cv.text(224, 34, f"{temp:4.1f}C", t_col, scale=2)
        cv.sparkline(12, 54, 296, 34, list(cpu_hist), max_val=100.0, fg=ACCENT)

        # RAM & Disk cards side-by-side
        cv.box(6, 100, 150, 48)
        cv.text(12, 106, f"RAM {m_used:.0f}/{m_tot:.0f}M", TEXT, scale=1)
        cv.text(122, 106, f"{m_pct:2.0f}%", ACCENT, scale=1)
        cv.hbar(12, 120, 138, 12, m_pct, fg=ACCENT)
        cv.text(12, 136, f"LOAD {load1}", MUTED, scale=1)

        cv.box(164, 100, 150, 48)
        cv.text(170, 106, f"DISK {d_used:.1f}/{d_tot:.0f}G", TEXT, scale=1)
        cv.text(280, 106, f"{d_pct:2.0f}%", WARN if d_pct > 85 else ACCENT2, scale=1)
        cv.hbar(170, 120, 138, 12, d_pct, fg=WARN if d_pct > 85 else ACCENT2)
        cv.text(170, 136, f"UP {up}", MUTED, scale=1)

        # Battery + Network card
        cv.box(6, 154, 308, 80)
        b_col = DANGER if (bat_pct is not None and bat_pct < 20) else ACCENT2
        cv.text(12, 162, f"BAT: {bat_str}", b_col, scale=2)
        cv.text(12, 184, f"IP : {ip}", WARN, scale=2)
        wifi_line = f"WIFI: {ssid}" + (f" ({rssi})" if rssi else "")
        cv.text(12, 208, wifi_line, TEXT, scale=1, max_chars=48)
        cv.text(12, 220, "Ctrl+Alt+Space: Menu | Ctrl+Alt+N: Next", MUTED, scale=1)

        cv.flush()
        sleep_interruptible(flag, interval)


def page_net(cv, interval):
    flag = make_stop_flag()
    prev_rx, prev_tx = read_net_bytes()
    prev_t = time.monotonic()
    rate_hist = collections.deque([0.0] * 50, maxlen=50)

    while flag["run"]:
        now_t = time.monotonic()
        rx, tx = read_net_bytes()
        dt = max(0.1, now_t - prev_t)
        rx_kbs = max(0.0, (rx - prev_rx) / 1024.0 / dt)
        tx_kbs = max(0.0, (tx - prev_tx) / 1024.0 / dt)
        prev_rx, prev_tx, prev_t = rx, tx, now_t
        rate_hist.append(rx_kbs + tx_kbs)

        ssid, rssi = read_wifi_info()
        ifaces = read_interfaces()

        cv.fill(BG)
        cv.header("NET / WIFI", f"{time.strftime('%H:%M:%S')}")

        # Throughput card
        cv.box(6, 28, 308, 72)
        cv.text(12, 34, f"RX {rx_kbs:6.1f} KB/s", ACCENT2, scale=2)
        cv.text(12, 52, f"TX {tx_kbs:6.1f} KB/s", WARN, scale=2)
        peak = max(10.0, max(rate_hist) * 1.1)
        cv.sparkline(202, 34, 106, 60, list(rate_hist), max_val=peak, fg=ACCENT)
        cv.text(12, 76, f"WIFI: {ssid} {rssi}", TEXT, scale=1, max_chars=30)
        cv.text(12, 88, f"PEAK: {max(rate_hist):.1f} KB/s", MUTED, scale=1)

        # Interfaces card
        cv.box(6, 106, 308, 128)
        cv.text(12, 112, "ACTIVE INTERFACES", ACCENT, scale=1)
        y = 126
        if not ifaces:
            cv.text(12, y, "No IPv4 interfaces up", WARN, scale=2)
        else:
            for name, addr in ifaces[:5]:
                cv.text(12, y, f"{name:7s} {addr}", TEXT, scale=2 if len(ifaces) <= 3 else 1, max_chars=24 if len(ifaces) <= 3 else 48)
                y += 22 if len(ifaces) <= 3 else 14

        cv.text(12, 220, "Ctrl+Alt+Space: Menu | Ctrl+Alt+N: Next", MUTED, scale=1)
        cv.flush()
        sleep_interruptible(flag, interval)


def page_proc(cv, interval):
    flag = make_stop_flag()
    prev_ticks = {}
    prev_total = sum(int(x) for x in open("/proc/stat").readline().split()[1:8])
    ncpu = os.cpu_count() or 1

    while flag["run"]:
        cur_total = sum(int(x) for x in open("/proc/stat").readline().split()[1:8])
        dt_ticks = max(1, (cur_total - prev_total) // ncpu)
        prev_total = cur_total
        top, prev_ticks = read_top_procs(prev_ticks, dt_ticks, limit=10)
        m_used, m_tot, m_pct = read_mem()

        cv.fill(BG)
        cv.header("TOP PROCS", f"RAM {m_pct:2.0f}%")
        cv.box(6, 28, 308, 206)
        cv.text(12, 34, "PID    CPU%  MEM%  COMMAND", ACCENT, scale=1)
        cv.rect(10, 44, 300, 1, BORDER)

        y = 50
        for cpu_p, mem_p, pid, comm in top:
            col = DANGER if cpu_p > 50 else (WARN if cpu_p > 15 else TEXT)
            line = f"{pid:>5s} {cpu_p:5.1f} {mem_p:5.1f}  {comm}"
            cv.text(12, y, line, col, scale=1, max_chars=49)
            y += 16

        cv.text(12, 220, f"TOTAL RAM {m_used:.0f}/{m_tot:.0f}M | Ctrl+Alt+Space: Menu", MUTED, scale=1)
        cv.flush()
        sleep_interruptible(flag, interval)


def page_clock(cv, interval):
    flag = make_stop_flag()
    while flag["run"]:
        now_hm = time.strftime("%H:%M")
        now_s = time.strftime(":%S")
        date_s = time.strftime("%a  %Y-%m-%d")
        bat_pct, bat_str = read_battery()
        ip = read_primary_ip()
        temp = read_temp_c()

        cv.fill(BG)
        cv.rect(0, 0, cv.w, 3, ACCENT)
        cv.rect(0, cv.h - 3, cv.w, 3, ACCENT)

        # Giant centered time
        cv.text(28, 42, now_hm, TEXT, scale=7)
        cv.text(242, 74, now_s, ACCENT, scale=3)

        # Date centered
        dx = max(8, (cv.w - len(date_s) * 12) // 2)
        cv.text(dx, 120, date_s, ACCENT2, scale=2)

        # Bottom telemetry bar
        cv.box(10, 158, 300, 68)
        b_col = DANGER if (bat_pct is not None and bat_pct < 20) else ACCENT2
        cv.text(18, 168, f"BAT {bat_str}", b_col, scale=2)
        cv.text(226, 168, f"{temp:4.1f}C", WARN if temp > 60 else TEXT, scale=2)
        cv.text(18, 194, f"IP  {ip}", WARN, scale=2)
        cv.text(18, 214, "Ctrl+Alt+Space: Menu | Ctrl+Alt+B: Sleep", MUTED, scale=1)

        cv.flush()
        sleep_interruptible(flag, min(1.0, interval))


def page_stream(cv, title, cmd_str, scale=1):
    """Stream stdout+stderr of any command onto /dev/fb_cap without touching VTs."""
    flag = make_stop_flag()
    char_w = 6 * scale
    line_h = 9 * scale
    max_cols = (cv.w - 12) // char_w
    max_rows = (cv.h - 34) // line_h
    lines = collections.deque(maxlen=max_rows)
    lines.append(f"$ {cmd_str}")

    def draw():
        cv.fill(BG)
        cv.header(title, time.strftime("%H:%M:%S"))
        y = 28
        for ln in lines:
            cv.text(6, y, ln, TEXT, scale=scale, max_chars=max_cols)
            y += line_h
        cv.flush()

    proc = subprocess.Popen(
        cmd_str,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    draw()
    last_draw = time.monotonic()
    dirty = False

    try:
        while flag["run"]:
            rlist, _, _ = select.select([proc.stdout], [], [], 0.2)
            if rlist:
                raw = proc.stdout.readline()
                if not raw:
                    if proc.poll() is not None:
                        lines.append(f"[exited rc={proc.returncode}]")
                        draw()
                        break
                else:
                    clean = raw.rstrip("\r\n").replace("\t", "  ")
                    # Wrap long lines cleanly
                    if not clean:
                        lines.append("")
                    else:
                        while len(clean) > max_cols:
                            lines.append(clean[:max_cols])
                            clean = clean[max_cols:]
                        lines.append(clean)
                    dirty = True
            now = time.monotonic()
            if dirty and (now - last_draw) >= 0.15:
                draw()
                last_draw = now
                dirty = False
        while flag["run"]:
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()


def page_watch(cv, cmd_str, interval=2.0, scale=1):
    flag = make_stop_flag()
    char_w = 6 * scale
    line_h = 9 * scale
    max_cols = (cv.w - 12) // char_w
    max_rows = (cv.h - 34) // line_h

    while flag["run"]:
        try:
            res = subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=max(2.0, interval))
            out = (res.stdout + res.stderr).splitlines()
        except Exception as e:
            out = [f"[ERR] {e}"]

        cv.fill(BG)
        cv.header(f"WATCH {interval:.0f}S", time.strftime("%H:%M:%S"))
        y = 28
        for ln in out[:max_rows]:
            cv.text(6, y, ln.replace("\t", "  "), TEXT, scale=scale, max_chars=max_cols)
            y += line_h
        cv.flush()
        sleep_interruptible(flag, interval)


def render_menu(cv, items, selected_idx, current_mode=""):
    cv.fill(BG)
    cv.header("CAP SCREEN MENU", current_mode.upper())
    y = 28
    row_h = 21
    for i, (key, label) in enumerate(items):
        is_sel = i == selected_idx
        if is_sel:
            cv.rect(6, y, cv.w - 12, row_h - 2, ACCENT)
            cv.text(12, y + 3, f"{i + 1}. {label}", (8, 12, 18), scale=2)
        else:
            cv.box(6, y, cv.w - 12, row_h - 2, fill_c=CARD, border_c=BORDER)
            cv.text(12, y + 3, f"{i + 1}. {label}", TEXT, scale=2)
        y += row_h
    cv.text(8, cv.h - 11, "1-9 / W,S / Arrows + Enter | Esc: Cancel", MUTED, scale=1)
    cv.flush()


def render_osd(cv, title, subtitle=""):
    cv.fill(BG)
    cv.box(16, 72, cv.w - 32, 96, fill_c=CARD, border_c=ACCENT)
    cv.rect(16, 72, cv.w - 32, 4, ACCENT)
    tx = max(24, (cv.w - len(title) * 12) // 2)
    cv.text(tx, 96, title, TEXT, scale=2)
    if subtitle:
        sx = max(24, (cv.w - len(subtitle) * 6) // 2)
        cv.text(sx, 130, subtitle, ACCENT2, scale=1)
    cv.flush()


def main():
    parser = argparse.ArgumentParser(description="Cardputer Zero Advanced Secondary CAP Display HUD")
    parser.add_argument("mode", choices=["status", "net", "proc", "clock", "log", "run", "watch", "osd", "clear", "test"])
    parser.add_argument("--fb", default=None)
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--cmd", default="")
    parser.add_argument("--unit", default="")
    parser.add_argument("--scale", type=int, choices=[1, 2], default=1)
    parser.add_argument("--title", default="")
    parser.add_argument("--subtitle", default="")
    args = parser.parse_args()

    cv = Canvas(args.fb)
    try:
        if args.mode == "clear":
            cv.fill((0, 0, 0))
            cv.flush()
        elif args.mode == "test":
            w, h = cv.w, cv.h
            cv.rect(0, 0, w // 3, h, (220, 30, 30))
            cv.rect(w // 3, 0, w // 3, h, (30, 200, 40))
            cv.rect(2 * w // 3, 0, w - 2 * w // 3, h, (30, 60, 230))
            cv.box(24, h // 2 - 22, w - 48, 44, fill_c=(10, 12, 18), border_c=(255, 255, 255))
            cv.text(34, h // 2 - 12, f"CAP {w}x{h} {cv.bpp}BPP", TEXT, scale=2)
            cv.flush()
        elif args.mode == "osd":
            render_osd(cv, args.title or "CAP SCREEN", args.subtitle)
        elif args.mode == "status":
            page_status(cv, args.interval)
        elif args.mode == "net":
            page_net(cv, args.interval)
        elif args.mode == "proc":
            page_proc(cv, args.interval)
        elif args.mode == "clock":
            page_clock(cv, args.interval)
        elif args.mode == "log":
            cmd = f"journalctl -u {args.unit} -f -n 20 --no-pager -o short-iso" if args.unit else "journalctl -f -n 20 --no-pager -o cat"
            page_stream(cv, f"LOG {args.unit}" if args.unit else "JOURNAL LOG", cmd, scale=args.scale)
        elif args.mode == "run":
            if not args.cmd:
                print("[ERR] --cmd required for run mode", file=sys.stderr)
                sys.exit(2)
            page_stream(cv, "RUN", args.cmd, scale=args.scale)
        elif args.mode == "watch":
            if not args.cmd:
                print("[ERR] --cmd required for watch mode", file=sys.stderr)
                sys.exit(2)
            page_watch(cv, args.cmd, interval=args.interval, scale=args.scale)
    finally:
        cv.close()


if __name__ == "__main__":
    main()
