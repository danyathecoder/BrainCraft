"""Brain-state service: the hub between EEG, game loop and HUD.

Endpoints:
- GET  /state          brain state (BrainState), ~1 Hz
- POST /mock           set mock values (mock source only, HUD sliders)
- GET/POST /status     game status (GameStatus), game loop -> HUD
- GET/POST /decisions  director decisions with reasons, game loop -> HUD
- GET/POST /mode       "rehab" | "rehab_01" | "fun", HUD button -> game loop
- GET  /raw            last seconds of filtered EEG for the HUD scope (eeg source)
- GET  /               HUD page

Brain-state sources (BRAINCRAFT_SOURCE):
- "mock" (default): values come from the HUD sliders via POST /mock.
- "eeg": values come from eeg.pipeline.run(); its input is chosen with
  BRAINCRAFT_EEG_INPUT = "lsl" (headset or synthetic LSL stream,
  default) or "synthetic" (in-process generator, no LSL needed).
  Without a baseline file it calibrates first; BRAINCRAFT_CALIBRATION=quick
  uses the first 30 clean seconds instead (not saved).

Run:  uvicorn state_service:app --port 8000
      BRAINCRAFT_SOURCE=eeg uvicorn state_service:app --port 8000
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from contracts import MODES, BrainState, GameStatus

SOURCE = os.environ.get("BRAINCRAFT_SOURCE", "mock")
EEG_INPUT = os.environ.get("BRAINCRAFT_EEG_INPUT", "lsl")
HUD_PAGE = Path(__file__).parent / "hud" / "index.html"
MAX_DECISIONS = 200

_lock = threading.Lock()
_state = BrainState()
_status = GameStatus()
_mode = "rehab"
_decisions: deque[dict] = deque(maxlen=MAX_DECISIONS)
_stop = threading.Event()
_stream = None  # EEGStream, set by the pipeline (eeg source only)
RAW_DECIMATE = 2  # 250 Hz -> 125 Hz for the HUD


def _set_stream(stream) -> None:
    global _stream
    _stream = stream


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if SOURCE == "eeg":
        from eeg.pipeline import run as run_pipeline

        threading.Thread(
            target=run_pipeline,
            args=(publish, _stop),
            kwargs={"source": EEG_INPUT, "on_stream": _set_stream},
            daemon=True,
        ).start()
    else:
        threading.Thread(target=_mock_heartbeat, daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="BrainCraft state service", lifespan=lifespan)


# --- brain state ---------------------------------------------------------


def publish(state: BrainState) -> None:
    """Replace the current state. Called by the EEG pipeline thread."""
    global _state
    with _lock:
        _state = state


def current() -> BrainState:
    with _lock:
        return _state


class MockUpdate(BaseModel):
    engagement_z: float | None = None
    fatigue_z: float | None = None
    alpha_low_z: float | None = None
    ali: float | None = None
    quality: str | None = None
    artifact: str | None = None
    jaw_clench: bool | None = None


@app.get("/state")
def get_state() -> dict:
    data = current().to_dict()
    data["source"] = SOURCE
    return data


@app.get("/raw")
def get_raw(seconds: float = 5.0) -> dict:
    """Last `seconds` of EEG, detrended + 50 Hz notch + 1-40 Hz, in uV.

    For looking at the signal (electrode fit, blinks, jaw), not analysis.
    """
    stream = _stream
    if SOURCE != "eeg":
        return {"available": False, "reason": "raw EEG only with BRAINCRAFT_SOURCE=eeg"}
    if stream is None or stream.total_samples == 0:
        return {"available": False, "reason": "waiting for the EEG stream"}
    import numpy as np

    from eeg.features import channel_status, preprocess
    from eeg.layout import EEG_CHANNELS

    seconds = min(max(seconds, 1.0), 8.0)
    fs = float(stream.fs)
    n = int(seconds * fs)
    window = stream.window(seconds)
    have = min(stream.total_samples, len(window))
    raw = window[-have:]
    x = preprocess(raw, fs) if have >= fs else np.zeros((0, len(EEG_CHANNELS)))
    rms = np.sqrt((x**2).mean(axis=0)) if len(x) else np.zeros(len(EEG_CHANNELS))
    # saturated (amplifier limit: electrode/reference contact), flat, noisy, ok
    flags = channel_status(raw, x) if len(x) else ["flat"] * len(EEG_CHANNELS)
    return {
        "available": True,
        "fs": fs / RAW_DECIMATE,
        "seconds": seconds,
        "missing_s": max(0.0, (n - len(x)) / fs),
        "age_s": round(stream.seconds_since_data, 2),
        "channels": EEG_CHANNELS,
        "data": np.round(x[::RAW_DECIMATE].T, 1).tolist(),
        "rms": [round(float(r), 1) for r in rms],
        "flags": flags,
    }


@app.post("/mock")
def set_mock(update: MockUpdate) -> dict:
    """Set mock values (only in mock mode). Unset fields keep their value."""
    if SOURCE != "mock":
        return {"ok": False, "error": "service is not in mock mode"}
    fields = current().to_dict()
    fields.update(update.model_dump(exclude_none=True))
    fields["t"] = time.time()
    publish(BrainState(**fields))
    return {"ok": True}


def _mock_heartbeat() -> None:
    """Keep the timestamp fresh so consumers see a live stream."""
    while not _stop.is_set():
        with _lock:
            _state.t = time.time()
        time.sleep(1.0)


# --- game status, decisions, mode ---------------------------------------


@app.get("/status")
def get_status() -> dict:
    with _lock:
        return _status.to_dict()


@app.post("/status")
def set_status(status: dict) -> dict:
    """Game loop posts its GameStatus about once per second."""
    global _status
    known = {k: v for k, v in status.items() if k in GameStatus.__dataclass_fields__}
    with _lock:
        _status = GameStatus(**known)
    return {"ok": True}


class DecisionIn(BaseModel):
    value: str
    reason: str
    mode: str | None = None
    t: float | None = None


@app.post("/decisions")
def add_decision(decision: DecisionIn) -> dict:
    if not decision.reason:
        raise HTTPException(422, "every decision needs a reason")
    row = decision.model_dump()
    row["t"] = row["t"] or time.time()
    with _lock:
        _decisions.append(row)
    return {"ok": True}


@app.get("/decisions")
def get_decisions(limit: int = 20) -> list[dict]:
    """Newest first."""
    with _lock:
        return list(reversed(_decisions))[:limit]


class ModeIn(BaseModel):
    mode: str


@app.get("/mode")
def get_mode() -> dict:
    with _lock:
        return {"mode": _mode}


@app.post("/mode")
def set_mode(body: ModeIn) -> dict:
    global _mode
    if body.mode not in MODES:
        raise HTTPException(422, f"mode must be one of {MODES}")
    with _lock:
        _mode = body.mode
    return {"ok": True, "mode": _mode}


# --- HUD -----------------------------------------------------------------


@app.get("/")
def hud() -> FileResponse:
    return FileResponse(HUD_PAGE)
