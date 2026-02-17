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
_af_settle_s = 0.5                     # settle time between focus moves (seconds)
_af_offset = 0                         # focus offset applied after sweep (compensates scoring bias)
_NORM_SIZE = (64, 32)                  # fixed crop size (w, h) for scale invariance
_LAPLACIAN_DIVISOR = 25000.0           # tuned for 64x32 CLAHE-normalized OLED crop (bumped to avoid saturation at 1.0)
_EDGE_MARGIN_PX = 2                    # bbox within this of frame edge = clipped

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

def _save_af_photo(frame: np.ndarray, ts: int, suffix: str) -> Path:
    path = PHOTOS_DIR / f"{ts}_{suffix}.jpg"
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

        progress("Detecting OLED...")
        log("Detecting OLED region at focus=30...")
        cam.set_ctrl("focus_absolute", 30)
        time.sleep(_af_settle_s)
        frame = _capture_frame()
        bbox = _find_oled_rect(frame)
        if bbox:
            x, y, w, h = bbox
            log(f"  OLED found: {w}x{h} at ({x},{y})", "af-good")
            if _check_bbox_clipping(frame, bbox):
                log(f"  Warning: OLED bbox touches frame edge (partial visibility)", "af-warn")
        else:
            log("  OLED not detected, using center crop", "af-warn")
            bbox = _center_crop_rect(frame)

        # Coarse sweep — every 20 steps across full range, 3-frame avg
        _af_stage = 3  # Coarse
        log("")
        log("--- Coarse sweep (3-frame avg) ---", "af-header")
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

        # Fine sweep — ±15 around best, step 5, 5-frame avg
        _af_stage = 4  # Fine
        log("")
        log("--- Fine sweep (5-frame avg) ---", "af-header")
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

        # Micro sweep — ±5 around best, step 2, 5-frame avg
        log("")
        log("--- Micro sweep (5-frame avg) ---", "af-header")
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

        # Ultra sweep — ±2 around micro best, step 1, 5-frame avg
        _af_stage = 5  # Ultra
        log("")
        log("--- Ultra sweep (5-frame avg, step 1) ---", "af-header")
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
                **restore,
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
                    style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;",
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
            list(PHOTOS_DIR.glob("*_oled.jpg")) + list(PHOTOS_DIR.glob("*_meta.json"))
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
    if not filename.endswith(".jpg"):
        return Response("Not found", status_code=404)
    path = PHOTOS_DIR / filename
    if not path.exists():
        return Response("Not found", status_code=404)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
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
