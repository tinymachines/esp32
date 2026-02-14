use game_of_life::{patterns, Grid};
use std::io::{self, Write};
use std::{env, thread, time::Duration};

const DEFAULT_FPS: u64 = 15;
const VIEWPORT_ROWS: i64 = 40;
const VIEWPORT_COLS: i64 = 80;

fn render(grid: &Grid, vr0: i64, vc0: i64, vr1: i64, vc1: i64) -> String {
    let mut buf = String::with_capacity(((vr1 - vr0 + 1) * (vc1 - vc0 + 2)) as usize);

    // Move cursor home + clear screen
    buf.push_str("\x1b[H\x1b[J");
    buf.push_str(&format!(
        " Generation: {}  Population: {}\n\n",
        grid.generation(),
        grid.population()
    ));

    for r in vr0..=vr1 {
        for c in vc0..=vc1 {
            buf.push(if grid.is_alive(&(r, c)) { '█' } else { ' ' });
        }
        buf.push('\n');
    }
    buf.push_str("\n Press Ctrl+C to quit.\n");
    buf
}

fn main() {
    let args: Vec<String> = env::args().collect();

    let pattern_name = args.get(1).map(String::as_str).unwrap_or("gun");
    let fps: u64 = args
        .get(2)
        .and_then(|s| s.parse().ok())
        .unwrap_or(DEFAULT_FPS);

    let (pattern, origin_r, origin_c) = match pattern_name {
        "glider" => (patterns::GLIDER, VIEWPORT_ROWS / 4, VIEWPORT_COLS / 4),
        "gun" => (patterns::GOSPER_GUN, 2, 2),
        "rpent" => (patterns::R_PENTOMINO, VIEWPORT_ROWS / 2, VIEWPORT_COLS / 2),
        "lwss" => (patterns::LWSS, VIEWPORT_ROWS / 2, 4),
        "pulsar" => (patterns::PULSAR, VIEWPORT_ROWS / 2 - 6, VIEWPORT_COLS / 2 - 6),
        other => {
            eprintln!("Unknown pattern: {other}");
            eprintln!("Available: glider, gun, rpent, lwss, pulsar");
            std::process::exit(1);
        }
    };

    let mut grid = Grid::from_pattern(pattern, origin_r, origin_c);
    let delay = Duration::from_millis(1000 / fps);
    let stdout = io::stdout();
    let mut out = stdout.lock();

    // Hide cursor
    write!(out, "\x1b[?25l").ok();

    loop {
        let frame = render(&grid, 0, 0, VIEWPORT_ROWS - 1, VIEWPORT_COLS - 1);
        write!(out, "{frame}").ok();
        out.flush().ok();
        grid.step();
        thread::sleep(delay);
    }
}
