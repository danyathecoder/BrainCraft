"""Rehab mode director: one mode, each signal with one job.

Visual scanning training inspired by therapy for post-stroke spatial
neglect (left side neglected). Pure logic: time is passed in.

  warm-up              the first `warmup_s` (60 s): targets on the right
                       only, so the player starts with successes; then
                       10% left / 90% right (`p_left`);
  performance: side    every hit +0.05 left chance, every miss -0.05,
                       within [0.10, 0.70] (not during the warm-up);
  performance: angle   the left eccentricity staircase (below) + cues;
  brain: reward        ALI rising >= 0.02 in one second (vs the player's
                       baseline, i.e. attention moving left) -> +2 HP,
                       at most every 5 s;
  brain: enemies       harder enemies (mc.enemies) very slowly: +1 tier
                       every `enemy_every_s` (5 min), +1 more while
                       engagement has fallen below its own recent peak
                       (EngagementDrop, slow settings);
  brain: when to rest  fatigue / strongly low engagement -> rest phase.

Trials: one target at a time. An unhit target lives `target_lifetime_s`
(60 s: time to search the whole scene; meanwhile it shoots, so ignoring
the left side has a cost, and the player can die); the next appears
`spawn_interval_s` after a miss, or `hit_gap_s` (1 s) after a hit.
Right targets stay at a fixed moderate eccentricity (`right_ecc_deg`)
as a stable control condition, so left/right differences in the report
reflect the left staircase, not a moving right target.

Spatial staircase (behaviour-driven), over the last 5 LEFT targets:
  >= 4 hits -> left eccentricity +1 step, cues off
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
from director.base import Decision, EngagementDrop, HoldTimer, SpawnSpec
from mc import config
from mc import enemies as E


@dataclass
class RehabParams:
    spawn_interval_s: float = 4.0  # next target when the previous was not hit
    min_interval_s: float = 2.0
    max_interval_s: float = 6.0
    interval_step_s: float = 0.5
    hit_gap_s: float = 1.0  # next target after a hit
    max_active: int = 1
    target_lifetime_s: float = 60.0  # how long an unhit target stays
    lead_in_s: float = 2.0  # first spawn after session start
    warmup_s: float = 60.0  # right-side targets only
    ali_reward_rise: float = 0.02  # ALI rise in one second that earns a heal
    ali_reward_cooldown_s: float = 5.0
    ali_reward_hp: int = 2
    enemy_every_s: float = 300.0  # +1 enemy tier this often (fun: 90 s)
    max_enemy_boost: int = 1
    max_enemy_tier: int = len(E.ROSTER) - 1
    enemy_drop_z: float = 0.5  # engagement this far below its own 2-min peak...
    enemy_drop_hold_s: float = 20.0  # ...for this many good seconds -> harder enemy
    enemy_recover_hold_s: float = 60.0
    enemy_boost_cooldown_s: float = 60.0
    newest_tier_share: float = 0.5
    p_left: float = 0.1  # after the warm-up: 10% left, 90% right
    p_left_min: float = 0.1
    p_left_max: float = 0.7
    p_left_hit_step: float = 0.05  # every hit moves spawns this much to the left
    p_left_miss_step: float = 0.05  # every miss this much back to the right
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
        self._logged_p_left = self.p_left
        self.ali: float | None = None
        self._ali_prev: tuple[float, float] | None = None  # (t, ali) of the last good second
        self._last_reward: float | None = None
        self.ali_rewards = 0
        # Relative drop only: strongly low absolute engagement is the rest rule.
        self.drop = EngagementDrop(drop_z=self.p.enemy_drop_z, drop_hold_s=self.p.enemy_drop_hold_s,
                                   band_low=float("-inf"),
                                   recover_hold_s=self.p.enemy_recover_hold_s,
                                   cooldown_s=self.p.enemy_boost_cooldown_s)
        self.enemy_boost = 0
        self.enemy_tier = 0
        self._announced_tier = 0
        self._t0: float | None = None
        self._warm = True
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

    @property
    def warming_up(self) -> bool:
        return self._warm

    @property
    def p_left_effective(self) -> float:
        """Chance of a left target now: 0 in the warm-up, then hit-driven p_left."""
        if self._warm:
            return 0.0
        return self.p_left

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "resting": self.resting,
            "spawn_interval_s": self.spawn_interval_s,
            "p_left": round(self.p_left_effective, 2),
            "left_ecc_deg": float(self.left_ecc_deg),
            "warmup": self._warm,
            "ali": None if self.ali is None else round(self.ali, 3),
            "ali_rewards": self.ali_rewards,
            "enemy_tier": self.enemy_tier,
            "enemy_boost": self.enemy_boost,
        }

    # --- interface ------------------------------------------------------

    def start(self, now: float) -> list[Decision]:
        self._t0 = now
        self._next_ok = now + self.p.lead_in_s
        self._warm = self.p.warmup_s > 0
        return [Decision("rehab_start", self.status() | {"right_ecc_deg": self.p.right_ecc_deg},
                         reason=(f"rehab session: {self.p.warmup_s:g} s warm-up on the right, then "
                                 f"p_left {self.p_left:.1f}, left targets at {self.left_ecc_deg}°, "
                                 f"right at {self.p.right_ecc_deg:g}°; one target at a time, "
                                 f"{self.p.target_lifetime_s:g} s to find it"))]

    def _check_warmup(self, now: float) -> list[Decision]:
        if not self._warm or self._t0 is None or now - self._t0 < self.p.warmup_s:
            return []
        self._warm = False
        return [Decision("warmup_end", {"p_left": self.p_left_effective},
                         reason=(f"{self.p.warmup_s:g} s warm-up on the right over → targets on "
                                 f"both sides, p_left {self.p_left_effective:.1f}"))]

    def _ali_reward(self, state: BrainState, now: float, ok: bool) -> list[Decision]:
        """Heal the player when ALI rises: attention moving to the left."""
        ali = getattr(state, "ali", None)
        if not ok or ali is None:
            return []
        prev, self._ali_prev = self._ali_prev, (now, float(ali))
        self.ali = float(ali)
        if prev is None or now - prev[0] > 1.5:  # needs consecutive good seconds
            return []
        rise = self.ali - prev[1]
        cooled = self._last_reward is None or now - self._last_reward >= self.p.ali_reward_cooldown_s
        if rise < self.p.ali_reward_rise or not cooled:
            return []
        self._last_reward = now
        self.ali_rewards += 1
        hearts = self.p.ali_reward_hp / 2
        return [Decision("ali_reward", {"hp": self.p.ali_reward_hp, "rise": round(rise, 3),
                                        "ali": round(self.ali, 3)},
                         reason=(f"ALI rose {rise:+.2f} in 1 s (attention moving left) → "
                                 f"+{hearts:g} heart{'s' if hearts != 1 else ''}"))]

    # --- enemies: very slow time schedule + falling engagement --------------

    def _sync_enemies(self, now: float, cause: str | None = None) -> list[Decision]:
        time_tier = 0 if self._t0 is None else int((now - self._t0) // self.p.enemy_every_s)
        tier = min(time_tier + self.enemy_boost, self.p.max_enemy_tier, len(E.ROSTER) - 1)
        self.enemy_tier = tier
        if tier <= self._announced_tier:
            return []
        self._announced_tier = tier
        e = E.ROSTER[tier]
        when = cause or f"{(now - self._t0) / 60:.0f} min in"
        return [Decision("enemy_unlock", {"enemy": e.key, "label": e.label, "threat": e.threat,
                                          "tier": tier},
                         reason=f"{when} → new enemy: {e.label} ({e.threat}, {e.health:g} HP)")]

    def _enemy_engagement(self, state: BrainState, now: float, ok: bool) -> list[Decision]:
        fall = self.drop.update(state.engagement_z, now, ok)
        if fall is None:
            return []
        if fall.kind == "falling":
            if self.enemy_boost >= self.p.max_enemy_boost:
                return []
            self.drop.push(fall.peak)
            self.enemy_boost += 1
            nxt = E.ROSTER[min(self.enemy_tier + 1, len(E.ROSTER) - 1)]
            return [Decision("enemy_boost_up", {"enemy_boost": self.enemy_boost},
                             reason=f"{fall.why} → harder enemies, up to {nxt.label}")
                    ] + self._sync_enemies(now, cause="engagement falling")
        if self.enemy_boost > 0:  # recovered
            self.enemy_boost -= 1
            self.drop.pop()
            out = [Decision("enemy_boost_down", {"enemy_boost": self.enemy_boost},
                            reason=f"{fall.why} → enemies back to the time schedule")]
            return out + self._sync_enemies(now)
        return []

    def _pick_enemy(self) -> str:
        top = self.enemy_tier
        if top == 0 or self.rng.random() < self.p.newest_tier_share:
            return E.ROSTER[top].key
        return E.ROSTER[self.rng.randint(0, top - 1)].key

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
        # A hit brings the next target in 1 s. A miss (the minute ran out)
        # waits the normal 4 s. Either way this replaces the timer set at spawn.
        gap = self.p.hit_gap_s if hit else self.spawn_interval_s
        self._next_ok = now + gap
        self.recent.append(hit)
        if not self._warm:
            out += self._shift_side(hit)
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

    def _shift_side(self, hit: bool) -> list[Decision]:
        """Every hit moves spawns a little to the left, every miss back right."""
        old = self.p_left
        step = self.p.p_left_hit_step if hit else -self.p.p_left_miss_step
        self.p_left = round(min(max(old + step, self.p.p_left_min), self.p.p_left_max), 2)
        # Log only after a full 0.1 move since the last logged value, so the
        # HUD isn't spammed by every +-0.05 (or by wobbling at the cap).
        if abs(self.p_left - self._logged_p_left) < 0.1 - 1e-9:
            return []
        prev, self._logged_p_left = self._logged_p_left, self.p_left
        what = "hits" if hit else "misses"
        return [Decision("left_chance", {"p_left": self.p_left},
                         reason=f"{what} → left targets {prev:.0%} → {self.p_left:.0%}")]

    def _step_up(self, hits: int, n: int) -> list[Decision]:
        old_ecc = self.left_ecc_deg
        self.left_ecc_step = min(self.left_ecc_step + 1, len(self.ecc) - 1)
        had_cues = self.cues_on
        self.cues_left = 0
        if self.left_ecc_deg == old_ecc and not had_cues:
            return []  # already at the top of the staircase
        parts = []
        if self.left_ecc_deg != old_ecc:
            parts.append(f"left eccentricity {old_ecc}° → {self.left_ecc_deg}°")
        else:
            parts.append(f"left eccentricity stays at max {self.left_ecc_deg}°")
        if had_cues:
            parts.append("cues off")
        return [Decision(
            "staircase_up",
            {"left_ecc_deg": self.left_ecc_deg, "cues": False},
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
        out += self._check_warmup(now)
        out += self._sync_enemies(now)
        if self.resting:
            if now >= self.rest_until:
                out += self._end_rest(now)
            return out
        ok = state.quality == "ok"
        out += self._ali_reward(state, now, ok)
        out += self._enemy_engagement(state, now, ok)
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

    def _spec(self, side: str, ecc_deg: float, cued: bool = False) -> SpawnSpec:
        return SpawnSpec(side, float(ecc_deg), cued=cued, lifetime_s=self.p.target_lifetime_s,
                         enemy=self._pick_enemy())

    def due_spawns(self, now: float, n_active: int) -> list[SpawnSpec]:
        """One new target, and only when none is already up.

        After a miss the next waits `spawn_interval_s` (4 s). After a hit
        it waits `hit_gap_s` (1 s). An unhit target stays for `target_lifetime_s`.
        """
        if self._next_ok is None:
            self._next_ok = now + self.p.lead_in_s
        if self._t0 is None:
            self._t0 = now
        self._pending.extend(self._check_warmup(now))
        if self.resting or n_active >= self.p.max_active or now < self._next_ok:
            return []
        self._last_spawn = now
        self._next_ok = now + self.spawn_interval_s
        if self._warm:  # warm-up: right side, any eccentricity, for early successes
            return [self._spec("R", self.rng.choice(self.ecc))]
        if self.rng.random() < self.p_left_effective:
            cued = self.cues_left > 0
            if cued:
                self.cues_left -= 1
                if self.cues_left == 0:
                    self._pending.append(Decision(
                        "cues_off", {}, reason=f"{self.p.cue_trials} cued left targets shown → cues off"))
            return [self._spec("L", self.left_ecc_deg, cued)]
        return [self._spec("R", self.p.right_ecc_deg)]
