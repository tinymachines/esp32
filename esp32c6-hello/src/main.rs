use esp_idf_svc::hal::gpio::PinDriver;
use esp_idf_svc::hal::i2c::{I2cConfig, I2cDriver};
use esp_idf_svc::hal::peripherals::Peripherals;
use smart_leds::hsv::{hsv2rgb, Hsv};
use smart_leds::SmartLedsWrite;
use ssd1306::{prelude::*, I2CDisplayInterface, Ssd1306};
use std::thread;
use std::time::Duration;
use ws2812_esp32_rmt_driver::Ws2812Esp32Rmt;

const WIDTH: usize = 128;
const HEIGHT: usize = 64;
const GRID_BYTES: usize = WIDTH * HEIGHT / 8; // 1024

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

/// Bitfield grid: 128x64, row-major, 1 bit per cell.
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
        let idx = y * WIDTH + x;
        self.cells[idx / 8] & (1 << (idx % 8)) != 0
    }

    #[inline]
    fn set(&mut self, x: usize, y: usize) {
        let idx = y * WIDTH + x;
        self.cells[idx / 8] |= 1 << (idx % 8);
    }

    fn clear(&mut self) {
        self.cells.fill(0);
    }

    /// Count total live cells using popcount.
    fn population(&self) -> u32 {
        self.cells.iter().map(|b| b.count_ones()).sum()
    }
}

/// Count live neighbors with toroidal wrapping.
#[inline]
fn count_neighbors(grid: &Grid, x: usize, y: usize) -> u8 {
    let mut count = 0u8;
    for dy in [HEIGHT - 1, 0, 1] {
        for dx in [WIDTH - 1, 0, 1] {
            if dx == 0 && dy == 0 {
                continue;
            }
            let nx = (x + dx) % WIDTH;
            let ny = (y + dy) % HEIGHT;
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
    for y in 0..HEIGHT {
        for x in 0..WIDTH {
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
                let x = (offset_x + col) % WIDTH;
                let y = (offset_y + row) % HEIGHT;
                grid.set(x, y);
            }
        }
    }
}

/// Scatter random live cells across the grid (~density/256 fill rate).
fn scatter_random(grid: &mut Grid, rng: &mut Rng, density: u8) {
    for y in 0..HEIGHT {
        for x in 0..WIDTH {
            if (rng.next() & 0xFF) < density as u32 {
                grid.set(x, y);
            }
        }
    }
}

/// Map population to LED color reflecting colony health.
/// Red = dying/empty, green = thriving, blue/cyan = overcrowded.
/// Brightness pulses with rate of change.
fn health_color(pop: u32, prev_pop: u32) -> Hsv {
    // Map population to hue: 0 (red) → 80 (green) → 140 (cyan)
    // Sweet spot ~300-800 cells = green
    let hue = if pop < 50 {
        0 // red — nearly dead
    } else if pop < 300 {
        // red → green as population grows
        ((pop - 50) * 80 / 250) as u8
    } else if pop < 800 {
        80 // green — thriving
    } else if pop < 1500 {
        // green → cyan as population gets dense
        (80 + (pop - 800) * 60 / 700) as u8
    } else {
        140 // cyan/blue — overcrowded
    };

    // Brightness based on rate of change — big changes = bright flash
    let delta = (pop as i32 - prev_pop as i32).unsigned_abs();
    let val = if delta > 100 {
        40 // bright flash — explosion or mass die-off
    } else if delta > 30 {
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
            stamp_pattern(grid, R_PENTOMINO, 62, 30);
            scatter_random(grid, rng, 8);
        },
    },
    Scene {
        name: "Gosper Gun + chaos",
        load: |grid, rng| {
            grid.clear();
            stamp_pattern(grid, GOSPER_GUN, 2, 2);
            stamp_pattern(grid, GOSPER_GUN, 90, 50);
            scatter_random(grid, rng, 10);
        },
    },
    Scene {
        name: "Random soup",
        load: |grid, rng| {
            grid.clear();
            scatter_random(grid, rng, 45);
        },
    },
    Scene {
        name: "Armada",
        load: |grid, rng| {
            grid.clear();
            for i in 0..6 {
                stamp_pattern(grid, GLIDER, i * 20, i * 10);
            }
            stamp_pattern(grid, LWSS, 4, 15);
            stamp_pattern(grid, LWSS, 4, 45);
            stamp_pattern(grid, LWSS, 60, 30);
            scatter_random(grid, rng, 5);
        },
    },
    Scene {
        name: "Pulsar garden",
        load: |grid, rng| {
            grid.clear();
            stamp_pattern(grid, PULSAR, 5, 5);
            stamp_pattern(grid, PULSAR, 55, 5);
            stamp_pattern(grid, PULSAR, 105, 5);
            stamp_pattern(grid, PULSAR, 5, 40);
            stamp_pattern(grid, PULSAR, 55, 40);
            stamp_pattern(grid, PULSAR, 105, 40);
            scatter_random(grid, rng, 3);
        },
    },
    Scene {
        name: "R-pentomino collider",
        load: |grid, rng| {
            grid.clear();
            stamp_pattern(grid, R_PENTOMINO, 20, 15);
            stamp_pattern(grid, R_PENTOMINO, 60, 30);
            stamp_pattern(grid, R_PENTOMINO, 100, 15);
            stamp_pattern(grid, R_PENTOMINO, 40, 50);
            stamp_pattern(grid, R_PENTOMINO, 80, 50);
            scatter_random(grid, rng, 6);
        },
    },
    Scene {
        name: "Primordial soup",
        load: |grid, rng| {
            grid.clear();
            scatter_random(grid, rng, 64);
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

    // Game of Life state — double buffered
    let mut grid_a = Grid::new();
    let mut grid_b = Grid::new();
    let mut use_a = true;

    let mut scene_idx: usize = 0;
    let mut generation: u32 = 0;
    let mut prev_pop: u32 = 0;
    let mut button_was_pressed = false;

    // Load initial scene
    let scene = &SCENES[scene_idx];
    (scene.load)(&mut grid_a, &mut rng);
    log::info!("Scene: {} (gen 0)", scene.name);

    loop {
        let current = if use_a { &grid_a } else { &grid_b };

        // Render
        display.clear_buffer();
        for y in 0..HEIGHT {
            for x in 0..WIDTH {
                if current.get(x, y) {
                    let _ = display.set_pixel(x as u32, y as u32, true);
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

        // Button: reroll current scene (edge-triggered, debounced)
        let pressed = button.is_low();
        if pressed && !button_was_pressed {
            // Re-seed the RNG from timer for fresh randomness
            rng = Rng::from_timer();
            let scene = &SCENES[scene_idx];
            let grid = if use_a { &mut grid_a } else { &mut grid_b };
            (scene.load)(grid, &mut rng);
            generation = 0;
            log::info!("Reroll: {} (button)", scene.name);
        }
        button_was_pressed = pressed;

        // Auto-cycle scene every 200 generations
        if generation % 200 == 0 && generation > 0 {
            scene_idx = (scene_idx + 1) % SCENES.len();
            let scene = &SCENES[scene_idx];
            let grid = if use_a { &mut grid_a } else { &mut grid_b };
            (scene.load)(grid, &mut rng);
            log::info!("Scene: {} (gen {})", scene.name, generation);
        }

        thread::sleep(Duration::from_millis(50));
    }
}
