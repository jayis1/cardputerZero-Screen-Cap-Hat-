#!/usr/bin/env bash
# Hardware enablement & verification script for secondary EXT 2.54-14P display
# on M5Stack Cardputer Zero (Rev v5 / CM0 / BCM2835).
#
# Guardrails:
#   - Never disables or resets I2C1 or internal SPI0 CE0 (/dev/fb_lcd).
#   - Ensures ext_usb_gpio_fun=0 so GPIO23 (BL) and GPIO26 (RST) stay in GPIO mode.
#   - Enables Pin 6 5V OUT via M5IOE1 kernel LED sysfs (/sys/class/leds/ext_5v_out).

set -euo pipefail

EXT_5V_LED="/sys/class/leds/ext_5v_out/brightness"
EXT_USB_MUX="/sys/class/leds/ext_usb_gpio_fun/brightness"
CAP_BACKLIGHT="/sys/class/backlight/cap-backlight/brightness"
SPI_DEV_NAME="spi0.1"
SPI_DEV_DIR="/sys/bus/spi/devices/${SPI_DEV_NAME}"

ENABLE_POWER=0
ENSURE_READY=0
DO_REINIT=0
DO_I2C_SCAN=0

usage() {
    cat <<'EOF'
Usage: check_ext_display.sh [options]
  --enable-power   Ensure Pin 6 5V OUT (ext_5v_out) is enabled and USB4 mux is off
  --ensure-ready   Enable power, verify /dev/fb_cap (or /dev/fb1), and turn backlight on
  --reinit         Unbind & rebind spi0.1 on panel-mipi-dbi after confirming 5V power
  --i2c-scan       Safely run i2cdetect -y 1 and report any external I2C display
  -h, --help       Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --enable-power) ENABLE_POWER=1 ;;
        --ensure-ready) ENABLE_POWER=1; ENSURE_READY=1 ;;
        --reinit)       ENABLE_POWER=1; DO_REINIT=1 ;;
        --i2c-scan)     DO_I2C_SCAN=1 ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "[ERR] Unknown option: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

ok()   { printf '[OK]   %s\n' "$*"; }
info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
err()  { printf '[ERR]  %s\n' "$*" >&2; }

echo "=== Cardputer Zero EXT Secondary Display Check ==="

# 1. Verify primary internal display is untouched
if [[ -e /dev/fb_lcd ]]; then
    ok "Primary internal display intact: /dev/fb_lcd -> $(readlink -f /dev/fb_lcd)"
elif [[ -e /dev/fb0 ]]; then
    ok "Primary framebuffer present: /dev/fb0"
else
    warn "Primary internal framebuffer (/dev/fb_lcd / /dev/fb0) not found!"
fi

# 2. Verify Pin 1 (GPIO26) & Pin 3 (GPIO23) are NOT muxed to USB4
if [[ -r "$EXT_USB_MUX" ]]; then
    mux_val="$(cat "$EXT_USB_MUX")"
    if [[ "$mux_val" != "0" ]]; then
        if [[ $ENABLE_POWER -eq 1 ]]; then
            info "Switching ext_usb_gpio_fun from $mux_val -> 0 (GPIO mode for GPIO23/GPIO26)..."
            echo 0 > "$EXT_USB_MUX"
            ok "ext_usb_gpio_fun=0 (GPIO mode restored)"
        else
            warn "ext_usb_gpio_fun=$mux_val (USB4 mode active! GPIO23/GPIO26 muxed away from header)"
            warn "Run with --enable-power or: echo 0 | sudo tee $EXT_USB_MUX"
        fi
    else
        ok "ext_usb_gpio_fun=0 (GPIO23=BL and GPIO26=RST available on 14P header)"
    fi
else
    warn "$EXT_USB_MUX not found (non-v5 kernel or m5ioe1 driver missing)"
fi

# 3. Verify / enable Pin 6 5V OUT (M5IOE1 PYG13_HAT_EN / ext_5v_out)
POWER_JUST_ENABLED=0
if [[ -r "$EXT_5V_LED" ]]; then
    pwr_val="$(cat "$EXT_5V_LED")"
    if [[ "$pwr_val" == "0" ]]; then
        if [[ $ENABLE_POWER -eq 1 ]]; then
            info "Enabling Pin 6 5V OUT via $EXT_5V_LED..."
            echo 255 > "$EXT_5V_LED"
            sleep 0.25
            POWER_JUST_ENABLED=1
            ok "ext_5v_out=$(cat "$EXT_5V_LED") (Pin 6 5V rail enabled)"
        else
            warn "ext_5v_out=0 (Pin 6 5V OUT is OFF; run with --enable-power to turn it on)"
        fi
    else
        ok "ext_5v_out=$pwr_val (Pin 6 5V OUT active)"
    fi
else
    warn "$EXT_5V_LED not found"
fi

# 4. Check SPI0 CE1 state (either user-space /dev/spidev0.1 OR kernel display driver on spi0.1)
SPI_DRV_PATH=""
SPI_DRV_NAME=""
if [[ -L "${SPI_DEV_DIR}/driver" ]]; then
    SPI_DRV_PATH="$(readlink -f "${SPI_DEV_DIR}/driver")"
    SPI_DRV_NAME="$(basename "$SPI_DRV_PATH")"
fi

if [[ -n "$SPI_DRV_NAME" && "$SPI_DRV_NAME" != "spidev" ]]; then
    ok "Kernel driver '${SPI_DRV_NAME}' bound to ${SPI_DEV_NAME}"

    # If 5V rail was just switched on (cold boot race) or --reinit requested, rebind spi0.1
    if [[ $DO_REINIT -eq 1 || $POWER_JUST_ENABLED -eq 1 ]]; then
        if [[ $EUID -eq 0 ]]; then
            info "Re-initializing ${SPI_DEV_NAME} now that 5V power is stable..."
            echo "$SPI_DEV_NAME" > "${SPI_DRV_PATH}/unbind" || true
            sleep 0.15
            echo "$SPI_DEV_NAME" > "${SPI_DRV_PATH}/bind"
            sleep 0.3
            ok "Rebound ${SPI_DEV_NAME}"
        else
            warn "Re-init requested but not root; run with sudo"
        fi
    fi

    # Wait briefly for udev symlink /dev/fb_cap if --ensure-ready
    if [[ $ENSURE_READY -eq 1 ]]; then
        for _ in $(seq 1 20); do
            [[ -e /dev/fb_cap ]] && break
            sleep 0.1
        done
    fi

    if [[ -e /dev/fb_cap ]]; then
        fb_real="$(readlink -f /dev/fb_cap)"
        fb_name="$(basename "$fb_real")"
        res="$(cat "/sys/class/graphics/${fb_name}/virtual_size" 2>/dev/null || echo 'unknown')"
        bpp="$(cat "/sys/class/graphics/${fb_name}/bits_per_pixel" 2>/dev/null || echo '?')"
        ok "Secondary framebuffer ready: /dev/fb_cap -> ${fb_real} (${res}, ${bpp}bpp)"
    elif [[ -e /dev/fb1 ]]; then
        ok "Secondary framebuffer ready: /dev/fb1 (udev symlink /dev/fb_cap not yet present)"
    else
        warn "Driver '${SPI_DRV_NAME}' bound to ${SPI_DEV_NAME}, but no /dev/fb_cap or /dev/fb1 found (check dmesg | grep -iE 'mipi|spi|drm')"
        [[ $ENSURE_READY -eq 1 ]] && exit 1
    fi

    if [[ -e /dev/dri/cardputer-zero-cap ]]; then
        ok "Secondary DRM card ready: /dev/dri/cardputer-zero-cap -> $(readlink -f /dev/dri/cardputer-zero-cap)"
    fi
elif [[ -e /dev/spidev0.1 ]]; then
    ok "/dev/spidev0.1 active (user-space SPI mode; Option B DT overlay not loaded yet)"
    if [[ $ENSURE_READY -eq 1 ]]; then
        warn "--ensure-ready expected kernel display driver (/dev/fb_cap), found raw /dev/spidev0.1"
    fi
else
    err "Neither a kernel display driver on ${SPI_DEV_NAME} nor /dev/spidev0.1 is present!"
    exit 1
fi

# 5. Backlight check
if [[ -e "$CAP_BACKLIGHT" ]]; then
    if [[ $ENSURE_READY -eq 1 && -w "$CAP_BACKLIGHT" ]]; then
        echo 1 > "$CAP_BACKLIGHT"
    fi
    ok "Backlight /sys/class/backlight/cap-backlight/brightness=$(cat "$CAP_BACKLIGHT")"
else
    info "No /sys/class/backlight/cap-backlight yet (normal before Option B overlay is installed)"
fi

# 6. Verify shared I2C1 bus (without resetting or claiming it)
if [[ -e /dev/i2c-1 ]]; then
    ok "/dev/i2c-1 active (shared with internal ES8390@0x10, RX8130@0x32, TCA8418@0x34, M5IOE1@0x4F, BQ27220@0x55, IMU@0x68)"
    if [[ $DO_I2C_SCAN -eq 1 ]]; then
        if command -v i2cdetect >/dev/null 2>&1; then
            info "Scanning /dev/i2c-1 (read-only probe, preserving kernel-bound UU devices)..."
            scan_out="$(i2cdetect -y 1)"
            echo "$scan_out"
            if echo "$scan_out" | grep -qE '\b3c\b|\b3d\b'; then
                ok "Detected external I2C OLED display at 0x3C/0x3D!"
            else
                info "No external I2C OLED detected at 0x3C/0x3D."
            fi
        else
            warn "i2cdetect not installed (install i2c-tools to scan I2C1)"
        fi
    fi
else
    warn "/dev/i2c-1 not found"
fi
