# OLED Display on ESP32-C6: How Pixels Get to the Screen

This document explains how our code drives a 0.96" SSD1306 OLED display — from the I2C bus all the way up to drawing text on screen.

## The Big Picture

Getting text onto the OLED involves four layers, each building on the one below:

```
┌─────────────────────────────────────┐
│  Your code: Text::new("Hello...")   │   ← what you write
├─────────────────────────────────────┤
│  embedded-graphics                  │   ← turns text into pixels
├─────────────────────────────────────┤
│  ssd1306 driver                     │   ← manages the framebuffer, talks to the chip
├─────────────────────────────────────┤
│  I2C bus (GPIO6 / GPIO7)            │   ← electrical signals on the wire
├─────────────────────────────────────┤
│  SSD1306 chip on the OLED module    │   ← lights up the actual pixels
└─────────────────────────────────────┘
```

When you call `Text::new("Hello").draw(&mut display)`, nothing appears on screen yet. The drawing operations write into a **framebuffer** — a 1024-byte array in the ESP32's RAM (128 x 64 pixels / 8 bits per byte). Only when you call `display.flush()` does the entire buffer get sent over I2C to the display controller, which updates the physical pixels.

This is the **buffered graphics mode** pattern: draw freely in memory, then send it all at once.

## The Hardware: What Is an SSD1306?

The SSD1306 is not the screen itself — it's the **controller chip** soldered onto the back of the OLED module. It has its own 1 KB of GDDRAM (Graphic Display Data RAM) that maps 1:1 to the 128x64 pixel grid. When you write a byte to its RAM, the corresponding 8 pixels update.

The OLED pixels are organic LEDs that emit light directly — there's no backlight. A pixel set to 1 lights up (white), a pixel set to 0 is truly off (black). This is why OLEDs have perfect blacks and are readable in the dark.

### Pixel Layout in Memory

The SSD1306 organizes its 128x64 display as **8 horizontal pages**, each 128 columns wide and 8 pixels tall:

```
Page 0:  rows  0– 7   (128 bytes)
Page 1:  rows  8–15   (128 bytes)
Page 2:  rows 16–23   (128 bytes)
  ...
Page 7:  rows 56–63   (128 bytes)
                        ──────────
                        1024 bytes total
```

Each byte controls a vertical strip of 8 pixels within a column. Bit 0 is the top pixel, bit 7 is the bottom:

```
Column 42, Page 0:

byte = 0b00111100
         ││││││││
         │││││││└─ row 0: OFF
         ││││││└── row 1: OFF
         │││││└─── row 2: ON
         ││││└──── row 3: ON
         │││└───── row 4: ON
         ││└────── row 5: ON
         │└─────── row 6: OFF
         └──────── row 7: OFF
```

This page-oriented layout is a hardware design choice — it allows the controller to update a full page of 128 bytes in a single I2C burst, which is efficient for horizontal text rendering.

## Layer 1: The I2C Bus

I2C (Inter-Integrated Circuit, pronounced "I-squared-C") is a two-wire protocol. One wire carries a clock signal (SCL), the other carries data (SDA). The ESP32 is the **master** — it generates the clock and initiates every transaction. The SSD1306 is a **slave** listening at address `0x3C`.

```rust
let i2c_config = I2cConfig::new().baudrate(400_000.into());
let i2c = I2cDriver::new(
    peripherals.i2c0,
    peripherals.pins.gpio6,   // SDA (data)
    peripherals.pins.gpio7,   // SCL (clock)
    &i2c_config,
)?;
```

**`peripherals.i2c0`** — the ESP32-C6 has one I2C hardware controller (I2C0). This is a dedicated peripheral that handles the low-level bit timing in hardware, so the CPU doesn't have to bit-bang each clock cycle.

**`baudrate(400_000.into())`** — 400 kHz, which is I2C "Fast Mode." At this speed, pushing the full 1024-byte framebuffer takes about 20 ms (1024 bytes x 8 bits / 400,000 Hz, plus protocol overhead). Standard mode is 100 kHz; we use fast mode because the SSD1306 supports it and it means quicker screen updates.

### What a Transfer Looks Like on the Wire

When `flush()` sends the framebuffer, the I2C transaction looks like:

```
START → [0x3C + Write bit] → ACK → [0x40 (data mode)] → ACK → [byte 0] → ACK → [byte 1] → ACK → ... → [byte 1023] → ACK → STOP
```

- **START**: SDA goes low while SCL is high — signals the beginning of a transaction.
- **0x3C**: The SSD1306's 7-bit I2C address. The 8th bit selects read/write.
- **0x40**: A "control byte" telling the SSD1306 that everything that follows is display data (not commands).
- **1024 data bytes**: The entire framebuffer, streamed sequentially.
- **ACK**: After each byte, the SSD1306 pulls SDA low for one clock cycle to acknowledge receipt.
- **STOP**: SDA goes high while SCL is high — transaction done.

The SSD1306 has an internal address counter that auto-increments, so you just stream bytes and they fill the GDDRAM page by page, column by column.

## Layer 2: The SSD1306 Driver Crate

The `ssd1306` crate provides a Rust driver that knows the SSD1306's command set and memory layout.

```rust
let interface = I2CDisplayInterface::new(i2c);
let mut display = Ssd1306::new(interface, DisplaySize128x64, DisplayRotation::Rotate0)
    .into_buffered_graphics_mode();
display.init().map_err(|e| anyhow::anyhow!("Display init: {:?}", e))?;
display.clear_buffer();
```

### `I2CDisplayInterface::new(i2c)`

This wraps our `I2cDriver` in an adapter that implements the `display-interface` trait. This abstraction lets the same SSD1306 driver work over I2C or SPI — you just swap the interface layer.

### `Ssd1306::new(interface, DisplaySize128x64, DisplayRotation::Rotate0)`

Creates the driver struct. `DisplaySize128x64` tells it the pixel dimensions (there are also 128x32 variants). `DisplayRotation::Rotate0` means no rotation — row 0 is at the top.

### `.into_buffered_graphics_mode()`

This is the key design choice. The driver allocates a `[u8; 1024]` buffer in ESP32 RAM. All drawing operations modify this local buffer — nothing goes over I2C until you explicitly call `flush()`. This is important for two reasons:

1. **I2C is slow.** If every `set_pixel()` call triggered an I2C transfer, drawing a single character (up to ~60 pixels) would require 60 separate I2C transactions. Buffered mode batches everything into one transfer.
2. **The SSD1306's page layout is awkward.** Setting a single pixel requires a read-modify-write of the byte containing that pixel's bit. With a local buffer, the driver can just flip a bit in RAM — no read-back from the display needed.

### `display.init()`

Sends a sequence of initialization commands over I2C to configure the SSD1306. This includes:

- Set multiplex ratio (how many rows are active: 64)
- Set display offset (0)
- Set start line (0)
- Set segment remap and COM scan direction (controls orientation)
- Set COM pins hardware configuration
- Set contrast (brightness level)
- Enable the charge pump (the OLED needs an internal voltage boost circuit to drive the pixels — the charge pump converts the 3.3V input to the ~7-8V needed by the OLED panel)
- Turn the display on

These are one-time setup commands. After `init()`, the display is ready to show whatever is in its GDDRAM.

### `display.clear_buffer()`

Zeroes out the local 1024-byte buffer. Since 0 = pixel off, this prepares a blank canvas. Note: this only clears the *local* buffer. If the display was previously showing something, it will keep showing it until the next `flush()`.

## Layer 3: embedded-graphics

The `embedded-graphics` crate provides a 2D drawing API — points, lines, rectangles, circles, text — that works on any display implementing the `DrawTarget` trait. The SSD1306 driver's buffered graphics mode implements this trait.

```rust
let text_style = MonoTextStyleBuilder::new()
    .font(&FONT_6X10)
    .text_color(BinaryColor::On)
    .build();

Text::new("Hello from ESP32!", Point::new(0, 10), text_style)
    .draw(&mut display)
    .map_err(|e| anyhow::anyhow!("Draw: {:?}", e))?;
```

### How Text Becomes Pixels

**`FONT_6X10`** is a bitmap font where each character fits in a 6-pixel-wide, 10-pixel-tall cell. The font data is a static array compiled into the firmware. Each character is stored as a series of bytes encoding which pixels are on and off — essentially a tiny sprite sheet.

For the letter "H" in a 6x10 font, the data might look like:

```
......    row 0
#....#    row 1
#....#    row 2
#....#    row 3
######    row 4
#....#    row 5
#....#    row 6
#....#    row 7
......    row 8
......    row 9
```

**`BinaryColor::On`** — on a monochrome display, pixels are either on (white) or off (black). `BinaryColor` is embedded-graphics' type for this.

**`Point::new(0, 10)`** — the anchor point for the text baseline. `(0, 10)` means the text starts at the left edge, 10 pixels down from the top. The Y coordinate in embedded-graphics text refers to the **baseline**, not the top of the character cell, so with a 10px-tall font starting at Y=10, the characters occupy roughly rows 1-10.

### What `.draw(&mut display)` Does

When `Text::draw()` is called, embedded-graphics:

1. Iterates through each character in the string ("H", "e", "l", "l", "o", ...)
2. For each character, looks up its bitmap data in `FONT_6X10`
3. For each set bit in the bitmap, calls `display.draw_pixel(Point, BinaryColor::On)`
4. The SSD1306 driver receives each `draw_pixel` call and sets the corresponding bit in its 1024-byte RAM buffer

For the string "Hello from ESP32!" (17 characters at 6px each = 102 pixels wide), this results in roughly 500-700 individual pixel writes — all happening in RAM, so it's fast (microseconds).

### The Second Text Line

```rust
let ip_str = format!("IP: {}", ip_info.ip);
Text::new(&ip_str, Point::new(0, 24), text_style)
    .draw(&mut display)
    .map_err(|e| anyhow::anyhow!("Draw: {:?}", e))?;
```

Same process, but at Y=24, which puts it about 14 pixels below the first line. The `format!` macro creates a string like `"IP: 192.168.1.42"` at runtime — this works because we have `std` available (the ESP-IDF build includes Rust's standard library with heap allocation).

## Layer 4: Flush — Sending It All to the Display

```rust
display.flush().map_err(|e| anyhow::anyhow!("Flush: {:?}", e))?;
```

This is where the physical display finally updates. `flush()` does the following:

1. Sends I2C commands to set the column address range (0-127) and page address range (0-7), positioning the SSD1306's internal write pointer at the top-left
2. Sends all 1024 bytes of the framebuffer as a data payload
3. The SSD1306 writes each byte into its GDDRAM, advancing its internal pointer automatically

After `flush()` completes (~20 ms later), the OLED is physically displaying both text lines. The display will continue showing this content indefinitely without any further communication — the SSD1306 continuously scans its GDDRAM and drives the OLED pixels. The ESP32 only needs to talk to it again if it wants to change what's displayed.

## Putting It All Together

Here's the complete flow from code to photons:

```
Text::new("Hello from ESP32!", Point::new(0, 10), text_style)
    .draw(&mut display)
```
**1.** embedded-graphics looks up each character's bitmap in FONT_6X10
**2.** For each "on" pixel, it calls draw_pixel on the SSD1306 driver
**3.** The driver sets the corresponding bit in its 1024-byte RAM buffer

```
display.flush()
```
**4.** The driver sends I2C commands to set the write address to (0, 0)
**5.** It streams all 1024 bytes over I2C at 400 kHz to the SSD1306
**6.** The SSD1306 stores them in its GDDRAM
**7.** The SSD1306's internal scan circuit drives current through the OLED pixels that correspond to "1" bits
**8.** Those organic LEDs emit light — you see "Hello from ESP32!"

The entire process from `draw()` to visible text takes about 20 ms, almost all of which is the I2C transfer. The CPU-side drawing is nearly instant.

## Why This Design?

It might seem like a lot of layers for a small display, but each layer earns its keep:

- **I2C hardware peripheral** frees the CPU from bit-banging the clock and data lines
- **`ssd1306` driver** encapsulates the chip's command set and page-oriented memory layout so you never think about control bytes or charge pumps
- **Buffered mode** turns expensive per-pixel I2C transfers into a single bulk write
- **`embedded-graphics`** provides a portable drawing API — the same `Text::new()` call works on SSD1306, ST7789, e-ink, or any other display with a `DrawTarget` implementation

The result: you write high-level drawing code, and four layers of abstraction turn it into precisely-timed electrical signals that light up individual organic LEDs on a 0.96" glass panel.
