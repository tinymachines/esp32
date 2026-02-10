# ESP32-C6 Rust Development on Raspberry Pi 5 — Complete Setup Guide

> **Hardware:** ESP32-C6-DevKitC-1-N8 · **Host:** Raspberry Pi 5 (aarch64) · **Date:** February 2026

---

## What is the ESP32-C6?

The ESP32-C6-DevKitC-1-N8 is a development board built around Espressif's ESP32-C6 SoC — a **RISC-V** microcontroller with triple radios.

| Spec | Detail |
|------|--------|
| CPU | Dual RISC-V cores — HP @ 160 MHz, LP @ 20 MHz |
| ISA | RV32IMAC (Integer, Multiply/Divide, Atomic, Compressed) |
| Flash | 8 MB (N8 variant) |
| SRAM | 512 KB + 16 KB low-power SRAM |
| Wi-Fi | 802.11ax (Wi-Fi 6), 2.4 GHz |
| Bluetooth | Bluetooth 5 LE |
| 802.15.4 | Zigbee / Thread / Matter |
| GPIO | 30 pins, 12-bit ADC, SPI, I2C, UART, I2S, RMT |
| USB | USB-C with USB-to-UART bridge |
| Onboard LED | WS2812 addressable RGB (NeoPixel) on GPIO8 |

**Why it's great for Rust:** Being RISC-V means you use the standard Rust nightly toolchain — no proprietary Xtensa compiler fork needed.

---

## How the Rust Toolchain Works

### Release Channels

Rust has three release channels:

- **Stable** — new release every 6 weeks (e.g., 1.84.0). Used by most Rust developers.
- **Beta** — the next stable, in testing.
- **Nightly** — built every night from the latest dev code. Includes unstable/experimental features.

**ESP32-C6 std development requires nightly** because it uses an unstable Cargo feature called `build-std`. This feature recompiles the Rust standard library from source for the ESP32 target, since Rust doesn't ship pre-built std binaries for `espidf`. Only nightly Cargo supports `-Z build-std`.

### How the Pieces Connect

```
You write Rust code (src/main.rs)
        │
        ▼
   cargo build
        │
        ├── rust-toolchain.toml  →  picks which rustc version
        ├── .cargo/config.toml   →  target, linker, build-std flags
        ├── Cargo.toml           →  your crate dependencies
        │
        ▼
   rustc (Rust compiler, from the pinned nightly)
        │
        ├── compiles YOUR code for riscv32imac-esp-espidf
        ├── recompiles Rust std from source (build-std)
        │
        ▼
   ldproxy → riscv32-esp-elf-gcc (linker)
        │
        ├── links Rust code with ESP-IDF C libraries
        │
        ▼
   firmware binary (.elf)
        │
        ▼
   espflash (flashes over USB serial to the board)
```

### The Tools

| Tool | What it does |
|------|-------------|
| **rustup** | Manages Rust toolchains. Installs/switches between stable, nightly, specific dates. Like `nvm` or `pyenv`. |
| **rustc** | The Rust compiler. Cargo calls it for you. |
| **cargo** | Build system + package manager. Reads `Cargo.toml`, resolves deps, runs rustc. |
| **espup** | ESP-specific installer. Sets up cross-compilation GCC, LLVM, and environment vars. |
| **ldproxy** | Linker wrapper. Forwards linker calls to the ESP-IDF GCC toolchain. |
| **espflash / cargo-espflash** | Flashes compiled firmware onto the ESP32 over USB serial. |
| **cargo-generate** | Scaffolds new projects from templates. |

### The Config Files

| File | Purpose |
|------|---------|
| `rust-toolchain.toml` | Pins the nightly version. Anyone cloning the project gets the same compiler. |
| `.cargo/config.toml` | Build target (`riscv32imac-esp-espidf`), linker (`ldproxy`), `build-std` flag. |
| `Cargo.toml` | Dependencies (`esp-idf-svc`, `log`, etc.) and project metadata. |
| `sdkconfig.defaults` | ESP-IDF config (stack size, CPU freq, peripherals). A C SDK concept passed through. |
| `build.rs` | Build script using `embuild` to auto-download and compile the ESP-IDF C SDK. |

---

## Step-by-Step Setup on Raspberry Pi 5

### 1. System Dependencies

```bash
sudo apt update
sudo apt install -y git curl gcc build-essential pkg-config \
    libudev-dev libssl-dev python3 python3-pip python3-venv \
    cmake ninja-build
```

### 2. Install Rust

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
```

### 3. Install ESP Toolchain

```bash
# ESP toolchain manager
cargo install espup

# Install ESP toolchain (RISC-V GCC, LLVM, etc.)
espup install --targets esp32c6

# Source environment variables — add this line to your ~/.bashrc
source ~/export-esp.sh
```

### 4. Install Dev Tools

```bash
cargo install cargo-espflash espflash cargo-generate ldproxy
```

### 5. USB Serial Permissions

```bash
# Add yourself to the dialout group
sudo usermod -aG dialout $USER

# LOG OUT AND BACK IN for this to take effect

# Verify the board is detected (plug it in first)
ls /dev/ttyUSB*    # should show /dev/ttyUSB0
```

### 6. (Recommended) Add Swap for Builds

ESP-IDF compilation is RAM-hungry. If you have the 4 GB Pi 5:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
# Add to /etc/fstab for persistence:
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## The Hello World Project (RGB LED Blink)

The ESP32-C6-DevKitC-1 has a **WS2812 addressable RGB LED** on GPIO8 — not a simple on/off LED. You need the RMT peripheral and a NeoPixel driver to control it.

### Project Structure

```
esp32c6-hello/
├── .cargo/
│   └── config.toml
├── src/
│   └── main.rs
├── Cargo.toml
├── build.rs
├── rust-toolchain.toml
└── sdkconfig.defaults
```

### rust-toolchain.toml

```toml
[toolchain]
channel = "nightly-2024-12-01"
components = ["rust-src"]
```

> **Why pin the nightly?** The latest nightly often breaks `build-std` for the `espidf` target because Rust's std internals change faster than ESP target support is updated. `nightly-2024-12-01` is confirmed working. If a dependency complains about rustc version (e.g., `home` crate), run:
> ```bash
> cargo update home@0.5.12 --precise 0.5.11
> ```

### .cargo/config.toml

```toml
[build]
target = "riscv32imac-esp-espidf"

[target.riscv32imac-esp-espidf]
linker = "ldproxy"
runner = "espflash flash --monitor"

[unstable]
build-std = ["std", "panic_abort"]

[env]
MCU = "esp32c6"
ESP_IDF_VERSION = "v5.2.2"
ESP_IDF_SDKCONFIG_DEFAULTS = { value = "sdkconfig.defaults", relative = true }
```

> **Critical:** The target is `riscv32imac-esp-espidf` — **not** `riscv32imc-esp-espidf` (missing the A for Atomic) and **not** `riscv32imac-unknown-none-elf` (that's for bare-metal no_std).

### Cargo.toml

```toml
[package]
name = "esp32c6-hello"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "esp32c6-hello"
harness = false

[profile.release]
opt-level = "s"

[profile.dev]
debug = true
opt-level = "z"

[features]
default = ["std", "embassy", "esp-idf-svc/native"]
pio = ["esp-idf-svc/pio"]
std = ["alloc", "esp-idf-svc/binstart", "esp-idf-svc/std"]
alloc = ["esp-idf-svc/alloc"]
nightly = ["esp-idf-svc/nightly"]
experimental = ["esp-idf-svc/experimental"]
embassy = [
    "esp-idf-svc/embassy-sync",
    "esp-idf-svc/critical-section",
    "esp-idf-svc/embassy-time-driver",
]

[dependencies]
log = { version = "0.4", default-features = false }
esp-idf-svc = { version = "0.49", default-features = false }
ws2812-esp32-rmt-driver = { version = "0.9", features = ["smart-leds-trait"] }
smart-leds = "0.4"

[build-dependencies]
embuild = "0.32.0"
```

### build.rs

```rust
fn main() {
    embuild::espidf::sysenv::output();
}
```

### sdkconfig.defaults

```
CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192
CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_160=y
```

### src/main.rs

```rust
use esp_idf_svc::hal::peripherals::Peripherals;
use smart_leds::hsv::{hsv2rgb, Hsv};
use smart_leds::SmartLedsWrite;
use ws2812_esp32_rmt_driver::Ws2812Esp32Rmt;
use std::thread;
use std::time::Duration;

fn main() {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take().unwrap();

    // GPIO8 = WS2812 RGB LED data pin on the DevKitC-1
    // channel0 = RMT peripheral channel for precise timing
    let mut ws2812 = Ws2812Esp32Rmt::new(
        peripherals.rmt.channel0,
        peripherals.pins.gpio8,
    ).unwrap();

    log::info!("RGB LED ready!");

    let mut hue: u8 = 0;
    loop {
        // Cycle through rainbow colors
        // val=20 keeps brightness low (max 255 = blinding)
        let color = hsv2rgb(Hsv { hue, sat: 255, val: 20 });
        ws2812.write([color].iter().copied()).unwrap();
        log::info!("Hue: {}", hue);

        hue = hue.wrapping_add(5);
        thread::sleep(Duration::from_millis(100));
    }
}
```

---

## Build & Flash

```bash
cd esp32c6-hello

# Build, flash, and open serial monitor
cargo espflash flash --release --monitor

# First build takes 15-25 minutes on Pi 5 (downloads & compiles ESP-IDF)
# Subsequent builds are fast (only your Rust code recompiles)
```

You should see the RGB LED cycling through rainbow colors, and serial output like:

```
I (328) esp32c6_hello: RGB LED ready!
I (328) esp32c6_hello: Hue: 0
I (428) esp32c6_hello: Hue: 5
I (528) esp32c6_hello: Hue: 10
...
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Permission denied on `/dev/ttyUSB0` | `sudo usermod -aG dialout $USER` then log out/in |
| Device not showing up | Try a different USB cable (must be data-capable, not charge-only) |
| Flash fails / times out | Hold BOOT button, press RESET, release BOOT, retry |
| `cfg_select` / `build-std` errors | Pin nightly in `rust-toolchain.toml` (see above) |
| `home` crate requires newer rustc | `cargo update home@0.5.12 --precise 0.5.11` |
| Wrong target errors | Must be `riscv32imac-esp-espidf` for ESP32-C6 with std |
| SIGBUS on `rustc` | Corrupted toolchain — `rustup toolchain uninstall` then reinstall |
| `espup install` fails | Ensure `python3`, `git`, `curl`, `cmake` are installed |
| Build runs out of memory | Add swap (see setup step 6) |
| LED not blinking | The DevKitC-1 has a WS2812 RGB LED, not a simple GPIO LED |

---

## Useful Commands

```bash
# Build without flashing
cargo build --release

# Check board connection
cargo espflash board-info

# Erase all flash
cargo espflash erase-flash

# Save firmware binary without flashing
cargo espflash save-image --release --chip esp32c6 firmware.bin

# View disassembly
cargo objdump --release -- -d

# Check binary section sizes
cargo size --release
```

---

## Key Crates & Resources

### Crates

| Crate | Purpose |
|-------|---------|
| `esp-idf-svc` | Top-level std crate — Wi-Fi, BLE, HTTP, MQTT, NVS. Re-exports hal and sys. |
| `esp-idf-hal` | Hardware abstraction (GPIO, SPI, I2C, UART, etc.) |
| `esp-idf-sys` | Raw FFI bindings to the ESP-IDF C SDK |
| `esp-hal` | Bare-metal HAL (no_std, officially supported by Espressif) |
| `ws2812-esp32-rmt-driver` | WS2812 / NeoPixel driver using the RMT peripheral |
| `smart-leds` | Portable LED strip traits and color utilities |
| `embedded-hal` | Cross-platform embedded hardware traits |

### Links

- esp-rs GitHub: https://github.com/esp-rs
- Rust on ESP Book: https://docs.espressif.com/projects/rust/book/
- esp-idf-svc: https://github.com/esp-rs/esp-idf-svc
- esp-idf-hal examples: https://github.com/esp-rs/esp-idf-hal/tree/master/examples
- esp-hal (no_std): https://github.com/esp-rs/esp-hal
- espup: https://github.com/esp-rs/espup
- espflash: https://github.com/esp-rs/espflash
- ESP32-C6 Datasheet: https://www.espressif.com/en/products/socs/esp32-c6

---

## Concepts Glossary

| Term | Meaning |
|------|---------|
| **std vs no_std** | `std` = full Rust standard library (threads, networking, heap). `no_std` = bare metal, no OS, minimal runtime. |
| **ESP-IDF** | Espressif's official C SDK. The `esp-idf-svc` crate builds on top of it, giving you std support + Wi-Fi/BLE. |
| **build-std** | Unstable Cargo feature that recompiles Rust's std from source. Required because Rust doesn't ship pre-built std for ESP targets. |
| **RMT** | Remote Control Transceiver — an ESP32 peripheral that generates precise timing signals. Used to drive WS2812 LEDs. |
| **WS2812 / NeoPixel** | Addressable RGB LEDs that use a single-wire protocol with precise timing (not simple high/low GPIO). |
| **RV32IMAC** | The RISC-V instruction set on the ESP32-C6: 32-bit Integer, Multiply/divide, Atomic, Compressed instructions. |
| **ldproxy** | A shim that forwards Rust linker calls to the ESP-IDF GCC cross-compiler. |
| **espup** | Sets up everything needed for Rust to cross-compile for ESP32 chips. |
