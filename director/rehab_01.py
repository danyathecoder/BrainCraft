"""Rehab 01 director: right hemifield, then the whole view, then ALI.

Pure logic: time is passed in. One target at a time. An unhit target
lives 60 s, then the next appears 4 s after that miss. A hit brings
the next target in 1 s.

  1. For the first `right_only_s` (60 s) every target is on the player's
     right, at a random eccentricity inside the field of view.
  2. After that, targets are drawn from the whole field of view: either
     side, any of those eccentricities.
  3. Once `ali_after_spawns` (10) targets have appeared, each good-quality
     brain state updates ALI. While ALI is below 0, new targets stay on
     the left. ALI at or above 0 returns to the whole field of view.

The first minute wins over ALI: a negative ALI does not pull targets
left until those 60 s are over. Seconds with quality "bad" do not update
ALI. ALI is the raw index (PO7 - PO8) / (PO7 + PO8), not a z-score.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from contracts import BrainState
from director.base import Decision, SpawnSpec
from mc import config

_P_LEFT = {"right": 0.0, "full": 0.5, "left": 1.0}


@dataclass
class Rehab01Params:
    spawn_interval_s: float = 4.0  # next target when the previous was not hit
    hit_gap_s: float = 1.0  # next target after a hit
    max_active: int = 1
    target_lifetime_s: float = 60.0  # how long an unhit target stays
    lead_in_s: float = 2.0  # first spawn after session start
    right_only_s: float = 60.0
    ali_after_spawns: int = 10
    ali_threshold: float = 0.0  # left-only while ali is strictly below this


class Rehab01Director:
    mode = "rehab_01"

    def __init__(self, params: Rehab01Params | None = None, seed: int | None = None,
                 eccentricities: tuple = config.ECCENTRICITIES):
        self.p = params or Rehab01Params()
        self.ecc = tuple(float(e) for e in eccentricities)
        self.rng = random.Random(seed)
        self.spawn_interval_s = self.p.spawn_interval_s
        self.ali = 0.0
        self._ali_known = False
        self._watching = False
        self.n_spawned = 0
        self._zone = "right"
        self._t0: float | None = None
        self._last_spawn: float | None = None
        self._last_end: float | None = None
        self._next_ok: float | None = None
        self._last_left_ecc = self.ecc[0]
        self._pending: list[Decision] = []

    @property
    def resting(self) -> bool:
        return False

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "resting": False,
            "spawn_interval_s": self.spawn_interval_s,
            "p_left": _P_LEFT[self._zone],
            "left_ecc_deg": float(self._last_left_ecc),
            "zone": self._zone,
            "ali": round(self.ali, 3) if self._ali_known else None,
            "targets": self.n_spawned,
        }

    def start(self, now: float) -> list[Decision]:
        self._t0 = now
        self._next_ok = now + self.p.lead_in_s
        self._zone = "right"
        return [Decision(
            "rehab_01_start",
            {"right_only_s": self.p.right_only_s, "ali_after_spawns": self.p.ali_after_spawns},
            reason=(f"rehab 01: right side for {self.p.right_only_s:g} s, then the whole view; "
                    f"after {self.p.ali_after_spawns} targets, ALI < 0 moves targets left"),
        )]

    def _drain(self) -> list[Decision]:
        out, self._pending = self._pending, []
        return out

    def on_trial_result(self, side: str, hit: bool, now: float, kind: str = "target",
                        ecc_deg: float | None = None) -> list[Decision]:
        out = self._drain()
        if kind != "target":
            return out
        self._last_end = now
        gap = self.p.hit_gap_s if hit else self.spawn_interval_s
        self._next_ok = now + gap
        return out

    def on_state(self, state: BrainState, now: float) -> list[Decision]:
        out = self._drain()
        if self._watching and state.quality == "ok":
            self.ali = float(state.ali)
            self._ali_known = True
        out += self._sync_zone(now)
        return out

    def _zone_at(self, now: float) -> str:
        if self._t0 is None or now - self._t0 < self.p.right_only_s:
            return "right"
        if self._watching and self._ali_known and self.ali < self.p.ali_threshold:
            return "left"
        return "full"

    def _sync_zone(self, now: float) -> list[Decision]:
        zone = self._zone_at(now)
        if zone == self._zone:
            return []
        prev, self._zone = self._zone, zone
        return [Decision("spawn_zone", {"zone": zone, "ali": round(self.ali, 3)},
                         reason=self._zone_reason(zone, prev))]

    def _zone_reason(self, zone: str, prev: str) -> str:
        if zone == "left" and prev == "right":
            return (f"first {self.p.right_only_s:g} s over and ALI {self.ali:+.2f} < 0 "
                    f"→ targets only on the left")
        if zone == "left":
            return f"ALI {self.ali:+.2f} < 0 after {self.n_spawned} targets → targets only on the left"
        if prev == "right":
            return f"first {self.p.right_only_s:g} s over → targets across the whole field of view"
        return f"ALI {self.ali:+.2f} ≥ 0 → targets across the whole field of view"

    def due_spawns(self, now: float, n_active: int) -> list[SpawnSpec]:
        if self._t0 is None:
            self._t0 = now
        if self._next_ok is None:
            self._next_ok = now + self.p.lead_in_s
        self._pending.extend(self._sync_zone(now))
        if n_active >= self.p.max_active or now < self._next_ok:
            return []
        self._last_spawn = now
        self._next_ok = now + self.spawn_interval_s
        spec = self._pick()
        self.n_spawned += 1
        if spec.side == "L":
            self._last_left_ecc = spec.ecc_deg
        if not self._watching and self.n_spawned >= self.p.ali_after_spawns:
            self._watching = True
            still_right = self._zone == "right"
            tail = (f"; targets stay on the right until {self.p.right_only_s:g} s"
                    if still_right else "")
            self._pending.append(Decision(
                "ali_watch",
                {"targets": self.n_spawned},
                reason=f"{self.n_spawned} targets spawned → watching ALI{tail}",
            ))
        return [spec]

    def _pick(self) -> SpawnSpec:
        ecc = float(self.rng.choice(self.ecc))
        if self._zone == "left":
            side = "L"
        elif self._zone == "right":
            side = "R"
        else:
            side = "L" if self.rng.random() < 0.5 else "R"
        return SpawnSpec(side, ecc, lifetime_s=self.p.target_lifetime_s)
