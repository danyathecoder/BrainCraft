"""Brain-state service: serves GET /state to the director and the HUD.

Two sources:
- "mock" (default): values come from the HUD sliders via POST /mock,
  so the game and the director can be built without a headset.
- "eeg": values come from the live EEG pipeline in eeg/ (P1, TODO).

Run:  uvicorn state_service:app --port 8000
      BRAINCRAFT_SOURCE=eeg uvicorn state_service:app --port 8000
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from contracts import BrainState

SOURCE = os.environ.get("BRAINCRAFT_SOURCE", "mock")
HUD_PAGE = Path(__file__).parent / "hud" / "index.html"

_lock = threading.Lock()
_state = BrainState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if SOURCE == "eeg":
        # TODO(P1): start the EEG pipeline in a background thread that
        # calls publish(BrainState(...)) about once per second.
        raise NotImplementedError("EEG source not wired yet, use mock")
    threading.Thread(target=_mock_heartbeat, daemon=True).start()
    yield


app = FastAPI(title="BrainCraft state service", lifespan=lifespan)


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
    quality: str | None = None
    artifact: str | None = None
    jaw_clench: bool | None = None


@app.get("/state")
def get_state() -> dict:
    data = current().to_dict()
    data["source"] = SOURCE
    return data


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


@app.get("/")
def hud() -> FileResponse:
    return FileResponse(HUD_PAGE)


def _mock_heartbeat() -> None:
    """Keep the timestamp fresh so consumers see a live stream."""
    while True:
        with _lock:
            _state.t = time.time()
        time.sleep(1.0)
