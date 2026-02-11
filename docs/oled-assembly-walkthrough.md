# OLED Display: RISC-V Assembly Walkthrough

A guided tour of the compiled RISC-V assembly for the SSD1306 OLED display code. Built with `opt-level = "s"` for the `riscv32imac-esp-espidf` target (RV32IMAC — 32-bit RISC-V with integer multiply, atomics, and compressed instructions).

To reproduce this disassembly:

```bash
WIFI_SSID="..." WIFI_PASS="..." CARGO_PROFILE_RELEASE_STRIP="none" \
  cargo objdump --release -- -d --no-show-raw-insn --demangle
```

(`CARGO_PROFILE_RELEASE_STRIP="none"` overrides the `strip = "symbols"` in Cargo.toml so function names are preserved.)

## Overview of Key Functions

| Function | Address | Purpose |
|----------|---------|---------|
| `esp32c6_hello::main` | `0x420083d6` | Application entry point |
| `ssd1306::Command::send` | `0x4200791c` | Sends SSD1306 commands over I2C |
| `I2cDriver::new` | `0x42007d16` | Initializes the I2C hardware peripheral |
| `I2cDriver::write` | `0x4200a4d8` | Performs an I2C write transaction |
| `BufferedGraphicsMode::set_pixel` | `0x4200a1a6` | Sets a single bit in the framebuffer |
| `BufferedGraphicsMode::flush` | `0x4200a060` | Sends the framebuffer to the display |
| `BufferedGraphicsMode::init` | `0x4200a23c` | Sends initialization command sequence |
| `BufferedGraphicsMode::clear_impl` | `0x4200a012` | Zeroes the framebuffer |
| `flush_buffer_chunks` | `0x42009f0c` | Streams framebuffer data over I2C in chunks |
| `DrawTarget::draw_iter` | `0x4200a458` | embedded-graphics pixel iterator |
| `DrawTarget::fill_solid` | `0x42009bc2` | Fills a rectangle with a solid color |

All our code lives in flash at `0x4200xxxx` (the `.flash.text` section). The ESP32-C6 memory map puts flash-mapped code at `0x42000000+`.

## `set_pixel` — Flipping a Single Bit in the Framebuffer

This is the innermost function in the display pipeline. Every pixel drawn by embedded-graphics ultimately calls this. It maps an (x, y) coordinate to a single bit in the 1024-byte framebuffer and sets or clears it.

```asm
4200a1a6 <ssd1306::...::BufferedGraphicsMode::set_pixel>:
  ; Arguments: a0 = &self (display struct), a1 = x, a2 = y, a3 = pixel value (0 or 1)

  ; --- Handle display rotation ---
  4200a1a6:  lbu   a5, 0x1(a0)         ; Load rotation field from display struct
  4200a1ac:  blt   a4, a5, ...         ; Branch based on rotation value (0, 1, 2, 3)
  4200a1b2:  mv    a4, a1              ; For Rotate0: a4 = x (column)
  4200a1b4:  ...                       ; (other rotations swap x/y)

  ; --- Compute byte index in framebuffer ---
  4200a1ba:  srli  a5, a6, 0x3         ; page = y >> 3  (divide by 8 → which page)
  4200a1be:  slli  a5, a5, 0x7         ; page_offset = page << 7  (× 128 bytes per page)
  4200a1c0:  add   a7, a5, a4          ; buffer_index = page_offset + x

  ; --- Bounds check ---
  4200a1c4:  li    a4, 0x3ff           ; 1023 (max valid index)
  4200a1c8:  bltu  a4, a7, ret         ; If index > 1023, bail out (off-screen pixel)

  ; --- Update dirty tracking (min/max x and y) ---
  4200a1cc:  lbu   a4, 0x405(a0)       ; Load current min_x
  4200a1d4:  bltu  a4, a1, skip        ; If current min_x < new x, keep it
  4200a1d8:  mv    a4, a1              ; Otherwise min_x = x
  4200a1da:  ...                       ; (same logic for max_x, min_y, max_y)
  4200a206:  sb    a2, 0x408(a0)       ; Store updated max_y

  ; --- The actual bit manipulation ---
  4200a210:  lbu   a0, 0x5(a5)         ; Load current byte from framebuffer[index]
  4200a214:  andi  a1, a6, 0x7         ; bit_pos = y & 7  (which bit within the byte)
  4200a218:  li    a2, 0x1             ; a2 = 1
  4200a21a:  sll   a2, a2, a1          ; mask = 1 << bit_pos
  4200a21e:  not   a2, a2              ; inverted_mask = ~mask  (all 1s except target bit)
  4200a222:  and   a0, a0, a2          ; Clear the target bit: byte &= ~mask
  4200a224:  sll   a1, a3, a1          ; new_bit = pixel_value << bit_pos
  4200a228:  or    a0, a0, a1          ; Set if ON: byte |= new_bit
  4200a22a:  sb    a0, 0x5(a5)         ; Store byte back to framebuffer
  4200a22e:  ret
```

### How the address math works

The SSD1306's 128x64 display is organized as 8 pages of 128 bytes. Each byte controls 8 vertical pixels:

```
Pixel (42, 19):
  page        = 19 >> 3       = 2
  page_offset = 2 << 7        = 256
  byte_index  = 256 + 42      = 298
  bit_pos     = 19 & 7        = 3
  mask        = 1 << 3        = 0b00001000
```

The bit manipulation is a classic read-modify-write pattern:
1. **Clear** the target bit with AND + inverted mask: `byte & 0b11110111`
2. **Set** it with OR if the pixel is on: `byte | 0b00001000`

This works for both setting (pixel=1) and clearing (pixel=0) without branching — if pixel is 0, the OR is a no-op. No branches in the hot path, no function calls. 44 instructions total, pure register-to-register bit twiddling.

## `flush` — Sending the Framebuffer Over I2C

After all drawing is done in RAM, `flush()` pushes the framebuffer to the SSD1306 over I2C.

```asm
4200a060 <ssd1306::...::BufferedGraphicsMode::flush>:
  ; Prologue — save 8 callee-saved registers
  4200a060:  addi  sp, sp, -0x30       ; Allocate 48 bytes of stack
  4200a062:  sw    ra, 0x2c(sp)        ; Save return address
  4200a064:  sw    s0, 0x28(sp)        ; Save s0-s7
  ...

  ; --- Check if anything changed (dirty region tracking) ---
  4200a076:  lbu   a2, 0x406(a0)       ; Load max_x from display struct
  4200a07a:  lbu   s0, 0x405(a0)       ; Load min_x
  4200a080:  bltu  a2, s0, epilogue    ; If max_x < min_x → nothing dirty, return early!
  4200a084:  lbu   a1, 0x408(s1)       ; Load max_y
  4200a088:  lbu   s2, 0x407(s1)       ; Load min_y
  4200a08c:  bgeu  a1, s2, send        ; If max_y >= min_y → dirty, proceed to send

  ; --- Early return (no pixels changed since last flush) ---
  epilogue:
  4200a090:  lw    ra, 0x2c(sp)        ; Restore return address
  ...
  4200a0a4:  ret                       ; Done — no I2C traffic at all

  ; --- Prepare for send (handle rotation, compute draw area) ---
  4200a0a6:  lbu   a0, 0x1(s1)        ; Load rotation
  4200a0aa:  slli  a3, a0, 0x3        ; rotation × 8 (index into lookup)
  ...

  ; --- Reset dirty tracking after flush ---
  4200a10e:  sb    a1, 0x405(s1)       ; min_x = 0xFF  (reset to max)
  4200a112:  sb    zero, 0x406(s1)     ; max_x = 0x00  (reset to min)
  4200a116:  sb    a1, 0x407(s1)       ; min_y = 0xFF
  4200a11c:  sb    zero, 0x408(s1)     ; max_y = 0x00

  ; --- Set draw area on the SSD1306, then send data ---
  4200a130:  jalr  ...                 ; → set_draw_area (sends column/page address commands)
  4200a138:  andi  a0, a0, 0xff        ; Check return value
  4200a13e:  bne   a0, a1, epilogue    ; If error (not 0x07), bail

  ; --- Tail call into flush_buffer_chunks ---
  4200a19e:  auipc t1, 0x0
  4200a1a2:  jr    -0x292(t1)          ; → flush_buffer_chunks (streams data over I2C)
```

### Key optimization: dirty region tracking

The driver maintains a bounding box (`min_x`, `max_x`, `min_y`, `max_y`) updated by every `set_pixel` call. If no pixels changed since the last flush, the check at `0x4200a080` catches it and returns immediately — zero I2C traffic. When there are changes, only the dirty region is sent, not the full 1024 bytes.

After sending, the dirty bounds are reset to their inverse extremes (min=0xFF, max=0x00) so the next frame starts with a clean slate.

The final `jr` is a **tail call** — instead of `call flush_buffer_chunks; ret`, the compiler jumps directly, reusing the caller's return address. This saves one instruction and one level of stack.

## `I2cDriver::write` — Where Rust Meets the C SDK

This function bridges the Rust I2C abstraction to the ESP-IDF C driver.

```asm
4200a4d8 <esp_idf_hal::i2c::I2cDriver::write>:
  ; Arguments: a0 = &self, a1 = I2C address, a2 = data ptr, a3 = data len, a4 = timeout

  ; Prologue
  4200a4d8:  addi  sp, sp, -0x20       ; 32 bytes of stack
  4200a4e6:  mv    s2, a4              ; Save timeout
  4200a4ec:  mv    s5, a3              ; Save data length
  4200a4ee:  mv    s4, a2              ; Save data pointer
  4200a4f0:  mv    s6, a1              ; Save I2C address (0x3C)
  4200a4f2:  mv    s3, a0              ; Save &self

  ; --- Create I2C command link ---
  4200a4f4:  jalr  ...                 ; i2c_cmd_link_create_static()
  4200a4fc:  beqz  a0, error           ; If NULL → allocation failed

  ; --- Send START condition + slave address ---
  4200a50a:  slli  a1, s6, 0x19        ; Address manipulation:
  4200a50e:  srli  a1, a1, 0x18        ;   (0x3C << 25) >> 24 = 0x78 = address byte with W bit
  4200a512:  li    a2, 0x1             ;   ACK check = true
  4200a514:  jalr  ...                 ; i2c_master_start() + i2c_master_write_byte()

  ; --- Write the data payload ---
  4200a546:  beqz  s5, stop            ; If data length = 0, skip to STOP
  4200a54c:  mv    a0, s0              ; cmd handle
  4200a54e:  mv    a1, s4              ; data buffer pointer
  4200a550:  mv    a2, s5              ; data length (e.g., 1024 for full framebuffer)
  4200a552:  jalr  ...                 ; i2c_master_write() — bulk data transfer

  ; --- Send STOP condition ---
  4200a55e:  jalr  ...                 ; i2c_master_stop()

  ; --- Execute the queued I2C transaction ---
  4200a56e:  mv    a2, s2              ; timeout value
  4200a570:  jalr  ...                 ; i2c_master_cmd_begin() — this is where the
                                       ;   I2C hardware peripheral actually fires,
                                       ;   clocking out all the bits on GPIO6/GPIO7

  ; Epilogue
  4200a544:  ret
```

### The I2C address byte

The `slli`/`srli` pair at `0x4200a50a` constructs the I2C address byte per the protocol spec. I2C addresses are 7 bits, transmitted in the upper 7 bits of the first byte, with bit 0 as the R/W flag:

```
0x3C = 0b0111100
Shifted: 0b01111000 = 0x78  (bit 0 = 0 = Write)
```

The `slli 25` + `srli 24` is a branchless way to do `(addr << 1) & 0xFF`. The compiler chose shifts over an explicit multiply — same result, single-cycle instructions.

### Command queuing pattern

ESP-IDF's I2C driver uses a **command queue** pattern. The `i2c_master_start()`, `i2c_master_write_byte()`, `i2c_master_write()`, and `i2c_master_stop()` calls don't immediately touch the hardware — they build a command list in RAM. The final `i2c_master_cmd_begin()` submits the entire sequence to the I2C hardware peripheral, which executes it autonomously while the CPU waits for completion.

## `Command::send` — SSD1306 Command Dispatch

The SSD1306 has ~30 different configuration commands (set contrast, set addressing mode, set column address, etc.). The `ssd1306` crate models these as a Rust enum, and the `send` method dispatches to the right handler.

```asm
4200791c <ssd1306::command::Command::send>:
  ; a0 = &Command enum, a6 = &I2C interface

  4200791c:  lbu   a3, 0x0(a0)         ; Load enum discriminant (which command variant)
  42007920:  addi  a4, a3, -0x3        ; Adjust for jump table base
  42007928:  li    a2, 0x1b            ; 27 variants in the table
  4200792c:  bltu  a5, a2, table       ; If in range, use jump table
  42007930:  li    a4, 0x5             ; Otherwise default case

  ; --- Jump table dispatch ---
  42007940:  addi  a2, a2, -0x1a4      ; Load jump table base address
  42007944:  add   a1, a1, a2          ; Compute entry: base + discriminant × 4
  42007946:  lw    a1, 0x0(a1)         ; Load handler address from table
  42007948:  jr    a1                   ; Jump to handler!

  ; --- Example handlers ---
  ; SetContrast(value):
  4200794a:  lbu   a0, 0x1(a0)         ; Load contrast value from enum payload
  4200794e:  li    a1, 0x81            ; SSD1306 contrast command byte
  42007952:  j     send_two_bytes      ; Send [0x81, value]

  ; SetSegmentRemap(value):
  42007954:  lbu   a0, 0x1(a0)         ; Load remap flag
  42007958:  addi  a0, a0, 0xa0        ; 0xA0 or 0xA1 depending on flag
  4200795c:  j     send_one_byte       ; Send single command byte

  ; SetPageAddress(start, end):
  42007972:  lbu   a1, 0x1(a0)         ; Load start page
  42007976:  lbu   a2, 0x3(a0)         ; Load end page
  ...
  42007982:  addi  a1, a1, 0x26        ; 0x22 + offset = page address command
  ...                                  ; Send [0x22, 0x00, start, end, 0x00, 0xFF]

  ; ChargePump(enabled):
  420079c6:  lbu   a0, 0x1(a0)         ; Load enable flag
  420079ca:  slli  a0, a0, 0x2         ; 0 → 0x10 (off), 1 → 0x14 (on)
  420079cc:  addi  a0, a0, 0x10
  420079ce:  li    a1, 0x8d            ; Charge pump command byte
  420079d2:  j     send_two_bytes      ; Send [0x8D, 0x10/0x14]
```

### The jump table

The Rust `match` on the `Command` enum compiles to a **jump table** — an array of code addresses in read-only memory. The discriminant byte (enum variant index) is used as an index to load the target address, then `jr` jumps there. This is O(1) dispatch regardless of how many variants exist, versus a chain of `if-else` comparisons.

The `0x8D` (charge pump), `0x81` (contrast), `0xA0/0xA1` (segment remap), `0x22` (page address) values are the actual SSD1306 command opcodes defined in the datasheet. The Rust enum provides a type-safe wrapper, but at the assembly level it boils down to loading a constant and sending it over I2C.

## `main` — Application Entry Point

The `main` function is large (~1300 instructions) because it contains all the initialization logic inlined. Here are the notable parts.

### Stack frame

```asm
420083d6 <esp32c6_hello::main>:
  420083d6:  addi  sp, sp, -0x540       ; Allocate 1344 bytes of stack
```

1344 bytes is large for an embedded function. The breakdown:
- **1024 bytes**: The SSD1306 framebuffer (allocated on the stack in buffered graphics mode)
- **~128 bytes**: WiFi configuration structs (SSID, password, auth settings)
- **~128 bytes**: I2C driver state, display struct, text rendering temporaries
- **~64 bytes**: Callee-saved registers (s0-s11, ra)

### The framebuffer clear

```asm
  ; display.clear_buffer() — zero 1024 bytes
  420088b4:  li    a2, 0x400            ; 1024 = 0x400
  420088b8:  mv    a0, s1              ; framebuffer pointer (on stack)
  420088ba:  li    a1, 0x0             ; fill value = 0 (all pixels off)
  420088bc:  jalr  ...                 ; → memset(buf, 0, 1024)
```

`clear_buffer()` compiles to a single `memset` call. The ESP-IDF libc provides an optimized `memset` that operates on word-aligned chunks.

### I2C address and display configuration

```asm
  ; I2CDisplayInterface::new() stores the slave address
  420088da:  li    a0, 0x3c            ; SSD1306 I2C address = 0x3C
  420088de:  sb    a0, 0x8f(sp)        ; Store in interface struct on stack

  ; Display size byte
  420088e2:  sb    s0, 0x8e(sp)        ; s0 = display size discriminant (128x64)

  ; Data mode control byte
  420088e6:  li    a0, 0x40            ; 0x40 = "data follows" control byte
  420088ea:  sb    a0, 0x90(sp)        ; (vs 0x00 = "command follows")
```

The `0x3C` and `0x40` are I2C protocol constants. Every I2C transaction to the SSD1306 starts with the slave address (`0x3C`), then a control byte: `0x00` means "the next bytes are commands", `0x40` means "the next bytes are display data."

### Text rendering call

```asm
  ; Text::new("Hello from ESP32!", Point::new(0, 10), text_style).draw(&mut display)
  42008976:  sw    s1, 0x4c0(sp)       ; text_style pointer
  4200897a:  sw    s2, 0x4bc(sp)       ; font reference (FONT_6X10)
  42008980:  sw    s0, 0x4b0(sp)       ; x = 0 (already 1 from earlier, used as flag)
  42008984:  li    s3, 0x64            ; ...
  42008988:  sw    s3, 0x4b4(sp)       ; ...
  4200898c:  li    s5, 0x300           ; ...
  42008990:  sh    s5, 0x4b8(sp)       ; Packed Point struct on stack
  42008994:  addi  a0, sp, 0x4a4       ; Result output location
  42008998:  addi  a1, sp, 0x4b0       ; Text drawable struct
  4200899c:  addi  a2, sp, 0x8c        ; &mut display (DrawTarget)
  4200899e:  jalr  ...                 ; → draw() — iterates character bitmaps,
                                       ;   calls set_pixel for each "on" pixel
```

### The HSV LED loop (unchanged)

```asm
  ; The LED color cycle loop at the end of main
  42008c78:  li    a0, 0x0             ; hue = 0
  42008c7e:  lui   s0, 0x10
  42008c80:  addi  s0, s0, -0x1        ; s0 = 0xFFFF (mask)
  42008c82:  li    s11, 0x55           ; s11 = 85 (used in HSV→RGB conversion)
  42008c86:  lui   a1, 0x8
  42008c88:  addi  s2, a1, 0x81        ; s2 = 0x8081 (magic multiply constant)
  42008c8c:  li    s7, 0xff            ; s7 = 255 (saturation)
  42008c90:  li    s8, 0x14            ; s8 = 20 (value/brightness)

  ; Loop body — HSV to RGB conversion
  42008ca4:  and   a3, a0, s0          ; hue & 0xFFFF
  42008ca8:  slli  a1, a0, 0x1         ; hue × 2
  42008cac:  slli  a2, a1, 0x10        ; shift for fixed-point math
  42008cb0:  lui   a4, 0x60610
  42008cb4:  mulhu a2, a2, a4          ; Fixed-point division by 85 via magic multiply
```

The `mulhu` instruction (multiply high unsigned) with the magic constant `0x60610xxx` implements fixed-point division by 85 — this is the compiler's classic trick for replacing slow division with fast multiplication. The value 85 = 255/3, used in HSV-to-RGB conversion to split the hue wheel into thirds.

## Memory Layout

The binary occupies these regions in the ESP32-C6 address space:

```
0x40800000  .iram0.text    — Interrupt vectors, critical ISRs (runs from SRAM)
0x42000000  .flash.text    — All application code including main, SSD1306 driver,
                             embedded-graphics (runs from flash via cache)
0x420C0000  .flash.rodata  — Read-only data: font bitmaps, string literals,
                             jump tables, format strings
```

Code in `.iram0.text` (SRAM) executes in a single cycle. Code in `.flash.text` runs through a cache — hits are fast, misses stall while a flash read completes. The ESP-IDF linker script automatically places interrupt handlers in IRAM and everything else in flash.

## Size Breakdown

```
$ cargo size --release
   text     data      bss      dec      hex
 860442   199352   895788  1955582   1dd6fe
```

- **text** (860 KB): All compiled code. Most of this is ESP-IDF (WiFi, TLS, lwIP, FreeRTOS). The OLED display code adds roughly 2-3 KB.
- **data** (199 KB): Initialized globals (WiFi calibration data, crypto tables, etc.)
- **bss** (895 KB): Zero-initialized globals (WiFi buffers, TCP/IP stack buffers). This lives in SRAM and is zeroed at boot.

The display-specific functions are compact:
- `set_pixel`: 44 instructions (~88 bytes)
- `flush`: ~100 instructions (~200 bytes)
- `Command::send`: ~200 instructions (~400 bytes)
- `I2cDriver::write`: ~50 instructions (~100 bytes)

Total OLED overhead in flash: roughly 2-3 KB out of 860 KB — the WiFi/TLS stack dwarfs everything else.
