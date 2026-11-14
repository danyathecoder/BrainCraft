"""Rehab mode director: spatial staircase + fatigue pacing (P2).

Built first. Rules from the plan:

Trials: one target every N s (start N = 4). Side drawn with p_left
(start 0.5).

Spatial staircase (behaviour-driven), over the last 5 left targets:
  >= 4 hits -> left eccentricity +1 step, p_left +0.1 (max 0.7)
  <= 2 hits -> left eccentricity -1 step, cues on for next left targets

Fatigue pacing (EEG-driven, only when quality == "ok"):
  fatigue_z > 1.5 for 20 s or engagement_z < -1.5 for 30 s
  -> 45 s rest phase, then N += 0.5 s; refractory 2 min

Challenge: engagement_z in band and hit rate > 85% -> N -= 0.5 s
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts import BrainState
from director.base import Decision
from mc import config


@dataclass
class RehabParams:
    spawn_interval_s: float = 4.0
    p_left: float = 0.5
    p_left_max: float = 0.7
    left_ecc_step: int = 0  # index into config.ECCENTRICITIES
    staircase_window: int = 5
    fatigue_z_threshold: float = 1.5
    fatigue_hold_s: float = 20.0
    engagement_z_low: float = -1.5
    engagement_hold_s: float = 30.0
    rest_s: float = 45.0
    rest_refractory_s: float = 120.0


class RehabDirector:
    def __init__(self, params: RehabParams | None = None):
        self.p = params or RehabParams()

    @property
    def left_ecc_deg(self) -> int:
        return config.ECCENTRICITIES[self.p.left_ecc_step]

    def on_trial_result(self, side: str, hit: bool) -> list[Decision]:
        """Called after every target: updates the spatial staircase."""
        # TODO(P2): keep last N left results, apply the staircase rule.
        return []

    def on_state(self, state: BrainState) -> list[Decision]:
        """Called every second with the latest brain state."""
        # TODO(P2): fatigue pacing + challenge rules with hold timers.
        return []
