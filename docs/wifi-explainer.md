# WiFi on ESP32-C6: How the Code Works

This document walks through `src/main.rs` and explains how WiFi connectivity is set up on the ESP32-C6 using Rust and the `esp-idf-svc` crate.

## The Big Picture

The ESP32-C6 has a built-in WiFi 6 radio. The ESP-IDF SDK (written in C) provides the driver stack that manages this radio — scanning for networks, handling WPA2 authentication, running DHCP, etc. The `esp-idf-svc` Rust crate wraps that entire C stack in safe, idiomatic Rust types.

Our code does five things in order:
1. Initialize the system
2. Configure WiFi in station (client) mode
3. Start the radio
4. Connect to the network and wait for an IP
5. Run the LED loop (with WiFi staying connected in the background)

## Line-by-Line Walkthrough

### Compile-Time Credentials

```rust
const WIFI_SSID: &str = env!("WIFI_SSID");
const WIFI_PASS: &str = env!("WIFI_PASS");
```

`env!()` is a Rust macro that reads an environment variable **at compile time** and embeds it as a string literal in the binary. If you forget to set `WIFI_SSID` or `WIFI_PASS` when building, the compiler will refuse to continue with a clear error message. This keeps credentials out of source code.

### System Initialization

```rust
esp_idf_svc::sys::link_patches();
esp_idf_svc::log::EspLogger::initialize_default();
```

Every ESP-IDF Rust program starts with these two lines. `link_patches()` ensures certain ESP-IDF C symbols are linked correctly. `EspLogger` routes Rust's `log` crate output to the serial monitor.

### Taking Ownership of Hardware

```rust
let peripherals = Peripherals::take()?;
let sysloop = EspSystemEventLoop::take()?;
let nvs = EspDefaultNvsPartition::take()?;
```

Three singletons are claimed here:

- **`Peripherals`** — exclusive access to all hardware (GPIO pins, RMT channels, the WiFi modem, etc.). Rust's ownership system guarantees no two drivers can fight over the same pin.
- **`EspSystemEventLoop`** — the ESP-IDF event bus. WiFi uses this to broadcast events like "connected", "got IP", "disconnected". The `BlockingWifi` wrapper listens on this loop internally.
- **`EspDefaultNvsPartition`** — Non-Volatile Storage. The WiFi stack stores calibration data and sometimes credentials here in flash memory. We pass it in but don't interact with it directly.

### Creating the WiFi Driver

```rust
let mut wifi = BlockingWifi::wrap(
    EspWifi::new(peripherals.modem, sysloop.clone(), Some(nvs))?,
    sysloop,
)?;
```

This is two things nested together:

1. **`EspWifi::new(peripherals.modem, ...)`** — takes ownership of the WiFi/BLE modem peripheral and initializes the low-level WiFi driver. After this call, no other code can use `peripherals.modem`.

2. **`BlockingWifi::wrap(...)`** — wraps the async-event-driven `EspWifi` in a blocking API. Without this wrapper, you'd need to manually listen for events on the event loop. `BlockingWifi` gives us simple `connect()` and `wait_netif_up()` methods that block the current thread until the operation completes.

### Configuring Station Mode

```rust
wifi.set_configuration(&Configuration::Client(ClientConfiguration {
    ssid: WIFI_SSID.try_into().map_err(|_| anyhow::anyhow!("SSID too long"))?,
    password: WIFI_PASS.try_into().map_err(|_| anyhow::anyhow!("Password too long"))?,
    auth_method: AuthMethod::WPA2Personal,
    ..Default::default()
}))?;
```

WiFi can operate in several modes: **Station** (client that joins a network), **Access Point** (creates a network), or both simultaneously. We use `Configuration::Client` for station mode.

The `ssid` and `password` fields are fixed-size byte arrays internally (32 and 64 bytes respectively), not Rust `String`s. The `.try_into()` converts our `&str` into these fixed arrays, failing if the string is too long.

`AuthMethod::WPA2Personal` tells the driver to use WPA2-PSK authentication. The `..Default::default()` fills in remaining fields (channel, scan method, etc.) with sensible defaults — the driver will auto-scan all channels to find the network.

### The Connection Sequence

```rust
wifi.start()?;        // Power on the radio, apply configuration
wifi.connect()?;      // Send authentication/association frames to the AP
wifi.wait_netif_up()?; // Block until DHCP assigns us an IP address
```

These three calls map to distinct phases of WiFi connection:

1. **`start()`** — powers on the radio hardware, loads calibration data, and prepares to transmit. After this, the chip is listening on the configured channel(s) but hasn't talked to any access point yet.

2. **`connect()`** — initiates the IEEE 802.11 handshake: sends authentication frames, then association frames, then the WPA2 4-way handshake to establish encrypted communication. Blocks until the handshake completes or fails.

3. **`wait_netif_up()`** — the radio is connected at layer 2 (WiFi), but we don't have an IP address yet. This call blocks until the network interface comes up, which means DHCP has completed and we have an IP, subnet mask, gateway, and DNS server.

### Reading the IP Address

```rust
let ip_info = wifi.wifi().sta_netif().get_ip_info()?;
log::info!("WiFi DHCP info: {:?}", ip_info);
```

`wifi.wifi()` returns a reference to the inner `EspWifi`. `.sta_netif()` gets the station network interface. `.get_ip_info()` reads the current IP configuration assigned by DHCP. The output looks like:

```
IpInfo { ip: 192.168.1.42, subnet: Subnet { gateway: 192.168.1.1, mask: Mask(24) }, dns: Some(192.168.1.1), ... }
```

### WiFi Stays Connected in the Background

After the connection sequence, the WiFi stack runs in a background FreeRTOS task managed by ESP-IDF. The main thread is free to do other work — in our case, the LED color cycle loop. The WiFi connection persists as long as `wifi` is not dropped (Rust's RAII ensures cleanup happens automatically if the variable goes out of scope).

## Error Handling

The function signature `fn main() -> anyhow::Result<()>` means every `?` operator will propagate errors up. If WiFi fails to connect (wrong password, AP not found, etc.), the error will be printed to the serial monitor and the program will exit. The `anyhow` crate gives us human-readable error messages without needing to define custom error types.

## What's Happening Under the Hood

When you see the serial output during boot, here's what maps to what:

| Serial log | What's happening |
|------------|-----------------|
| `wifi:Init data frame dynamic rx buffer...` | Radio hardware initialization |
| `phy_init: phy_version 290...` | RF calibration loading |
| `wifi:mode : sta (98:a3:16:...)` | Station mode activated with MAC address |
| `esp32c6_hello: WiFi started` | Our `wifi.start()` returned OK |
| `wifi:state: init -> auth (b0)` | Sending 802.11 authentication frame |
| `wifi:state: auth -> assoc (0)` | Authentication succeeded, associating |
| `wifi:state: assoc -> run (10)` | Association complete, WPA2 handshake done |
| `wifi:connected with lunchable, aid = 30...` | Full L2 connection established |
| `esp32c6_hello: WiFi connected` | Our `wifi.connect()` returned OK |
| `sta ip: 192.168.x.x, mask: ...` | DHCP assigned an IP |
| `esp32c6_hello: WiFi DHCP info: ...` | Our `wait_netif_up()` returned OK |
| `esp32c6_hello: RGB LED ready!` | LED loop starts, WiFi runs in background |
