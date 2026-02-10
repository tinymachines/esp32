# esp32c6-hello

Hello World for the ESP32-C6-DevKitC-1-N8 in Rust.

Prints a startup banner to the serial monitor and blinks the onboard LED (GPIO8) at 1 Hz.

## Prerequisites

```bash
# Install Rust (if needed)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Install ESP toolchain
cargo install espup
espup install
source ~/export-esp.sh   # add to .bashrc/.zshrc

# Install flashing tools
cargo install cargo-espflash
cargo install ldproxy

# Linux: serial port permissions
sudo usermod -aG dialout $USER   # then log out/in
```

## Build & Flash

```bash
# Build, flash, and open serial monitor in one command
cargo espflash flash --release --monitor

# Or build only (without flashing)
cargo build --release
```

## Expected Serial Output

```
I (xxx) esp32c6_hello: ========================================
I (xxx) esp32c6_hello:   Hello from ESP32-C6!
I (xxx) esp32c6_hello:   Chip:   ESP32-C6 (RISC-V @ 160 MHz)
I (xxx) esp32c6_hello:   Target: riscv32imac-unknown-none-elf
I (xxx) esp32c6_hello:   Lang:   Rust 🦀
I (xxx) esp32c6_hello: ========================================
I (xxx) esp32c6_hello: Starting blink loop on GPIO8...
I (xxx) esp32c6_hello: [0] LED ON
I (xxx) esp32c6_hello: [0] LED OFF
I (xxx) esp32c6_hello: [1] LED ON
...
```

## Project Structure

```
esp32c6-hello/
├── .cargo/
│   └── config.toml          # target, linker, runner, build-std config
├── src/
│   └── main.rs              # application entry point
├── build.rs                 # ESP-IDF build integration
├── Cargo.toml               # dependencies and profiles
├── rust-toolchain.toml      # pins nightly + rust-src
└── sdkconfig.defaults       # ESP-IDF Kconfig overrides
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Permission denied on `/dev/ttyUSB0` | `sudo usermod -aG dialout $USER` then log out/in |
| Flash fails / times out | Hold BOOT, press RESET, release BOOT, then retry |
| `ldproxy` not found | `cargo install ldproxy` |
| `error: no matching package` on esp-idf-svc | Make sure `source ~/export-esp.sh` is in your shell |
| Device not detected | Try a different USB cable (must be data-capable) |
