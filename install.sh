#!/usr/bin/env bash
# Safe installer for M5Stack Cardputer Zero Secondary SPI CAP Display (Option B: /dev/fb1)
#
# Guardrails:
#   - Backs up /boot/firmware/config.txt with a timestamp before any edit.
#   - Never modifies or disables cardputerzero-v5-overlay, /dev/fb0, or spidev0.0.
#   - Never binds GPIO23 or GPIO26 to USB host mode.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_TXT="/boot/firmware/config.txt"
OVERLAY_DST="/boot/firmware/overlays/cardputer-cap-display.dtbo"
FW_DST="/lib/firmware/cardputer-cap-lcd.bin"
UDEV_DST="/etc/udev/rules.d/99-cardputer-cap-lcd.rules"
SERVICE_DST="/etc/systemd/system/cardputer-cap-display.service"
DEFAULTS_DST="/etc/default/cardputer-cap-display"

PRESET="st7789-320x240"
WIDTH=""
HEIGHT=""
X_OFFSET="0"
Y_OFFSET="0"
SPEED="32000000"
SPI_MODE="0"
ROTATION=""
MADCTL=""
COLOR_FLAG=""
INV_FLAG=""
MODE="mirror"
MIRROR_SCALE="smooth"
MIRROR_FPS="20"
ENABLE_SERVICE=1

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [options]
  --preset NAME       Panel preset (st7789-320x240 [default], st7789-320x240-noinv,
                      ili9341-320x240, st7789-240x240, st7735-160x128)
  --width PX          Override panel width (default from preset)
  --height PX         Override panel height (default from preset)
  --x-offset PX       Panel GRAM X offset (default: 0)
  --y-offset PX       Panel GRAM Y offset (default: 0)
  --rotation DEG      Override rotation (0, 90, 180, 270)
  --madctl HEX        Override raw MADCTL byte (e.g. 0x68, 0xA8, 0x28, 0xE8)
  --bgr / --rgb       Override RGB/BGR color order
  --invert/--no-invert Override display inversion
  --speed HZ          SPI clock in Hz (default: 32000000)
  --spi-mode 0|3      SPI clock mode (default: 0; pass 3 for CPOL=1,CPHA=1)
  --mode MODE         Service boot mode: mirror (default), status, ready, or test
  --scale MODE        Mirror scale mode: smooth (default), nearest, or center
  --fps N             Mirror max FPS (default: 20)
  --no-enable         Install systemd service without enabling at boot
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --preset)     PRESET="$2"; shift ;;
        --width)      WIDTH="$2"; shift ;;
        --height)     HEIGHT="$2"; shift ;;
        --x-offset)   X_OFFSET="$2"; shift ;;
        --y-offset)   Y_OFFSET="$2"; shift ;;
        --rotation)   ROTATION="$2"; shift ;;
        --madctl)     MADCTL="$2"; shift ;;
        --bgr)        COLOR_FLAG="--bgr" ;;
        --rgb)        COLOR_FLAG="--rgb" ;;
        --invert)     INV_FLAG="--invert" ;;
        --no-invert)  INV_FLAG="--no-invert" ;;
        --speed)      SPEED="$2"; shift ;;
        --spi-mode)   SPI_MODE="$2"; shift ;;
        --mode)       MODE="$2"; shift ;;
        --scale)      MIRROR_SCALE="$2"; shift ;;
        --fps)        MIRROR_FPS="$2"; shift ;;
        --no-enable)  ENABLE_SERVICE=0 ;;
        -h|--help)    usage; exit 0 ;;
        *)            echo "[ERR] Unknown option: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

if [[ $EUID -ne 0 ]]; then
    echo "[ERR] Please run with sudo: sudo $0 $*" >&2
    exit 1
fi

for tool in dtc python3 systemctl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[ERR] Required tool '$tool' not found." >&2
        exit 1
    fi
done

# Default width/height per preset if not overridden
if [[ -z "$WIDTH" || -z "$HEIGHT" ]]; then
    case "$PRESET" in
        term512-ili9341|st7789-320x240|st7789-320x240-noinv|ili9341-320x240)
            WIDTH="${WIDTH:-320}"; HEIGHT="${HEIGHT:-240}" ;;
        st7789-240x240)
            WIDTH="${WIDTH:-240}"; HEIGHT="${HEIGHT:-240}" ;;
        st7735-160x128)
            WIDTH="${WIDTH:-160}"; HEIGHT="${HEIGHT:-128}" ;;
        *)
            WIDTH="${WIDTH:-320}"; HEIGHT="${HEIGHT:-240}" ;;
    esac
fi

echo "=== Installing Cardputer Zero Secondary CAP Display (Option B) ==="
echo "  Preset   : $PRESET (${WIDTH}x${HEIGHT}, offset=${X_OFFSET},${Y_OFFSET})"
echo "  SPI      : spi0.1 @ ${SPEED} Hz (mode ${SPI_MODE})"
echo "  Service  : mode=${MODE}"

# 1. Generate & verify MIPI-DBI firmware binary
FW_ARGS=(--preset "$PRESET" -o "$FW_DST")
[[ -n "$ROTATION" ]] && FW_ARGS+=(--rotation "$ROTATION")
[[ -n "$MADCTL" ]] && FW_ARGS+=(--madctl "$MADCTL")
[[ -n "$COLOR_FLAG" ]] && FW_ARGS+=("$COLOR_FLAG")
[[ -n "$INV_FLAG" ]] && FW_ARGS+=("$INV_FLAG")

python3 "${SCRIPT_DIR}/gen_panel_fw.py" "${FW_ARGS[@]}"
python3 "${SCRIPT_DIR}/gen_panel_fw.py" --verify "$FW_DST" >/dev/null
echo "[OK] Installed verified firmware: $FW_DST"

# 2. Compile & install Device Tree Overlay
dtc -@ -I dts -O dtb -Wno-unit_address_vs_reg -o "$OVERLAY_DST" "${SCRIPT_DIR}/cardputer-cap-display.dts"
echo "[OK] Compiled & installed overlay: $OVERLAY_DST"

# 3. Compile fb_mirror daemon if gcc is available
if command -v gcc >/dev/null 2>&1 && [[ -f "${SCRIPT_DIR}/fb_mirror.c" ]]; then
    gcc -O2 -Wall -Wextra -o /usr/local/bin/fb_mirror "${SCRIPT_DIR}/fb_mirror.c"
    chmod 0755 /usr/local/bin/fb_mirror
    echo "[OK] Compiled & installed /usr/local/bin/fb_mirror"
fi

# 4. Install scripts, udev rule, defaults, and systemd units
install -m 0755 "${SCRIPT_DIR}/check_ext_display.sh" /usr/local/bin/check_ext_display.sh
install -m 0755 "${SCRIPT_DIR}/fb_cap_tool.py" /usr/local/bin/fb_cap_tool.py
install -m 0755 "${SCRIPT_DIR}/cap_hud.py" /usr/local/bin/cap_hud.py
install -m 0755 "${SCRIPT_DIR}/cap_supervisor.py" /usr/local/bin/cap_supervisor.py
install -m 0755 "${SCRIPT_DIR}/cap_hotkeys.py" /usr/local/bin/cap_hotkeys.py
install -m 0755 "${SCRIPT_DIR}/cap-screen" /usr/local/bin/cap-screen
install -m 0644 "${SCRIPT_DIR}/99-cardputer-cap-lcd.rules" "$UDEV_DST"
install -m 0644 "${SCRIPT_DIR}/cardputer-cap-display.service" "$SERVICE_DST"
install -m 0644 "${SCRIPT_DIR}/cardputer-cap-hotkeys.service" /etc/systemd/system/cardputer-cap-hotkeys.service

cat > "$DEFAULTS_DST" <<EOF
# Mode for cardputer-cap-display.service:
#   mirror - live mirror built-in screen (/dev/fb_lcd) onto /dev/fb_cap
#   status - System telemetry + live CPU sparkline HUD
#   net    - Network / Wi-Fi / RX-TX throughput HUD
#   proc   - Top processes HUD
#   clock  - Giant cyberdeck clock standby HUD
#   log    - Live scrolling journalctl feed
#   ready  - initialize & clear /dev/fb_cap (ready for pygame, fbi, mpv, etc.)
#   off    - start with top screen asleep (wake via cap-screen or Ctrl+Alt+B)
CAP_DISPLAY_MODE=${MODE}
CAP_MIRROR_SCALE=${MIRROR_SCALE}
CAP_MIRROR_FPS=${MIRROR_FPS}
CAP_STATUS_INTERVAL=1.5
EOF
echo "[OK] Installed scripts, cap-screen CLI, HUD, hotkeys, udev rule, and systemd units"

# 4. Back up and safely update /boot/firmware/config.txt
if [[ ! -f "$CONFIG_TXT" ]]; then
    echo "[ERR] $CONFIG_TXT not found!" >&2
    exit 1
fi

BACKUP="${CONFIG_TXT}.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$CONFIG_TXT" "$BACKUP"
echo "[OK] Backed up config.txt -> $BACKUP"

OVERLAY_LINE="dtoverlay=cardputer-cap-display,width=${WIDTH},height=${HEIGHT},speed=${SPEED}"
[[ "$X_OFFSET" != "0" ]] && OVERLAY_LINE="${OVERLAY_LINE},x-offset=${X_OFFSET}"
[[ "$Y_OFFSET" != "0" ]] && OVERLAY_LINE="${OVERLAY_LINE},y-offset=${Y_OFFSET}"
if [[ "$SPI_MODE" == "3" ]]; then
    OVERLAY_LINE="${OVERLAY_LINE},cpol,cpha"
fi

# Remove any prior cardputer-cap-display block (idempotent reinstall) while leaving everything else intact
python3 - "$CONFIG_TXT" "$OVERLAY_LINE" <<'PYEOF'
import sys

cfg_path, overlay_line = sys.argv[1], sys.argv[2]
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

# Ensure trailing block is under [all] at the end (after cardputerzero-v5-overlay)
out.extend([
    "",
    "# --- cardputer-cap-display begin ---",
    "# Secondary SPI CAP display on EXT 2.54-14P (SPI0 CE1, DC=22, RST=26, BL=23)",
    overlay_line,
    "# --- cardputer-cap-display end ---",
])

with open(cfg_path, "w", encoding="utf-8") as f:
    f.write("\n".join(out).rstrip() + "\n")
PYEOF
echo "[OK] Added '${OVERLAY_LINE}' to ${CONFIG_TXT}"

# 5. Reload udev & systemd
udevadm control --reload-rules
systemctl daemon-reload
if [[ $ENABLE_SERVICE -eq 1 ]]; then
    systemctl enable cardputer-cap-display.service cardputer-cap-hotkeys.service
    echo "[OK] Enabled cardputer-cap-display.service and cardputer-cap-hotkeys.service"
fi

echo
echo "=== Installation complete! ==="
echo "Quick usage:"
echo "  cap-screen menu                  # Open interactive menu on top screen"
echo "  cap-screen mirror [center]       # Live mirror built-in screen"
echo "  cap-screen status | net | proc | clock | log | tty | off"
echo "  cap-screen run \"ping 1.1.1.1\"    # Stream any command onto top screen"
echo "  Keyboard: Ctrl+Alt+Space (Menu), Ctrl+Alt+N (Next), Ctrl+Alt+B (Sleep), Ctrl+Alt+M (Mirror), Ctrl+Alt+T (TTY)"
