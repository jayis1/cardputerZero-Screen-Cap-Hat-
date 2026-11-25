#!/usr/bin/env bash
# Clean rollback script for Cardputer Zero Secondary CAP Display (Option B)

set -euo pipefail

CONFIG_TXT="/boot/firmware/config.txt"

if [[ $EUID -ne 0 ]]; then
    echo "[ERR] Please run with sudo: sudo $0" >&2
    exit 1
fi

echo "=== Uninstalling Cardputer Zero Secondary CAP Display ==="

systemctl disable --now cardputer-cap-hotkeys.service cardputer-cap-display.service 2>/dev/null || true
rm -f /etc/systemd/system/cardputer-cap-display.service \
      /etc/systemd/system/cardputer-cap-hotkeys.service \
      /etc/default/cardputer-cap-display \
      /etc/udev/rules.d/99-cardputer-cap-lcd.rules \
      /usr/local/bin/check_ext_display.sh \
      /usr/local/bin/fb_cap_tool.py \
      /usr/local/bin/fb_mirror \
      /usr/local/bin/cap_hud.py \
      /usr/local/bin/cap_supervisor.py \
      /usr/local/bin/cap_hotkeys.py \
      /usr/local/bin/cap-screen \
      /boot/firmware/overlays/cardputer-cap-display.dtbo \
      /lib/firmware/cardputer-cap-lcd.bin

if [[ -f "$CONFIG_TXT" ]]; then
    BACKUP="${CONFIG_TXT}.bak.uninstall.$(date +%Y%m%d-%H%M%S)"
    cp -a "$CONFIG_TXT" "$BACKUP"
    python3 - "$CONFIG_TXT" <<'PYEOF'
import sys

cfg_path = sys.argv[1]
lines = open(cfg_path, "r", encoding="utf-8").read().splitlines()
out = []
skip = False
for line in lines:
    if line.strip() == "# --- cardputer-cap-display begin ---":
        skip = True
        continue
    if line.strip() == "# --- cardputer-cap-display end ---":
        skip = False
        continue
    if not skip and not line.strip().startswith("dtoverlay=cardputer-cap-display"):
        out.append(line)

with open(cfg_path, "w", encoding="utf-8") as f:
    f.write("\n".join(out).rstrip() + "\n")
PYEOF
    echo "[OK] Removed overlay from $CONFIG_TXT (backup: $BACKUP)"
fi

udevadm control --reload-rules
systemctl daemon-reload
echo "[OK] Uninstall complete. Reboot to restore /dev/spidev0.1."
