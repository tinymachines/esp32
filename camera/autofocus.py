"""OLED Autofocus — tinygrad CNN + Laplacian hybrid.

Finds optimal manual focus for a Logitech BRIO pointing at an SSD1306
128x64 OLED running Game of Life on ESP32-C6.

Usage:
    python autofocus.py train   # generate synthetic data + train CNN (~40s)
    python autofocus.py focus   # run coarse→fine autofocus sweep (~15s)
    python autofocus.py sweep   # full 51-level diagnostic sweep
"""

import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CAM_DEV = "/dev/video1"
CAM_W, CAM_H, CAM_FPS = 1280, 720, 15
MODEL_PATH = Path(__file__).parent / "autofocus_model.safetensors"
FOCUS_MIN, FOCUS_MAX, FOCUS_STEP = 0, 255, 5
SETTLE_MS = 300

# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def set_focus(value: int):
    """Set manual focus via v4l2-ctl."""
    subprocess.run(
        ["v4l2-ctl", "-d", CAM_DEV, "--set-ctrl", f"focus_absolute={value}"],
        capture_output=True,
    )

def set_ctrl(name: str, value: int):
    subprocess.run(
        ["v4l2-ctl", "-d", CAM_DEV, "--set-ctrl", f"{name}={value}"],
        capture_output=True,
    )

def open_camera() -> cv2.VideoCapture:
    cap = cv2.VideoCapture(CAM_DEV, cv2.CAP_V4L2)
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    cap.set(cv2.CAP_PROP_FOURCC, fourcc)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
    cap.set(cv2.CAP_PROP_FPS, CAM_FPS)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera {CAM_DEV}")
    return cap

def capture_frame(cap: cv2.VideoCapture) -> np.ndarray:
    """Capture a frame, discarding the first read to flush stale buffer."""
    cap.read()  # discard buffered frame
    ok, frame = cap.read()
    if not ok:
        raise RuntimeError("Failed to capture frame")
    return frame

# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------

def gol_step(grid: np.ndarray) -> np.ndarray:
    """One step of Conway's Game of Life."""
    neighbors = np.zeros_like(grid)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            neighbors += np.roll(np.roll(grid, dy, axis=0), dx, axis=1)
    return ((neighbors == 3) | ((grid == 1) & (neighbors == 2))).astype(np.uint8)

def random_gol_frame(rng: np.random.Generator, density: float, steps: int) -> np.ndarray:
    """Generate a random Game of Life frame (64x128)."""
    grid = (rng.random((64, 128)) < density).astype(np.uint8)
    for _ in range(steps):
        grid = gol_step(grid)
    return grid

def generate_dataset(n_samples: int = 2000, seed: int = 42):
    """Generate synthetic GoL frames with varying blur for sharpness training."""
    rng = np.random.default_rng(seed)
    X = np.empty((n_samples, 1, 32, 64), dtype=np.float32)
    Y = np.empty((n_samples, 1), dtype=np.float32)

    for i in range(n_samples):
        density = rng.uniform(0.05, 0.40)
        steps = rng.integers(0, 21)
        frame = random_gol_frame(rng, density, steps)

        # Resize 128x64 → 64x32 nearest-neighbor
        small = cv2.resize(frame.astype(np.float32), (64, 32),
                           interpolation=cv2.INTER_NEAREST)

        # Apply Gaussian blur (sigma 0–4)
        sigma = rng.uniform(0.0, 4.0)
        if sigma > 0.3:
            ksize = int(np.ceil(sigma * 3)) * 2 + 1
            small = cv2.GaussianBlur(small, (ksize, ksize), sigma)

        # Add sensor noise
        small += rng.normal(0, 0.02, small.shape).astype(np.float32)
        small = np.clip(small, 0, 1)

        X[i, 0] = small
        Y[i, 0] = 1.0 / (1.0 + sigma * sigma)

    return X, Y

# ---------------------------------------------------------------------------
# SharpnessNet (tinygrad, 3585 params)
# ---------------------------------------------------------------------------

from tinygrad import Tensor, nn
from tinygrad.nn.state import safe_save, safe_load, get_state_dict, get_parameters, load_state_dict
from tinygrad.engine.jit import TinyJit

class SharpnessNet:
    def __init__(self):
        self.c1 = nn.Conv2d(1, 8, 3, padding=1)    # 80 params
        self.c2 = nn.Conv2d(8, 16, 3, padding=1)    # 1168 params
        self.c3 = nn.Conv2d(16, 16, 3, padding=1)   # 2320 params
        self.fc = nn.Linear(16, 1)                   # 17 params

    def __call__(self, x: Tensor) -> Tensor:
        x = self.c1(x).relu().max_pool2d(kernel_size=2)   # → Bx8x16x32
        x = self.c2(x).relu().max_pool2d(kernel_size=2)   # → Bx16x8x16
        x = self.c3(x).relu()                              # → Bx16x8x16
        x = x.mean(axis=(-2, -1))                          # → Bx16
        return self.fc(x).sigmoid()                         # → Bx1

# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train(n_steps: int = 200, batch_size: int = 128, lr: float = 1e-3):
    print("Generating synthetic dataset (2000 samples)...")
    t0 = time.time()
    X_np, Y_np = generate_dataset(2000)
    print(f"  done in {time.time() - t0:.1f}s")

    X_train = Tensor(X_np)
    Y_train = Tensor(Y_np)

    model = SharpnessNet()
    opt = nn.optim.Adam(get_parameters(model), lr=lr)

    @TinyJit
    def train_step():
        Tensor.training = True
        opt.zero_grad()
        idx = Tensor.randint(batch_size, high=X_train.shape[0])
        pred = model(X_train[idx])
        loss = (pred - Y_train[idx]).square().mean()
        loss.backward()
        opt.step()
        return loss.realize()

    print(f"Training SharpnessNet ({sum(p.numel() for p in get_parameters(model))} params, {n_steps} steps)...")
    t0 = time.time()
    for i in range(n_steps):
        loss = train_step()
        if i % 20 == 0 or i == n_steps - 1:
            print(f"  step {i:3d}/{n_steps}: loss={loss.item():.4f}")
    Tensor.training = False
    elapsed = time.time() - t0
    print(f"  done in {elapsed:.1f}s")

    safe_save(get_state_dict(model), str(MODEL_PATH))
    print(f"Model saved to {MODEL_PATH}")

# ---------------------------------------------------------------------------
# Load model
# ---------------------------------------------------------------------------

def load_model() -> SharpnessNet:
    model = SharpnessNet()
    load_state_dict(model, safe_load(str(MODEL_PATH)))
    return model

# ---------------------------------------------------------------------------
# OLED detection (classical CV)
# ---------------------------------------------------------------------------

def find_oled(frame_bgr: np.ndarray) -> tuple[int, int, int, int] | None:
    """Find the OLED screen region via blue HSV threshold.

    Returns (x, y, w, h) bounding box or None.
    """
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    # Blue OLED glow: H 90–130, S 50–255, V 30–255
    mask = cv2.inRange(hsv, (90, 50, 30), (130, 255, 255))
    # Dilate to fill gaps between pixels
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    mask = cv2.dilate(mask, kernel, iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    # Largest contour
    c = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < 500:  # too small
        return None

    x, y, w, h = cv2.boundingRect(c)
    aspect = w / max(h, 1)
    # OLED is 128x64 = 2:1; allow 1.3–3.0 for perspective
    if not (1.3 < aspect < 3.0):
        return None

    return (x, y, w, h)

def center_crop(frame_bgr: np.ndarray) -> tuple[int, int, int, int]:
    """Fallback: crop center 40% of frame."""
    h, w = frame_bgr.shape[:2]
    cw, ch = int(w * 0.4), int(h * 0.4)
    cx, cy = w // 2 - cw // 2, h // 2 - ch // 2
    return (cx, cy, cw, ch)

# ---------------------------------------------------------------------------
# Sharpness scoring (hybrid Laplacian + CNN)
# ---------------------------------------------------------------------------

def extract_and_resize(frame_bgr: np.ndarray, bbox: tuple[int, int, int, int],
                       size: tuple[int, int] = (64, 32)) -> np.ndarray:
    """Extract bbox region, convert to grayscale float [0,1], resize to (w, h)."""
    x, y, w, h = bbox
    crop = frame_bgr[y:y+h, x:x+w]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA)

def score_sharpness(frame_bgr: np.ndarray, bbox: tuple[int, int, int, int],
                    model: SharpnessNet) -> float:
    """Hybrid sharpness score: 70% Laplacian variance + 30% CNN."""
    crop = extract_and_resize(frame_bgr, bbox, (64, 32))

    # Laplacian variance
    lap = cv2.Laplacian((crop * 255).astype(np.uint8), cv2.CV_64F).var()
    lap_norm = min(lap / 500.0, 1.0)

    # CNN score
    inp = Tensor(crop.reshape(1, 1, 32, 64))
    cnn = model(inp).item()

    return 0.7 * lap_norm + 0.3 * cnn

# ---------------------------------------------------------------------------
# Autofocus sweep
# ---------------------------------------------------------------------------

COARSE_POSITIONS = [0, 10, 20, 30, 45, 60, 80]

def sweep_positions(cap: cv2.VideoCapture, positions: list[int], model: SharpnessNet,
                    bbox: tuple[int, int, int, int] | None = None) -> list[tuple[int, float]]:
    """Sweep focus positions and return (position, score) pairs."""
    results = []
    for pos in positions:
        set_focus(pos)
        time.sleep(SETTLE_MS / 1000.0)
        frame = capture_frame(cap)

        if bbox is None:
            detected = find_oled(frame)
            roi = detected if detected else center_crop(frame)
        else:
            roi = bbox

        score = score_sharpness(frame, roi, model)
        results.append((pos, score))
        print(f"  focus={pos:3d}  score={score:.4f}")
    return results

def autofocus(cap: cv2.VideoCapture, model: SharpnessNet) -> int:
    """Run coarse→fine autofocus and return best focus value."""
    # Disable autofocus
    set_ctrl("focus_automatic_continuous", 0)
    time.sleep(0.1)

    # Detect OLED at a known-good focus first
    set_focus(30)
    time.sleep(SETTLE_MS / 1000.0)
    frame = capture_frame(cap)
    bbox = find_oled(frame)
    if bbox:
        x, y, w, h = bbox
        print(f"OLED detected: {w}x{h} at ({x},{y})")
    else:
        print("OLED not detected, using center crop")
        bbox = center_crop(frame)

    # Phase 1: Coarse sweep
    print("\n--- Coarse sweep ---")
    coarse = sweep_positions(cap, COARSE_POSITIONS, model, bbox)
    best_pos, best_score = max(coarse, key=lambda x: x[1])
    print(f"  coarse best: focus={best_pos} score={best_score:.4f}")

    # Phase 2: Fine sweep around best
    fine_lo = max(FOCUS_MIN, best_pos - 10)
    fine_hi = min(FOCUS_MAX, best_pos + 10)
    fine_positions = list(range(fine_lo, fine_hi + 1, FOCUS_STEP))
    # Remove duplicates from coarse
    fine_positions = [p for p in fine_positions if p not in dict(coarse)]

    if fine_positions:
        print("\n--- Fine sweep ---")
        fine = sweep_positions(cap, fine_positions, model, bbox)
        all_results = coarse + fine
    else:
        all_results = coarse

    best_pos, best_score = max(all_results, key=lambda x: x[1])

    # Phase 3: Verify
    print("\n--- Verify ---")
    set_focus(best_pos)
    time.sleep(SETTLE_MS / 1000.0)
    frame = capture_frame(cap)
    verify_score = score_sharpness(frame, bbox, model)
    print(f"  verify: focus={best_pos} score={verify_score:.4f}")

    set_focus(best_pos)
    print(f"\nAutofocus complete: focus={best_pos} (score={verify_score:.4f})")
    return best_pos

def full_sweep(cap: cv2.VideoCapture, model: SharpnessNet):
    """Diagnostic: sweep all 51 focus levels."""
    set_ctrl("focus_automatic_continuous", 0)
    time.sleep(0.1)

    positions = list(range(FOCUS_MIN, FOCUS_MAX + 1, FOCUS_STEP))
    print(f"Full sweep: {len(positions)} positions\n")

    # Detect OLED at known-good focus
    set_focus(30)
    time.sleep(SETTLE_MS / 1000.0)
    frame = capture_frame(cap)
    bbox = find_oled(frame)
    if bbox:
        x, y, w, h = bbox
        print(f"OLED detected: {w}x{h} at ({x},{y})")
    else:
        print("OLED not detected, using center crop")
        bbox = center_crop(frame)

    print(f"\n{'Focus':>5}  {'Score':>8}  {'Bar'}")
    print("-" * 50)

    results = []
    for pos in positions:
        set_focus(pos)
        time.sleep(SETTLE_MS / 1000.0)
        frame = capture_frame(cap)
        score = score_sharpness(frame, bbox, model)
        results.append((pos, score))
        bar = "#" * int(score * 40)
        print(f"  {pos:3d}    {score:.4f}  {bar}")

    best_pos, best_score = max(results, key=lambda x: x[1])
    print(f"\nBest: focus={best_pos} score={best_score:.4f}")

    # Set to best
    set_focus(best_pos)
    print(f"Focus set to {best_pos}")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "train":
        train()
    elif cmd == "focus":
        if not MODEL_PATH.exists():
            print(f"Model not found at {MODEL_PATH}")
            print("Run 'python autofocus.py train' first")
            sys.exit(1)
        model = load_model()
        cap = open_camera()
        try:
            autofocus(cap, model)
        finally:
            cap.release()
    elif cmd == "sweep":
        if not MODEL_PATH.exists():
            print(f"Model not found at {MODEL_PATH}")
            print("Run 'python autofocus.py train' first")
            sys.exit(1)
        model = load_model()
        cap = open_camera()
        try:
            full_sweep(cap, model)
        finally:
            cap.release()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

if __name__ == "__main__":
    main()
