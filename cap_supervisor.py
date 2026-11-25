#!/usr/bin/env python3
"""
cap_supervisor.py — Fast Mode Supervisor for Cardputer Zero Secondary CAP Display.

Managed by cardputer-cap-display.service. Listens on /run/cap-display/ctl.sock
for instant (<50ms) mode switching without restarting systemd or flickering.

Supported modes:
  mirror  (scale: smooth|nearest|center, fps: int)
  status  (system HUD + sparklines)
  net     (network / Wi-Fi / RX-TX HUD)
  proc    (top processes HUD)
  clock   (giant standby clock HUD)
  log     (live journalctl stream)
  run     (stream arbitrary command output)
  watch   (periodically refresh command output)
  tty     (interactive Linux console on tty3 mapped to /dev/fb_cap)
  ready   (clear screen, hold idle for external apps like pygame/fbi/mpv)
  test    (RGB test pattern)
  off     (clear screen, turn off GPIO23 backlight, stop active renderer)
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

RUN_DIR = "/run/cap-display"
SOCK_PATH = os.path.join(RUN_DIR, "ctl.sock")
STATE_FILE = os.path.join(RUN_DIR, "state.json")
DEFAULTS_FILE = "/etc/default/cardputer-cap-display"
BACKLIGHT_PATH = "/sys/class/backlight/cap-backlight/brightness"
EXT_5V_PATH = "/sys/class/leds/ext_5v_out/brightness"

CYCLE_ORDER = ["mirror", "status", "net", "proc", "clock", "off"]


def write_sysfs(path, val):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(val))
        return True
    except OSError:
        return False


def get_cap_fb_num():
    for cand in ("/dev/fb_cap", "/dev/fb1"):
        if os.path.exists(cand):
            real = os.path.realpath(cand)
            m = re.search(r"fb(\d+)$", real)
            if m:
                return int(m.group(1))
    return 1


def load_defaults():
    cfg = {
        "mode": "mirror",
        "scale": "smooth",
        "fps": 20,
        "interval": 1.5,
    }
    if os.path.exists(DEFAULTS_FILE):
        for line in open(DEFAULTS_FILE, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "CAP_DISPLAY_MODE":
                cfg["mode"] = v
            elif k == "CAP_MIRROR_SCALE":
                cfg["scale"] = v
            elif k == "CAP_MIRROR_FPS":
                try:
                    cfg["fps"] = int(v)
                except ValueError:
                    pass
            elif k == "CAP_STATUS_INTERVAL":
                try:
                    cfg["interval"] = float(v)
                except ValueError:
                    pass
    return cfg


def save_defaults(mode, scale, fps, interval):
    content = (
        "# Persisted defaults for cardputer-cap-display.service\n"
        f"CAP_DISPLAY_MODE={mode}\n"
        f"CAP_MIRROR_SCALE={scale}\n"
        f"CAP_MIRROR_FPS={fps}\n"
        f"CAP_STATUS_INTERVAL={interval}\n"
    )
    with open(DEFAULTS_FILE, "w", encoding="utf-8") as f:
        f.write(content)


class Supervisor:
    def __init__(self):
        cfg = load_defaults()
        self.mode = cfg["mode"]
        self.prev_active_mode = cfg["mode"] if cfg["mode"] != "off" else "mirror"
        self.scale = cfg["scale"]
        self.fps = cfg["fps"]
        self.interval = cfg["interval"]
        self.extra_cmd = ""
        self.extra_unit = ""
        self.child = None
        self.in_tty = False
        self.paused = False
        self.running = True

    def stop_child(self):
        if self.child is not None:
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=1.0)
            self.child = None

    def leave_tty(self):
        if self.in_tty:
            subprocess.run(["chvt", "1"], check=False)
            self.in_tty = False

    def clear_screen(self):
        subprocess.run(["/usr/bin/python3", "/usr/local/bin/cap_hud.py", "clear"], check=False)

    def set_backlight(self, on):
        if on:
            write_sysfs(EXT_5V_PATH, 255)
        write_sysfs(BACKLIGHT_PATH, 1 if on else 0)

    def write_state(self):
        os.makedirs(RUN_DIR, exist_ok=True)
        state = {
            "mode": self.mode,
            "prev_active_mode": self.prev_active_mode,
            "scale": self.scale,
            "fps": self.fps,
            "paused": self.paused,
            "backlight": open(BACKLIGHT_PATH).read().strip() if os.path.exists(BACKLIGHT_PATH) else "?",
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def apply_mode(self, mode, scale=None, fps=None, cmd="", unit="", interval=None, save=False):
        self.paused = False
        if scale:
            self.scale = scale
        if fps:
            self.fps = int(fps)
        if interval:
            self.interval = float(interval)
        self.extra_cmd = cmd
        self.extra_unit = unit

        self.stop_child()
        self.leave_tty()

        if mode != "off":
            self.prev_active_mode = mode
        self.mode = mode

        if mode == "off":
            self.clear_screen()
            self.set_backlight(False)
        elif mode == "ready":
            self.set_backlight(True)
            self.clear_screen()
        elif mode == "test":
            self.set_backlight(True)
            subprocess.run(["/usr/bin/python3", "/usr/local/bin/cap_hud.py", "test"], check=False)
        elif mode == "mirror":
            self.set_backlight(True)
            self.child = subprocess.Popen(
                ["/usr/local/bin/fb_mirror", "--scale", self.scale, "--fps", str(self.fps)]
            )
        elif mode in ("status", "net", "proc", "clock"):
            self.set_backlight(True)
            self.child = subprocess.Popen(
                ["/usr/bin/python3", "/usr/local/bin/cap_hud.py", mode, "--interval", str(self.interval)]
            )
        elif mode == "log":
            self.set_backlight(True)
            argv = ["/usr/bin/python3", "/usr/local/bin/cap_hud.py", "log"]
            if self.extra_unit:
                argv += ["--unit", self.extra_unit]
            self.child = subprocess.Popen(argv)
        elif mode == "run":
            self.set_backlight(True)
            self.child = subprocess.Popen(
                ["/usr/bin/python3", "/usr/local/bin/cap_hud.py", "run", "--cmd", self.extra_cmd]
            )
        elif mode == "watch":
            self.set_backlight(True)
            self.child = subprocess.Popen(
                ["/usr/bin/python3", "/usr/local/bin/cap_hud.py", "watch",
                 "--interval", str(self.interval), "--cmd", self.extra_cmd]
            )
        elif mode == "tty":
            self.set_backlight(True)
            fb_num = get_cap_fb_num()
            # Map ONLY tty3 to secondary fb_cap (never touch fb0!)
            subprocess.run(["con2fbmap", "3", str(fb_num)], check=False)
            subprocess.run(["systemctl", "start", "getty@tty3.service"], check=False)
            subprocess.run(["chvt", "3"], check=False)
            self.in_tty = True
        else:
            return {"ok": False, "error": f"Unknown mode: {mode}"}

        if save and mode in ("mirror", "status", "net", "proc", "clock", "log", "ready", "off"):
            save_defaults(mode, self.scale, self.fps, self.interval)

        self.write_state()
        return {"ok": True, "mode": self.mode, "scale": self.scale}

    def handle_request(self, req):
        action = req.get("action", "")
        if action == "info":
            self.write_state()
            return {"ok": True, **json.load(open(STATE_FILE))}
        if action == "pause":
            # Temporarily pause active renderer (e.g., while on-screen menu draws)
            self.stop_child()
            self.leave_tty()
            self.set_backlight(True)
            self.paused = True
            self.write_state()
            return {"ok": True, "mode": self.mode, "paused": True}
        if action == "resume":
            return self.apply_mode(self.mode, cmd=self.extra_cmd, unit=self.extra_unit)
        if action == "on":
            target = self.prev_active_mode if self.mode == "off" else self.mode
            return self.apply_mode(target)
        if action == "toggle":
            if self.mode == "off":
                return self.apply_mode(self.prev_active_mode)
            return self.apply_mode("off")
        if action == "toggle-tty":
            if self.mode == "tty":
                target = self.prev_active_mode if self.prev_active_mode != "tty" else "mirror"
                return self.apply_mode(target)
            return self.apply_mode("tty")
        if action == "toggle-mirror":
            if self.mode == "mirror":
                new_scale = "center" if self.scale != "center" else "smooth"
                return self.apply_mode("mirror", scale=new_scale)
            return self.apply_mode("mirror")
        if action in ("next", "prev"):
            step = 1 if action == "next" else -1
            idx = CYCLE_ORDER.index(self.mode) if self.mode in CYCLE_ORDER else -1
            nxt = CYCLE_ORDER[(idx + step) % len(CYCLE_ORDER)]
            return self.apply_mode(nxt)
        if action == "set":
            return self.apply_mode(
                req.get("mode", "mirror"),
                scale=req.get("scale"),
                fps=req.get("fps"),
                cmd=req.get("cmd", ""),
                unit=req.get("unit", ""),
                interval=req.get("interval"),
                save=bool(req.get("save", False)),
            )
        return {"ok": False, "error": f"Unknown action: {action}"}

    def serve(self):
        os.makedirs(RUN_DIR, exist_ok=True)
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o666)
        srv.listen(8)
        srv.settimeout(0.5)

        def _stop(_s, _f):
            self.running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        # Start initial boot mode
        self.apply_mode(self.mode)

        try:
            while self.running:
                # Auto-respawn crashed child if not paused/off
                if not self.paused and self.child is not None and self.child.poll() is not None:
                    if self.mode in ("mirror", "status", "net", "proc", "clock"):
                        time.sleep(1.0)
                        if self.running:
                            self.apply_mode(self.mode)

                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                with conn:
                    conn.settimeout(2.0)
                    raw = b""
                    while b"\n" not in raw and len(raw) < 16384:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        raw += chunk
                    try:
                        req = json.loads(raw.decode("utf-8").strip())
                        resp = self.handle_request(req)
                    except Exception as e:
                        resp = {"ok": False, "error": str(e)}
                    try:
                        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                    except OSError:
                        pass
        finally:
            self.stop_child()
            self.leave_tty()
            self.clear_screen()
            self.set_backlight(False)
            srv.close()
            if os.path.exists(SOCK_PATH):
                try:
                    os.unlink(SOCK_PATH)
                except OSError:
                    pass


if __name__ == "__main__":
    Supervisor().serve()
