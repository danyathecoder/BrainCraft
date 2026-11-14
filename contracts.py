"""Shared contracts between the EEG, director, game and HUD parts.

Agreed at the start of the hackathon. Do not change field names
without telling the whole team: every part reads these.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Literal

Quality = Literal["ok", "bad"]
Artifact = Literal["none", "blink", "jaw", "motion"]


@dataclass
class BrainState:
    """Payload of GET /state, published at ~1 Hz.

    All *_z values are z-scores against the player's own calibration
    baseline (0 = baseline, +1 = one standard deviation above).
    """

    t: float = field(default_factory=time.time)
    engagement_z: float = 0.0  # beta / (alpha + theta), Fz + Cz
    fatigue_z: float = 0.0  # (theta + alpha) / beta, Fz
    alpha_low_z: float = 0.0  # lower alpha 8-10 Hz, Pz / Oz
    quality: Quality = "ok"
    artifact: Artifact = "none"
    jaw_clench: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# Events CSV: one row per event, one file per session (sessions/).
EVENT_FIELDS = [
    "t",  # unix time, seconds
    "event",  # one of EVENT_TYPES
    "target_id",  # e.g. "t17", empty if not about a target
    "side",  # "L" or "R", empty if not about a target
    "ecc_deg",  # target eccentricity in degrees from straight ahead
    "rt_ms",  # reaction time for hits
    "value",  # free numeric/text value (e.g. yaw, new spawn interval)
    "reason",  # human-readable why, for director decisions
]

EVENT_TYPES = {
    "spawn",
    "hit",
    "miss",
    "false_tag",
    "rest_start",
    "rest_end",
    "decision",
    "yaw",
}

Side = Literal["L", "R"]
Mode = Literal["rehab", "fun"]
