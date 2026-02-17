"""
ESP Camera Calibration — live MJPEG stream + v4l2 controls.

Run: python app.py
Visit: http://localhost:3000
"""

import base64
import json
import random
import re
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
PHOTOS_DIR = BASE_DIR / "photos"

SNAP_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
PHOTOS_DIR.mkdir(exist_ok=True)

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
_af_lock = threading.Lock()
_af_final_focus: int | None = None
_af_progress = ""                      # e.g. "Coarse 3/9"
_af_stage = 0                          # 0=idle, 1=Scramble, 2=Detect, 3=Coarse, 4=Fine, 5=Ultra, 6=Focus
_af_settle_s = 0.1                     # settle time between focus moves (seconds)
_af_offset = 0                         # focus offset applied after sweep (compensates scoring bias)
_NORM_SIZE = (64, 32)                  # fixed crop size (w, h) for scale invariance
_LAPLACIAN_DIVISOR = 25000.0           # tuned for 64x32 CLAHE-normalized OLED crop (bumped to avoid saturation at 1.0)
_EDGE_MARGIN_PX = 2                    # bbox within this of frame edge = clipped
_CROP_COARSE = 80                      # progressive crop sizes per sweep phase
_CROP_FINE = 100
_CROP_MICRO = 120
_CROP_ULTRA = 120
_BAIL_DOMINANCE = 1.5                  # best must be ≥1.5x second-best to bail early

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
.layout { max-width: 1100px; margin: 0 auto; }
.stream-panel img { width: 100%; border: 2px solid #333; border-radius: 4px; }
.drawer { max-height: 0; overflow: hidden; transition: max-height 0.3s ease; }
.drawer.open { max-height: 800px; overflow-y: auto; }
.drawer-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 0 24px; padding: 12px 0;
}
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
    .drawer-grid { grid-template-columns: 1fr; }
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
.af-progress { color: #0ff; font-size: 0.8rem; min-height: 1.4em; margin-top: 6px; font-family: 'JetBrains Mono', monospace; }
.af-progress:empty { display: none; }
.stage-bar { display: flex; align-items: center; margin: 10px 0 4px; gap: 0; }
.stage-node { display: flex; flex-direction: column; align-items: center; min-width: 56px; }
.stage-dot {
    width: 14px; height: 14px; border-radius: 50%;
    border: 2px solid #333; background: #222;
    transition: all 0.3s;
}
.stage-dot.done { background: #0a0; border-color: #0f0; box-shadow: 0 0 6px #0f04; }
.stage-dot.active { background: #0aa; border-color: #0ff; box-shadow: 0 0 8px #0ff6; animation: pulse-dot 1s infinite; }
.stage-label { font-size: 0.65rem; color: #555; margin-top: 3px; }
.stage-label.done { color: #0a0; }
.stage-label.active { color: #0ff; }
.stage-line { flex: 1; height: 2px; background: #333; min-width: 16px; margin-top: -16px; }
.stage-line.done { background: #0a0; }
@keyframes pulse-dot { 0%,100% { box-shadow: 0 0 4px #0ff4; } 50% { box-shadow: 0 0 12px #0ff8; } }
body.af-locked .btn,
body.af-locked nav a,
body.af-locked input[type=range] { pointer-events: none; opacity: 0.3; transition: opacity 0.3s; }
body.af-locked .btn-hero { pointer-events: none; opacity: 0.5; }
"""

# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def nav_bar(active="camera"):
    return Nav(
        A("Camera", href="/", cls="active" if active == "camera" else ""),
        A("Photos", href="/photos", cls="active" if active == "photos" else ""),
        A("Docs", href="/docs", cls="active" if active == "docs" else ""),
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


def _drawer_toggle(label, drawer_id):
    """Reusable toggle button for a collapsible drawer."""
    return Button(
        f"{label} \u25b8", cls="btn", style="margin-top:8px;",
        onclick=f"var d=document.getElementById('{drawer_id}');"
                f"d.classList.toggle('open');"
                f"this.textContent=d.classList.contains('open')"
                f"?'{label} \\u25be':'{label} \\u25b8';",
    )


def main_buttons():
    """Presets + autofocus buttons — always visible above the stream."""
    return Div(
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
                "Autofocus",
                hx_post="/autofocus-only",
                hx_target="#af-panel",
                hx_swap="outerHTML",
                hx_disabled_elt="this",
                cls="btn btn-hero",
            ),
            Button(
                "Randomize + Autofocus",
                hx_post="/randomize-autofocus",
                hx_target="#af-panel",
                hx_swap="outerHTML",
                hx_disabled_elt="this",
                cls="btn btn-hero",
            ),
            cls="btn-row",
        ),
        Div(id="status", cls="status"),
    )


def actions_drawer():
    """Save/Randomize actions hidden in a collapsible drawer."""
    return Div(
        _drawer_toggle("Actions", "actions-drawer"),
        Div(
            Div(
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
                cls="btn-row", style="padding:8px 0;",
            ),
            id="actions-drawer", cls="drawer",
        ),
    )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@rt("/")
def index():
    settle_ms = int(_af_settle_s * 1000)
    offset = _af_offset
    return (
        Title("ESP Camera"),
        Style(CSS),
        H1("ESP Camera Calibration"),
        nav_bar("camera"),
        Div(
            main_buttons(),
            Div(
                Img(src="/stream", alt="Live camera stream"),
                Div(id="af-progress", cls="af-progress"),
                cls="stream-panel",
            ),
            actions_drawer(),
            Div(
                _drawer_toggle("Output", "output-drawer"),
                Div(
                    _af_panel_current(),
                    id="output-drawer", cls="drawer",
                ),
            ),
            Div(
                _drawer_toggle("Controls", "drawer"),
                Div(
                    Div(
                        ctrl_group("Position", POSITION_CTRLS),
                        ctrl_group("Focus", FOCUS_CTRLS),
                        ctrl_group("Autofocus", [
                            ("Settle Time", "af_settle", 100, 1000, 50, settle_ms),
                            ("Focus Offset", "af_offset", -20, 20, 1, offset),
                        ]),
                        ctrl_group("Image", IMAGE_CTRLS),
                        ctrl_group("Exposure", EXPOSURE_CTRLS),
                        cls="drawer-grid",
                    ),
                    id="drawer", cls="drawer",
                ),
            ),
            cls="layout",
        ),
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


@rt("/ctrl/af_settle")
async def ctrl_af_settle(value: int):
    global _af_settle_s
    _af_settle_s = max(100, min(1000, value)) / 1000.0
    return Response(status_code=204)


@rt("/ctrl/af_offset")
async def ctrl_af_offset(value: int):
    global _af_offset
    _af_offset = max(-20, min(20, value))
    return Response(status_code=204)


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

def _save_af_photo(frame: np.ndarray, ts: int, suffix: str, fmt: str = "jpg") -> Path:
    path = PHOTOS_DIR / f"{ts}_{suffix}.{fmt}"
    if fmt == "png":
        cv2.imwrite(str(path), frame)
    else:
        cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return path

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
    """Score current focus by averaging n normalized crops, then computing Laplacian once.

    Averaging pixel data before scoring cancels out temporal artifacts
    (OLED refresh flicker, sensor noise) that can corrupt individual frames.
    """
    acc = None
    for _ in range(n):
        frame = _capture_frame()
        crop = _normalize_crop(frame, bbox)
        acc = crop if acc is None else acc + crop
        time.sleep(0.05)
    avg_crop = acc / n
    lap = cv2.Laplacian((avg_crop * 255).astype(np.uint8), cv2.CV_64F)
    return min(float(lap.var()) / _LAPLACIAN_DIVISOR, 1.0)

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

def _find_focus_center(frame: np.ndarray) -> tuple[int, int]:
    """Find center of nearest bright cluster for focus targeting. Returns (cx, cy)."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (90, 50, 30), (130, 255, 255))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.dilate(mask, kernel, iterations=1)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    fh, fw = frame.shape[:2]
    fcx, fcy = fw // 2, fh // 2
    # Filter contours > 50px area, pick closest to frame center
    candidates = []
    for c in contours:
        if cv2.contourArea(c) < 50:
            continue
        x, y, w, h = cv2.boundingRect(c)
        ccx, ccy = x + w // 2, y + h // 2
        dist = (ccx - fcx) ** 2 + (ccy - fcy) ** 2
        candidates.append((dist, ccx, ccy))
    if candidates:
        candidates.sort()
        _, cx, cy = candidates[0]
        return (cx, cy)
    # Fallback: frame center
    return (fcx, fcy)

def _make_crop_bbox(cx: int, cy: int, size: int, frame_shape) -> tuple[int, int, int, int]:
    """Create a size x size square bbox centered at (cx, cy), clamped to frame bounds."""
    fh, fw = frame_shape[:2]
    x = max(0, min(cx - size // 2, fw - size))
    y = max(0, min(cy - size // 2, fh - size))
    w = min(size, fw - x)
    h = min(size, fh - y)
    return (x, y, w, h)

def _center_crop_rect(frame: np.ndarray):
    h, w = frame.shape[:2]
    cw, ch = int(w * 0.4), int(h * 0.4)
    return (w // 2 - cw // 2, h // 2 - ch // 2, cw, ch)

def _normalize_crop(frame: np.ndarray, bbox) -> np.ndarray:
    """Extract bbox, resize to fixed size, grayscale, CLAHE normalize."""
    x, y, w, h = bbox
    gray = cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, _NORM_SIZE, interpolation=cv2.INTER_AREA)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    gray = clahe.apply(gray)
    return gray.astype(np.float32) / 255.0

def _check_bbox_clipping(frame: np.ndarray, bbox) -> bool:
    """Return True if bbox is within _EDGE_MARGIN_PX of any frame edge."""
    fh, fw = frame.shape[:2]
    x, y, w, h = bbox
    return (x <= _EDGE_MARGIN_PX or y <= _EDGE_MARGIN_PX
            or (x + w) >= (fw - _EDGE_MARGIN_PX)
            or (y + h) >= (fh - _EDGE_MARGIN_PX))

def _detect_grid_rotation(frame: np.ndarray, cx: int, cy: int) -> float | None:
    """Detect OLED pixel grid angle via angular energy integration of the FFT.

    The periodic pixel grid concentrates FFT energy along specific orientations.
    Instead of hunting for a single point peak (which fails when the grid signal
    is a broad ridge rather than a sharp spike), we integrate magnitude in an
    annular band and find the dominant angle.

    Returns angle in degrees ([-45, 45]) or None if no clear directional signal.
    """
    fh, fw = frame.shape[:2]
    half = 100
    size = 2 * half
    x0 = max(0, min(cx - half, fw - size))
    y0 = max(0, min(cy - half, fh - size))
    crop = frame[y0:y0 + size, x0:x0 + size]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)

    # Hanning window reduces spectral leakage
    window = np.outer(np.hanning(size), np.hanning(size)).astype(np.float32)
    gray = gray * window

    # 2D FFT → magnitude spectrum
    fft = np.fft.fft2(gray)
    fft_shift = np.fft.fftshift(fft)
    magnitude = np.log1p(np.abs(fft_shift))

    # Build coordinate grids relative to center
    cy_f, cx_f = size // 2, size // 2
    yy, xx = np.mgrid[:size, :size]
    dy = (yy - cy_f).astype(np.float32)
    dx = (xx - cx_f).astype(np.float32)
    radius = np.sqrt(dy ** 2 + dx ** 2)

    # Annular band: skip DC skirt (r < 15) and noisy high freqs (r > 80)
    band = (radius >= 15) & (radius <= 80)
    if not np.any(band):
        return None

    # Compute angle for each pixel (0–180°, folded by conjugate symmetry)
    angles = np.degrees(np.arctan2(dy, dx)) % 180.0

    # Bin magnitude by angle (1° bins, 180 bins total)
    n_bins = 180
    bin_edges = np.linspace(0, 180, n_bins + 1)
    bin_idx = np.digitize(angles[band], bin_edges) - 1
    bin_idx = np.clip(bin_idx, 0, n_bins - 1)
    mag_vals = magnitude[band]

    angular_energy = np.zeros(n_bins)
    np.add.at(angular_energy, bin_idx, mag_vals)
    # Normalize by bin count to avoid bias from geometry
    bin_counts = np.zeros(n_bins)
    np.add.at(bin_counts, bin_idx, 1)
    bin_counts[bin_counts == 0] = 1
    angular_energy /= bin_counts

    # Smooth with a small kernel to reduce noise
    kernel = np.ones(5) / 5
    angular_smooth = np.convolve(angular_energy, kernel, mode='same')

    # Find dominant angle
    peak_bin = np.argmax(angular_smooth)
    peak_val = angular_smooth[peak_bin]
    mean_val = np.mean(angular_smooth)
    std_val = np.std(angular_smooth)

    # Prominence check: peak must be ≥ 1.5 sigma above mean
    if std_val <= 0 or (peak_val - mean_val) < 1.5 * std_val:
        return None

    # Convert bin index to angle (bin centers)
    peak_angle = (bin_edges[peak_bin] + bin_edges[peak_bin + 1]) / 2.0

    # FFT energy direction is perpendicular to grid lines → rotate 90°
    angle = peak_angle + 90.0

    # Normalize to [-45, 45]
    angle = angle % 180.0
    if angle > 135:
        angle -= 180
    elif angle > 45:
        angle -= 90

    return float(angle)

def _fft_magnitude_image(frame: np.ndarray, cx: int, cy: int,
                         annotate: bool = True) -> np.ndarray:
    """Return a colorized BGR image of the FFT magnitude spectrum.

    Green-on-black color ramp. If annotate=True, draws the annular band boundaries
    and the detected dominant angle line in cyan.
    """
    fh, fw = frame.shape[:2]
    half = 100
    size = 2 * half
    x0 = max(0, min(cx - half, fw - size))
    y0 = max(0, min(cy - half, fh - size))
    crop = frame[y0:y0 + size, x0:x0 + size]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)

    win = np.outer(np.hanning(size), np.hanning(size)).astype(np.float32)
    gray = gray * win

    fft = np.fft.fft2(gray)
    fft_shift = np.fft.fftshift(fft)
    magnitude = np.log1p(np.abs(fft_shift))

    # Normalize to [0, 255]
    mag_max = magnitude.max()
    if mag_max > 0:
        mag_norm = (magnitude / mag_max * 255).astype(np.uint8)
    else:
        mag_norm = np.zeros((size, size), dtype=np.uint8)

    # Green-on-black colormap
    bgr = np.zeros((size, size, 3), dtype=np.uint8)
    bgr[:, :, 1] = mag_norm  # green channel

    if annotate:
        c = size // 2
        # Draw annular band boundaries (dim cyan circles)
        cv2.circle(bgr, (c, c), 15, (128, 128, 0), 1, cv2.LINE_AA)
        cv2.circle(bgr, (c, c), 80, (128, 128, 0), 1, cv2.LINE_AA)

        # Detect grid angle and draw the dominant direction line
        grid_angle = _detect_grid_rotation(frame, cx, cy)
        if grid_angle is not None:
            # grid_angle is the grid direction; FFT energy is perpendicular
            fft_angle = grid_angle - 90.0
            rad = np.radians(fft_angle)
            length = 90
            dx = int(length * np.cos(rad))
            dy = int(length * np.sin(rad))
            cv2.line(bgr, (c - dx, c - dy), (c + dx, c + dy),
                     (255, 255, 0), 1, cv2.LINE_AA)

    return bgr


def _deskew_frame(frame: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotate frame by -angle_deg around its center to deskew."""
    h, w = frame.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    return cv2.warpAffine(frame, M, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)

def _laplacian_score(frame: np.ndarray, bbox) -> float:
    """Laplacian variance sharpness score, normalized to [0, 1]."""
    crop = _normalize_crop(frame, bbox)
    lap = cv2.Laplacian((crop * 255).astype(np.uint8), cv2.CV_64F)
    return min(float(lap.var()) / _LAPLACIAN_DIVISOR, 1.0)

_STAGE_NAMES = ["Scramble", "Detect", "Coarse", "Fine", "Ultra", "Focus"]

def _stage_html(current: int):
    """Render a 6-stage pipeline graphic. current: 1–6 (active), 0=idle."""
    nodes = []
    for i, name in enumerate(_STAGE_NAMES, 1):
        if i < current:
            dot_cls, lbl_cls = "stage-dot done", "stage-label done"
        elif i == current:
            dot_cls, lbl_cls = "stage-dot active", "stage-label active"
        else:
            dot_cls, lbl_cls = "stage-dot", "stage-label"
        nodes.append(Div(Div(cls=dot_cls), Span(name, cls=lbl_cls), cls="stage-node"))
        if i < len(_STAGE_NAMES):
            line_cls = "stage-line done" if i < current else "stage-line"
            nodes.append(Div(cls=line_cls))
    return Div(*nodes, cls="stage-bar")

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

def _run_autofocus(initial_values: dict):
    """Background thread: coarse-to-fine autofocus with Laplacian scoring."""
    global _af_running, _af_log, _af_final_focus, _af_progress, _af_stage
    _af_log = []
    _af_final_focus = None
    _af_progress = ""
    _af_stage = 1  # Scramble

    def log(msg, cls="af-info"):
        _af_log.append((msg, cls))

    def progress(text):
        global _af_progress
        _af_progress = text

    try:
        progress("Scrambling...")
        time.sleep(0.5)
        af_ts = int(time.time())
        pre_frame = _capture_frame()
        _save_af_photo(pre_frame, af_ts, "pre")
        _af_stage = 2  # Detect
        # Restore everything except focus — leave focus as the variable
        log("Restoring camera preset (keeping random focus)...")
        restore = {
            "zoom_absolute": 150, "pan_absolute": 0, "tilt_absolute": 0,
            "focus_automatic_continuous": 0, "sharpness": 180,
            "brightness": 128, "contrast": 128, "saturation": 128,
            "gain": 0, "backlight_compensation": 0,
            "auto_exposure": 1, "exposure_time_absolute": 250,
            "white_balance_automatic": 0, "white_balance_temperature": 4000,
        }
        for name, val in restore.items():
            cam.set_ctrl(name, val)
        time.sleep(1.2)  # settle for zoom motor + image pipeline

        # --- Pass 1: find focus center ---
        progress("Finding focus center...")
        log("Finding focus center at focus=30...")
        cam.set_ctrl("focus_absolute", 30)
        time.sleep(_af_settle_s)
        frame = _capture_frame()
        focus_cx, focus_cy = _find_focus_center(frame)
        log(f"  Focus center: ({focus_cx}, {focus_cy})", "af-good")

        # Coarse sweep — every 20 steps across full range, 3-frame avg
        _af_stage = 3  # Coarse
        bbox = _make_crop_bbox(focus_cx, focus_cy, _CROP_COARSE, frame.shape)
        log("")
        log(f"--- Coarse sweep (3-frame avg, {_CROP_COARSE}x{_CROP_COARSE} crop) ---", "af-header")
        coarse = list(range(0, 256, 20))  # [0, 20, 40, ..., 240]
        results = []
        for i, pos in enumerate(coarse):
            progress(f"Coarse {i+1}/{len(coarse)}")
            cam.set_ctrl("focus_absolute", pos)
            time.sleep(_af_settle_s)
            score = _score_position(bbox, n=3)
            results.append((pos, score))
            bar = chr(9608) * int(score * 30)
            log(f"  focus={pos:3d}  score={score:.4f}  {bar}")

        best_pos, best_score = max(results, key=lambda r: r[1])
        log(f"  * best: focus={best_pos} (score={best_score:.4f})", "af-good")

        # Bail check after coarse
        bail_to_ultra = False
        sorted_scores = sorted([s for _, s in results], reverse=True)
        if len(sorted_scores) >= 3 and sorted_scores[1] > 0:
            dominance = sorted_scores[0] / sorted_scores[1]
            if dominance >= _BAIL_DOMINANCE:
                log(f"  Early bail: dominance {dominance:.2f}x (≥{_BAIL_DOMINANCE}x), skipping fine+micro", "af-warn")
                bail_to_ultra = True

        if not bail_to_ultra:
            # Fine sweep — ±15 around best, step 5, 5-frame avg
            _af_stage = 4  # Fine
            bbox = _make_crop_bbox(focus_cx, focus_cy, _CROP_FINE, frame.shape)
            log("")
            log(f"--- Fine sweep (5-frame avg, {_CROP_FINE}x{_CROP_FINE} crop) ---", "af-header")
            fine_lo = max(0, best_pos - 15)
            fine_hi = min(255, best_pos + 15)
            tested = {r[0] for r in results}
            fine_positions = [p for p in range(fine_lo, fine_hi + 1, 5) if p not in tested]
            for i, pos in enumerate(fine_positions):
                progress(f"Fine {i+1}/{len(fine_positions)}")
                cam.set_ctrl("focus_absolute", pos)
                time.sleep(_af_settle_s)
                score = _score_position(bbox, n=5)
                results.append((pos, score))
                bar = chr(9608) * int(score * 30)
                log(f"  focus={pos:3d}  score={score:.4f}  {bar}")

            best_pos, best_score = max(results, key=lambda r: r[1])
            log(f"  * best: focus={best_pos} (score={best_score:.4f})", "af-good")

            # Bail check after fine
            fine_scores = sorted([s for _, s in results], reverse=True)
            if len(fine_scores) >= 3 and fine_scores[1] > 0:
                dominance = fine_scores[0] / fine_scores[1]
                if dominance >= _BAIL_DOMINANCE:
                    log(f"  Early bail: dominance {dominance:.2f}x (≥{_BAIL_DOMINANCE}x), skipping micro", "af-warn")
                    bail_to_ultra = True

        if not bail_to_ultra:
            # Micro sweep — ±5 around best, step 2, 5-frame avg
            bbox = _make_crop_bbox(focus_cx, focus_cy, _CROP_MICRO, frame.shape)
            log("")
            log(f"--- Micro sweep (5-frame avg, {_CROP_MICRO}x{_CROP_MICRO} crop) ---", "af-header")
            micro_lo = max(0, best_pos - 5)
            micro_hi = min(255, best_pos + 5)
            tested = {r[0] for r in results}
            micro_positions = [p for p in range(micro_lo, micro_hi + 1, 2) if p not in tested]
            for i, pos in enumerate(micro_positions):
                progress(f"Micro {i+1}/{len(micro_positions)}")
                cam.set_ctrl("focus_absolute", pos)
                time.sleep(_af_settle_s)
                score = _score_position(bbox, n=5)
                results.append((pos, score))
                bar = chr(9608) * int(score * 30)
                log(f"  focus={pos:3d}  score={score:.4f}  {bar}")

            # Pick best from micro range only (prevents overshoot from noisy coarse/fine scores)
            micro_results = [(p, s) for p, s in results if micro_lo <= p <= micro_hi]
            best_pos, best_score = max(micro_results, key=lambda r: r[1])
            log(f"  * best: focus={best_pos} (score={best_score:.4f})", "af-good")

        # Ultra sweep — ±2 around best, step 1, 5-frame avg (always runs)
        _af_stage = 5  # Ultra
        bbox = _make_crop_bbox(focus_cx, focus_cy, _CROP_ULTRA, frame.shape)
        log("")
        log(f"--- Ultra sweep (5-frame avg, step 1, {_CROP_ULTRA}x{_CROP_ULTRA} crop) ---", "af-header")
        ultra_lo = max(0, best_pos - 2)
        ultra_hi = min(255, best_pos + 2)
        tested = {r[0] for r in results}
        ultra_positions = [p for p in range(ultra_lo, ultra_hi + 1) if p not in tested]
        for i, pos in enumerate(ultra_positions):
            progress(f"Ultra {i+1}/{len(ultra_positions)}")
            cam.set_ctrl("focus_absolute", pos)
            time.sleep(_af_settle_s)
            score = _score_position(bbox, n=5)
            results.append((pos, score))
            bar = chr(9608) * int(score * 30)
            log(f"  focus={pos:3d}  score={score:.4f}  {bar}")

        # Final best from ultra range only
        ultra_all = [(p, s) for p, s in results if ultra_lo <= p <= ultra_hi]
        best_pos, best_score = max(ultra_all, key=lambda r: r[1])
        log(f"  * best: focus={best_pos} (score={best_score:.4f})", "af-good")

        # Apply focus offset
        if _af_offset != 0:
            best_pos = max(0, min(255, best_pos + _af_offset))
            log(f"  + offset {_af_offset:+d} → focus={best_pos}", "af-info")

        # --- Grid rotation detection ---
        cam.set_ctrl("focus_absolute", best_pos)
        time.sleep(_af_settle_s)
        frame = _capture_frame()
        log("")
        log("--- Grid rotation detection ---", "af-header")
        grid_angle = _detect_grid_rotation(frame, focus_cx, focus_cy)
        if grid_angle is not None:
            log(f"  Grid angle: {grid_angle:.1f} degrees", "af-good")
        else:
            log("  Grid angle: not detected (no dominant FFT peak)", "af-warn")

        # Save FFT spectrum image
        fft_img = _fft_magnitude_image(frame, focus_cx, focus_cy, annotate=True)
        _save_af_photo(fft_img, af_ts, "fft", fmt="png")

        # --- Re-detect OLED (deskewed if angle found) ---
        log("")
        if grid_angle is not None and abs(grid_angle) > 0.5:
            log("--- Re-detect OLED (deskewed) ---", "af-header")
            deskewed = _deskew_frame(frame, grid_angle)
        else:
            log("--- Re-detect OLED (sharp focus) ---", "af-header")
            deskewed = frame
        oled_bbox = _find_oled_rect(deskewed)
        if oled_bbox:
            ox, oy, ow, oh = oled_bbox
            log(f"  OLED found: {ow}x{oh} at ({ox},{oy})", "af-good")
            if _check_bbox_clipping(deskewed, oled_bbox):
                log("  Warning: bbox touches frame edge", "af-warn")
            bbox = oled_bbox
        else:
            log("  OLED not detected, using focus crop fallback", "af-warn")
            bbox = _make_crop_bbox(focus_cx, focus_cy, _CROP_ULTRA, frame.shape)

        # Verify — 5-frame avg
        _af_stage = 6  # Focus
        progress("Verifying...")
        log("")
        log("--- Verify (5-frame avg) ---", "af-header")
        cam.set_ctrl("focus_absolute", best_pos)
        time.sleep(0.5)
        final_score = _score_position(bbox, n=5)
        log(f"  focus={best_pos}  score={final_score:.4f}", "af-good")

        # Save post-autofocus photo with green bounding box + OLED crop
        post_frame = _capture_frame()
        if grid_angle is not None and abs(grid_angle) > 0.5:
            post_frame = _deskew_frame(post_frame, grid_angle)
        post_annotated = post_frame.copy()
        bx, by, bw, bh = bbox
        cv2.rectangle(post_annotated, (bx, by), (bx + bw, by + bh), (0, 255, 0), 2)
        _save_af_photo(post_annotated, af_ts, "post")
        _save_af_photo(post_frame[by:by+bh, bx:bx+bw], af_ts, "oled")

        # Save metadata JSON
        meta = {
            "timestamp": af_ts,
            "settle_ms": int(_af_settle_s * 1000),
            "initial": initial_values,
            "final": {
                "focus_absolute": best_pos,
                "score": round(final_score, 4),
                "grid_angle_deg": round(grid_angle, 2) if grid_angle is not None else None,
                "grid_detection_method": "fft",
                **restore,
            },
            "focus_center": {"x": focus_cx, "y": focus_cy},
            "crop_sizes": {
                "coarse": _CROP_COARSE, "fine": _CROP_FINE,
                "micro": _CROP_MICRO, "ultra": _CROP_ULTRA,
            },
            "oled_bbox": {"x": bx, "y": by, "w": bw, "h": bh},
        }
        (PHOTOS_DIR / f"{af_ts}_meta.json").write_text(json.dumps(meta, indent=2))

        log("")
        log(f"Done! Best focus = {best_pos}  (score = {final_score:.4f})", "af-best")
        _af_final_focus = best_pos
        progress("")

    except Exception as e:
        log(f"Error: {e}", "af-error")
        progress("")
    finally:
        _af_stage = 0
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
    global _af_running
    with _af_lock:
        if _af_running:
            return Div(Div("Autofocus already running...", cls="af-warn"), id="af-panel", cls="af-panel")
        _af_running = True
    # Randomize controls now so we can return slider JS immediately
    values = _randomize_controls()
    # Start background autofocus (will restore zoom and sweep focus)
    threading.Thread(target=_run_autofocus, args=(values,), daemon=True).start()
    # Lock UI + open output drawer + kick off progress polling
    progress_js = (
        "document.body.classList.add('af-locked');" +
        _slider_js(values) +
        "var od=document.getElementById('output-drawer');if(od)od.classList.add('open');"
        "var p=document.getElementById('af-progress');"
        "if(p){p.setAttribute('hx-get','/af-progress');"
        "p.setAttribute('hx-trigger','load delay:300ms');"
        "p.setAttribute('hx-swap','outerHTML');"
        "htmx.process(p);}"
    )
    return Div(
        Div("Randomized! Starting autofocus...", cls="af-info"),
        Script(progress_js),
        hx_get="/autofocus-status",
        hx_trigger="load delay:500ms",
        hx_swap="outerHTML",
        id="af-panel", cls="af-panel",
    )


@rt("/autofocus-only")
async def autofocus_only():
    global _af_running
    with _af_lock:
        if _af_running:
            return Div(Div("Autofocus already running...", cls="af-warn"), id="af-panel", cls="af-panel")
        _af_running = True
    # Start autofocus with current settings (no randomize)
    threading.Thread(target=_run_autofocus, args=({"mode": "autofocus_only"},), daemon=True).start()
    progress_js = (
        "document.body.classList.add('af-locked');"
        "var od=document.getElementById('output-drawer');if(od)od.classList.add('open');"
        "var p=document.getElementById('af-progress');"
        "if(p){p.setAttribute('hx-get','/af-progress');"
        "p.setAttribute('hx-trigger','load delay:300ms');"
        "p.setAttribute('hx-swap','outerHTML');"
        "htmx.process(p);}"
    )
    return Div(
        Div("Starting autofocus (keeping current settings)...", cls="af-info"),
        Script(progress_js),
        hx_get="/autofocus-status",
        hx_trigger="load delay:500ms",
        hx_swap="outerHTML",
        id="af-panel", cls="af-panel",
    )


@rt("/af-progress")
async def af_progress():
    if _af_running:
        children = [_stage_html(_af_stage)]
        if _af_progress:
            children.append(Div(_af_progress, style="color:#0ff;font-size:0.8rem;margin-top:2px;"))
        return Div(
            *children,
            hx_get="/af-progress",
            hx_trigger="load delay:300ms",
            hx_swap="outerHTML",
            id="af-progress", cls="af-progress",
        )
    return Div(id="af-progress", cls="af-progress")


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

    # Done — unlock UI + sync sliders for all restored settings + final focus
    final_values = {
        "zoom_absolute": 150, "pan_absolute": 0, "tilt_absolute": 0,
        "focus_automatic_continuous": 0, "sharpness": 180,
        "brightness": 128, "contrast": 128, "saturation": 128,
        "gain": 0, "backlight_compensation": 0,
        "auto_exposure": 1, "exposure_time_absolute": 250,
        "white_balance_automatic": 0, "white_balance_temperature": 4000,
    }
    if _af_final_focus is not None:
        final_values["focus_absolute"] = _af_final_focus
    unlock_js = "document.body.classList.remove('af-locked');" + _slider_js(final_values)
    elements.append(Script(unlock_js))
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


@rt("/photos")
def photos_page():
    # Find all pre-images, sorted newest-first, limit 20
    pre_files = sorted(PHOTOS_DIR.glob("*_pre.jpg"), reverse=True)[:20]
    if not pre_files:
        return (
            Title("Photos — Autofocus Runs"),
            Style(CSS),
            H1("Photos // Autofocus Runs"),
            nav_bar("photos"),
            Div(
                P("No runs yet — go to Camera and click ",
                  Strong("Randomize + Autofocus"), " to capture your first set.",
                  style="color:#888;font-size:0.9rem;margin-top:40px;text-align:center;"),
                style="max-width:900px;margin:0 auto;",
            ),
        )
    cards = []
    for pre_path in pre_files:
        ts_str = pre_path.stem.rsplit("_", 1)[0]
        ts = int(ts_str)
        date_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        post_name = f"{ts_str}_post.jpg"
        oled_name = f"{ts_str}_oled.jpg"
        fft_name = f"{ts_str}_fft.png"
        has_fft = (PHOTOS_DIR / fft_name).exists()
        pre_name = pre_path.name
        # Load metadata if available
        meta_path = PHOTOS_DIR / f"{ts_str}_meta.json"
        meta = None
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                pass
        meta_info = ""
        if meta:
            f_val = meta.get("final", {}).get("focus_absolute", "?")
            f_score = meta.get("final", {}).get("score", "?")
            meta_info = f"focus={f_val}  score={f_score}"
        drawer_id = f"card-{ts_str}"
        cards.append(
            Div(
                Div(f"#{ts_str}", style="color:#0a0;font-size:0.85rem;font-weight:bold;"),
                Div(
                    Span(date_str, style="color:#666;font-size:0.75rem;"),
                    Span(f"  {meta_info}", style="color:#0f0;font-size:0.75rem;") if meta_info else "",
                    style="margin-bottom:8px;",
                ),
                Div(
                    Div(
                        Img(src=f"/photos/{pre_name}", style="width:100%;border:1px solid #333;border-radius:2px;"),
                        Div("Pre (scrambled)", style="color:#888;font-size:0.7rem;margin-top:4px;"),
                        style="text-align:center;",
                    ),
                    Div(
                        Img(src=f"/photos/{post_name}", style="width:100%;border:1px solid #333;border-radius:2px;"),
                        Div("Post (focused)", style="color:#888;font-size:0.7rem;margin-top:4px;"),
                        style="text-align:center;",
                    ),
                    Div(
                        Img(src=f"/photos/{oled_name}", style="width:100%;border:2px solid #0f0;border-radius:2px;image-rendering:pixelated;"),
                        Div("OLED crop", style="color:#888;font-size:0.7rem;margin-top:4px;"),
                        style="text-align:center;",
                    ),
                    *([Div(
                        Img(src=f"/photos/{fft_name}", style="width:100%;border:2px solid #0a0;border-radius:2px;image-rendering:pixelated;"),
                        Div("FFT spectrum", style="color:#888;font-size:0.7rem;margin-top:4px;"),
                        style="text-align:center;",
                    )] if has_fft else []),
                    style=f"display:grid;grid-template-columns:{'1fr 1fr 1fr 1fr' if has_fft else '1fr 1fr 1fr'};gap:10px;",
                ),
                Button("\u25b8", cls="btn",
                       style="font-size:0.75rem;padding:3px 10px;margin-top:8px;",
                       onclick=f"var d=document.getElementById('{drawer_id}');"
                               f"d.classList.toggle('open');"
                               f"this.textContent=d.classList.contains('open')?'\\u25be':'\\u25b8';"),
                Div(
                    Div(
                        A("Wide View", href=f"/photos/{post_name}", target="_blank", cls="btn",
                          style="text-decoration:none;display:inline-block;"),
                        A("OLED", href=f"/photos/{oled_name}", target="_blank", cls="btn",
                          style="text-decoration:none;display:inline-block;border-color:#0f0;"),
                        A("Save", href=f"/photos/{post_name}", download=post_name, cls="btn",
                          style="text-decoration:none;display:inline-block;"),
                        cls="btn-row",
                    ),
                    id=drawer_id, cls="drawer",
                ),
                style="background:#0a0a0a;border:1px solid #333;border-radius:4px;padding:12px 16px;margin-bottom:16px;",
            )
        )
    return (
        Title("Photos — Autofocus Runs"),
        Style(CSS),
        H1("Photos // Autofocus Runs"),
        nav_bar("photos"),
        Div(
            Div(
                Button("Archive All", hx_post="/photos/archive", hx_swap="innerHTML",
                       hx_target="#photos-status", cls="btn btn-warn",
                       hx_confirm="Move all photos to archive?"),
                Div(id="photos-status", cls="status"),
                cls="btn-row", style="margin-bottom:16px;",
            ),
            *cards,
            style="max-width:1100px;margin:0 auto;",
        ),
    )


ARCHIVE_DIR = PHOTOS_DIR / "archive"


@rt("/photos/archive")
async def photos_archive():
    files = list(PHOTOS_DIR.glob("*_pre.jpg")) + list(PHOTOS_DIR.glob("*_post.jpg")) + \
            list(PHOTOS_DIR.glob("*_oled.jpg")) + list(PHOTOS_DIR.glob("*_fft.png")) + \
            list(PHOTOS_DIR.glob("*_meta.json"))
    if not files:
        return "Nothing to archive"
    ts = int(time.time())
    dest = ARCHIVE_DIR / str(ts)
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.move(str(f), str(dest / f.name))
    return f"Archived {len(files)} files to archive/{ts}"


@rt("/photos/{filename}")
async def photos_file(filename: str):
    if not re.fullmatch(r"[a-zA-Z0-9_.]+", filename):
        return Response("Not found", status_code=404)
    if filename.endswith(".json"):
        path = PHOTOS_DIR / filename
        if not path.exists():
            return Response("Not found", status_code=404)
        return FileResponse(path, media_type="application/json")
    if filename.endswith(".jpg"):
        media = "image/jpeg"
    elif filename.endswith(".png"):
        media = "image/png"
    else:
        return Response("Not found", status_code=404)
    path = PHOTOS_DIR / filename
    if not path.exists():
        return Response("Not found", status_code=404)
    return FileResponse(
        path,
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400"},
    )

# ---------------------------------------------------------------------------
# FFT demo helpers
# ---------------------------------------------------------------------------

def _make_rotated_grid(angle_deg: float, size: int = 200, pitch: int = 6, seed: int = 42) -> np.ndarray:
    """Synthetic OLED pixel grid at a known rotation angle. Returns BGR image."""
    rng = np.random.default_rng(seed)
    # Start with larger canvas to avoid border artifacts after rotation
    pad = size
    big = pad * 2 + size
    canvas = np.zeros((big, big), dtype=np.uint8)

    # Draw filled rectangles simulating OLED pixels (some on, some off)
    pw, ph = pitch - 1, pitch - 1  # pixel size (leave 1px gap)
    for y in range(0, big, pitch):
        for x in range(0, big, pitch):
            if rng.random() < 0.45:  # ~45% pixels lit
                canvas[y:y+ph, x:x+pw] = 180 + rng.integers(0, 75)

    # Rotate
    center = (big // 2, big // 2)
    M = cv2.getRotationMatrix2D(center, -angle_deg, 1.0)
    rotated = cv2.warpAffine(canvas, M, (big, big), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    # Crop center
    x0 = (big - size) // 2
    y0 = (big - size) // 2
    crop = rotated[y0:y0+size, x0:x0+size]

    # Convert to blue-on-black BGR (mimicking OLED appearance)
    bgr = np.zeros((size, size, 3), dtype=np.uint8)
    bgr[:, :, 0] = crop  # blue channel
    bgr[:, :, 1] = (crop * 0.3).astype(np.uint8)  # faint green
    return bgr


def _fft_spectrum_data_uri(img: np.ndarray, scale: int = 1,
                           show_angle: bool = False) -> str:
    """FFT magnitude spectrum as green-on-black PNG data URI.

    img: BGR or grayscale image.
    If show_angle=True, draws the annular band and detected angle line in cyan.
    """
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    else:
        gray = img.astype(np.float32)
    h, w = gray.shape

    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    gray = gray * win

    fft = np.fft.fft2(gray)
    fft_shift = np.fft.fftshift(fft)
    magnitude = np.log1p(np.abs(fft_shift))

    mag_max = magnitude.max()
    if mag_max > 0:
        mag_norm = (magnitude / mag_max * 255).astype(np.uint8)
    else:
        mag_norm = np.zeros_like(gray, dtype=np.uint8)

    # Green-on-black
    bgr = np.zeros((h, w, 3), dtype=np.uint8)
    bgr[:, :, 1] = mag_norm

    c = min(h, w) // 2

    if show_angle:
        # Draw annular band boundaries
        cv2.circle(bgr, (w // 2, h // 2), 15, (128, 128, 0), 1, cv2.LINE_AA)
        cv2.circle(bgr, (w // 2, h // 2), 80, (128, 128, 0), 1, cv2.LINE_AA)

        # Use _detect_grid_rotation to find the angle
        # For synthetic images, we pass a fake frame with the image at center
        grid_angle = _detect_grid_rotation(
            cv2.copyMakeBorder(img if len(img.shape) == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR),
                               0, 0, 0, 0, cv2.BORDER_CONSTANT),
            w // 2, h // 2,
        )
        if grid_angle is not None:
            fft_angle = grid_angle - 90.0
            rad = np.radians(fft_angle)
            length = min(c, 90)
            dx = int(length * np.cos(rad))
            dy = int(length * np.sin(rad))
            cv2.line(bgr, (w // 2 - dx, h // 2 - dy), (w // 2 + dx, h // 2 + dy),
                     (255, 255, 0), 1, cv2.LINE_AA)

    if scale > 1:
        bgr = cv2.resize(bgr, (w * scale, h * scale), interpolation=cv2.INTER_NEAREST)

    _, buf = cv2.imencode(".png", bgr)
    return f"data:image/png;base64,{base64.b64encode(buf).decode()}"


def _bgr_to_data_uri(img: np.ndarray, scale: int = 1) -> str:
    """Convert a BGR image to a PNG data URI."""
    if scale > 1:
        h, w = img.shape[:2]
        img = cv2.resize(img, (w * scale, h * scale), interpolation=cv2.INTER_NEAREST)
    _, buf = cv2.imencode(".png", img)
    return f"data:image/png;base64,{base64.b64encode(buf).decode()}"


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

def _add_pixel_grid(frame: np.ndarray, cell_px: int = 4) -> np.ndarray:
    """Upscale binary GoL frame and add 1px black grid lines between cells."""
    h, w = frame.shape
    big = cv2.resize(frame.astype(np.float32), (w * cell_px, h * cell_px),
                     interpolation=cv2.INTER_NEAREST)
    big[::cell_px, :] = 0
    big[:, ::cell_px] = 0
    return big

def _make_sample(density, steps, sigma, seed=42):
    """Generate one synthetic GoL sample. Returns (image_float32, label)."""
    rng = np.random.default_rng(seed)
    grid = (rng.random((64, 128)) < density).astype(np.uint8)
    for _ in range(steps):
        grid = _gol_step(grid)
    grid_frame = _add_pixel_grid(grid, cell_px=4)
    small = cv2.resize(grid_frame, (64, 32), interpolation=cv2.INTER_AREA)
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
    subgraph focus["Autofocus (live)"]
        I["Camera\\n1280×720"] --> J["Find Focus\\nCenter"]
        J --> K["4-Phase Sweep\\nCoarse→Fine→\\nMicro→Ultra"]
        K --> L["Grid Angle\\nDetection"]
        L --> M["Deskew +\\nOLED Re-detect"]
        M --> N["Set Focus\\nv4l2-ctl"]
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

@rt("/docs")
def docs_page():
    model_path = BASE_DIR / "autofocus_model.safetensors"
    model_exists = model_path.exists()
    model_size = f"{model_path.stat().st_size / 1024:.1f} KB" if model_exists else "not trained"
    model_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(model_path.stat().st_mtime)) if model_exists else "—"

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

    # --- FFT demo: synthetic rotated grid at 15° ---
    fft_demo_angle = 15.0
    fft_demo_grid = _make_rotated_grid(fft_demo_angle, size=200, pitch=6, seed=42)
    fft_demo_grid_uri = _bgr_to_data_uri(fft_demo_grid, scale=1)

    # Plain FFT spectrum (no annotations)
    fft_demo_plain_uri = _fft_spectrum_data_uri(fft_demo_grid, scale=1)

    # Annotated FFT spectrum (with annular band + angle line)
    fft_demo_annotated_uri = _fft_spectrum_data_uri(
        fft_demo_grid, scale=1, show_angle=True,
    )

    # Deskewed result
    fft_demo_deskewed = _deskew_frame(fft_demo_grid, fft_demo_angle)
    fft_demo_deskewed_uri = _bgr_to_data_uri(fft_demo_deskewed, scale=1)

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
        Title("OLED Autofocus — System Documentation"),
        Style(CSS),
        Script(mermaid_js, type="module"),
        H1("OLED Autofocus // System Documentation"),
        nav_bar("docs"),
        Div(
            # ---- Intro ----
            P("Automated focus for a Logitech BRIO camera pointing at an SSD1306 128x64 OLED "
              "running Conway's Game of Life on an ESP32-C6. Combines Laplacian variance scoring "
              "with a four-phase progressive sweep, early bail on dominant peaks, "
              "and grid rotation detection for deskewed OLED re-detection."),
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
                Tr(Td("Focus control"), Td("Manual via v4l2-ctl: focus_absolute 0–255")),
                Tr(Td("Target display"), Td("SSD1306 0.96\" OLED, 128x64 pixels, I2C (GPIO6 SDA, GPIO7 SCL)")),
                Tr(Td("Microcontroller"), Td("ESP32-C6-DevKitC-1-N8, RISC-V, running Game of Life firmware")),
                Tr(Td("Focus scene"), Td("Blue OLED pixels on black + starburst calibration card behind")),
            ),

            # ---- End-to-End Flow ----
            H2("End-to-End Flow"),
            P("Two phases: offline CNN training on synthetic data (~35s), then live autofocus "
              "via progressive four-phase sweep. No real camera images needed for training."),
            Div(NotStr(f'<pre class="mermaid">\n{MERMAID_PIPELINE}\n</pre>'), cls="mermaid-wrap"),

            # ---- Autofocus Algorithm ----
            H2("Autofocus Algorithm"),
            P("Six-stage pipeline: Scramble, Detect, Coarse, Fine, Ultra, Focus. "
              "Each sweep phase uses a progressively larger crop around the detected focus center "
              "for better signal discrimination."),

            H3("Focus Detection"),
            P("HSV blue threshold finds bright clusters in the camera frame. "
              "The center point of the nearest cluster to frame center is used as the "
              "focus target — a single (cx, cy) coordinate, not a bounding box. "
              "Fallback: frame center."),

            H3("Four-Phase Sweep"),
            P("Each phase builds its own crop box around the focus center. "
              "Smaller crops give more focused Laplacian signal in early phases; "
              "larger crops provide better discrimination in later phases:"),
            Table(
                Tr(Th("Phase"), Th("Crop"), Th("Range"), Th("Step"), Th("Avg Frames")),
                Tr(Td("1. Coarse"), Td("20x20"), Td("0–255"), Td("20"), Td("3")),
                Tr(Td("2. Fine"), Td("30x30"), Td("best ± 15"), Td("5"), Td("5")),
                Tr(Td("3. Micro"), Td("40x40"), Td("best ± 5"), Td("2"), Td("5")),
                Tr(Td("4. Ultra"), Td("40x40"), Td("best ± 2"), Td("1"), Td("5")),
            ),

            H3("Early Bail"),
            P("After coarse and fine sweeps, if the best score is ", Code("≥ 1.5x"),
              " the second-best (with at least 3 results), the algorithm skips "
              "intermediate phases and jumps straight to ultra. "
              "Ultra always runs — it's only ~5 positions. This can cut sweep time significantly "
              "when the focus peak is unambiguous."),

            H3("Grid Rotation Detection (FFT)"),
            P("The OLED pixel grid creates a periodic pattern that produces sharp peaks "
              "in the 2D FFT magnitude spectrum. The angle from the DC center to the "
              "dominant peak equals the grid rotation angle. This works at any zoom where "
              "the grid is visible — even when individual pixels are too small for contour "
              "detection (< 3 camera pixels wide)."),

            Div(
                Div(
                    Img(src=fft_demo_grid_uri),
                    Div(f"Input grid ({fft_demo_angle:.0f}°)", cls="cap"),
                    cls="img-cell",
                ),
                Div(
                    Img(src=fft_demo_plain_uri),
                    Div("FFT magnitude", cls="cap"),
                    cls="img-cell",
                ),
                Div(
                    Img(src=fft_demo_annotated_uri),
                    Div(Span("Peak detection", cls="hi"), cls="cap"),
                    cls="img-cell",
                ),
                Div(
                    Img(src=fft_demo_deskewed_uri),
                    Div("Deskewed result", cls="cap"),
                    cls="img-cell",
                ),
                cls="img-grid cols-4",
            ),

            P("Algorithm:"),
            Ul(
                Li("Take a 200x200 crop around the focus center (more periods = sharper peaks)"),
                Li("Grayscale, multiply by 2D Hanning window (reduces spectral leakage)"),
                Li(Code("np.fft.fft2"), " → ", Code("fftshift"), " → ", Code("log1p(abs())"),
                   " = magnitude spectrum"),
                Li("Mask out DC region (radius < 5px from center)"),
                Li("Find dominant peak in upper half (conjugate symmetry)"),
                Li("Prominence check: peak must be ≥ 3× median (rejects flat/noisy spectra)"),
                Li(Code("atan2(dy, dx)"), " → angle in degrees, rotated 90° (peak ⊥ grid), "
                   "normalized to [-45°, 45°]"),
            ),
            P("Advantages over contour-based detection:"),
            Ul(
                Li(Strong("Zoom-invariant:"), " Works at any magnification where the grid is visible — "
                   "no minimum pixel size required"),
                Li(Strong("Single-peak aggregation:"), " One dominant peak instead of "
                   "voting across dozens of noisy contour angles"),
                Li(Strong("Sub-pixel pitch:"), " Even 2–3 camera pixels per OLED pixel "
                   "creates a measurable FFT peak"),
            ),
            P("If detected (|angle| > 0.5°), the frame is deskewed via ", Code("cv2.warpAffine"),
              " before OLED re-detection. This produces tighter, axis-aligned bounding boxes "
              "and cleaner OLED crops."),

            H3("OLED Re-detection"),
            P("After the sweep finds best focus and applies any offset, the OLED is "
              "re-detected on the (possibly deskewed) sharp frame using the full HSV "
              "pipeline. The post-autofocus photo and OLED crop use this refined bbox. "
              "If the OLED isn't found, the focus crop is used as fallback."),

            # ---- Scoring ----
            H2("Sharpness Scoring"),
            H3("OLED Detection (HSV)"),
            P("Classical CV pipeline finds the blue OLED region in the camera frame:"),
            Table(
                Tr(Th("Step"), Th("Details")),
                Tr(Td("Color space"), Td("BGR → HSV")),
                Tr(Td("Threshold"), Td("H: 90–130, S: 50–255, V: 30–255 (blue glow)")),
                Tr(Td("Morphology"), Td("Dilate with 7x7 rect kernel, 2 iterations")),
                Tr(Td("Contour"), Td("Largest contour, area > 500px, aspect ratio 1.3–3.0")),
                Tr(Td("Fallback"), Td("Focus crop bbox if OLED not detected")),
            ),
            H3("Laplacian Score"),
            P("Crops are extracted, resized to 64x32, CLAHE-normalized, then scored "
              "via Laplacian variance. Multi-frame averaging (3–5 frames per position) "
              "cancels OLED refresh flicker and sensor noise:"),
            Div("score = min(Laplacian(crop).var() / 25000, 1.0)", cls="formula"),

            # ---- SharpnessNet ----
            H2("SharpnessNet (CNN)"),
            P("A 3-layer CNN with global average pooling. Trained on synthetic Game of Life "
              "frames, predicts sharpness in [0, 1]. 3,585 parameters, 15 KB on disk:"),
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

            # ---- Training Data ----
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

            H3("Sharpness Label"),
            P("The label maps blur sigma to a sharpness score in [0, 1]:"),
            Div("label = 1 / (1 + σ²)", cls="formula"),
            Table(
                Tr(Th("σ (blur)"), Th("0.0"), Th("0.5"), Th("1.0"), Th("2.0"), Th("4.0")),
                Tr(Td("label"), Td("1.000", cls="mono"), Td("0.800", cls="mono"),
                   Td("0.500", cls="mono"), Td("0.200", cls="mono"), Td("0.059", cls="mono")),
            ),

            # ---- Training Loss ----
            H2("Training"),
            Table(
                Tr(Th("Parameter"), Th("Value")),
                Tr(Td("Optimizer"), Td("Adam")),
                Tr(Td("Learning rate"), Td(Code("1e-3"))),
                Tr(Td("Batch size"), Td("128")),
                Tr(Td("Training steps"), Td("200")),
                Tr(Td("Loss function"), Td("MSE (mean squared error)")),
                Tr(Td("Wall time"), Td("~30s on Raspberry Pi 5 CPU (tinygrad)")),
                Tr(Td("Model size"), Td(f"{model_size} (safetensors)")),
                Tr(Td("Last trained"), Td(model_date)),
            ),
            P("MSE loss over 200 Adam steps. Converges to ~0.01 in the first 80 steps:"),
            Div(NotStr(loss_svg), cls="chart-wrap"),

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

            # ---- Reproduce ----
            H2("Reproduce"),
            Pre(Code(
                "# Install dependencies\n"
                "pip install tinygrad opencv-python numpy\n"
                "sudo apt install v4l-utils\n"
                "\n"
                "# Train the model (~35s on RPi5)\n"
                "python camera/autofocus.py train\n"
                "\n"
                "# Run autofocus\n"
                "python camera/autofocus.py focus\n"
                "\n"
                "# Full diagnostic sweep (all 51 focus levels)\n"
                "python camera/autofocus.py sweep"
            )),
            H3("Adapting to Your Setup"),
            Ul(
                Li(Strong("Different camera:"), " Change ", Code("CAM_DEV"), " and ", Code("CAM_W/CAM_H/CAM_FPS"), " in autofocus.py"),
                Li(Strong("Different display:"), " Adjust HSV thresholds in ", Code("find_oled()"), " for your display's color"),
                Li(Strong("Different scene:"), " The Laplacian component works with any high-frequency content; "
                   "retrain the CNN on synthetic frames matching your scene"),
            ),

            # ---- Source ----
            H2("Source"),
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
