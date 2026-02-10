# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Rust firmware for the **ESP32-C6-DevKitC-1-N8** development board (RISC-V). Developed on a **Raspberry Pi 5** (aarch64 Linux) host. The project connects to WiFi and drives the onboard WS2812 RGB LED through a color cycle; the goal is to incrementally bring more peripherals online (BLE, GPIO, etc.).

## Build & Flash Commands

All commands run from `esp32c6-hello/`.

```bash
# Source ESP environment (required in each new shell, or add to .bashrc)
source ~/export-esp.sh

# Build only
cargo build --release

# Build, flash to board, and open serial monitor (most common)
cargo espflash flash --release --monitor

# Flash without monitor
cargo espflash flash --release

# Serial monitor only (Ctrl+] to quit)
cargo espmonitor /dev/ttyUSB0

# Verify board connection
cargo espflash board-info

# Erase all flash
cargo espflash erase-flash

# Save firmware image without flashing
cargo espflash save-image --release --chip esp32c6 firmware.bin

# View binary size by section
cargo size --release

# Disassemble the binary
cargo objdump --release -- -d
```

```bash
# Build/flash with WiFi (credentials via env vars, not stored in source)
WIFI_SSID="MyNetwork" WIFI_PASS="MyPassword" cargo espflash flash --release --monitor
```

First build takes a long time (downloads and compiles the ESP-IDF C SDK). Subsequent builds are fast.

## Troubleshooting

- **Permission denied on `/dev/ttyUSB0`**: `sudo usermod -aG dialout $USER`, then log out/in.
- **Flash fails / times out**: Hold BOOT, press RESET, release BOOT, then retry.
- **`ldproxy` not found**: `cargo install ldproxy`.
- **Device not detected**: Try a different USB cable (must be data-capable, not charge-only).
- **Out of memory during build**: Enable swap (`sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`).

## Architecture

```
esp32c6-hello/
├── src/main.rs              # Single application entry point
├── Cargo.toml               # Dependencies and build profiles
├── .cargo/config.toml       # Target, linker, build-std, env vars
├── rust-toolchain.toml      # Pins nightly-2024-12-01 + rust-src
├── sdkconfig.defaults       # ESP-IDF Kconfig overrides (stack size, CPU freq, log level)
└── build.rs                 # embuild integration — compiles the ESP-IDF C SDK
```

### Toolchain & Build Pipeline

**Target triple:** `riscv32imac-esp-espidf` — RISC-V 32-bit with std support via ESP-IDF.

**Why nightly Rust is required:** The `build-std` Cargo feature (recompiles Rust's std library for the ESP-IDF target) is unstable and only available on nightly. The exact nightly date is pinned in `rust-toolchain.toml`.

**Build flow:** `cargo build` → `rustc` compiles for riscv32imac → `build.rs` triggers `embuild` to download/compile ESP-IDF C SDK → `ldproxy` forwards to `riscv32-esp-elf-gcc` linker → ELF binary → `espflash` writes to board over USB serial.

### Key Crates

| Crate | Purpose |
|-------|---------|
| `esp-idf-svc` | Top-level ESP-IDF Rust bindings (HAL, WiFi, BLE, HTTP, GPIO, etc.) |
| `ws2812-esp32-rmt-driver` | WS2812 RGB LED control via the RMT peripheral |
| `smart-leds` | LED color utilities (HSV-to-RGB conversion) |
| `log` | Logging facade; backend provided by `EspLogger` |
| `anyhow` | Ergonomic error handling — lets `main()` return `Result` |
| `embuild` | Build-time: downloads and compiles the ESP-IDF C SDK |

### Hardware Details

- **MCU:** ESP32-C6 — dual RISC-V cores (HP 160 MHz, LP 20 MHz), 8 MB flash, 512 KB SRAM
- **Onboard LED:** WS2812 (NeoPixel) on **GPIO8**, driven via the **RMT** peripheral (precise timing protocol, not simple GPIO toggle)
- **WiFi:** WiFi 6 (2.4 GHz), connected via `BlockingWifi` in station mode. Credentials provided via `WIFI_SSID`/`WIFI_PASS` env vars at compile time.
- **Radios (not yet enabled):** Bluetooth 5 LE, 802.15.4 (Zigbee/Thread/Matter)
- **Serial connection:** USB-C via USB-to-UART bridge, typically at `/dev/ttyUSB0`

### Code Patterns

- **Peripheral singleton:** `Peripherals::take().unwrap()` gives exclusive ownership of all hardware — pass individual peripherals to drivers.
- **Initialization boilerplate:** Every `main()` must call `esp_idf_svc::sys::link_patches()` and `esp_idf_svc::log::EspLogger::initialize_default()` before doing anything else.
- **Logging:** Use the `log` crate macros (`log::info!`, `log::error!`, etc.) — output goes to the serial monitor.
- **Size optimization:** Both dev and release profiles optimize for size (`opt-level = "z"` / `"s"`) because flash is limited.

### ESP-IDF Configuration (`sdkconfig.defaults`)

Add ESP-IDF Kconfig options here to enable/configure subsystems. Current settings:
- Main task stack: 16384 bytes (WiFi + Rust std needs more than C default)
- CPU frequency: 160 MHz
- Default log level: INFO

When enabling WiFi, BLE, or other subsystems, corresponding `CONFIG_*` options may need to be added here.

## Documentation

Detailed reference docs are in `docs/`:
- `esp32-c6-rust-complete-guide.md` — full setup walkthrough, toolchain explanation, troubleshooting
- `esp32-c6-cpu-architecture.md` — RISC-V ISA deep dive, memory map, register file, instruction encoding
- `esp32-c6-linux-cheatsheet.md` — quick-reference command sheet
