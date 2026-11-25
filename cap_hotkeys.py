#!/usr/bin/env python3
"""
cap_hotkeys.py — Passive Keyboard Hotkey Daemon & Interactive On-Screen Menu
for the M5Stack Cardputer Zero Secondary CAP Display.

Reads /dev/input/cardputer-zero-internal (TCA8418 matrix keyboard) passively
without interfering with normal typing or M5CardputerZero launcher input.

Hotkeys (Ctrl + Alt + Key):
  Ctrl + Alt + Space : Open Interactive Top-Screen Mode Menu (1-9 / W,S / Arrows + Enter, Esc cancels)
  Ctrl + Alt + N     : Cycle to Next mode (Mirror -> Sys HUD -> Net -> Procs -> Clock -> Off)
  Ctrl + Alt + B     : Toggle Top Screen Sleep / Backlight On-Off
  Ctrl + Alt + M     : Mirror mode (press again to toggle Smooth Fill <-> 1:1 Center)
  Ctrl + Alt + S     : System Telemetry + Sparklines HUD
  Ctrl + Alt + W     : Network / Wi-Fi / RX-TX Throughput HUD
  Ctrl + Alt + P     : Top Processes HUD
  Ctrl + Alt + C     : Big Cyberdeck Clock Standby HUD
  Ctrl + Alt + T     : Toggle Interactive Linux Terminal (TTY3) on Top Screen
"""

import argparse
import fcntl
import json
import os
import select
import signal
import socket
import struct
import sys
import time

# Allow importing Canvas/render_menu from cap_hud in same directory or /usr/local/bin
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/usr/local/bin")
import cap_hud  # noqa: E402

INPUT_DEV = "/dev/input/cardputer-zero-internal"
SOCK_PATH = "/run/cap-display/ctl.sock"

# Linux evdev constants
EV_KEY = 0x01
EVIOCGRAB = 0x40044590
EVENT_STRUCT = struct.Struct("llHHi")

# Keycodes (Linux input-event-codes.h, verified against cardputerzero-v5-overlay.dtbo)
KEY_ESC = 1
KEY_1 = 2
KEY_2 = 3
KEY_3 = 4
KEY_4 = 5
KEY_5 = 6
KEY_6 = 7
KEY_7 = 8
KEY_8 = 9
KEY_9 = 10
KEY_Q = 16
KEY_W = 17
KEY_T = 20
KEY_P = 25
KEY_ENTER = 28
KEY_LEFTCTRL = 29
KEY_RIGHTCTRL = 97
KEY_S = 31
KEY_J = 36
KEY_K = 37
KEY_L = 38
KEY_C = 46
KEY_B = 48
KEY_N = 49
KEY_M = 50
KEY_LEFTALT = 56
KEY_RIGHTALT = 100
KEY_SPACE = 57
KEY_UP = 103
KEY_DOWN = 108

MENU_ITEMS = [
    ("mirror-smooth", "MIRROR (FILL)"),
    ("mirror-center", "MIRROR (1:1)"),
    ("status",        "SYS HUD + GRAPH"),
    ("net",           "NET / WIFI HUD"),
    ("proc",          "TOP PROCESSES"),
    ("log",           "LIVE JOURNAL"),
    ("clock",         "BIG CLOCK"),
    ("tty",           "INTERACTIVE TTY"),
    ("off",           "SCREEN SLEEP"),
]


def send_ctl(payload):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(SOCK_PATH)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def activate_menu_choice(key):
    if key == "mirror-smooth":
        return send_ctl({"action": "set", "mode": "mirror", "scale": "smooth"})
    if key == "mirror-center":
        return send_ctl({"action": "set", "mode": "mirror", "scale": "center"})
    return send_ctl({"action": "set", "mode": key})


def run_interactive_menu(fd):
    """
    Temporarily grabs the keyboard (auto-released in finally & on 12s timeout),
    renders the mode menu directly onto /dev/fb_cap, and applies the user's pick.
    """
    info = send_ctl({"action": "info"})
    cur_mode = info.get("mode", "mirror")
    cur_scale = info.get("scale", "smooth")

    # Pre-select current mode in the list
    sel = 0
    for idx, (k, _lbl) in enumerate(MENU_ITEMS):
        if k == cur_mode or (cur_mode == "mirror" and k == f"mirror-{cur_scale}"):
            sel = idx
            break

    send_ctl({"action": "pause"})
    cv = cap_hud.Canvas()
    grabbed = False
    chosen = None

    try:
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
            grabbed = True
        except OSError:
            pass

        # Drain any queued release events from the hotkey chord
        while select.select([fd], [], [], 0.05)[0]:
            os.read(fd, EVENT_STRUCT.size * 8)

        cap_hud.render_menu(cv, MENU_ITEMS, sel, current_mode=cur_mode)
        deadline = time.monotonic() + 12.0

        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.25)
            if not r:
                continue
            raw = os.read(fd, EVENT_STRUCT.size)
            if len(raw) < EVENT_STRUCT.size:
                continue
            _sec, _usec, ev_type, code, value = EVENT_STRUCT.unpack(raw)
            if ev_type != EV_KEY or value != 1:  # fresh key press only
                continue

            deadline = time.monotonic() + 12.0

            if code in (KEY_ESC, KEY_Q):
                chosen = None
                break
            elif code in (KEY_UP, KEY_W, KEY_K):
                sel = (sel - 1) % len(MENU_ITEMS)
                cap_hud.render_menu(cv, MENU_ITEMS, sel, current_mode=cur_mode)
            elif code in (KEY_DOWN, KEY_S, KEY_J):
                sel = (sel + 1) % len(MENU_ITEMS)
                cap_hud.render_menu(cv, MENU_ITEMS, sel, current_mode=cur_mode)
            elif code in (KEY_ENTER, KEY_SPACE):
                chosen = MENU_ITEMS[sel][0]
                break
            elif KEY_1 <= code <= KEY_9:
                idx = code - KEY_1
                if idx < len(MENU_ITEMS):
                    chosen = MENU_ITEMS[idx][0]
                    break
    finally:
        if grabbed:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
            except OSError:
                pass
        cv.close()

    if chosen is not None:
        activate_menu_choice(chosen)
    else:
        send_ctl({"action": "resume"})


def open_keyboard_dev(path):
    while True:
        if os.path.exists(path):
            try:
                return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                pass
        time.sleep(1.0)


def daemon_loop(dev_path):
    running = True

    def _stop(_s, _f):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    ctrl_down = False
    alt_down = False
    last_trigger = 0.0

    fd = open_keyboard_dev(dev_path)
    print(f"[OK] cap_hotkeys listening on {dev_path}", flush=True)

    try:
        while running:
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
            except OSError:
                os.close(fd)
                fd = open_keyboard_dev(dev_path)
                ctrl_down = alt_down = False
                continue

            if not r:
                continue

            try:
                buf = os.read(fd, EVENT_STRUCT.size * 16)
            except BlockingIOError:
                continue
            except OSError:
                os.close(fd)
                fd = open_keyboard_dev(dev_path)
                ctrl_down = alt_down = False
                continue

            for off in range(0, len(buf) - (len(buf) % EVENT_STRUCT.size), EVENT_STRUCT.size):
                _sec, _usec, ev_type, code, value = EVENT_STRUCT.unpack_from(buf, off)
                if ev_type != EV_KEY:
                    continue

                if code in (KEY_LEFTCTRL, KEY_RIGHTCTRL):
                    ctrl_down = value != 0
                    continue
                if code in (KEY_LEFTALT, KEY_RIGHTALT):
                    alt_down = value != 0
                    continue

                if value != 1 or not (ctrl_down and alt_down):
                    continue

                now = time.monotonic()
                if now - last_trigger < 0.25:
                    continue

                if code == KEY_SPACE:
                    last_trigger = now
                    run_interactive_menu(fd)
                    ctrl_down = alt_down = False
                    break
                elif code == KEY_N:
                    last_trigger = now
                    send_ctl({"action": "next"})
                elif code == KEY_B:
                    last_trigger = now
                    send_ctl({"action": "toggle"})
                elif code == KEY_M:
                    last_trigger = now
                    send_ctl({"action": "toggle-mirror"})
                elif code == KEY_S:
                    last_trigger = now
                    send_ctl({"action": "set", "mode": "status"})
                elif code == KEY_W:
                    last_trigger = now
                    send_ctl({"action": "set", "mode": "net"})
                elif code == KEY_P:
                    last_trigger = now
                    send_ctl({"action": "set", "mode": "proc"})
                elif code == KEY_C:
                    last_trigger = now
                    send_ctl({"action": "set", "mode": "clock"})
                elif code == KEY_L:
                    last_trigger = now
                    send_ctl({"action": "set", "mode": "log"})
                elif code == KEY_T:
                    last_trigger = now
                    send_ctl({"action": "toggle-tty"})
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description="Cardputer Zero CAP Display Keyboard Hotkey Daemon")
    parser.add_argument("--device", default=INPUT_DEV)
    parser.add_argument("--open-menu", action="store_true", help="Open interactive menu once and exit")
    args = parser.parse_args()

    if args.open_menu:
        fd = open_keyboard_dev(args.device)
        try:
            run_interactive_menu(fd)
        finally:
            os.close(fd)
        return

    daemon_loop(args.device)


if __name__ == "__main__":
    main()
