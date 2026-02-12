#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/esp32c6-hello"

# Require WiFi credentials
: "${WIFI_SSID:?Set WIFI_SSID}"
: "${WIFI_PASS:?Set WIFI_PASS}"

# 1. Commit — stage all changes and commit (skip if working tree is clean)
if [ -n "$(git -C .. status --porcelain)" ]; then
    git -C .. add -A
    echo "Enter commit message:"
    read -r msg
    git -C .. commit -m "$msg"
else
    echo "Working tree clean, nothing to commit."
fi

# 2. Bump — increment patch version in Cargo.toml and tag
current=$(grep '^version' Cargo.toml | head -1 | sed 's/.*"\(.*\)"/\1/')
IFS='.' read -r major minor patch <<< "$current"
patch=$((patch + 1))
new_version="$major.$minor.$patch"
sed -i "s/^version = \"$current\"/version = \"$new_version\"/" Cargo.toml
git -C .. add esp32c6-hello/Cargo.toml
git -C .. commit -m "Bump version to v$new_version"
git -C .. tag "v$new_version"
echo "Bumped $current -> $new_version"

# 3. Push
git -C .. push
git -C .. push --tags
echo "Pushed to remote."

# 4. Flash
echo "Building and flashing..."
source ~/export-esp.sh

# Auto-detect serial port
if [ -e /dev/ttyACM0 ]; then
    PORT=/dev/ttyACM0
elif [ -e /dev/ttyUSB0 ]; then
    PORT=/dev/ttyUSB0
else
    echo "Error: No serial device found at /dev/ttyACM0 or /dev/ttyUSB0"
    exit 1
fi
echo "Using port: $PORT"

cargo espflash flash --release --monitor --port "$PORT"
