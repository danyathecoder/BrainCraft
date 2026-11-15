"""Fun mode director: difficulty grows over time, faster when engagement falls.

Pure logic, time passed in.

  level = base level (from time) + boost (from the brain), 0..4
    base level: +1 every `level_every_s` (90 s), so the game always
      progresses, also for a player whose engagement stays high (it
      did in real sessions: absolute z-scores are a poor trigger)
    boost: +1 (max `max_boost`) when engagement is FALLING:
      engagement_z at least `drop_z` (0.5) below its own peak of the
      last `peak_window_s` (120 s) for `drop_hold_s` (10 good s), or
      below `band_low` (-0.5) for `calm_hold_s` (15 good s);
      `boost_cooldown_s` (30 s) between steps;
      -1 when it recovers to within `recover_z` (0.2) of where it was
      before the drop for `recover_hold_s` (30 good s)
  higher level: faster spawns, several targets at once, wider angles,
    walking enemies, sheep distractors (shooting a sheep = false_tag),
    darkness, cave sounds
  enemy tier = level (mc.enemies): skeleton -> stray -> bogged ->
    pillager -> blaze; each spawn is the newest tier half the time,
    otherwise a lower one; tougher enemies have more health, live longer
  overloaded (fatigue_z > 1.5 for 5 s, or 3 misses in a row)
    -> 20 s breather (no spawns, darkness cleared, boost -1), then a
       5-mob wave
  jaw clench -> "flare": all targets glow for 5 s, 30 s cooldown
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from contracts import BrainState
from director.base import Decision, EngagementDrop, HoldTimer, SpawnSpec
from mc import config
from mc import enemies as E

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
    "enemies walk toward you, sheep distractors",
    "three targets at once, darkness",
    "maximum pace",
)
MAX_LEVEL = len(LEVELS) - 1


@dataclass
class FunParams:
    level_every_s: float = 90.0  # base level +1 this often
    max_boost: int = 2
    peak_window_s: float = 120.0
    drop_z: float = 0.5
    drop_hold_s: float = 10.0
    band_low: float = -0.5  # absolute "bored" level, also counts as falling
    calm_hold_s: float = 15.0
    recover_z: float = 0.2
    recover_hold_s: float = 30.0
    boost_cooldown_s: float = 30.0
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
    newest_tier_share: float = 0.5


class FunDirector:
    mode = "fun"

    def __init__(self, params: FunParams | None = None, seed: int | None = None,
                 eccentricities: tuple = config.ECCENTRICITIES):
        self.p = params or FunParams()
        self.ecc = tuple(eccentricities)
        self.rng = random.Random(seed)
        self.base_level = 0
        self.boost = 0
        self.level = 0
        self.drop = EngagementDrop(self.p.peak_window_s, self.p.drop_z, self.p.drop_hold_s,
                                   self.p.band_low, self.p.calm_hold_s, self.p.recover_z,
                                   self.p.recover_hold_s, self.p.boost_cooldown_s)
        self.fatigue_timer = HoldTimer(self.p.fatigue_hold_s)
        self.misses_in_row = 0
        self.breather_until: float | None = None
        self.last_breather_end: float | None = None
        self.last_flare: float | None = None
        self.pending_wave = 0
        self._next_spawn: float | None = None
        self._pending: list[Decision] = []
        self._t0: float | None = None
        self.enemy_tier = 0
        self._announced_tier = 0

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
            "base_level": self.base_level,
            "boost": self.boost,
            "enemy_tier": self.enemy_tier,
            "enemy": E.ROSTER[self.enemy_tier].key,
        }

    # --- level: time baseline + engagement boost --------------------------

    def _minutes(self, now: float) -> float:
        return (now - self._t0) / 60 if self._t0 is not None else 0.0

    def _sync_level(self, now: float, cause: str | None = None) -> list[Decision]:
        """Recompute level and enemy tier; decisions for every change."""
        out: list[Decision] = []
        if self._t0 is not None:
            self.base_level = min(int((now - self._t0) // self.p.level_every_s), MAX_LEVEL)
        old, new = self.level, min(self.base_level + self.boost, MAX_LEVEL)
        if new != old:
            self.level = new
            why = cause or f"{self._minutes(now):.1f} min in"
            if new > old:
                out.append(Decision("escalate", {"level": new, "base_level": self.base_level,
                                                 "boost": self.boost,
                                                 "spawn_interval_s": self.spawn_interval_s,
                                                 "max_active": self.max_active},
                                    reason=f"{why} → level {old} → {new}: {LEVEL_TEXT[new]}"))
                out.append(Decision("cave_sound", {}, reason=f"level {new} → cave sounds"))
                if LEVELS[new][5]:
                    out.append(Decision("darkness", {"seconds": self.p.darkness_s},
                                        reason=f"level {new} → darkness for {self.p.darkness_s} s"))
            else:
                out.append(Decision("deescalate", {"level": new, "base_level": self.base_level,
                                                   "boost": self.boost},
                                    reason=f"{why} → level {old} → {new}"))
                if LEVELS[old][5] and not LEVELS[new][5]:
                    out.append(Decision("clear_darkness", {}, reason=f"level {new} has no darkness"))
        self.enemy_tier = min(self.level, len(E.ROSTER) - 1)
        if self.enemy_tier > self._announced_tier:
            self._announced_tier = self.enemy_tier
            e = E.ROSTER[self.enemy_tier]
            out.append(Decision("enemy_unlock", {"enemy": e.key, "label": e.label,
                                                 "threat": e.threat, "tier": self.enemy_tier},
                                reason=(f"level {self.level} ({self._minutes(now):.1f} min in) → "
                                        f"new enemy: {e.label} ({e.threat}, {e.health:g} HP)")))
        return out

    def _engagement(self, state: BrainState, now: float, ok: bool) -> list[Decision]:
        """Boost difficulty when engagement falls; ease it when it recovers."""
        fall = self.drop.update(state.engagement_z, now, ok)
        if fall is None:
            return []
        if fall.kind == "falling":
            if self.boost >= self.p.max_boost or self.base_level + self.boost >= MAX_LEVEL:
                out = [Decision("cave_sound", {}, reason=f"{fall.why} at max difficulty → cave sounds")]
                if LEVELS[self.level][5]:
                    out.append(Decision("darkness", {"seconds": self.p.darkness_s},
                                        reason=f"{fall.why} at max difficulty → darkness"))
                return out
            self.drop.push(fall.peak)
            self.boost += 1
            return [Decision("boost_up", {"boost": self.boost, "drop": round(fall.drop, 2)},
                             reason=f"{fall.why} → harder: +{self.boost} level above the time schedule")
                    ] + self._sync_level(now, cause=f"engagement falling (boost +{self.boost})")
        if self.boost > 0:  # recovered
            self.boost -= 1
            self.drop.pop()
            return [Decision("boost_down", {"boost": self.boost},
                             reason=f"{fall.why} → boost {self.boost}")
                    ] + self._sync_level(now, cause="engagement recovered")
        return []

    # --- enemies --------------------------------------------------------

    def _pick_enemy(self) -> E.Enemy:
        top = self.enemy_tier
        if top == 0 or self.rng.random() < self.p.newest_tier_share:
            return E.ROSTER[top]
        return E.ROSTER[self.rng.randint(0, top - 1)]

    # --- interface ------------------------------------------------------

    def start(self, now: float) -> list[Decision]:
        self._next_spawn = now + self.p.lead_in_s
        if self._t0 is None:  # a mode switch back keeps the session clock
            self._t0 = now
        return [Decision("fun_start", {"level": self.level},
                         reason=(f"fun mode: level {self.level} ({LEVEL_TEXT[self.level]}); "
                                 f"+1 level every {self.p.level_every_s:g} s, more when "
                                 f"engagement falls"))]

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
        out += self._sync_level(now)
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
        tired = self.fatigue_timer.update(state.fatigue_z > self.p.fatigue_z_threshold, now, ok)
        if tired and self._may_overload(now):
            out += self._start_breather(
                now, f"fatigue_z > {self.p.fatigue_z_threshold:g} for {self.p.fatigue_hold_s:g} s "
                     f"(now {state.fatigue_z:+.1f})")
            return out
        out += self._engagement(state, now, ok)
        return out

    def _may_overload(self, now: float) -> bool:
        if self.resting:
            return False
        return self.last_breather_end is None or now - self.last_breather_end >= self.p.overload_cooldown_s

    def _start_breather(self, now: float, why: str) -> list[Decision]:
        self.breather_until = now + self.p.breather_s
        self.misses_in_row = 0
        self.drop.reset_timers()
        self.fatigue_timer.reset()
        eased = ""
        if self.boost > 0:  # ease off, but never below the time schedule
            self.boost -= 1
            self.drop.pop()
            eased = f", boost {self.boost + 1} → {self.boost}"
        out = [Decision("breather_start", {"duration_s": self.p.breather_s, "boost": self.boost},
                        reason=f"overloaded: {why} → {self.p.breather_s:g} s breather{eased}, "
                               f"then a {self.p.wave_size}-mob wave")]
        return out + self._sync_level(now, cause="overloaded")

    def _end_breather(self, now: float) -> list[Decision]:
        self.breather_until = None
        self.last_breather_end = now
        self.pending_wave = self.p.wave_size
        return [Decision("breather_end", {}, reason=f"{self.p.breather_s:g} s breather over"),
                Decision("wave", {"n": self.p.wave_size},
                         reason=f"breather over → wave of {self.p.wave_size} enemies walking toward you")]

    def due_spawns(self, now: float, n_active: int) -> list[SpawnSpec]:
        if self._next_spawn is None:
            self._next_spawn = now + self.p.lead_in_s
        if self.resting:
            return []
        if self._t0 is None:
            self._t0 = now
        self._pending.extend(self._sync_level(now))
        if self.pending_wave:
            n, self.pending_wave = self.pending_wave, 0
            self._next_spawn = now + self.spawn_interval_s
            return [self._mob_spec("L" if i % 2 == 0 else "R") for i in range(n)]
        if n_active >= self.max_active or now < self._next_spawn:
            return []
        self._next_spawn = now + self.spawn_interval_s
        _, _, _, mob_share, sheep_share, _ = LEVELS[self.level]
        side = "L" if self.rng.random() < 0.5 else "R"
        r = self.rng.random()
        if r < sheep_share:
            return [SpawnSpec(side, self._pick_ecc(), kind="sheep", lifetime_s=self.p.sheep_lifetime_s)]
        if r < sheep_share + mob_share:
            return [self._mob_spec(side)]
        e = self._pick_enemy()
        # Tougher enemies take several arrows: they get their own, longer lifetime.
        life = max(self.p.target_lifetime_s, e.lifetime_s)
        return [SpawnSpec(side, self._pick_ecc(), kind="target", lifetime_s=life, enemy=e.key)]

    def _mob_spec(self, side: str) -> SpawnSpec:
        e = self._pick_enemy()
        life = max(self.p.mob_lifetime_s, e.lifetime_s + 4.0)
        return SpawnSpec(side, self._pick_ecc(), kind="mob", lifetime_s=life, enemy=e.key)

    def _pick_ecc(self) -> float:
        top = min(LEVELS[self.level][2], len(self.ecc) - 1)
        return float(self.ecc[self.rng.randint(0, top)])
