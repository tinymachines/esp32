use std::collections::{HashMap, HashSet};
use std::fmt;

/// A cell coordinate on the infinite grid.
pub type Cell = (i64, i64);

/// Sparse representation of an infinite Game of Life grid.
/// Only live cells are stored.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Grid {
    alive: HashSet<Cell>,
    generation: u64,
}

/// The eight orthogonal + diagonal neighbor offsets.
const NEIGHBORS: [(i64, i64); 8] = [
    (-1, -1), (-1, 0), (-1, 1),
    ( 0, -1),          ( 0, 1),
    ( 1, -1), ( 1, 0), ( 1, 1),
];

impl Grid {
    /// Create an empty grid.
    pub fn new() -> Self {
        Self {
            alive: HashSet::new(),
            generation: 0,
        }
    }

    /// Create a grid from an iterator of live cell positions.
    pub fn from_cells(cells: impl IntoIterator<Item = Cell>) -> Self {
        Self {
            alive: cells.into_iter().collect(),
            generation: 0,
        }
    }

    /// Parse a grid from a multi-line string where `#` or `O` = alive.
    /// The pattern is anchored at (offset_row, offset_col).
    pub fn from_pattern(pattern: &str, offset_row: i64, offset_col: i64) -> Self {
        let cells = pattern
            .lines()
            .enumerate()
            .flat_map(|(r, line)| {
                line.chars().enumerate().filter_map(move |(c, ch)| {
                    matches!(ch, '#' | 'O').then(|| (r as i64 + offset_row, c as i64 + offset_col))
                })
            });
        Self::from_cells(cells)
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn population(&self) -> usize {
        self.alive.len()
    }

    pub fn is_alive(&self, cell: &Cell) -> bool {
        self.alive.contains(cell)
    }

    pub fn set_alive(&mut self, cell: Cell) {
        self.alive.insert(cell);
    }

    pub fn set_dead(&mut self, cell: &Cell) {
        self.alive.remove(cell);
    }

    pub fn cells(&self) -> &HashSet<Cell> {
        &self.alive
    }

    /// Compute the bounding box of all live cells: (min_row, min_col, max_row, max_col).
    /// Returns `None` if the grid is empty.
    pub fn bounds(&self) -> Option<(i64, i64, i64, i64)> {
        let mut iter = self.alive.iter();
        let &(r, c) = iter.next()?;
        let (mut r0, mut c0, mut r1, mut c1) = (r, c, r, c);
        for &(r, c) in iter {
            r0 = r0.min(r);
            c0 = c0.min(c);
            r1 = r1.max(r);
            c1 = c1.max(c);
        }
        Some((r0, c0, r1, c1))
    }

    /// Advance the grid by one generation.
    ///
    /// Algorithm: for every live cell, increment the neighbor count of all
    /// its eight neighbors. Then apply the birth/survival rules.
    /// This runs in O(alive) time — dead regions cost nothing.
    pub fn step(&mut self) {
        let mut neighbor_counts: HashMap<Cell, u8> = HashMap::with_capacity(self.alive.len() * 4);

        for &(r, c) in &self.alive {
            for &(dr, dc) in &NEIGHBORS {
                *neighbor_counts.entry((r + dr, c + dc)).or_insert(0) += 1;
            }
        }

        self.alive = neighbor_counts
            .into_iter()
            .filter(|&(cell, count)| match count {
                3 => true,                       // birth or survival
                2 => self.alive.contains(&cell), // survival only
                _ => false,                      // death or stays dead
            })
            .map(|(cell, _)| cell)
            .collect();

        self.generation += 1;
    }

    /// Advance by `n` generations.
    pub fn step_n(&mut self, n: u64) {
        for _ in 0..n {
            self.step();
        }
    }
}

impl Default for Grid {
    fn default() -> Self {
        Self::new()
    }
}

impl fmt::Display for Grid {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let Some((r0, c0, r1, c1)) = self.bounds() else {
            return write!(f, "(empty)");
        };
        for r in r0..=r1 {
            for c in c0..=c1 {
                let ch = if self.alive.contains(&(r, c)) { '█' } else { '·' };
                write!(f, "{ch}")?;
            }
            if r < r1 {
                writeln!(f)?;
            }
        }
        Ok(())
    }
}

// ─── Classic patterns ────────────────────────────────────────────

pub mod patterns {
    /// Glider: a small spaceship that moves diagonally.
    pub const GLIDER: &str = "\
.O.
..O
OOO";

    /// Gosper Glider Gun: produces a new glider every 30 generations.
    pub const GOSPER_GUN: &str = "\
........................O...........
......................O.O...........
............OO......OO............OO
...........O...O....OO............OO
OO........O.....O...OO..............
OO........O...O.OO....O.O...........
..........O.....O.......O...........
...........O...O....................
............OO......................";

    /// R-pentomino: a tiny pattern with chaotic long-lived evolution.
    pub const R_PENTOMINO: &str = "\
.OO
OO.
.O.";

    /// Lightweight Spaceship (LWSS): moves horizontally.
    pub const LWSS: &str = "\
.O..O
O....
O...O
OOOO.";

    /// Pulsar: a period-3 oscillator.
    pub const PULSAR: &str = "\
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
}

// ─── Tests ───────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn block_is_still_life() {
        let mut grid = Grid::from_cells([(0, 0), (0, 1), (1, 0), (1, 1)]);
        let before = grid.clone();
        grid.step();
        assert_eq!(grid.cells(), before.cells());
    }

    #[test]
    fn blinker_oscillates() {
        let mut grid = Grid::from_cells([(0, -1), (0, 0), (0, 1)]);
        let gen0 = grid.clone();
        grid.step();
        assert_ne!(grid.cells(), gen0.cells());
        assert_eq!(grid.population(), 3);
        grid.step();
        assert_eq!(grid.cells(), gen0.cells());
    }

    #[test]
    fn glider_moves() {
        let mut grid = Grid::from_pattern(patterns::GLIDER, 0, 0);
        assert_eq!(grid.population(), 5);
        grid.step_n(4); // one full glider cycle
        assert_eq!(grid.population(), 5);
    }

    #[test]
    fn empty_stays_empty() {
        let mut grid = Grid::new();
        grid.step_n(100);
        assert_eq!(grid.population(), 0);
    }

    #[test]
    fn r_pentomino_grows() {
        let mut grid = Grid::from_pattern(patterns::R_PENTOMINO, 0, 0);
        assert_eq!(grid.population(), 5);
        grid.step_n(10);
        assert!(grid.population() > 5);
    }
}
