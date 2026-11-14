"""Fun mode director: tension arcs. Pure logic, time passed in.

Goal: keep engagement_z in [-0.5, 1.0].
  too calm (engagement_z < -0.5) for 15 s -> escalate one level:
    faster spawns, several targets at once, wider angles, cave sounds,
    skeletons with AI walking toward the player, sheep distractors (shooting a
    sheep = false_tag), darkness
  too hyped (engagement_z > 1.0) for 15 s -> ease off one level
  overloaded (fatigue_z > 1.5 for 5 s, or 3 misses in a row)
    -> 20 s breather (no spawns, darkness cleared), then a 5-mob wave
  jaw clench -> "flare": all targets glow for 5 s, 30 s cooldown
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from contracts import BrainState
from director.base import Decision, HoldTimer, SpawnSpec
from mc import config

# level -> (spawn interval s, max targets at once, max eccentricity index,
#           mob share, sheep share, darkness)
LEVELS = (
    (3.0, 1, 1, 0.0, 0.0, False),
    (2.5, 2, 2, 0.0, 0.0, False),
    (2.5, 2, 3, 0.3, 0.2, False),
    (2.0, 3, 3, 0.35, 0.2, True),
    (1.5, 3, 3, 0.4, 0.25, True),
)
LEVEL_TEXT = (
    "calm watch",
    "faster spawns, two targets at once, cave sounds",
    "skeletons walk toward you, sheep distractors",
    "three targets at once, darkness",
    "maximum pace",
)


@dataclass
class FunParams:
    band_low: float = -0.5
    band_high: float = 1.0
    calm_hold_s: float = 15.0
    hype_hold_s: float = 15.0
    fatigue_z_threshold: float = 1.5
    fatigue_hold_s: float = 5.0
    miss_streak: int = 3
    breather_s: float = 20.0
    wave_size: int = 5
    overload_cooldown_s: float = 45.0
    flare_s: float = 5.0
    flare_cooldown_s: float = 30.0
    darkness_s: int = 30
    lead_in_s: float = 2.0
    target_lifetime_s: float = config.TARGET_LIFETIME_S
    mob_lifetime_s: float = 14.0
    sheep_lifetime_s: float = 8.0


class FunDirector:
    mode = "fun"

    def __init__(self, params: FunParams | None = None, seed: int | None = None,
                 eccentricities: tuple = config.ECCENTRICITIES):
        self.p = params or FunParams()
        self.ecc = tuple(eccentricities)
        self.rng = random.Random(seed)
        self.level = 0
        self.calm_timer = HoldTimer(self.p.calm_hold_s)
        self.hype_timer = HoldTimer(self.p.hype_hold_s)
        self.fatigue_timer = HoldTimer(self.p.fatigue_hold_s)
        self.misses_in_row = 0
        self.breather_until: float | None = None
        self.last_breather_end: float | None = None
        self.last_flare: float | None = None
        self.pending_wave = 0
        self._next_spawn: float | None = None
        self._pending: list[Decision] = []

    # --- parameters -----------------------------------------------------

    @property
    def spawn_interval_s(self) -> float:
        return LEVELS[self.level][0]

    @property
    def max_active(self) -> int:
        return LEVELS[self.level][1]

    @property
    def max_ecc_deg(self) -> float:
        return float(self.ecc[min(LEVELS[self.level][2], len(self.ecc) - 1)])

    @property
    def resting(self) -> bool:
        return self.breather_until is not None

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "resting": self.resting,
            "spawn_interval_s": self.spawn_interval_s,
            "p_left": 0.5,
            "left_ecc_deg": self.max_ecc_deg,
            "level": self.level,
        }

    # --- interface ------------------------------------------------------

    def start(self, now: float) -> list[Decision]:
        self._next_spawn = now + self.p.lead_in_s
        return [Decision("fun_start", {"level": self.level},
                         reason=f"fun mode: keep engagement_z in [{self.p.band_low:g}, "
                                f"{self.p.band_high:g}], level {self.level} ({LEVEL_TEXT[self.level]})")]

    def _drain(self) -> list[Decision]:
        out, self._pending = self._pending, []
        return out

    def on_trial_result(self, side: str, hit: bool, now: float, kind: str = "target",
                        ecc_deg: float | None = None) -> list[Decision]:
        out = self._drain()
        if kind == "sheep":
            return out  # false tags are scored by the game, not the director
        self.misses_in_row = 0 if hit else self.misses_in_row + 1
        if self.misses_in_row >= self.p.miss_streak and self._may_overload(now):
            out += self._start_breather(now, f"{self.misses_in_row} misses in a row")
        return out

    def on_state(self, state: BrainState, now: float) -> list[Decision]:
        out = self._drain()
        if state.jaw_clench and (self.last_flare is None
                                 or now - self.last_flare >= self.p.flare_cooldown_s):
            self.last_flare = now
            out.append(Decision("flare", {"seconds": self.p.flare_s},
                                reason=f"jaw clench → flare: targets glow for {self.p.flare_s:g} s "
                                       f"({self.p.flare_cooldown_s:g} s cooldown)"))
        if self.resting:
            if now >= self.breather_until:
                out += self._end_breather(now)
            return out
        ok = state.quality == "ok"
        calm = self.calm_timer.update(state.engagement_z < self.p.band_low, now, ok)
        hype = self.hype_timer.update(state.engagement_z > self.p.band_high, now, ok)
        tired = self.fatigue_timer.update(state.fatigue_z > self.p.fatigue_z_threshold, now, ok)
        if tired and self._may_overload(now):
            out += self._start_breather(
                now, f"fatigue_z > {self.p.fatigue_z_threshold:g} for {self.p.fatigue_hold_s:g} s "
                     f"(now {state.fatigue_z:+.1f})")
        elif calm:
            self.calm_timer.reset()
            out += self._escalate(state)
        elif hype:
            self.hype_timer.reset()
            if self.level > 0:
                old = self.level
                self.level -= 1
                out.append(Decision("deescalate", {"level": self.level},
                                    reason=f"engagement_z > {self.p.band_high:g} for "
                                           f"{self.p.hype_hold_s:g} s (now {state.engagement_z:+.1f}) "
                                           f"→ ease off, level {old} → {self.level}"))
                if not LEVELS[self.level][5] and LEVELS[old][5]:
                    out.append(Decision("clear_darkness", {}, reason=f"level {self.level} has no darkness"))
        return out

    def _escalate(self, state: BrainState) -> list[Decision]:
        why = (f"engagement_z < {self.p.band_low:g} for {self.p.calm_hold_s:g} s "
               f"(now {state.engagement_z:+.1f})")
        out = []
        if self.level < len(LEVELS) - 1:
            old = self.level
            self.level += 1
            out.append(Decision("escalate", {"level": self.level, "spawn_interval_s": self.spawn_interval_s,
                                             "max_active": self.max_active},
                                reason=f"{why} → escalate level {old} → {self.level}: "
                                       f"{LEVEL_TEXT[self.level]}"))
        else:
            why += " at max level"
        out.append(Decision("cave_sound", {}, reason=f"{why} → cave sounds"))
        if LEVELS[self.level][5]:
            out.append(Decision("darkness", {"seconds": self.p.darkness_s},
                                reason=f"{why} → darkness for {self.p.darkness_s} s"))
        return out

    def _may_overload(self, now: float) -> bool:
        if self.resting:
            return False
        return self.last_breather_end is None or now - self.last_breather_end >= self.p.overload_cooldown_s

    def _start_breather(self, now: float, why: str) -> list[Decision]:
        self.breather_until = now + self.p.breather_s
        self.misses_in_row = 0
        old = self.level
        self.level = max(self.level - 1, 0)
        for t in (self.calm_timer, self.hype_timer, self.fatigue_timer):
            t.reset()
        lvl = f", level {old} → {self.level}" if old != self.level else ""
        return [Decision("breather_start", {"duration_s": self.p.breather_s, "level": self.level},
                         reason=f"overloaded: {why} → {self.p.breather_s:g} s breather{lvl}, "
                                f"then a {self.p.wave_size}-mob wave")]

    def _end_breather(self, now: float) -> list[Decision]:
        self.breather_until = None
        self.last_breather_end = now
        self.pending_wave = self.p.wave_size
        return [Decision("breather_end", {}, reason=f"{self.p.breather_s:g} s breather over"),
                Decision("wave", {"n": self.p.wave_size},
                         reason=f"breather over → wave of {self.p.wave_size} skeletons walking toward you")]

    def due_spawns(self, now: float, n_active: int) -> list[SpawnSpec]:
        if self._next_spawn is None:
            self._next_spawn = now + self.p.lead_in_s
        if self.resting:
            return []
        if self.pending_wave:
            n, self.pending_wave = self.pending_wave, 0
            self._next_spawn = now + self.spawn_interval_s
            return [SpawnSpec("L" if i % 2 == 0 else "R", self._pick_ecc(), kind="mob",
                              lifetime_s=self.p.mob_lifetime_s) for i in range(n)]
        if n_active >= self.max_active or now < self._next_spawn:
            return []
        self._next_spawn = now + self.spawn_interval_s
        _, _, _, mob_share, sheep_share, _ = LEVELS[self.level]
        side = "L" if self.rng.random() < 0.5 else "R"
        r = self.rng.random()
        if r < sheep_share:
            return [SpawnSpec(side, self._pick_ecc(), kind="sheep", lifetime_s=self.p.sheep_lifetime_s)]
        if r < sheep_share + mob_share:
            return [SpawnSpec(side, self._pick_ecc(), kind="mob", lifetime_s=self.p.mob_lifetime_s)]
        return [SpawnSpec(side, self._pick_ecc(), kind="target", lifetime_s=self.p.target_lifetime_s)]

    def _pick_ecc(self) -> float:
        top = min(LEVELS[self.level][2], len(self.ecc) - 1)
        return float(self.ecc[self.rng.randint(0, top)])
