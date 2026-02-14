# Version 2: Conway's Game of Life on ESP32-C6

## What We Built

A pixel-level Conway's Game of Life simulation running on the ESP32-C6 microcontroller, rendered on a 128x64 SSD1306 OLED display. Each pixel is one cell, giving us an 8,192-cell toroidal universe that wraps around at the edges. The simulation cycles through 7 visually distinct scenes every ~10 seconds, seeded with randomness from the hardware timer.

## What Changed From Version 1

Version 1 was a WiFi + DHT11 temperature sensor demo with text on the OLED. Version 2 replaces all of that:

| | v1 | v2 |
|---|---|---|
| **Display** | Text lines (temp, IP, status) | Pixel-per-cell Game of Life |
| **WiFi** | Connected, showed IP | Removed entirely |
| **DHT11 sensor** | Read temp/humidity | Removed |
| **Flash usage** | 99.73% (dangerously full) | 38.24% (plenty of room) |
| **Binary size** | ~2 MB | ~400 KB |
| **LED** | Fast hue cycle, bright | Slow hue cycle, dim heartbeat |

Disabling WiFi and Bluetooth in `sdkconfig.defaults` (`CONFIG_ESP_WIFI_ENABLED=n`, `CONFIG_BT_ENABLED=n`) was the key to the massive size reduction. The WiFi stack alone is ~1.2 MB of compiled code.

## How Conway's Game of Life Works

The Game of Life is a cellular automaton invented by mathematician John Conway in 1970. It runs on an infinite 2D grid where each cell is either alive or dead. Every generation, all cells update simultaneously according to two simple rules:

- **Birth**: A dead cell with exactly 3 live neighbors becomes alive
- **Survival**: A live cell with 2 or 3 live neighbors stays alive
- **Death**: Everything else dies (loneliness if <2 neighbors, overcrowding if >3)

These simple rules produce extraordinarily complex emergent behavior — gliders that travel across the grid, guns that produce streams of gliders, oscillators that pulse, and chaotic patterns that evolve for thousands of generations before stabilizing.

## The Grid: Bitfield vs HashSet

The reference implementation in `docs/life/lib.rs` uses a `HashSet<(i64, i64)>` — a sparse representation where only live cells are stored. This is elegant for an infinite grid on a desktop, but terrible for a microcontroller:

- `HashSet` does heap allocations (unpredictable on embedded)
- Each cell costs ~16 bytes of heap + hash table overhead
- Hash lookups are slow compared to bit operations

Instead, we use a **fixed-size bitfield**: a `[u8; 1024]` array where each bit represents one cell. For a 128x64 grid, that's exactly 8,192 bits = 1,024 bytes. Two grids (double buffering) cost just 2 KB total.

```
Bit index = y * 128 + x
Byte index = bit_index / 8
Bit position = bit_index % 8
```

Reading a cell is a single AND operation. Setting a cell is a single OR. The entire grid fits in L1 cache.

## Double Buffering

We can't update cells in-place because each cell's next state depends on its current neighbors — if we modified cells as we went, later cells would see a mix of old and new states.

Solution: two grids, `grid_a` and `grid_b`. Each generation reads from one and writes to the other, then we swap which is "current":

```
Gen 0: read grid_a → write grid_b
Gen 1: read grid_b → write grid_a
Gen 2: read grid_a → write grid_b
...
```

No allocation, no copying — just a boolean flip.

## Toroidal Wrapping

Our grid wraps around at the edges — a glider that flies off the right side reappears on the left. This is called a **torus** topology (imagine wrapping the grid into a donut shape).

The neighbor counting uses modular arithmetic with a trick to avoid signed integers:

```rust
for dy in [HEIGHT - 1, 0, 1] {   // equivalent to [-1, 0, +1]
    for dx in [WIDTH - 1, 0, 1] {
        let nx = (x + dx) % WIDTH;
        let ny = (y + dy) % HEIGHT;
    }
}
```

Adding `HEIGHT - 1` and then taking `% HEIGHT` is the same as subtracting 1 with wrapping, but stays in unsigned arithmetic. This avoids any signed/unsigned conversion on the RISC-V CPU.

## The PRNG

We need randomness to seed the grid, but we don't have a `rand` crate (and don't want the binary size hit). Instead, we use **xorshift32** — a minimal pseudo-random number generator that fits in 4 lines:

```rust
fn next(&mut self) -> u32 {
    self.0 ^= self.0 << 13;
    self.0 ^= self.0 >> 17;
    self.0 ^= self.0 << 5;
    self.0
}
```

It's seeded from `esp_timer_get_time()`, the ESP32's microsecond hardware timer. Since the exact microsecond of boot varies slightly each time, we get different patterns on every power cycle.

Xorshift32 has a period of 2^32 - 1 (about 4 billion values before repeating) and passes basic randomness tests. It's not cryptographically secure, but for scattering pixels it's perfect.

## The 7 Scenes

Each scene combines classic Game of Life patterns with random noise to fill the screen:

### 1. R-pentomino + soup
The R-pentomino is just 5 cells, but it's one of the most chaotic patterns in Game of Life — it takes 1,103 generations to stabilize on an infinite grid and produces gliders, blocks, blinkers, and other debris. We place it center-screen with ~3% random fill around it. The random cells create interactions that wouldn't happen in isolation.

### 2. Gosper Gun + chaos
Two Gosper Glider Guns placed in opposite corners, firing streams of gliders into ~4% random debris. The Gosper Gun (discovered in 1970 by Bill Gosper) was the first known finite pattern that grows without bound — it produces a new glider every 30 generations. With two guns and random obstacles, the gliders collide and create unpredictable structures.

### 3. Random soup (18% fill)
No patterns, just random cells at ~18% density. This is the sweet spot for maximum visual chaos — enough cells to sustain complex interactions, but not so many that everything dies of overcrowding immediately. The initial explosion is spectacular.

### 4. Armada
Six gliders spaced diagonally across the screen, plus three LWSS (Lightweight Spaceships) moving horizontally, all through light random debris. The glider moves diagonally at c/4 (one cell per 4 generations), while the LWSS moves horizontally at c/2 — watching them navigate through random obstacles is mesmerizing.

### 5. Pulsar garden
Six pulsars arranged in a 3x2 grid with tiny random perturbation. The pulsar is a period-3 oscillator (the most common non-trivial oscillator in Game of Life), and normally it's perfectly stable. But the ~1% random noise breaks the symmetry, causing some pulsars to "melt" into chaotic debris while others survive — a study in fragility.

### 6. R-pentomino collider
Five R-pentominoes placed at different positions, expanding into each other. Each one produces its own chaotic wavefront, and when they collide, the interactions create entirely new structures. Like a particle collider, but for cellular automata.

### 7. Primordial soup (25% fill)
Dense random fill at 25%. The initial generations are explosive — massive die-off with pockets of stability forming. After ~50 generations it settles into a rich ecosystem of still lifes, oscillators, and occasionally escaping spaceships.

## Performance

Each generation computes 8,192 cells × 8 neighbors = 65,536 neighbor lookups. At 160 MHz on the RISC-V core, this takes well under 1ms. The bottleneck is I2C display transfer — flushing 1024 bytes to the SSD1306 at 400 kHz I2C takes ~20-25ms. With the 50ms sleep, we get roughly 12-14 fps.

## The Display Pipeline

Each frame follows this sequence:

```
1. clear_buffer()           — zero the 1024-byte framebuffer in RAM
2. for each live cell:
     set_pixel(x, y, true)  — set corresponding bit in framebuffer
3. flush()                  — I2C transfer: send framebuffer to SSD1306
```

The SSD1306 stores pixels in a column-major format (8 vertical pixels per byte), while our Game of Life grid is row-major. The `set_pixel()` method handles this translation internally — we just give it (x, y) coordinates and it maps to the right byte and bit in the display buffer.

## Files Modified

```
esp32c6-hello/
├── src/main.rs           # Complete rewrite — Game of Life engine + renderer
├── sdkconfig.defaults    # Disabled WiFi/BT, reduced stack 16K → 8K
└── .cargo/config.toml    # Added partition table flag to runner
```

## What's Still There (Unchanged)

- ESP-IDF boilerplate (`link_patches`, `EspLogger`)
- I2C OLED initialization (same pins, same config)
- WS2812 RGB LED (now a slow dim heartbeat instead of bright cycle)
- All Cargo.toml dependencies (unused WiFi code is dead-code eliminated by LTO)
