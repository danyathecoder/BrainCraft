"""Rehab mode director: spatial staircase + fatigue pacing.

Visual scanning training inspired by therapy for post-stroke spatial
neglect (left side neglected). Pure logic: time is passed in.

Trials: one target at a time. The next target appears N s after the
previous spawn (start N = 4), but never while a target is still up and
not sooner than `min_gap_s` after the previous one ended (hit or miss).
Side drawn with p_left (start 0.5). Right targets stay at a fixed
moderate eccentricity (`right_ecc_deg`, 35 deg) as a stable control
condition, so left/right differences in the report reflect the left
staircase, not a moving right target.

Spatial staircase (behaviour-driven), over the last 5 LEFT targets:
  >= 4 hits -> left eccentricity +1 step, p_left +0.1 (max 0.7), cues off
  <= 2 hits -> left eccentricity -1 step, cues on for the next 5 left
               targets (Glowing + positional sound at the target)
  the window is cleared after every step decision.

Fatigue pacing (EEG-driven, only seconds with quality == "ok" count):
  fatigue_z > 1.5 for 20 s or engagement_z < -1.5 for 30 s
  -> 45 s rest phase, then N += 0.5 s; refractory 2 min after a rest.

Challenge: engagement_z in [-0.5, 1.0] and hit rate > 85% over the
last 10 trials -> N -= 0.5 s (floor 2 s); the trial window is cleared
afterwards, so the next step needs 10 fresh trials.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass

from contracts import BrainState
from director.base import Decision, HoldTimer, SpawnSpec
from mc import config


@dataclass
class RehabParams:
    spawn_interval_s: float = 4.0
    min_interval_s: float = 2.0
    max_interval_s: float = 6.0
    interval_step_s: float = 0.5
    min_gap_s: float = 1.0  # pause after a trial ends before the next spawn
    lead_in_s: float = 2.0  # first spawn after session start
    p_left: float = 0.5
    p_left_step: float = 0.1
    p_left_max: float = 0.7
    left_ecc_step: int = 0  # index into config.ECCENTRICITIES
    right_ecc_deg: float = 35.0
    staircase_window: int = 5
    staircase_up_hits: int = 4
    staircase_down_hits: int = 2
    cue_trials: int = 5  # cued left targets after a step down
    fatigue_z_threshold: float = 1.5
    fatigue_hold_s: float = 20.0
    engagement_z_low: float = -1.5
    engagement_hold_s: float = 30.0
    rest_s: float = 45.0
    rest_refractory_s: float = 120.0
    band_low: float = -0.5
    band_high: float = 1.0
    challenge_window: int = 10
    challenge_hit_rate: float = 0.85


class RehabDirector:
    mode = "rehab"

    def __init__(self, params: RehabParams | None = None, seed: int | None = None,
                 eccentricities: tuple = config.ECCENTRICITIES):
        self.p = params or RehabParams()
        self.ecc = tuple(eccentricities)
        self.rng = random.Random(seed)
        self.spawn_interval_s = self.p.spawn_interval_s
        self.p_left = self.p.p_left
        self.left_ecc_step = self.p.left_ecc_step
        self.cues_left = 0  # remaining cued left targets
        self.left_window: deque[bool] = deque(maxlen=self.p.staircase_window)
        self.recent: deque[bool] = deque(maxlen=self.p.challenge_window)
        self.fatigue_timer = HoldTimer(self.p.fatigue_hold_s)
        self.engagement_timer = HoldTimer(self.p.engagement_hold_s)
        self.rest_until: float | None = None
        self.last_rest_end: float | None = None
        self._last_spawn: float | None = None
        self._last_end: float | None = None
        self._next_ok: float | None = None
        self._pending: list[Decision] = []

    # --- parameters -----------------------------------------------------

    @property
    def left_ecc_deg(self) -> float:
        return self.ecc[self.left_ecc_step]

    @property
    def resting(self) -> bool:
        return self.rest_until is not None

    @property
    def cues_on(self) -> bool:
        return self.cues_left > 0

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "resting": self.resting,
            "spawn_interval_s": self.spawn_interval_s,
            "p_left": round(self.p_left, 2),
            "left_ecc_deg": float(self.left_ecc_deg),
        }

    # --- interface ------------------------------------------------------

    def start(self, now: float) -> list[Decision]:
        self._next_ok = now + self.p.lead_in_s
        return [Decision("rehab_start", self.status() | {"right_ecc_deg": self.p.right_ecc_deg},
                         reason=(f"rehab session: one target every {self.spawn_interval_s:.1f} s, "
                                 f"p_left {self.p_left:.1f}, left targets at {self.left_ecc_deg}°, "
                                 f"right targets at {self.p.right_ecc_deg:g}°"))]

    def _drain(self) -> list[Decision]:
        out, self._pending = self._pending, []
        return out

    def on_trial_result(self, side: str, hit: bool, now: float, kind: str = "target",
                        ecc_deg: float | None = None) -> list[Decision]:
        """Called after every target: updates the spatial staircase."""
        out = self._drain()
        if kind != "target":
            return out
        self._last_end = now
        self.recent.append(hit)
        if side != "L":
            return out
        self.left_window.append(hit)
        if len(self.left_window) < self.p.staircase_window:
            return out
        hits = sum(self.left_window)
        n = len(self.left_window)
        if hits >= self.p.staircase_up_hits:
            out += self._step_up(hits, n)
            self.left_window.clear()
        elif hits <= self.p.staircase_down_hits:
            out += self._step_down(hits, n)
            self.left_window.clear()
        return out

    def _step_up(self, hits: int, n: int) -> list[Decision]:
        old_ecc, old_p = self.left_ecc_deg, self.p_left
        self.left_ecc_step = min(self.left_ecc_step + 1, len(self.ecc) - 1)
        self.p_left = round(min(self.p_left + self.p.p_left_step, self.p.p_left_max), 2)
        had_cues = self.cues_on
        self.cues_left = 0
        if self.left_ecc_deg == old_ecc and self.p_left == old_p and not had_cues:
            return []  # already at the top of the staircase
        parts = []
        if self.left_ecc_deg != old_ecc:
            parts.append(f"left eccentricity {old_ecc}° → {self.left_ecc_deg}°")
        else:
            parts.append(f"left eccentricity stays at max {self.left_ecc_deg}°")
        if self.p_left != old_p:
            parts.append(f"p_left {old_p:.1f} → {self.p_left:.1f}")
        if had_cues:
            parts.append("cues off")
        return [Decision(
            "staircase_up",
            {"left_ecc_deg": self.left_ecc_deg, "p_left": self.p_left, "cues": False},
            reason=f"{hits}/{n} left hits ≥ {self.p.staircase_up_hits} → " + ", ".join(parts),
        )]

    def _step_down(self, hits: int, n: int) -> list[Decision]:
        old_ecc = self.left_ecc_deg
        self.left_ecc_step = max(self.left_ecc_step - 1, 0)
        self.cues_left = self.p.cue_trials
        if self.left_ecc_deg != old_ecc:
            what = f"left eccentricity {old_ecc}° → {self.left_ecc_deg}°"
        else:
            what = f"left eccentricity stays at min {self.left_ecc_deg}°"
        return [Decision(
            "staircase_down",
            {"left_ecc_deg": self.left_ecc_deg, "cues": True, "cue_trials": self.cues_left},
            reason=(f"{hits}/{n} left hits ≤ {self.p.staircase_down_hits} → {what}, "
                    f"cues on for the next {self.cues_left} left targets"),
        )]

    def on_state(self, state: BrainState, now: float) -> list[Decision]:
        """Called about once per second with the latest brain state."""
        out = self._drain()
        if self.resting:
            if now >= self.rest_until:
                out += self._end_rest(now)
            return out
        ok = state.quality == "ok"
        fatigued = self.fatigue_timer.update(state.fatigue_z > self.p.fatigue_z_threshold, now, ok)
        disengaged = self.engagement_timer.update(state.engagement_z < self.p.engagement_z_low, now, ok)
        refractory = (self.last_rest_end is not None
                      and now - self.last_rest_end < self.p.rest_refractory_s)
        if (fatigued or disengaged) and not refractory:
            if fatigued:
                why = (f"fatigue_z > {self.p.fatigue_z_threshold:g} for {self.p.fatigue_hold_s:g} s "
                       f"(now {state.fatigue_z:+.1f})")
            else:
                why = (f"engagement_z < {self.p.engagement_z_low:g} for {self.p.engagement_hold_s:g} s "
                       f"(now {state.engagement_z:+.1f})")
            out += self._start_rest(now, why)
            return out
        if ok:
            out += self._challenge(state, now)
        return out

    def _start_rest(self, now: float, why: str) -> list[Decision]:
        self.rest_until = now + self.p.rest_s
        self.fatigue_timer.reset()
        self.engagement_timer.reset()
        return [Decision("rest_start", {"duration_s": self.p.rest_s},
                         reason=f"{why} → {self.p.rest_s:g} s rest phase")]

    def _end_rest(self, now: float) -> list[Decision]:
        self.rest_until = None
        self.last_rest_end = now
        self._last_end = now
        self._next_ok = now + self.p.lead_in_s
        self.fatigue_timer.reset()
        self.engagement_timer.reset()
        self.recent.clear()
        old = self.spawn_interval_s
        self.spawn_interval_s = min(old + self.p.interval_step_s, self.p.max_interval_s)
        out = [Decision("rest_end", {"duration_s": self.p.rest_s},
                        reason=f"{self.p.rest_s:g} s rest over → back to the watch "
                               f"(no new rest for {self.p.rest_refractory_s:g} s)")]
        if self.spawn_interval_s != old:
            out.append(Decision("spawn_interval", {"old": old, "new": self.spawn_interval_s},
                                reason=f"after a fatigue rest → slower pacing, spawn interval "
                                       f"{old:.1f} → {self.spawn_interval_s:.1f} s"))
        return out

    def _challenge(self, state: BrainState, now: float) -> list[Decision]:
        if len(self.recent) < self.p.challenge_window:
            return []
        if not (self.p.band_low <= state.engagement_z <= self.p.band_high):
            return []
        rate = sum(self.recent) / len(self.recent)
        if rate <= self.p.challenge_hit_rate:
            return []
        old = self.spawn_interval_s
        new = max(old - self.p.interval_step_s, self.p.min_interval_s)
        if new == old:
            return []
        self.spawn_interval_s = new
        n = len(self.recent)
        self.recent.clear()
        return [Decision("spawn_interval", {"old": old, "new": new},
                         reason=(f"engagement_z {state.engagement_z:+.1f} in "
                                 f"[{self.p.band_low:g}, {self.p.band_high:g}] and hit rate "
                                 f"{rate:.0%} > {self.p.challenge_hit_rate:.0%} over last {n} trials "
                                 f"→ faster: spawn interval {old:.1f} → {new:.1f} s"))]

    def due_spawns(self, now: float, n_active: int) -> list[SpawnSpec]:
        """What to spawn now: at most one target, only when none is up."""
        if self._next_ok is None:
            self._next_ok = now + self.p.lead_in_s
        if self.resting or n_active > 0 or now < self._next_ok:
            return []
        if self._last_spawn is not None and now < self._last_spawn + self.spawn_interval_s:
            return []
        if self._last_end is not None and now < self._last_end + self.p.min_gap_s:
            return []
        self._last_spawn = now
        if self.rng.random() < self.p_left:
            cued = self.cues_left > 0
            if cued:
                self.cues_left -= 1
                if self.cues_left == 0:
                    self._pending.append(Decision(
                        "cues_off", {}, reason=f"{self.p.cue_trials} cued left targets shown → cues off"))
            return [SpawnSpec("L", float(self.left_ecc_deg), cued=cued)]
        return [SpawnSpec("R", float(self.p.right_ecc_deg))]
