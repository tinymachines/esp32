# Resources

Everything you need to study the hardware and software stack behind this project.

## Hardware

### ESP32-C6-DevKitC-1-N8

The development board at the center of this project.

| Spec | Detail |
|------|--------|
| MCU | ESP32-C6 (single-core RISC-V at 160 MHz + low-power core at 20 MHz) |
| Flash | 8 MB SPI NOR |
| SRAM | 512 KB |
| WiFi | WiFi 6 (802.11ax), 2.4 GHz |
| Bluetooth | Bluetooth 5 LE |
| Other radios | 802.15.4 (Zigbee / Thread / Matter) |
| USB | USB-C via USB-to-UART bridge |
| Onboard LED | WS2812 (NeoPixel) on GPIO8 |

- **Datasheet**: https://www.espressif.com/sites/default/files/documentation/esp32-c6_datasheet_en.pdf
- **Technical Reference Manual**: https://www.espressif.com/sites/default/files/documentation/esp32-c6_technical_reference_manual_en.pdf
- **DevKit Schematic**: https://dl.espressif.com/dl/schematics/SCH_ESP32-C6-DevKitC-1_V1.2_20240116.pdf
- **DevKit User Guide**: https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32c6/esp32-c6-devkitc-1/index.html
- **ESP32-C6 Product Page**: https://www.espressif.com/en/products/socs/esp32-c6

### SSD1306 OLED Display (0.96", 128x64, I2C)

| Spec | Detail |
|------|--------|
| Controller | SSD1306 |
| Resolution | 128 x 64 pixels, monochrome |
| Interface | I2C (address 0x3C) |
| Voltage | 3.3V (onboard regulator) |
| Wiring | VCC→3.3V, GND→GND, SDA→GPIO6, SCL→GPIO7 |

- **SSD1306 Datasheet**: https://cdn-shop.adafruit.com/datasheets/SSD1306.pdf
- **SSD1306 App Note (command reference)**: https://www.solomon-systech.com/product/ssd1306/

### WS2812 (NeoPixel) RGB LED

| Spec | Detail |
|------|--------|
| Protocol | Single-wire, timing-critical (800 kHz) |
| GPIO | GPIO8 |
| Driver | RMT peripheral (hardware timing) |
| Colors | 24-bit RGB (8 bits per channel) |

- **WS2812 Datasheet**: https://cdn-shop.adafruit.com/datasheets/WS2812.pdf
- **RMT Peripheral Docs**: https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-reference/peripherals/rmt.html

### I2C Bus

- **I2C Specification (NXP)**: https://www.nxp.com/docs/en/user-guide/UM10204.pdf
- **ESP32-C6 I2C Docs**: https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-reference/peripherals/i2c.html

## Rust Crates

### ESP-IDF Ecosystem

The three-layer stack that bridges Rust to the ESP-IDF C SDK.

| Crate | Version | Description |
|-------|---------|-------------|
| [`esp-idf-svc`](https://github.com/esp-rs/esp-idf-svc) | 0.49.1 | Top-level: WiFi, BLE, HTTP, NVS, event loop |
| [`esp-idf-hal`](https://github.com/esp-rs/esp-idf-hal) | 0.44.1 | Hardware abstraction: GPIO, I2C, SPI, UART, RMT |
| [`esp-idf-sys`](https://github.com/esp-rs/esp-idf-sys) | 0.35.0 | Raw FFI bindings to the ESP-IDF C SDK |

- **Docs (esp-idf-svc)**: https://docs.rs/esp-idf-svc
- **Docs (esp-idf-hal)**: https://docs.rs/esp-idf-hal
- **Docs (esp-idf-sys)**: https://docs.rs/esp-idf-sys

How they fit together:
```
  Your code
     ↓
  esp-idf-svc  (WiFi, BLE, HTTP — high-level services)
     ↓
  esp-idf-hal  (GPIO, I2C, SPI — hardware abstraction)
     ↓
  esp-idf-sys  (raw C FFI bindings)
     ↓
  ESP-IDF C SDK (compiled by embuild at build time)
```

### Display

| Crate | Version | Description |
|-------|---------|-------------|
| [`ssd1306`](https://github.com/rust-embedded-community/ssd1306) | 0.9.0 | SSD1306 OLED driver (I2C/SPI, buffered graphics mode) |
| [`embedded-graphics`](https://github.com/embedded-graphics/embedded-graphics) | 0.8.1 | 2D drawing: text, shapes, fonts, images |
| [`embedded-graphics-core`](https://github.com/embedded-graphics/embedded-graphics) | 0.4.0 | Core traits (`DrawTarget`, `Drawable`, `PixelColor`) |

- **Docs (ssd1306)**: https://docs.rs/ssd1306
- **Docs (embedded-graphics)**: https://docs.rs/embedded-graphics
- **embedded-graphics examples**: https://github.com/embedded-graphics/examples
- **embedded-graphics simulator** (test on desktop): https://github.com/embedded-graphics/simulator

### LED

| Crate | Version | Description |
|-------|---------|-------------|
| [`ws2812-esp32-rmt-driver`](https://github.com/cat-in-136/ws2812-esp32-rmt-driver) | 0.9.0 | WS2812 control via ESP32 RMT peripheral |
| [`smart-leds`](https://github.com/smart-leds-rs/smart-leds) | 0.4.0 | LED color utilities and `SmartLedsWrite` trait |

- **Docs (ws2812-esp32-rmt-driver)**: https://docs.rs/ws2812-esp32-rmt-driver
- **Docs (smart-leds)**: https://docs.rs/smart-leds

### Embedded HAL

The traits that make drivers portable across microcontrollers.

| Crate | Version | Description |
|-------|---------|-------------|
| [`embedded-hal`](https://github.com/rust-embedded/embedded-hal) | 1.0.0 | Standard traits: I2C, SPI, GPIO, delay |
| [`embedded-hal`](https://github.com/rust-embedded/embedded-hal) | 0.2.7 | Legacy traits (still used by some drivers) |

- **Docs**: https://docs.rs/embedded-hal
- **Embedded HAL book**: https://docs.rust-embedded.org/book/

How it works: `embedded-hal` defines traits like `I2cBus`. `esp-idf-hal` implements them for ESP32 hardware. `ssd1306` consumes them. This means the same `ssd1306` crate works on ESP32, STM32, nRF, RP2040 — any chip with an `embedded-hal` implementation.

### Utilities

| Crate | Version | Description |
|-------|---------|-------------|
| [`anyhow`](https://github.com/dtolnay/anyhow) | 1.x | Ergonomic error handling (`Result<T, anyhow::Error>`) |
| [`log`](https://github.com/rust-lang/log) | 0.4.x | Logging facade (`log::info!`, etc.) |
| [`embuild`](https://github.com/esp-rs/embuild) | 0.32 | Build-time: downloads and compiles ESP-IDF C SDK |

- **Docs (anyhow)**: https://docs.rs/anyhow
- **Docs (log)**: https://docs.rs/log

## Toolchain

| Component | Version / Detail |
|-----------|-----------------|
| Rust | nightly-2024-12-01 (pinned in `rust-toolchain.toml`) |
| Target | `riscv32imac-esp-espidf` |
| Linker | `ldproxy` → `riscv32-esp-elf-gcc` |
| Flash tool | `espflash` (via `cargo espflash`) |
| Monitor | `espmonitor` (via `cargo espmonitor`) |

- **esp-rs organization (all ESP32 Rust tools)**: https://github.com/esp-rs
- **The Rust on ESP Book**: https://docs.esp-rs.org/book/
- **esp-rs std training**: https://docs.esp-rs.org/std-training/
- **espflash**: https://github.com/esp-rs/espflash
- **RISC-V ISA spec**: https://riscv.org/technical/specifications/

## ESP-IDF (The C SDK Underneath)

Everything compiles on top of Espressif's C SDK. Understanding it helps debug low-level issues.

- **ESP-IDF Programming Guide**: https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/
- **ESP-IDF GitHub**: https://github.com/espressif/esp-idf
- **API Reference (WiFi)**: https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-reference/network/esp_wifi.html
- **API Reference (I2C)**: https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-reference/peripherals/i2c.html
- **API Reference (RMT)**: https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-reference/peripherals/rmt.html
- **Partition Tables**: https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-guides/partition-tables.html
- **Kconfig Reference**: https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-reference/kconfig.html

## Project Docs

Explainers written for this project, in `docs/`:

| Document | What it covers |
|----------|---------------|
| [`esp32-c6-rust-complete-guide.md`](esp32-c6-rust-complete-guide.md) | Full setup walkthrough, toolchain, troubleshooting |
| [`esp32-c6-cpu-architecture.md`](esp32-c6-cpu-architecture.md) | RISC-V ISA deep dive, memory map, registers |
| [`esp32-c6-linux-cheatsheet.md`](esp32-c6-linux-cheatsheet.md) | Quick-reference command sheet |
| [`wifi-explainer.md`](wifi-explainer.md) | How the WiFi code works, line by line |
| [`oled-explainer.md`](oled-explainer.md) | How pixels get to the OLED: I2C, framebuffer, fonts |
| [`oled-assembly-walkthrough.md`](oled-assembly-walkthrough.md) | Annotated RISC-V disassembly of the display code |
| [`flash-partitions.md`](flash-partitions.md) | Flash partition layout, the 1MB wall, custom partitions |

## Community

- **esp-rs Matrix chat**: https://matrix.to/#/#esp-rs:matrix.org
- **Espressif forums**: https://www.esp32.com/
- **Rust Embedded WG**: https://github.com/rust-embedded/wg
- **Awesome ESP Rust**: https://github.com/esp-rs/awesome-esp-rust
