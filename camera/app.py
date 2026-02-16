"""
ESP Camera Calibration — live MJPEG stream + v4l2 controls.

Run: python app.py
Visit: http://localhost:3000
"""

import base64
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from fasthtml.common import *
from starlette.responses import StreamingResponse, FileResponse, Response

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 3000
CAM_DEV = "/dev/video1"
CAM_W, CAM_H, CAM_FPS = 1280, 720, 15
STREAM_FPS = 3        # MJPEG stream rate (lower for remote clients)
STREAM_QUALITY = 50   # JPEG quality for stream (lower = smaller frames)
BASE_DIR = Path(__file__).resolve().parent
SNAP_DIR = BASE_DIR / "snapshots"
SNAP_LATEST = BASE_DIR / "snapshot.jpg"
LOG_DIR = BASE_DIR / "logs"

SNAP_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Camera controls — (label, v4l2 name, min, max, step, default)
# ---------------------------------------------------------------------------

POSITION_CTRLS = [
    ("Zoom",        "zoom_absolute",                100, 500,   1,    100),
    ("Pan",         "pan_absolute",              -36000, 36000, 3600, 0),
    ("Tilt",        "tilt_absolute",             -36000, 36000, 3600, 0),
]

FOCUS_CTRLS = [
    ("Autofocus",   "focus_automatic_continuous",   0,   1,     1,    1),
    ("Focus",       "focus_absolute",               0,   255,   1,    30),
]

IMAGE_CTRLS = [
    ("Brightness",  "brightness",                   0,   255,   1,    128),
    ("Contrast",    "contrast",                     0,   255,   1,    128),
    ("Sharpness",   "sharpness",                    0,   255,   1,    128),
    ("Saturation",  "saturation",                   0,   255,   1,    128),
    ("Gain",        "gain",                         0,   255,   1,    0),
    ("Backlight Comp", "backlight_compensation",    0,   1,     1,    1),
]

EXPOSURE_CTRLS = [
    ("Auto Exposure",  "auto_exposure",             0,   3,     1,    3),
    ("Exposure Time",  "exposure_time_absolute",    3,   2047,  1,    250),
    ("Auto WB",        "white_balance_automatic",   0,   1,     1,    1),
    ("WB Temperature", "white_balance_temperature", 2000, 7500, 10,   4000),
]

ALL_CTRLS = POSITION_CTRLS + FOCUS_CTRLS + IMAGE_CTRLS + EXPOSURE_CTRLS

# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESETS = {
    "oled": {
        "label": "OLED Closeup",
        "ctrls": {
            "zoom_absolute": 150,
            "focus_automatic_continuous": 0,
            "focus_absolute": 30,
            "sharpness": 180,
        },
    },
    "wide": {
        "label": "Wide View",
        "ctrls": {
            "zoom_absolute": 100,
            "focus_automatic_continuous": 1,
        },
    },
    "reset": {
        "label": "Reset Defaults",
        "ctrls": {c[1]: c[5] for c in ALL_CTRLS},
    },
}

# ---------------------------------------------------------------------------
# CameraManager
# ---------------------------------------------------------------------------

class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.cap = None

    def open(self):
        self.cap = cv2.VideoCapture(CAM_DEV, cv2.CAP_V4L2)
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        self.cap.set(cv2.CAP_PROP_FOURCC, fourcc)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_W)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
        self.cap.set(cv2.CAP_PROP_FPS, CAM_FPS)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera {CAM_DEV}")
        print(f"Camera opened: {CAM_DEV} {CAM_W}x{CAM_H}@{CAM_FPS}fps")

    def close(self):
        if self.cap:
            self.cap.release()
            self.cap = None

    def read_jpeg(self, quality: int = 85, scale: float = 1.0) -> bytes | None:
        with self.lock:
            if not self.cap:
                return None
            ok, frame = self.cap.read()
            if not ok:
                return None
            if scale < 1.0:
                w = int(frame.shape[1] * scale)
                h = int(frame.shape[0] * scale)
                frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            return buf.tobytes()

    def set_ctrl(self, name: str, value: int):
        subprocess.run(
            ["v4l2-ctl", "-d", CAM_DEV, "--set-ctrl", f"{name}={value}"],
            capture_output=True,
        )

    def snapshot(self) -> Path | None:
        data = self.read_jpeg()
        if data is None:
            return None
        ts = int(time.time())
        path = SNAP_DIR / f"{ts}.jpg"
        path.write_bytes(data)
        shutil.copy2(path, SNAP_LATEST)
        return path


cam = CameraManager()

# ---------------------------------------------------------------------------
# Autofocus live state (shared between background thread and status endpoint)
# ---------------------------------------------------------------------------

_af_log: list[tuple[str, str]] = []   # (text, css_class)
_af_running = False
_af_final_focus: int | None = None

# ---------------------------------------------------------------------------
# FastHTML app
# ---------------------------------------------------------------------------

app, rt = fast_app(
    pico=False,
    hdrs=(
        Script(src="https://unpkg.com/htmx.org@2.0.4"),
    ),
)

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #111; color: #ccc; font-family: 'JetBrains Mono', monospace;
    padding: 20px;
}
h1 { color: #0f0; font-size: 1.3rem; margin-bottom: 16px; }
h2 { color: #0a0; font-size: 1rem; margin: 16px 0 8px; border-bottom: 1px solid #333; padding-bottom: 4px; }
.layout { display: grid; grid-template-columns: 1fr 360px; gap: 24px; max-width: 1400px; margin: 0 auto; }
.stream-panel img { width: 100%; border: 2px solid #333; border-radius: 4px; }
.controls { max-height: 90vh; overflow-y: auto; padding-right: 8px; }
.controls::-webkit-scrollbar { width: 6px; }
.controls::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
.ctrl-row {
    display: grid; grid-template-columns: 110px 1fr 50px; gap: 8px;
    align-items: center; margin-bottom: 6px;
}
.ctrl-row label { color: #aaa; font-size: 0.8rem; }
.ctrl-row input[type=range] { width: 100%; accent-color: #0f0; }
.ctrl-row .val { color: #0f0; font-size: 0.8rem; text-align: right; font-variant-numeric: tabular-nums; }
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }
.btn {
    background: #222; color: #0f0; border: 1px solid #333; padding: 6px 14px;
    font-family: monospace; font-size: 0.85rem; cursor: pointer; border-radius: 4px;
}
.btn:hover { background: #333; }
.btn-warn { color: #f80; border-color: #f80; }
.status { color: #888; font-size: 0.8rem; margin-top: 8px; min-height: 1.2em; }
nav { margin-bottom: 16px; }
nav a { color: #0f0; margin-right: 16px; text-decoration: none; font-size: 0.9rem; }
nav a:hover { text-decoration: underline; }
nav a.active { border-bottom: 2px solid #0f0; }
@media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; }
}
.af-page { max-width: 900px; margin: 0 auto; }
.af-page h2 { color: #0a0; font-size: 1.1rem; margin: 28px 0 10px; border-bottom: 1px solid #333; padding-bottom: 4px; }
.af-page h3 { color: #0a0; font-size: 0.95rem; margin: 20px 0 8px; }
.af-page p, .af-page li { color: #bbb; font-size: 0.85rem; line-height: 1.6; margin-bottom: 8px; }
.af-page ul { padding-left: 20px; margin-bottom: 12px; }
.af-page table { width: 100%; border-collapse: collapse; margin: 12px 0 16px; font-size: 0.82rem; }
.af-page th { text-align: left; color: #0a0; border-bottom: 1px solid #333; padding: 6px 12px; }
.af-page td { color: #ccc; border-bottom: 1px solid #222; padding: 5px 12px; }
.af-page tr:hover td { background: #1a1a1a; }
.af-page .mono { font-family: 'JetBrains Mono', monospace; color: #0f0; }
.af-page code { background: #1a1a1a; color: #0f0; padding: 1px 5px; border-radius: 3px; font-size: 0.82rem; }
.af-page pre {
    background: #0a0a0a; border: 1px solid #333; border-radius: 4px;
    padding: 12px 16px; overflow-x: auto; margin: 10px 0 16px;
    font-size: 0.8rem; line-height: 1.5; color: #ccc;
}
.af-page pre code { background: none; padding: 0; }
.af-page .arch-box {
    background: #0a0a0a; border: 1px solid #333; border-radius: 4px;
    padding: 16px; margin: 12px 0; font-size: 0.8rem; line-height: 1.6;
    color: #888; white-space: pre; overflow-x: auto; font-family: 'JetBrains Mono', monospace;
}
.af-page .arch-box span { color: #0f0; }
.af-page .formula { color: #0f0; font-size: 0.9rem; text-align: center; padding: 12px; margin: 12px 0; background: #0a0a0a; border-radius: 4px; }
.af-page .tag { display: inline-block; background: #1a2a1a; color: #0a0; border: 1px solid #333; border-radius: 3px; padding: 2px 8px; font-size: 0.75rem; margin: 2px 4px 2px 0; }
.img-grid { display: grid; gap: 10px; margin: 16px 0; }
.img-grid.cols-5 { grid-template-columns: repeat(5, 1fr); }
.img-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }
.img-cell { text-align: center; }
.img-cell img { width: 100%; border: 1px solid #333; border-radius: 2px; image-rendering: pixelated; }
.img-cell .cap { color: #888; font-size: 0.7rem; margin-top: 4px; line-height: 1.4; }
.img-cell .cap .hi { color: #0f0; }
.chart-wrap { text-align: center; margin: 16px 0; }
.chart-wrap svg { font-family: 'JetBrains Mono', monospace; }
.mermaid-wrap { margin: 16px 0; background: #0a0a0a; border: 1px solid #333; border-radius: 4px; padding: 16px; overflow-x: auto; }
.eval-grid { display: grid; grid-template-columns: auto 1fr; gap: 0 16px; align-items: center; margin: 16px 0; }
.eval-grid img { height: 48px; border: 1px solid #333; border-radius: 2px; image-rendering: pixelated; }
.eval-grid .eval-row { display: contents; }
@media (max-width: 700px) {
    .img-grid.cols-5 { grid-template-columns: repeat(3, 1fr); }
    .img-grid.cols-4 { grid-template-columns: repeat(2, 1fr); }
}
.af-panel {
    background: #0a0a0a; border: 1px solid #333; border-radius: 4px;
    padding: 12px 16px; margin-top: 20px; max-width: 1400px; margin-left: auto; margin-right: auto;
    max-height: 420px; overflow-y: auto;
    font-family: 'JetBrains Mono', monospace; font-size: 0.78rem; line-height: 1.5;
}
.af-panel:empty { display: none; }
.af-info { color: #888; }
.af-dim { color: #555; }
.af-good { color: #0f0; }
.af-warn { color: #f80; }
.af-error { color: #f00; }
.af-header { color: #0a0; font-weight: bold; }
.af-best { color: #0f0; font-weight: bold; font-size: 0.88rem; padding: 4px 0; }
.btn-hero { color: #0ff; border-color: #0ff; }
.btn-hero:hover { background: #002a2a; }
"""

# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def nav_bar(active="camera"):
    return Nav(
        A("Camera", href="/", cls="active" if active == "camera" else ""),
        A("Game of Life", href="/life", cls="active" if active == "life" else ""),
        A("Autofocus", href="/autofocus", cls="active" if active == "autofocus" else ""),
        A("Pipeline", href="/pipeline", cls="active" if active == "pipeline" else ""),
    )


def slider(label, name, lo, hi, step, val):
    return Div(
        Label(label, fr=name),
        Input(
            type="range", name="value", id=name,
            min=str(lo), max=str(hi), step=str(step), value=str(val),
            hx_post=f"/ctrl/{name}", hx_trigger="change", hx_swap="none",
            oninput=f"document.getElementById('{name}_v').textContent=this.value",
        ),
        Span(str(val), id=f"{name}_v", cls="val"),
        cls="ctrl-row",
    )


def ctrl_group(title, ctrls):
    return Div(
        H2(title),
        *[slider(c[0], c[1], c[2], c[3], c[4], c[5]) for c in ctrls],
    )


def preset_buttons():
    return Div(
        H2("Presets"),
        Div(
            *[
                Button(
                    p["label"],
                    hx_post=f"/preset/{key}",
                    hx_swap="innerHTML",
                    hx_target="#status",
                    cls="btn" + (" btn-warn" if key == "reset" else ""),
                )
                for key, p in PRESETS.items()
            ],
            Button(
                "Save Snapshot",
                hx_post="/snapshot",
                hx_swap="innerHTML",
                hx_target="#status",
                cls="btn",
            ),
            Button(
                "Randomize",
                hx_post="/randomize",
                hx_swap="innerHTML",
                hx_target="#status",
                cls="btn btn-warn",
            ),
            Button(
                "Randomize + Autofocus",
                hx_post="/randomize-autofocus",
                hx_target="#af-panel",
                hx_swap="outerHTML",
                cls="btn btn-hero",
            ),
            cls="btn-row",
        ),
        Div(id="status", cls="status"),
    )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@rt("/")
def index():
    return (
        Title("ESP Camera"),
        Style(CSS),
        H1("ESP Camera Calibration"),
        nav_bar("camera"),
        Div(
            Div(
                Img(src="/stream", alt="Live camera stream"),
                cls="stream-panel",
            ),
            Div(
                preset_buttons(),
                ctrl_group("Position", POSITION_CTRLS),
                ctrl_group("Focus", FOCUS_CTRLS),
                ctrl_group("Image", IMAGE_CTRLS),
                ctrl_group("Exposure", EXPOSURE_CTRLS),
                cls="controls",
            ),
            cls="layout",
        ),
        _af_panel_current(),
    )


@rt("/stream")
async def stream():
    def generate():
        while True:
            frame = cam.read_jpeg(quality=STREAM_QUALITY, scale=0.5)
            if frame is None:
                time.sleep(0.1)
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
            time.sleep(1.0 / STREAM_FPS)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@rt("/ctrl/{name}")
async def ctrl(name: str, value: int):
    cam.set_ctrl(name, value)
    return Response(status_code=204)


@rt("/preset/{key}")
async def preset(key: str):
    p = PRESETS.get(key)
    if not p:
        return "Unknown preset"
    for name, value in p["ctrls"].items():
        cam.set_ctrl(name, value)
    # Return JS to update slider positions
    js_parts = []
    for name, value in p["ctrls"].items():
        js_parts.append(
            f"var el=document.getElementById('{name}');"
            f"if(el){{el.value={value};"
            f"var v=document.getElementById('{name}_v');if(v)v.textContent='{value}';}}"
        )
    js = "".join(js_parts)
    return f"Applied: {p['label']}<script>{js}</script>"


# ---------------------------------------------------------------------------
# Randomize + Autofocus helpers
# ---------------------------------------------------------------------------

def _randomize_controls() -> dict[str, int]:
    """Randomize all camera controls. Returns {name: value}."""
    values = {}
    for ctrl_list in [POSITION_CTRLS, FOCUS_CTRLS, IMAGE_CTRLS, EXPOSURE_CTRLS]:
        for label, name, lo, hi, step, default in ctrl_list:
            n_steps = (hi - lo) // step
            values[name] = lo + random.randint(0, n_steps) * step
    values["focus_automatic_continuous"] = 0
    for name, value in values.items():
        cam.set_ctrl(name, value)
    return values

def _slider_js(values: dict[str, int]) -> str:
    """Build JS to sync slider UI to given control values."""
    parts = []
    for name, value in values.items():
        parts.append(
            f"var el=document.getElementById('{name}');"
            f"if(el){{el.value={value};"
            f"var v=document.getElementById('{name}_v');if(v)v.textContent='{value}';}}"
        )
    return "".join(parts)

def _capture_frame() -> np.ndarray:
    """Capture a fresh frame from the shared camera (discards 2 buffered frames)."""
    with cam.lock:
        cam.cap.read()
        cam.cap.read()
        ok, frame = cam.cap.read()
    if not ok:
        raise RuntimeError("Capture failed")
    return frame

def _score_position(bbox, n=3) -> float:
    """Score current focus by taking median Laplacian over n frames."""
    scores = []
    for _ in range(n):
        frame = _capture_frame()
        scores.append(_laplacian_score(frame, bbox))
        time.sleep(0.05)
    scores.sort()
    return scores[len(scores) // 2]

def _find_oled_rect(frame: np.ndarray):
    """Detect OLED via blue HSV threshold. Returns (x,y,w,h) or None."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (90, 50, 30), (130, 255, 255))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    mask = cv2.dilate(mask, kernel, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < 500:
        return None
    x, y, w, h = cv2.boundingRect(c)
    # Reject if bbox covers >50% of frame (likely false positive)
    frame_area = frame.shape[0] * frame.shape[1]
    if (w * h) > frame_area * 0.5:
        return None
    if not (1.3 < w / max(h, 1) < 3.0):
        return None
    return (x, y, w, h)

def _center_crop_rect(frame: np.ndarray):
    h, w = frame.shape[:2]
    cw, ch = int(w * 0.4), int(h * 0.4)
    return (w // 2 - cw // 2, h // 2 - ch // 2, cw, ch)

def _laplacian_score(frame: np.ndarray, bbox) -> float:
    """Laplacian variance sharpness score, normalized to [0, 1]."""
    x, y, w, h = bbox
    gray = cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)
    return min(cv2.Laplacian(gray, cv2.CV_64F).var() / 2000.0, 1.0)

def _af_panel_current():
    """Return the appropriate af-panel for current state."""
    if _af_running:
        lines = list(_af_log)
        return Div(
            *[Div(t, cls=c) for t, c in lines],
            Script("var p=document.getElementById('af-panel');if(p)p.scrollTop=p.scrollHeight;"),
            hx_get="/autofocus-status",
            hx_trigger="load delay:300ms",
            hx_swap="outerHTML",
            id="af-panel", cls="af-panel",
        )
    if _af_log:
        return Div(
            *[Div(t, cls=c) for t, c in _af_log],
            id="af-panel", cls="af-panel",
        )
    return Div(id="af-panel", cls="af-panel")

def _run_autofocus():
    """Background thread: coarse-to-fine autofocus with Laplacian scoring."""
    global _af_running, _af_log, _af_final_focus
    _af_running = True
    _af_log = []
    _af_final_focus = None

    def log(msg, cls="af-info"):
        _af_log.append((msg, cls))

    try:
        time.sleep(0.5)
        # Restore everything except focus — leave focus as the variable
        log("Restoring camera preset (keeping random focus)...")
        restore = {
            "zoom_absolute": 150, "pan_absolute": 0, "tilt_absolute": 0,
            "focus_automatic_continuous": 0, "sharpness": 180,
            "brightness": 128, "contrast": 128, "saturation": 128,
            "gain": 0, "backlight_compensation": 1,
            "auto_exposure": 3, "white_balance_automatic": 1,
        }
        for name, val in restore.items():
            cam.set_ctrl(name, val)
        time.sleep(1.2)  # settle for zoom motor + image pipeline

        log("Detecting OLED region at focus=30...")
        cam.set_ctrl("focus_absolute", 30)
        time.sleep(0.4)
        frame = _capture_frame()
        bbox = _find_oled_rect(frame)
        if bbox:
            x, y, w, h = bbox
            log(f"  OLED found: {w}x{h} at ({x},{y})", "af-good")
        else:
            log("  OLED not detected, using center crop", "af-warn")
            bbox = _center_crop_rect(frame)

        # Coarse sweep — every 10 steps, 3-frame median at each
        log("")
        log("--- Coarse sweep (3-frame median) ---", "af-header")
        coarse = list(range(0, 90, 10))  # [0, 10, 20, 30, 40, 50, 60, 70, 80]
        results = []
        for pos in coarse:
            cam.set_ctrl("focus_absolute", pos)
            time.sleep(0.4)
            score = _score_position(bbox, n=3)
            results.append((pos, score))
            bar = chr(9608) * int(score * 30)
            log(f"  focus={pos:3d}  score={score:.4f}  {bar}")

        best_pos, best_score = max(results, key=lambda r: r[1])
        log(f"  * best: focus={best_pos} (score={best_score:.4f})", "af-good")

        # Fine sweep — ±15 around best, step 5, 3-frame median
        log("")
        log("--- Fine sweep (3-frame median) ---", "af-header")
        fine_lo = max(0, best_pos - 15)
        fine_hi = min(255, best_pos + 15)
        tested = {r[0] for r in results}
        fine_positions = [p for p in range(fine_lo, fine_hi + 1, 5) if p not in tested]
        for pos in fine_positions:
            cam.set_ctrl("focus_absolute", pos)
            time.sleep(0.4)
            score = _score_position(bbox, n=3)
            results.append((pos, score))
            bar = chr(9608) * int(score * 30)
            log(f"  focus={pos:3d}  score={score:.4f}  {bar}")

        best_pos, best_score = max(results, key=lambda r: r[1])
        log(f"  * best: focus={best_pos} (score={best_score:.4f})", "af-good")

        # Micro sweep — ±5 around best, step 2, 5-frame median
        log("")
        log("--- Micro sweep (5-frame median) ---", "af-header")
        micro_lo = max(0, best_pos - 5)
        micro_hi = min(255, best_pos + 5)
        tested = {r[0] for r in results}
        micro_positions = [p for p in range(micro_lo, micro_hi + 1, 2) if p not in tested]
        for pos in micro_positions:
            cam.set_ctrl("focus_absolute", pos)
            time.sleep(0.4)
            score = _score_position(bbox, n=5)
            results.append((pos, score))
            bar = chr(9608) * int(score * 30)
            log(f"  focus={pos:3d}  score={score:.4f}  {bar}")

        best_pos, best_score = max(results, key=lambda r: r[1])

        # Verify — 5-frame median
        log("")
        log("--- Verify (5-frame median) ---", "af-header")
        cam.set_ctrl("focus_absolute", best_pos)
        time.sleep(0.5)
        final_score = _score_position(bbox, n=5)
        log(f"  focus={best_pos}  score={final_score:.4f}", "af-good")

        log("")
        log(f"Done! Best focus = {best_pos}  (score = {final_score:.4f})", "af-best")
        _af_final_focus = best_pos

    except Exception as e:
        log(f"Error: {e}", "af-error")
    finally:
        _af_running = False

# ---------------------------------------------------------------------------
# Routes — camera controls
# ---------------------------------------------------------------------------

@rt("/randomize")
async def randomize():
    values = _randomize_controls()
    return f"Randomized all controls<script>{_slider_js(values)}</script>"


@rt("/randomize-autofocus")
async def randomize_autofocus():
    if _af_running:
        return Div(Div("Autofocus already running...", cls="af-warn"), id="af-panel", cls="af-panel")
    # Randomize controls now so we can return slider JS immediately
    values = _randomize_controls()
    # Start background autofocus (will restore zoom and sweep focus)
    threading.Thread(target=_run_autofocus, daemon=True).start()
    return Div(
        Div("Randomized! Starting autofocus...", cls="af-info"),
        Script(_slider_js(values)),
        hx_get="/autofocus-status",
        hx_trigger="load delay:500ms",
        hx_swap="outerHTML",
        id="af-panel", cls="af-panel",
    )


@rt("/autofocus-status")
async def autofocus_status():
    lines = list(_af_log)
    elements = [Div(t, cls=c) for t, c in lines]

    if _af_running:
        return Div(
            *elements,
            Script("var p=document.getElementById('af-panel');if(p)p.scrollTop=p.scrollHeight;"),
            hx_get="/autofocus-status",
            hx_trigger="load delay:300ms",
            hx_swap="outerHTML",
            id="af-panel", cls="af-panel",
        )

    # Done — sync sliders for all restored settings + final focus
    final_values = {
        "zoom_absolute": 150, "pan_absolute": 0, "tilt_absolute": 0,
        "focus_automatic_continuous": 0, "sharpness": 180,
        "brightness": 128, "contrast": 128, "saturation": 128,
        "gain": 0, "backlight_compensation": 1,
        "auto_exposure": 3, "white_balance_automatic": 1,
    }
    if _af_final_focus is not None:
        final_values["focus_absolute"] = _af_final_focus
    elements.append(Script(_slider_js(final_values)))
    return Div(*elements, id="af-panel", cls="af-panel")


@rt("/snapshot")
async def snapshot():
    path = cam.snapshot()
    if path:
        return f"Saved: {path.name}"
    return "Failed to capture"


@rt("/snapshot.jpg")
async def snapshot_jpg():
    if SNAP_LATEST.exists():
        return FileResponse(
            SNAP_LATEST,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-cache"},
        )
    return Response("No snapshot yet", status_code=404)


@rt("/life")
def life():
    return (
        Title("ESP32-C6 Game of Life"),
        Style(CSS),
        H1("ESP32-C6 // Conway's Game of Life"),
        nav_bar("life"),
        Div(
            Img(id="snapshot", src="/snapshot.jpg", style="max-width:100%;max-height:70vh;border:2px solid #333;border-radius:4px;"),
            Div("Auto-refreshes every 5 seconds", style="color:#888;font-size:0.85rem;margin-top:12px;"),
            Button("Refresh Now", cls="btn", onclick="refresh()"),
            style="text-align:center;padding:20px;",
        ),
        Script("""
            function refresh() {
                document.getElementById('snapshot').src = '/snapshot.jpg?' + Date.now();
            }
            setInterval(refresh, 5000);
        """),
    )

# ---------------------------------------------------------------------------
# Pipeline page helpers
# ---------------------------------------------------------------------------

def _gol_step(grid):
    neighbors = np.zeros_like(grid)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            neighbors += np.roll(np.roll(grid, dy, axis=0), dx, axis=1)
    return ((neighbors == 3) | ((grid == 1) & (neighbors == 2))).astype(np.uint8)

def _make_sample(density, steps, sigma, seed=42):
    """Generate one synthetic GoL sample. Returns (image_float32, label)."""
    rng = np.random.default_rng(seed)
    grid = (rng.random((64, 128)) < density).astype(np.uint8)
    for _ in range(steps):
        grid = _gol_step(grid)
    small = cv2.resize(grid.astype(np.float32), (64, 32), interpolation=cv2.INTER_NEAREST)
    if sigma > 0.3:
        ksize = int(np.ceil(sigma * 3)) * 2 + 1
        small = cv2.GaussianBlur(small, (ksize, ksize), sigma)
    small = np.clip(small, 0, 1)
    label = 1.0 / (1.0 + sigma * sigma)
    return small, label

def _to_data_uri(img, scale=4):
    """Convert float32 [0,1] grayscale to blue-on-black PNG data URI."""
    h, w = img.shape
    big = cv2.resize(img, (w * scale, h * scale), interpolation=cv2.INTER_NEAREST)
    bgr = np.zeros((*big.shape, 3), dtype=np.uint8)
    vals = (big * 255).astype(np.uint8)
    bgr[:, :, 0] = vals                           # blue
    bgr[:, :, 1] = (vals * 0.35).astype(np.uint8) # subtle green
    _, buf = cv2.imencode(".png", bgr)
    return f"data:image/png;base64,{base64.b64encode(buf).decode()}"

def _loss_curve_svg():
    """Inline SVG of training loss curve."""
    steps =  [0, 20, 40, 60, 80, 100, 120, 140, 160, 180, 199]
    losses = [0.1379, 0.0859, 0.0565, 0.0204, 0.0108, 0.0112, 0.0096, 0.0121, 0.0115, 0.0093, 0.0149]
    px, py, pw, ph = 50, 15, 370, 175
    max_loss, max_step = 0.15, 200
    def tx(s): return px + (s / max_step) * pw
    def ty(l): return py + ph - (l / max_loss) * ph

    points = [(tx(s), ty(l)) for s, l in zip(steps, losses)]
    path_d = f"M {points[0][0]:.1f},{points[0][1]:.1f}"
    for x, y in points[1:]:
        path_d += f" L {x:.1f},{y:.1f}"

    # Area under curve
    area_d = path_d + f" L {points[-1][0]:.1f},{py+ph} L {points[0][0]:.1f},{py+ph} Z"

    grid = ""
    for v in [0.05, 0.10]:
        grid += f'<line x1="{px}" y1="{ty(v):.0f}" x2="{px+pw}" y2="{ty(v):.0f}" stroke="#222" stroke-dasharray="4"/>\n'
    for s in [50, 100, 150]:
        grid += f'<line x1="{tx(s):.0f}" y1="{py}" x2="{tx(s):.0f}" y2="{py+ph}" stroke="#1a1a1a" stroke-dasharray="4"/>\n'

    ylabels = ""
    for v in [0, 0.05, 0.10, 0.15]:
        ylabels += f'<text x="{px-6}" y="{ty(v):.0f}" fill="#666" font-size="10" text-anchor="end" dominant-baseline="middle">{v:.2f}</text>\n'
    xlabels = ""
    for s in [0, 50, 100, 150, 200]:
        xlabels += f'<text x="{tx(s):.0f}" y="{py+ph+14}" fill="#666" font-size="10" text-anchor="middle">{s}</text>\n'

    dots = ""
    for x, y in points:
        dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#0f0" stroke="#0a0a0a" stroke-width="1"/>\n'

    return (
        f'<svg viewBox="0 0 450 220" xmlns="http://www.w3.org/2000/svg">\n'
        f'<rect width="450" height="220" fill="#0a0a0a" rx="4"/>\n'
        f'{grid}'
        f'<line x1="{px}" y1="{py}" x2="{px}" y2="{py+ph}" stroke="#444"/>\n'
        f'<line x1="{px}" y1="{py+ph}" x2="{px+pw}" y2="{py+ph}" stroke="#444"/>\n'
        f'{ylabels}{xlabels}'
        f'<text x="{px + pw//2}" y="210" fill="#666" font-size="10" text-anchor="middle">training step</text>\n'
        f'<text x="10" y="{py + ph//2}" fill="#666" font-size="10" text-anchor="middle" '
        f'transform="rotate(-90,10,{py + ph//2})">MSE loss</text>\n'
        f'<path d="{area_d}" fill="#0f03" />\n'
        f'<path d="{path_d}" fill="none" stroke="#0f0" stroke-width="2" stroke-linejoin="round"/>\n'
        f'{dots}'
        f'</svg>'
    )

# ---------------------------------------------------------------------------
# Pipeline route
# ---------------------------------------------------------------------------

MERMAID_PIPELINE = """\
flowchart LR
    subgraph train["Training (offline, ~35s)"]
        A["Random GoL\\n64×128"] --> B["Resize\\n32×64"]
        B --> C["Gaussian Blur\\nσ ~ U(0, 4)"]
        C --> D["Add Noise\\nσ = 0.02"]
        D --> E["Label\\n1/(1+σ²)"]
        E --> F["2000 samples"]
        F --> G["SharpnessNet\\nAdam, 200 steps"]
        G --> H["safetensors\\n15 KB"]
    end
    subgraph focus["Autofocus (live, ~13s)"]
        I["Camera\\n1280×720"] --> J["Find OLED\\nHSV threshold"]
        J --> K["Crop & Resize\\n32×64"]
        K --> L["Laplacian\\nvariance"]
        K --> M["CNN\\ninference"]
        H --> M
        L --> N["0.7·Lap + 0.3·CNN"]
        M --> N
        N --> O["Coarse→Fine\\nsweep"]
        O --> P["Set Focus\\nv4l2-ctl"]
    end
"""

MERMAID_MODEL = """\
flowchart TD
    A["Input\\n1 × 32 × 64"] --> B["Conv2d(1→8, 3×3, pad=1)\\n+ ReLU"]
    B --> C["MaxPool2d(2)\\n8 × 16 × 32"]
    C --> D["Conv2d(8→16, 3×3, pad=1)\\n+ ReLU"]
    D --> E["MaxPool2d(2)\\n16 × 8 × 16"]
    E --> F["Conv2d(16→16, 3×3, pad=1)\\n+ ReLU"]
    F --> G["GlobalAvgPool\\n→ 16"]
    G --> H["Linear(16→1) + Sigmoid"]
    H --> I["Sharpness ∈ \\[0, 1\\]"]
    style A fill:#1a2a1a,stroke:#0a0,color:#ccc
    style I fill:#1a2a1a,stroke:#0a0,color:#0f0
"""

@rt("/pipeline")
def pipeline_page():
    # --- Generate sample images ---
    blur_sigmas = [0.0, 0.5, 1.5, 2.5, 4.0]
    blur_samples = []
    for sigma in blur_sigmas:
        img, label = _make_sample(0.25, 8, sigma, seed=77)
        uri = _to_data_uri(img)
        blur_samples.append((sigma, label, uri))

    density_configs = [
        (0.10, 3, 10),
        (0.20, 8, 20),
        (0.30, 12, 30),
        (0.40, 5, 40),
    ]
    density_samples = []
    for dens, steps, seed in density_configs:
        img, label = _make_sample(dens, steps, 0.0, seed=seed)
        uri = _to_data_uri(img)
        density_samples.append((dens, steps, uri))

    # --- Test evaluation data (from validation run) ---
    eval_data = [
        (0.25, 8, 0.2, 55, 0.962, 0.961),
        (0.30, 5, 1.0, 66, 0.500, 0.523),
        (0.15, 12, 2.5, 77, 0.138, 0.148),
        (0.35, 3, 0.0, 88, 1.000, 0.972),
        (0.20, 15, 3.5, 99, 0.075, 0.108),
        (0.40, 1, 1.8, 11, 0.236, 0.219),
    ]
    eval_rows = []
    for dens, steps, sigma, seed, true_v, pred_v in eval_data:
        img, _ = _make_sample(dens, steps, sigma, seed=seed)
        uri = _to_data_uri(img, scale=3)
        err = abs(true_v - pred_v)
        eval_rows.append((uri, sigma, true_v, pred_v, err))

    loss_svg = _loss_curve_svg()

    mermaid_js = (
        "import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';\n"
        "mermaid.initialize({ startOnLoad: true, theme: 'base', themeVariables: {"
        " background:'#0a0a0a', primaryColor:'#1a2a1a', primaryBorderColor:'#0a0',"
        " primaryTextColor:'#ccc', secondaryColor:'#0a1a2a', secondaryBorderColor:'#333',"
        " secondaryTextColor:'#ccc', tertiaryColor:'#1a1a1a', tertiaryBorderColor:'#333',"
        " lineColor:'#0a0', textColor:'#ccc', fontSize:'13px',"
        " fontFamily:'\\'JetBrains Mono\\', monospace'"
        "} });"
    )

    return (
        Title("ML Pipeline — Data → Training → Autofocus"),
        Style(CSS),
        Script(mermaid_js, type="module"),
        H1("ML Pipeline // Data → Training → Autofocus"),
        nav_bar("pipeline"),
        Div(
            # ---- End-to-End Flow ----
            H2("End-to-End Flow"),
            P("Two phases: offline training on synthetic data (~35s), then live autofocus via "
              "coarse-to-fine sweep (~13s). No real camera images needed for training."),
            Div(NotStr(f'<pre class="mermaid">\n{MERMAID_PIPELINE}\n</pre>'), cls="mermaid-wrap"),

            # ---- Synthetic Data ----
            H2("Synthetic Training Data"),
            P("Each sample is a Game of Life frame rendered at 64×128, resized to 32×64, "
              "then degraded with Gaussian blur and sensor noise. The sharpness label is "
              "derived directly from the blur sigma — no human annotation needed."),

            H3("Blur Progression"),
            P("Same GoL pattern (density 25%, 8 evolution steps) at increasing blur levels. "
              "As sigma increases, high-frequency pixel edges wash out:"),
            Div(
                *[Div(
                    Img(src=uri),
                    Div(Span(f"σ = {sigma:.1f}", cls="hi"), Br(),
                        f"score = {label:.3f}", cls="cap"),
                    cls="img-cell",
                ) for sigma, label, uri in blur_samples],
                cls="img-grid cols-5",
            ),

            H3("Pattern Variety"),
            P("Different initial densities and evolution steps produce diverse pixel patterns — "
              "from sparse gliders to dense still-lifes:"),
            Div(
                *[Div(
                    Img(src=uri),
                    Div(f"{dens:.0%} density, {steps} steps", cls="cap"),
                    cls="img-cell",
                ) for dens, steps, uri in density_samples],
                cls="img-grid cols-4",
            ),

            P("The full training set has 2,000 samples: density uniform in 5–40%, "
              "evolution steps 0–20, blur sigma uniform in 0–4. "
              "Generated in ~5s on a Raspberry Pi 5."),

            # ---- Label Formula ----
            H3("Sharpness Label"),
            P("The label maps blur sigma to a sharpness score in [0, 1]:"),
            Div("label = 1 / (1 + σ²)", cls="formula"),
            Table(
                Tr(Th("σ (blur)"), Th("0.0"), Th("0.5"), Th("1.0"), Th("2.0"), Th("4.0")),
                Tr(Td("label"), Td("1.000", cls="mono"), Td("0.800", cls="mono"),
                   Td("0.500", cls="mono"), Td("0.200", cls="mono"), Td("0.059", cls="mono")),
            ),
            P("This gives a smooth, monotonically decreasing curve: sharp images → 1.0, "
              "heavily blurred → near 0. The inverse-quadratic shape was chosen because "
              "perceived sharpness drops quickly with initial defocus, then plateaus."),

            # ---- Model Architecture ----
            H2("SharpnessNet Architecture"),
            P("Three conv layers with max-pooling progressively reduce spatial dimensions. "
              "Global average pooling replaces a fully-connected classifier, keeping "
              "the total parameter count at just 3,585:"),
            Div(NotStr(f'<pre class="mermaid">\n{MERMAID_MODEL}\n</pre>'), cls="mermaid-wrap"),
            Table(
                Tr(Th("Layer"), Th("Operation"), Th("Output"), Th("Params")),
                Tr(Td("1"), Td("Conv2d(1→8, 3×3) + ReLU + MaxPool"), Td("8 × 16 × 32"), Td("80", cls="mono")),
                Tr(Td("2"), Td("Conv2d(8→16, 3×3) + ReLU + MaxPool"), Td("16 × 8 × 16"), Td("1,168", cls="mono")),
                Tr(Td("3"), Td("Conv2d(16→16, 3×3) + ReLU"), Td("16 × 8 × 16"), Td("2,320", cls="mono")),
                Tr(Td("4"), Td("GlobalAvgPool"), Td("16"), Td("0", cls="mono")),
                Tr(Td("5"), Td("Linear(16→1) + Sigmoid"), Td("1"), Td("17", cls="mono")),
                Tr(Td(""), Td(Strong("Total")), Td(""), Td(Strong("3,585"), cls="mono")),
            ),

            # ---- Training Loss ----
            H2("Training Loss"),
            P("MSE loss over 200 Adam steps (batch size 128, lr=1e-3). "
              "Loss drops from 0.14 to ~0.01 in the first 80 steps, "
              "then plateaus — the model converges fast on this simple task:"),
            Div(NotStr(loss_svg), cls="chart-wrap"),
            Table(
                Tr(Th("Metric"), Th("Value")),
                Tr(Td("Initial loss"), Td("0.1379", cls="mono")),
                Tr(Td("Final loss"), Td("0.0149", cls="mono")),
                Tr(Td("Best loss"), Td("0.0093 (step 180)", cls="mono")),
                Tr(Td("Convergence"), Td("~80 steps to reach < 0.02")),
                Tr(Td("Wall time"), Td("29.4s on RPi5 CPU (tinygrad)")),
                Tr(Td("Model size"), Td("15 KB (safetensors)")),
            ),

            # ---- Evaluation ----
            H2("Evaluation"),
            P("CNN predictions vs ground truth on held-out samples. "
              "Mean absolute error is ~0.02 — well within the noise floor "
              "of real camera captures:"),
            Table(
                Tr(Th("Sample"), Th("σ"), Th("True"), Th("Predicted"), Th("Error")),
                *[Tr(
                    Td(Img(src=uri, style="height:40px;image-rendering:pixelated;border:1px solid #333;border-radius:2px;vertical-align:middle;")),
                    Td(f"{sigma:.1f}"),
                    Td(f"{true_v:.3f}", cls="mono"),
                    Td(f"{pred_v:.3f}", cls="mono"),
                    Td(f"{err:.3f}", cls="mono"),
                ) for uri, sigma, true_v, pred_v, err in eval_rows],
                Tr(Td(""), Td(""), Td(""), Td(Strong("MAE")),
                   Td(Strong(f"{np.mean([r[4] for r in eval_rows]):.3f}"), cls="mono")),
            ),

            H3("Why Hybrid Scoring?"),
            P("The CNN alone is trained on synthetic data and may not generalize "
              "perfectly to real camera frames (lens aberrations, JPEG artifacts, "
              "ambient light reflections). The Laplacian variance is domain-agnostic — "
              "it measures high-frequency content regardless of scene content. "
              "Blending 70% Laplacian + 30% CNN gives the best of both: "
              "a physically-grounded signal with a learned regularizer."),
            Div("score = 0.7 × Laplacian(crop).var() / 500 + 0.3 × CNN(crop)", cls="formula"),

            cls="af-page",
        ),
    )


@rt("/autofocus")
def autofocus_page():
    model_path = BASE_DIR / "autofocus_model.safetensors"
    model_exists = model_path.exists()
    model_size = f"{model_path.stat().st_size / 1024:.1f} KB" if model_exists else "not trained"
    model_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(model_path.stat().st_mtime)) if model_exists else "—"

    return (
        Title("OLED Autofocus — CNN + Laplacian"),
        Style(CSS),
        H1("OLED Autofocus // CNN + Laplacian Hybrid"),
        nav_bar("autofocus"),
        Div(
            # ---- Intro ----
            P("Automated focus for a Logitech BRIO camera pointing at an SSD1306 128x64 OLED "
              "running Conway's Game of Life on an ESP32-C6. Combines a tiny CNN trained on "
              "synthetic data with classical Laplacian variance to score image sharpness, "
              "then runs a coarse-to-fine sweep over the camera's manual focus range."),
            Div(
                Span("tinygrad", cls="tag"), Span("OpenCV", cls="tag"),
                Span("v4l2-ctl", cls="tag"), Span("ESP32-C6", cls="tag"),
                Span("SSD1306 OLED", cls="tag"), Span("Logitech BRIO", cls="tag"),
            ),

            # ---- Hardware ----
            H2("Hardware Setup"),
            Table(
                Tr(Th("Component"), Th("Details")),
                Tr(Td("Camera"), Td("Logitech BRIO 4K, /dev/video1, 1280x720 @ 15fps MJPG")),
                Tr(Td("Focus control"), Td("Manual via v4l2-ctl: focus_absolute 0–255, step 5 (51 levels)")),
                Tr(Td("Target display"), Td("SSD1306 0.96\" OLED, 128x64 pixels, I2C (GPIO6 SDA, GPIO7 SCL)")),
                Tr(Td("Microcontroller"), Td("ESP32-C6-DevKitC-1-N8, RISC-V, running Game of Life firmware")),
                Tr(Td("Focus scene"), Td("Blue OLED pixels on black + starburst calibration card behind")),
            ),

            # ---- Model ----
            H2("SharpnessNet Architecture"),
            P("A 3-layer CNN with global average pooling. Predicts a sharpness score in [0, 1] from a 32x64 grayscale crop."),
            Div(
                "Input: 1x32x64 (grayscale)\n"
                "  │\n"
                "  ├─ Conv2d(1→8, 3x3, pad=1) + ReLU + MaxPool2d(2)\n"
                "  │    └─ output: 8x16x32\n"
                "  ├─ Conv2d(8→16, 3x3, pad=1) + ReLU + MaxPool2d(2)\n"
                "  │    └─ output: 16x8x16\n"
                "  ├─ Conv2d(16→16, 3x3, pad=1) + ReLU\n"
                "  │    └─ output: 16x8x16\n"
                "  ├─ GlobalAvgPool (mean over H,W)\n"
                "  │    └─ output: 16\n"
                "  └─ Linear(16→1) + Sigmoid\n"
                "       └─ output: 1 (sharpness score)",
                cls="arch-box",
            ),
            Table(
                Tr(Th("Layer"), Th("Params"), Th("Output Shape")),
                Tr(Td("Conv2d(1, 8, 3)"), Td("80", cls="mono"), Td("B x 8 x 16 x 32")),
                Tr(Td("Conv2d(8, 16, 3)"), Td("1,168", cls="mono"), Td("B x 16 x 8 x 16")),
                Tr(Td("Conv2d(16, 16, 3)"), Td("2,320", cls="mono"), Td("B x 16 x 8 x 16")),
                Tr(Td("GlobalAvgPool"), Td("0", cls="mono"), Td("B x 16")),
                Tr(Td("Linear(16, 1)"), Td("17", cls="mono"), Td("B x 1")),
                Tr(Td(Strong("Total")), Td(Strong("3,585", cls="mono")), Td("")),
            ),

            # ---- Training ----
            H2("Training"),
            H3("Synthetic Data Generation"),
            P("2,000 samples generated on-the-fly from random Game of Life simulations — no real camera images needed."),
            Table(
                Tr(Th("Step"), Th("Details")),
                Tr(Td("1. Random GoL"), Td("64x128 grid, density 5–40%, 0–20 evolution steps")),
                Tr(Td("2. Resize"), Td("128x64 → 64x32, nearest-neighbor interpolation")),
                Tr(Td("3. Blur"), Td("Gaussian blur, sigma ~ Uniform(0.0, 4.0)")),
                Tr(Td("4. Noise"), Td("Additive Gaussian, sigma = 0.02")),
                Tr(Td("5. Label"), Td(Code("1.0 / (1.0 + sigma²)"), " — sharpness in [0, 1]")),
            ),

            H3("Hyperparameters"),
            Table(
                Tr(Th("Parameter"), Th("Value")),
                Tr(Td("Optimizer"), Td("Adam")),
                Tr(Td("Learning rate"), Td(Code("1e-3"))),
                Tr(Td("Batch size"), Td("128")),
                Tr(Td("Training steps"), Td("200")),
                Tr(Td("Loss function"), Td("MSE (mean squared error)")),
                Tr(Td("JIT"), Td("TinyJit (tinygrad kernel fusion)")),
                Tr(Td("Training time"), Td("~30s on Raspberry Pi 5 CPU")),
                Tr(Td("Data generation"), Td("~5s for 2,000 samples")),
            ),

            H3("Training Results"),
            Pre(Code(
                "step   0/200: loss=0.1379\n"
                "step  20/200: loss=0.0859\n"
                "step  40/200: loss=0.0565\n"
                "step  60/200: loss=0.0204\n"
                "step  80/200: loss=0.0108\n"
                "step 100/200: loss=0.0112\n"
                "step 120/200: loss=0.0096\n"
                "step 140/200: loss=0.0121\n"
                "step 160/200: loss=0.0115\n"
                "step 180/200: loss=0.0093\n"
                "step 199/200: loss=0.0149"
            )),

            H3("Model File"),
            Table(
                Tr(Th("Property"), Th("Value")),
                Tr(Td("Format"), Td("safetensors")),
                Tr(Td("Size"), Td(model_size, cls="mono")),
                Tr(Td("Last trained"), Td(model_date)),
                Tr(Td("Path"), Td(Code("camera/autofocus_model.safetensors"))),
            ),

            # ---- Scoring ----
            H2("Sharpness Scoring"),
            H3("OLED Detection"),
            P("Classical CV pipeline finds the blue OLED region in the camera frame:"),
            Table(
                Tr(Th("Step"), Th("Details")),
                Tr(Td("Color space"), Td("BGR → HSV")),
                Tr(Td("Threshold"), Td("H: 90–130, S: 50–255, V: 30–255 (blue glow)")),
                Tr(Td("Morphology"), Td("Dilate with 7x7 rect kernel, 2 iterations")),
                Tr(Td("Contour"), Td("Largest contour, area > 500px, aspect ratio 1.3–3.0")),
                Tr(Td("Fallback"), Td("Center 40% crop if OLED not detected")),
            ),
            H3("Hybrid Score"),
            P("Combines two complementary sharpness measures:"),
            Div("score = 0.7 * laplacian_norm + 0.3 * cnn_score", cls="formula"),
            Table(
                Tr(Th("Component"), Th("Weight"), Th("Method")),
                Tr(Td("Laplacian variance"), Td("70%"), Td(Code("cv2.Laplacian(crop, CV_64F).var() / 500"))),
                Tr(Td("CNN prediction"), Td("30%"), Td(Code("SharpnessNet(crop).sigmoid()"))),
            ),
            P("The Laplacian detects high-frequency edges (sharp = lots of edges). "
              "The CNN provides a learned prior from synthetic GoL frames, regularizing "
              "the score when the scene has low contrast or unusual content."),

            # ---- Algorithm ----
            H2("Autofocus Algorithm"),
            P("Three-phase coarse-to-fine search with 300ms settle time between focus changes:"),
            Table(
                Tr(Th("Phase"), Th("Positions"), Th("Captures"), Th("Time")),
                Tr(Td("1. Coarse"), Td(Code("[0, 10, 20, 30, 45, 60, 80]")), Td("7"), Td("~7s")),
                Tr(Td("2. Fine"), Td(Code("best ± 10, step 5")), Td("~5"), Td("~5s")),
                Tr(Td("3. Verify"), Td("winner"), Td("1"), Td("~1s")),
                Tr(Td(Strong("Total")), Td(""), Td(Strong("~13")), Td(Strong("~13s"))),
            ),

            # ---- Reproduce ----
            H2("Reproduce"),
            H3("Requirements"),
            Table(
                Tr(Th("Dependency"), Th("Version"), Th("Purpose")),
                Tr(Td("Python"), Td("3.11+"), Td("Runtime")),
                Tr(Td("tinygrad"), Td("0.11"), Td("CNN training & inference")),
                Tr(Td("OpenCV"), Td("4.12"), Td("Image capture, Laplacian, HSV detection")),
                Tr(Td("NumPy"), Td("1.26+"), Td("Synthetic data generation")),
                Tr(Td("v4l2-ctl"), Td("—"), Td("Camera focus control (v4l-utils package)")),
            ),
            H3("Commands"),
            Pre(Code(
                "# Install dependencies\n"
                "pip install tinygrad opencv-python numpy\n"
                "sudo apt install v4l-utils\n"
                "\n"
                "# Train the model (~35s on RPi5)\n"
                "python camera/autofocus.py train\n"
                "\n"
                "# Run autofocus (~13s)\n"
                "python camera/autofocus.py focus\n"
                "\n"
                "# Full diagnostic sweep (all 51 focus levels, ~3min)\n"
                "python camera/autofocus.py sweep"
            )),
            H3("Adapting to Your Setup"),
            Ul(
                Li(Strong("Different camera:"), " Change ", Code("CAM_DEV"), " and ", Code("CAM_W/CAM_H/CAM_FPS"), " in autofocus.py"),
                Li(Strong("Different display:"), " Adjust HSV thresholds in ", Code("find_oled()"), " for your display's color"),
                Li(Strong("Different scene:"), " The Laplacian component (70% weight) works with any high-frequency content; "
                   "retrain the CNN on synthetic frames matching your scene"),
                Li(Strong("Wider focus range:"), " Expand ", Code("COARSE_POSITIONS"), " and increase settle time for long-travel lenses"),
            ),

            # ---- Source ----
            H2("Source"),
            P("Single file implementation: ", Code("camera/autofocus.py"), " (~280 lines)"),
            Pre(Code(
                "camera/\n"
                "├── autofocus.py                  # training + autofocus logic\n"
                "├── autofocus_model.safetensors   # trained weights (15 KB)\n"
                "└── app.py                        # web UI (this site)"
            )),
            cls="af-page",
        ),
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    cam.open()


@app.on_event("shutdown")
def on_shutdown():
    cam.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
