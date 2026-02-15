#!/bin/bash
#
# Camera snapshot script for Logitech BRIO on Raspberry Pi 5.
#
# Usage:
#   ./snapshot.sh              # default: zoom into OLED
#   ./snapshot.sh wide         # full wide view (no zoom)
#   ./snapshot.sh zoom 300     # custom zoom level (100-500)
#   ./snapshot.sh focus 40     # manual focus at value 40 (0-255)
#
# Camera controls (v4l2-ctl -d /dev/video0 --set-ctrl key=val):
#   zoom_absolute:   100 (wide) to 500 (5x zoom)
#   pan_absolute:    -36000 to 36000
#   tilt_absolute:   -36000 to 36000
#   focus_absolute:  0 (infinity) to 255 (macro) — disable autofocus first
#   focus_automatic_continuous: 0 (manual) / 1 (auto)
#   brightness:      0-255 (default 128)
#   contrast:        0-255 (default 128)
#   sharpness:       0-255 (default 128)

set -euo pipefail

CAM=/dev/video0
DIR=/home/bisenbek/projects/esp/camera
RES=1920x1080
TS=$(date +%s)

mkdir -p "${DIR}/snapshots" 2>/dev/null || true

# Parse command
MODE="${1:-default}"
case "$MODE" in
    wide)
        v4l2-ctl -d "$CAM" --set-ctrl zoom_absolute=100
        v4l2-ctl -d "$CAM" --set-ctrl focus_automatic_continuous=1
        ;;
    zoom)
        LEVEL="${2:-300}"
        v4l2-ctl -d "$CAM" --set-ctrl zoom_absolute="$LEVEL"
        ;;
    focus)
        VAL="${2:-30}"
        v4l2-ctl -d "$CAM" --set-ctrl focus_automatic_continuous=0
        v4l2-ctl -d "$CAM" --set-ctrl focus_absolute="$VAL"
        ;;
    default)
        # Dialed-in defaults for OLED closeup
        v4l2-ctl -d "$CAM" --set-ctrl zoom_absolute=150
        v4l2-ctl -d "$CAM" --set-ctrl focus_automatic_continuous=0
        sleep 0.2
        v4l2-ctl -d "$CAM" --set-ctrl focus_absolute=30
        v4l2-ctl -d "$CAM" --set-ctrl sharpness=180
        ;;
esac

# Capture — skip first 2s for auto-exposure/focus to settle
ffmpeg -y \
    -f video4linux2 \
    -s "$RES" \
    -i "$CAM" \
    -ss 0:0:2 \
    -frames 1 \
    -q:v 2 \
    "${DIR}/snapshots/${TS}.jpg" \
    2>"${DIR}/snapshots/log.txt"

cp "${DIR}/snapshots/${TS}.jpg" "${DIR}/snapshot.jpg"
