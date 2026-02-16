use esp_idf_svc::hal::gpio::PinDriver;
use esp_idf_svc::hal::i2c::{I2cConfig, I2cDriver};
use esp_idf_svc::hal::peripherals::Peripherals;
use smart_leds::hsv::{hsv2rgb, Hsv};
use smart_leds::SmartLedsWrite;
use ssd1306::{prelude::*, I2CDisplayInterface, Ssd1306};
use std::thread;
use std::time::Duration;
use ws2812_esp32_rmt_driver::Ws2812Esp32Rmt;

const WORLD_W: usize = 512;
const WORLD_H: usize = 256;
const SCREEN_W: usize = 128;
const SCREEN_H: usize = 64;
const GRID_BYTES: usize = WORLD_W * WORLD_H / 8; // 16,384
const TILE_W: usize = 64;
const TILE_H: usize = 64;
const TILES_X: usize = WORLD_W / TILE_W; // 8
const TILES_Y: usize = WORLD_H / TILE_H; // 4

/// Simple xorshift32 PRNG seeded from hardware timer.
struct Rng(u32);

impl Rng {
    fn from_timer() -> Self {
        let seed = unsafe { esp_idf_svc::sys::esp_timer_get_time() } as u32;
        Self(seed | 1)
    }

    fn next(&mut self) -> u32 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 17;
        self.0 ^= self.0 << 5;
        self.0
    }
}

/// Bitfield grid: WORLD_W x WORLD_H, row-major, 1 bit per cell.
struct Grid {
    cells: [u8; GRID_BYTES],
}

impl Grid {
    fn new() -> Self {
        Self {
            cells: [0u8; GRID_BYTES],
        }
    }

    #[inline]
    fn get(&self, x: usize, y: usize) -> bool {
        let idx = y * WORLD_W + x;
        self.cells[idx / 8] & (1 << (idx % 8)) != 0
    }

    #[inline]
    fn set(&mut self, x: usize, y: usize) {
        let idx = y * WORLD_W + x;
        self.cells[idx / 8] |= 1 << (idx % 8);
    }

    fn clear(&mut self) {
        self.cells.fill(0);
    }

    /// Count total live cells using popcount.
    fn population(&self) -> u32 {
        self.cells.iter().map(|b| b.count_ones()).sum()
    }

    /// Count population in a tile (TILE_W x TILE_H block).
    fn tile_population(&self, tx: usize, ty: usize) -> u32 {
        let mut count = 0u32;
        let x0_byte = tx * (TILE_W / 8);
        let row_bytes = WORLD_W / 8;
        for row in 0..TILE_H {
            let base = (ty * TILE_H + row) * row_bytes + x0_byte;
            for b in 0..(TILE_W / 8) {
                count += self.cells[base + b].count_ones();
            }
        }
        count
    }
}

/// Count live neighbors with toroidal wrapping.
#[inline]
fn count_neighbors(grid: &Grid, x: usize, y: usize) -> u8 {
    let mut count = 0u8;
    for dy in [WORLD_H - 1, 0, 1] {
        for dx in [WORLD_W - 1, 0, 1] {
            if dx == 0 && dy == 0 {
                continue;
            }
            let nx = (x + dx) % WORLD_W;
            let ny = (y + dy) % WORLD_H;
            if grid.get(nx, ny) {
                count += 1;
            }
        }
    }
    count
}

/// Advance one generation: read from `current`, write into `next`.
fn step(current: &Grid, next: &mut Grid) {
    next.clear();
    for y in 0..WORLD_H {
        for x in 0..WORLD_W {
            let neighbors = count_neighbors(current, x, y);
            let alive = current.get(x, y);
            if neighbors == 3 || (alive && neighbors == 2) {
                next.set(x, y);
            }
        }
    }
}

/// Stamp a pattern into grid (additive — doesn't clear first).
fn stamp_pattern(grid: &mut Grid, pattern: &str, offset_x: usize, offset_y: usize) {
    for (row, line) in pattern.lines().enumerate() {
        for (col, ch) in line.chars().enumerate() {
            if ch == 'O' {
                let x = (offset_x + col) % WORLD_W;
                let y = (offset_y + row) % WORLD_H;
                grid.set(x, y);
            }
        }
    }
}

/// Scatter random live cells across the grid (~density/256 fill rate).
fn scatter_random(grid: &mut Grid, rng: &mut Rng, density: u8) {
    for y in 0..WORLD_H {
        for x in 0..WORLD_W {
            if (rng.next() & 0xFF) < density as u32 {
                grid.set(x, y);
            }
        }
    }
}

/// Map population to LED color reflecting colony health.
/// Red = dying/empty, green = thriving, blue/cyan = overcrowded.
/// Brightness pulses with rate of change.
/// Thresholds scaled for 512x256 world (~131K cells).
fn health_color(pop: u32, prev_pop: u32) -> Hsv {
    // Map population to hue: 0 (red) → 80 (green) → 140 (cyan)
    // Sweet spot ~5000-12000 cells = green (16x the old 128x64 thresholds)
    let hue = if pop < 800 {
        0 // red — nearly dead
    } else if pop < 5000 {
        // red → green as population grows
        ((pop - 800) * 80 / 4200) as u8
    } else if pop < 12000 {
        80 // green — thriving
    } else if pop < 24000 {
        // green → cyan as population gets dense
        (80 + (pop - 12000) * 60 / 12000) as u8
    } else {
        140 // cyan/blue — overcrowded
    };

    // Brightness based on rate of change — big changes = bright flash
    let delta = (pop as i32 - prev_pop as i32).unsigned_abs();
    let val = if delta > 1600 {
        40 // bright flash — explosion or mass die-off
    } else if delta > 500 {
        20
    } else {
        8 // calm
    };

    Hsv {
        hue,
        sat: 255,
        val,
    }
}

// ─── Viewport ────────────────────────────────────────────────────

struct Viewport {
    x: i32,
    y: i32,
    tx: i32,
    ty: i32,
    linger: u32,
}

impl Viewport {
    fn new() -> Self {
        Self {
            x: 0,
            y: 0,
            tx: 0,
            ty: 0,
            linger: 0,
        }
    }

    /// Pick a new random target and linger duration.
    fn pick_target(&mut self, rng: &mut Rng) {
        self.tx = (rng.next() % WORLD_W as u32) as i32;
        self.ty = (rng.next() % WORLD_H as u32) as i32;
        self.linger = 60 + (rng.next() % 61); // 60–120 generations
    }

    /// Pick target biased toward regions with live cells.
    /// Scans 8x4 tiles, picks one weighted by population.
    fn pick_target_seeking(&mut self, grid: &Grid, rng: &mut Rng) {
        // Compute population per tile
        let mut pops = [0u32; TILES_X * TILES_Y];
        let mut total = 0u32;
        for ty in 0..TILES_Y {
            for tx in 0..TILES_X {
                let p = grid.tile_population(tx, ty);
                pops[ty * TILES_X + tx] = p;
                total += p;
            }
        }

        if total == 0 {
            // World is empty, pick random
            self.pick_target(rng);
            return;
        }

        // Weighted random selection
        let mut threshold = rng.next() % total;
        let mut chosen_tx = 0;
        let mut chosen_ty = 0;
        'outer: for ty in 0..TILES_Y {
            for tx in 0..TILES_X {
                let p = pops[ty * TILES_X + tx];
                if threshold < p {
                    chosen_tx = tx;
                    chosen_ty = ty;
                    break 'outer;
                }
                threshold -= p;
            }
        }

        // Target center of chosen tile + random jitter within tile
        self.tx = (chosen_tx * TILE_W + (rng.next() as usize % TILE_W)) as i32;
        self.ty = (chosen_ty * TILE_H + (rng.next() as usize % TILE_H)) as i32;
        self.linger = 60 + (rng.next() % 61);
    }

    /// Move one pixel toward target each axis, wrapping toroidally.
    fn update(&mut self, grid: &Grid, rng: &mut Rng) {
        if self.linger > 0 {
            self.linger -= 1;
            if self.linger == 0 {
                self.pick_target_seeking(grid, rng);
            }
            return;
        }

        // Shortest-path movement on torus for x
        let dx = ((self.tx - self.x).rem_euclid(WORLD_W as i32) + WORLD_W as i32 / 2)
            % WORLD_W as i32
            - WORLD_W as i32 / 2;
        if dx > 0 {
            self.x = (self.x + 1).rem_euclid(WORLD_W as i32);
        } else if dx < 0 {
            self.x = (self.x - 1).rem_euclid(WORLD_W as i32);
        }

        // Shortest-path movement on torus for y
        let dy = ((self.ty - self.y).rem_euclid(WORLD_H as i32) + WORLD_H as i32 / 2)
            % WORLD_H as i32
            - WORLD_H as i32 / 2;
        if dy > 0 {
            self.y = (self.y + 1).rem_euclid(WORLD_H as i32);
        } else if dy < 0 {
            self.y = (self.y - 1).rem_euclid(WORLD_H as i32);
        }

        // Arrived at target — start lingering
        if dx == 0 && dy == 0 {
            self.linger = 60 + (rng.next() % 61);
        }
    }
}

// ─── Patterns ────────────────────────────────────────────────────

const GLIDER: &str = "\
.O.
..O
OOO";

const GOSPER_GUN: &str = "\
........................O...........
......................O.O...........
............OO......OO............OO
...........O...O....OO............OO
OO........O.....O...OO..............
OO........O...O.OO....O.O...........
..........O.....O.......O...........
...........O...O....................
............OO......................";

const R_PENTOMINO: &str = "\
.OO
OO.
.O.";

const LWSS: &str = "\
.O..O
O....
O...O
OOOO.";

const PULSAR: &str = "\
..OOO...OOO..
.............
O....O.O....O
O....O.O....O
O....O.O....O
..OOO...OOO..
.............
..OOO...OOO..
O....O.O....O
O....O.O....O
O....O.O....O
.............
..OOO...OOO..";

struct Scene {
    name: &'static str,
    load: fn(&mut Grid, &mut Rng),
}

const SCENES: &[Scene] = &[
    Scene {
        name: "R-pentomino + soup",
        load: |grid, rng| {
            grid.clear();
            // Scatter R-pentominoes across the world
            for _ in 0..16 {
                let x = (rng.next() % WORLD_W as u32) as usize;
                let y = (rng.next() % WORLD_H as u32) as usize;
                stamp_pattern(grid, R_PENTOMINO, x, y);
            }
            scatter_random(grid, rng, 20);
        },
    },
    Scene {
        name: "Gosper Gun + chaos",
        load: |grid, rng| {
            grid.clear();
            // Place guns in each quadrant
            stamp_pattern(grid, GOSPER_GUN, 20, 20);
            stamp_pattern(grid, GOSPER_GUN, 280, 20);
            stamp_pattern(grid, GOSPER_GUN, 20, 140);
            stamp_pattern(grid, GOSPER_GUN, 280, 140);
            stamp_pattern(grid, GOSPER_GUN, 150, 80);
            stamp_pattern(grid, GOSPER_GUN, 400, 200);
            scatter_random(grid, rng, 25);
        },
    },
    Scene {
        name: "Random soup",
        load: |grid, rng| {
            grid.clear();
            scatter_random(grid, rng, 70);
        },
    },
    Scene {
        name: "Armada",
        load: |grid, rng| {
            grid.clear();
            for _ in 0..32 {
                let x = (rng.next() % WORLD_W as u32) as usize;
                let y = (rng.next() % WORLD_H as u32) as usize;
                stamp_pattern(grid, GLIDER, x, y);
            }
            for _ in 0..16 {
                let x = (rng.next() % WORLD_W as u32) as usize;
                let y = (rng.next() % WORLD_H as u32) as usize;
                stamp_pattern(grid, LWSS, x, y);
            }
            scatter_random(grid, rng, 15);
        },
    },
    Scene {
        name: "Pulsar garden",
        load: |grid, rng| {
            grid.clear();
            // Grid of pulsars spread across the world
            for row in 0..4 {
                for col in 0..8 {
                    stamp_pattern(
                        grid,
                        PULSAR,
                        col * 60 + 10,
                        row * 60 + 10,
                    );
                }
            }
            scatter_random(grid, rng, 12);
        },
    },
    Scene {
        name: "R-pentomino collider",
        load: |grid, rng| {
            grid.clear();
            for _ in 0..20 {
                let x = (rng.next() % WORLD_W as u32) as usize;
                let y = (rng.next() % WORLD_H as u32) as usize;
                stamp_pattern(grid, R_PENTOMINO, x, y);
            }
            scatter_random(grid, rng, 18);
        },
    },
    Scene {
        name: "Primordial soup",
        load: |grid, rng| {
            grid.clear();
            scatter_random(grid, rng, 90);
        },
    },
];

fn main() -> anyhow::Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take()?;

    // LED setup
    let mut ws2812 = Ws2812Esp32Rmt::new(peripherals.rmt.channel0, peripherals.pins.gpio8)?;
    log::info!("RGB LED ready");

    // BOOT button on GPIO9 — active low, internal pull-up
    let button = PinDriver::input(peripherals.pins.gpio9)?;
    log::info!("Button ready (GPIO9 BOOT)");

    // OLED display setup (SSD1306 128x64 I2C on GPIO6/GPIO7)
    let i2c_config = I2cConfig::new().baudrate(400_000.into());
    let i2c = I2cDriver::new(
        peripherals.i2c0,
        peripherals.pins.gpio6,
        peripherals.pins.gpio7,
        &i2c_config,
    )?;

    let interface = I2CDisplayInterface::new(i2c);
    let mut display = Ssd1306::new(interface, DisplaySize128x64, DisplayRotation::Rotate0)
        .into_buffered_graphics_mode();
    display
        .init()
        .map_err(|e| anyhow::anyhow!("Display init: {:?}", e))?;
    display.clear_buffer();
    display
        .flush()
        .map_err(|e| anyhow::anyhow!("Flush: {:?}", e))?;
    log::info!("OLED display ready");

    let mut rng = Rng::from_timer();

    // Game of Life state — double buffered (heap-allocated for 32 KB each)
    let mut grid_a = Box::new(Grid::new());
    let mut grid_b = Box::new(Grid::new());
    let mut use_a = true;

    let mut scene_idx: usize = 0;
    let mut generation: u32 = 0;
    let mut prev_pop: u32 = 0;
    let mut button_was_pressed = false;

    let mut vp = Viewport::new();

    // Load initial scene
    let scene = &SCENES[scene_idx];
    (scene.load)(&mut grid_a, &mut rng);
    vp.pick_target_seeking(&grid_a, &mut rng);
    log::info!("Scene: {} (gen 0)", scene.name);

    loop {
        let current = if use_a { &*grid_a } else { &*grid_b };

        // Render viewport region
        display.clear_buffer();
        for sy in 0..SCREEN_H {
            for sx in 0..SCREEN_W {
                let wx = (vp.x as usize + sx) % WORLD_W;
                let wy = (vp.y as usize + sy) % WORLD_H;
                if current.get(wx, wy) {
                    let _ = display.set_pixel(sx as u32, sy as u32, true);
                }
            }
        }
        display
            .flush()
            .map_err(|e| anyhow::anyhow!("Flush: {:?}", e))?;

        // Population + health LED
        let pop = current.population();
        let color = hsv2rgb(health_color(pop, prev_pop));
        ws2812.write([color].iter().copied())?;
        prev_pop = pop;

        // Step
        if use_a {
            step(&grid_a, &mut grid_b);
        } else {
            step(&grid_b, &mut grid_a);
        }
        use_a = !use_a;
        generation += 1;

        // Pan viewport
        let current_ref = if use_a { &*grid_a } else { &*grid_b };
        vp.update(current_ref, &mut rng);

        // Button: reroll current scene (edge-triggered, debounced)
        let pressed = button.is_low();
        if pressed && !button_was_pressed {
            rng = Rng::from_timer();
            let scene = &SCENES[scene_idx];
            let grid = if use_a { &mut *grid_a } else { &mut *grid_b };
            (scene.load)(grid, &mut rng);
            generation = 0;
            vp = Viewport::new();
            vp.pick_target_seeking(grid, &mut rng);
            log::info!("Reroll: {} (button)", scene.name);
        }
        button_was_pressed = pressed;

        // Auto-cycle scene every 200 generations
        if generation % 200 == 0 && generation > 0 {
            scene_idx = (scene_idx + 1) % SCENES.len();
            let scene = &SCENES[scene_idx];
            let grid = if use_a { &mut *grid_a } else { &mut *grid_b };
            (scene.load)(grid, &mut rng);
            vp = Viewport::new();
            vp.pick_target_seeking(grid, &mut rng);
            log::info!("Scene: {} (gen {})", scene.name, generation);
        }

        thread::sleep(Duration::from_millis(50));
    }
}
