# ESP32-C6 Rust on Linux — Bootstrap Cheat Sheet

## 1. USB Serial Setup

The ESP32-C6 DevKitC shows up as a serial device when plugged in. Linux includes the drivers (cp210x / cdc_acm) out of the box.

```bash
# Plug in the board, then check it's detected
dmesg | tail -20            # look for "cp210x" or "cdc_acm" and note the device
ls /dev/ttyUSB*             # usually /dev/ttyUSB0
ls /dev/ttyACM*             # or sometimes /dev/ttyACM0

# Grant yourself serial access (avoids needing sudo)
sudo usermod -aG dialout $USER
# >>> LOG OUT AND BACK IN for this to take effect <<<

# Quick sanity check — open a serial monitor (Ctrl+] to quit)
picocom -b 115200 /dev/ttyUSB0
# or: screen /dev/ttyUSB0 115200
```

> **Tip:** If you see permission denied after adding to `dialout`, you *must* log out/in (or reboot). Running `newgrp dialout` in your current shell is a quick workaround.


## 2. Install Rust Toolchain

```bash
# Install Rust (if not already installed)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

# Install ESP toolchain manager
cargo install espup

# Install the ESP Rust toolchain (downloads RISC-V target, LLVM, etc.)
espup install

# Source the generated environment file (add to .bashrc/.zshrc for persistence)
source ~/export-esp.sh
# or: . $HOME/export-esp.sh
```


## 3. Install Flashing & Dev Tools

```bash
# Flasher
cargo install cargo-espflash

# Optional but useful
cargo install espflash          # standalone flasher (no cargo integration)
cargo install cargo-generate    # project templates
cargo install cargo-espmonitor  # serial monitor integrated with cargo

# System dependencies (Ubuntu/Debian)
sudo apt install -y git curl gcc build-essential pkg-config \
    libudev-dev libssl-dev python3 python3-pip python3-venv
```


## 4. Create a New Project

### Option A: std (ESP-IDF based) — recommended to start
```bash
cargo generate esp-rs/esp-idf-template
# When prompted:
#   - Project name: your choice
#   - MCU: esp32c6
#   - Dev container: false
#   - STD support: true
```

### Option B: no_std (bare metal)
```bash
cargo generate esp-rs/esp-template
# When prompted:
#   - MCU: esp32c6
```


## 5. Build & Flash

```bash
cd your-project-name

# Build
cargo build --release

# Flash and open serial monitor in one shot
cargo espflash flash --release --monitor

# Or separately:
cargo espflash flash --release
cargo espmonitor /dev/ttyUSB0
```

> **If flash fails:** Hold the **BOOT** button on the board, press **RESET**, then release BOOT. This forces the chip into download mode. Most of the time auto-reset works fine though.


## 6. Common Serial Monitor Commands

```bash
# Using cargo-espmonitor
cargo espmonitor /dev/ttyUSB0

# Using espflash's built-in monitor
espflash monitor

# Using picocom (lightweight, always available)
picocom -b 115200 /dev/ttyUSB0       # Ctrl+A then Ctrl+X to quit

# Using screen
screen /dev/ttyUSB0 115200            # Ctrl+A then K to quit
```


## 7. Useful Cargo Commands

```bash
cargo build --release          # Build without flashing
cargo clean                    # Clear build artifacts
cargo doc --open               # Generate & view docs for your deps
cargo espflash board-info      # Print chip info (verify connection)
cargo espflash erase-flash     # Wipe the entire flash
cargo espflash save-image --release --chip esp32c6 firmware.bin
                               # Save binary without flashing
```


## 8. Project Structure (std / ESP-IDF template)

```
my-project/
├── .cargo/
│   └── config.toml        # target, linker, runner config
├── src/
│   └── main.rs            # your code starts here
├── sdkconfig.defaults     # ESP-IDF config overrides
├── build.rs               # build script for ESP-IDF integration
└── Cargo.toml             # dependencies
```


## 9. Key Crates for ESP32-C6

| Crate | Purpose |
|---|---|
| `esp-idf-svc` | Wi-Fi, BLE, HTTP, MQTT, NVS, GPIO (std) |
| `esp-idf-hal` | Hardware abstraction (std) |
| `esp-hal` | Hardware abstraction (no_std) |
| `esp-wifi` | Wi-Fi/BLE for no_std |
| `embedded-hal` | Portable embedded traits |
| `heapless` | Stack-allocated data structures (no_std) |
| `log` / `esp-println` | Logging to serial |


## 10. Troubleshooting

| Problem | Fix |
|---|---|
| `/dev/ttyUSB0` permission denied | `sudo usermod -aG dialout $USER` then log out/in |
| Device not showing up | Try a different USB cable (data cable, not charge-only) |
| Flash fails / times out | Hold BOOT, press RESET, release BOOT, then retry |
| `espup install` errors | Make sure `python3`, `git`, `curl` are installed |
| Build fails on `esp-idf-sys` | Check `source ~/export-esp.sh` is in your shell config |
| Wrong serial port | Run `ls /dev/tty{USB,ACM}*` to find the right one |


## Quick Reference: Full Flow

```bash
# One-time setup
cargo install espup cargo-espflash cargo-generate
espup install && source ~/export-esp.sh

# New project
cargo generate esp-rs/esp-idf-template   # pick esp32c6

# Dev loop
cd my-project
cargo espflash flash --release --monitor  # build → flash → watch output
```
