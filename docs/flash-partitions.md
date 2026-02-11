# ESP32-C6 Flash Partitions

The ESP32-C6 has **8 MB of SPI NOR flash** — a single chip that stores everything: the bootloader, your firmware, WiFi calibration data, and any persistent key-value storage. The ESP-IDF divides this flash into a **partition table**, like slicing a disk into partitions.

## Default Layout

ESP-IDF ships with a built-in partition table that looks like this:

```
Flash (8 MB)
┌──────────────────────────┐ 0x000000
│ Bootloader (24 KB)       │ ← Second-stage bootloader
├──────────────────────────┤ 0x008000
│ Partition Table (4 KB)   │ ← Describes the layout below
├──────────────────────────┤ 0x009000
│ NVS (24 KB)              │ ← Non-volatile storage (WiFi cal, key-value store)
├──────────────────────────┤ 0x00F000
│ PHY Init Data (4 KB)     │ ← WiFi/BT radio calibration
├──────────────────────────┤ 0x010000
│ App Partition (1 MB)     │ ← YOUR FIRMWARE LIVES HERE
├──────────────────────────┤ 0x110000
│ (unallocated ~6.9 MB)   │
└──────────────────────────┘ 0x800000
```

### What each partition does

**Bootloader** (0x000000, 24 KB) — The first code that runs after power-on. It reads the partition table, finds the app partition, and jumps to your firmware. This is an ESP-IDF binary, not something you write — it gets flashed automatically by `espflash`.

**Partition Table** (0x008000, 4 KB) — A small binary table that tells the bootloader where everything is. Each entry has a name, type, subtype, offset, and size. The bootloader reads this to find the app partition; your firmware reads it to find NVS and PHY data.

**NVS** (0x009000, 24 KB) — Non-Volatile Storage, a key-value store in flash. The WiFi stack uses it to persist calibration data and connection settings. You can also use it from application code to store settings that survive reboots (e.g., `nvs::EspDefaultNvs` in Rust).

**PHY Init Data** (0x00F000, 4 KB) — Radio calibration parameters for WiFi and Bluetooth. Written once during factory calibration or first boot.

**App Partition** (0x010000, 1 MB) — Your compiled firmware. The ELF binary produced by `cargo build` gets converted to a raw flash image and written here. This is the partition with the size limit we hit.

## Why We Hit the 1 MB Wall

Our firmware includes:

| Component | Approximate Size |
|-----------|-----------------|
| ESP-IDF (WiFi, TLS, lwIP, FreeRTOS) | ~800 KB |
| Rust std library (for RISC-V ESP-IDF) | ~40 KB |
| SSD1306 + embedded-graphics + fonts | ~10 KB |
| WS2812 LED driver | ~5 KB |
| Our application code | ~3 KB |
| Other (string formatting, error handling, etc.) | ~90 KB |
| **Total** | **~1,046 KB** |

The WiFi/TLS stack dominates — it's ~80% of the binary. The OLED display code adds only 2-3 KB, but we were already close to the limit.

### `opt-level "s"` vs `"z"`

When the binary was 16 bytes over 1 MB, we switched the release profile from `"s"` to `"z"`:

- **`"s"` (optimize for size)**: Reduces code size but still inlines small functions when the speed benefit outweighs the size cost. This is a balanced trade-off.
- **`"z"` (optimize aggressively for size)**: Minimizes code size above all else. Avoids inlining, prefers smaller instruction sequences (e.g., function calls instead of inline code), even if it means slightly slower execution.

The difference saved ~3 KB, bringing us from 1,048,592 bytes down to 1,045,664 bytes (99.72% of the partition). On a 160 MHz RISC-V core, the speed difference is negligible for our use case.

## How to Increase the App Partition

When the next feature (BLE, HTTP server, etc.) pushes us over again, we can create a custom partition table that gives the app more space.

### Step 1: Create `partitions.csv`

Create `esp32c6-hello/partitions.csv`:

```csv
# Name,    Type,  SubType, Offset,   Size
nvs,       data,  nvs,     0x9000,   0x6000
phy_init,  data,  phy,     0xf000,   0x1000
factory,   app,   factory, 0x10000,  0x300000
```

This gives the app partition **3 MB** (0x300000) instead of 1 MB:

```
Flash (8 MB) — Custom Layout
┌──────────────────────────┐ 0x000000
│ Bootloader (24 KB)       │
├──────────────────────────┤ 0x008000
│ Partition Table (4 KB)   │
├──────────────────────────┤ 0x009000
│ NVS (24 KB)              │
├──────────────────────────┤ 0x00F000
│ PHY Init Data (4 KB)     │
├──────────────────────────┤ 0x010000
│ App Partition (3 MB)     │ ← 3× larger
├──────────────────────────┤ 0x310000
│ (unallocated ~4.9 MB)   │
└──────────────────────────┘ 0x800000
```

### Step 2: Tell ESP-IDF to use it

Add to `esp32c6-hello/sdkconfig.defaults`:

```
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
```

### Step 3: Clean build

The partition table change requires a clean build since it affects the ESP-IDF C SDK configuration:

```bash
cargo clean
WIFI_SSID="..." WIFI_PASS="..." cargo espflash flash --release --monitor
```

## OTA Partitions (For Later)

The default 1 MB limit exists partly to leave room for **OTA (Over-The-Air) updates**. An OTA setup needs two app partitions — the running firmware and the new firmware being downloaded:

```
# OTA partition layout example
nvs,       data,  nvs,      0x9000,   0x6000
phy_init,  data,  phy,      0xf000,   0x1000
ota_0,     app,   ota_0,    0x10000,  0x200000   # 2 MB — running firmware
ota_1,     app,   ota_1,    0x210000, 0x200000   # 2 MB — downloaded firmware
otadata,   data,  ota,      0x410000, 0x2000     # 8 KB — tracks which slot is active
```

The bootloader checks `otadata` to know which slot to boot from. After downloading a new image to the inactive slot, the firmware marks it as pending, reboots, and the bootloader switches. If the new firmware fails to boot, it rolls back.

Since we flash over USB, OTA isn't needed yet. But it's good to know why the default partition is conservative — it's leaving room for this pattern.

## Inspecting the Current Partition Table

You can read the partition table from the board:

```bash
espflash partition-table /dev/ttyUSB0
```

Or check the binary size against the partition:

```bash
cargo size --release
```

The `App/part. size` line in the `espflash` output shows usage directly:

```
App/part. size:    1,045,664/1,048,576 bytes, 99.72%
```
