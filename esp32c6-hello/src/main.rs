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
            // B3/S23: born if 3 neighbors, survive if 2 or 3
            if neighbors == 3 || (alive && neighbors == 2) {
                next.set(x, y);
            }
        }
    }
}

/// Load a pattern string ('O' = alive, '.' = dead) into grid at offset.
fn load_pattern(grid: &mut Grid, pattern: &str, offset_x: usize, offset_y: usize) {
    grid.clear();
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

// ─── Patterns ported from docs/life/lib.rs ───────────────────────

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

struct PatternInfo {
    name: &'static str,
    data: &'static str,
    offset_x: usize,
    offset_y: usize,
}

const PATTERNS: &[PatternInfo] = &[
    PatternInfo {
        name: "Gosper Glider Gun",
        data: GOSPER_GUN,
        offset_x: 2,
        offset_y: 2,
    },
    PatternInfo {
        name: "R-pentomino",
        data: R_PENTOMINO,
        offset_x: 62,
        offset_y: 30,
    },
    PatternInfo {
        name: "Pulsar",
        data: PULSAR,
        offset_x: 57,
        offset_y: 25,
    },
    PatternInfo {
        name: "Glider",
        data: GLIDER,
        offset_x: 10,
        offset_y: 10,
    },
    PatternInfo {
        name: "LWSS",
        data: LWSS,
        offset_x: 4,
        offset_y: 30,
    },
];

fn main() -> anyhow::Result<()> {
    esp_idf_svc::sys::link_patches();
    esp_idf_svc::log::EspLogger::initialize_default();

    let peripherals = Peripherals::take()?;

    // LED setup
    let mut ws2812 = Ws2812Esp32Rmt::new(peripherals.rmt.channel0, peripherals.pins.gpio8)?;
    log::info!("RGB LED ready");

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

    // Game of Life state — double buffered
    let mut grid_a = Grid::new();
    let mut grid_b = Grid::new();
    let mut use_a = true; // which grid is current

    let mut pattern_idx: usize = 0;
    let mut generation: u32 = 0;
    let mut hue: u8 = 0;

    // Load initial pattern
    let p = &PATTERNS[pattern_idx];
    load_pattern(&mut grid_a, p.data, p.offset_x, p.offset_y);
    log::info!("Pattern: {} (gen 0)", p.name);

    loop {
        let current = if use_a { &grid_a } else { &grid_b };

        // Render: set pixels for live cells
        display.clear_buffer();
        for y in 0..HEIGHT {
            for x in 0..WIDTH {
                if current.get(x, y) {
                    // Pixel::new returns a drawable but set_pixel is more direct
                    let _ = display.set_pixel(x as u32, y as u32, true);
                }
            }
        }
        display
            .flush()
            .map_err(|e| anyhow::anyhow!("Flush: {:?}", e))?;

        // Step: compute next generation
        if use_a {
            step(&grid_a, &mut grid_b);
        } else {
            step(&grid_b, &mut grid_a);
        }
        use_a = !use_a;
        generation += 1;

        // LED heartbeat: slow hue cycle
        let color = hsv2rgb(Hsv {
            hue,
            sat: 255,
            val: 8,
        });
        ws2812.write([color].iter().copied())?;
        hue = hue.wrapping_add(1);

        // Cycle pattern every 500 generations
        if generation % 500 == 0 {
            pattern_idx = (pattern_idx + 1) % PATTERNS.len();
            let p = &PATTERNS[pattern_idx];
            let grid = if use_a { &mut grid_a } else { &mut grid_b };
            load_pattern(grid, p.data, p.offset_x, p.offset_y);
            log::info!("Pattern: {} (gen {})", p.name, generation);
        }

        thread::sleep(Duration::from_millis(70));
    }
}
