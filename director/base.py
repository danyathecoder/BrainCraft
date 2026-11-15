"""What a director returns, and the interface both directors share.

A director is pure logic: no I/O, no clock of its own. The game loop
passes the current time (`now`, unix seconds or simulated seconds) into
every call, so tests can drive it with a fake clock.

Common interface (RehabDirector and FunDirector):

    start(now)                         -> [Decision]   session / mode start
    on_state(state, now)               -> [Decision]   ~1 Hz brain state
    on_trial_result(side, hit, now, kind="target", ecc_deg=None) -> [Decision]
    due_spawns(now, n_active)          -> [SpawnSpec]  what to spawn now
    status()                           -> dict         GameStatus fields
    resting                            -> bool         rest / breather phase
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from contracts import BrainState


@dataclass
class Decision:
    action: str  # e.g. "spawn_interval", "rest_start", "staircase_up"
    params: dict = field(default_factory=dict)
    reason: str = ""  # shown on the HUD and logged; never empty

    def __post_init__(self) -> None:
        if not self.reason or not self.reason.strip():
            raise ValueError(f"decision {self.action!r} needs a human-readable reason")

    @property
    def value(self) -> str:
        """Short machine-friendly summary for the CSV `value` column."""
        if not self.params:
            return self.action
        args = " ".join(f"{k}={_fmt(v)}" for k, v in self.params.items())
        return f"{self.action} {args}"


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


@dataclass
class SpawnSpec:
    """One thing the director wants spawned now."""

    side: str  # "L" | "R"
    ecc_deg: float
    cued: bool = False
    kind: str = "target"  # "target" (still, on its mark), "mob" (walks in), "sheep"
    lifetime_s: float | None = None  # None -> TrialRunner default
    enemy: str = "skeleton"  # mc.enemies key: skeleton, stray, bogged, pillager, blaze


class Director(Protocol):
    mode: str

    @property
    def resting(self) -> bool: ...

    def start(self, now: float) -> list[Decision]: ...

    def on_state(self, state: BrainState, now: float) -> list[Decision]: ...

    def on_trial_result(
        self, side: str, hit: bool, now: float, kind: str = "target", ecc_deg: float | None = None
    ) -> list[Decision]: ...

    def due_spawns(self, now: float, n_active: int) -> list[SpawnSpec]: ...

    def status(self) -> dict: ...


class HoldTimer:
    """How long a condition has held, counting only valid (good-quality) samples.

    Call update() about once per second. A sample with valid=False
    (e.g. quality "bad") neither adds time nor resets the timer. A valid
    sample where the condition is false resets it. Each sample counts
    for the time since the previous one (capped at max_step_s), the
    first sample counts as one nominal second.
    """

    def __init__(self, hold_s: float, max_step_s: float = 2.0, first_step_s: float = 1.0):
        self.hold_s = hold_s
        self.max_step_s = max_step_s
        self.first_step_s = first_step_s
        self.elapsed = 0.0
        self._last: float | None = None

    def update(self, condition: bool, now: float, valid: bool = True) -> bool:
        if self._last is None:
            dt = self.first_step_s
        else:
            dt = min(max(now - self._last, 0.0), self.max_step_s)
        self._last = now
        if valid:
            self.elapsed = self.elapsed + dt if condition else 0.0
        return self.fired

    @property
    def fired(self) -> bool:
        return self.elapsed >= self.hold_s - 1e-9

    def reset(self) -> None:
        self.elapsed = 0.0


@dataclass
class Fall:
    """What EngagementDrop.update() saw this second."""

    kind: str  # "falling" | "recovered"
    why: str  # human-readable cause, for decision reasons
    drop: float = 0.0
    peak: float = 0.0
    z: float = 0.0


class EngagementDrop:
    """Engagement falling relative to the player's OWN recent level.

    Absolute z-scores can sit high all session (baseline mismatch,
    smoothing), so "falling" means: engagement_z at least `drop_z` below
    its peak of the last `peak_window_s` for `drop_hold_s` good seconds,
    or below the absolute `band_low` for `calm_hold_s`. At most one
    "falling" per `cooldown_s`; after one the peak restarts from now, so
    the next needs a fresh drop. The caller decides what a fall does and
    calls push(peak) when it raises difficulty a step; "recovered" fires
    when engagement is back within `recover_z` of where it was before
    that step for `recover_hold_s` good seconds (caller then pop()s).
    Bad-quality seconds count for nothing.
    """

    def __init__(self, peak_window_s: float = 120.0, drop_z: float = 0.5,
                 drop_hold_s: float = 10.0, band_low: float = -0.5, calm_hold_s: float = 15.0,
                 recover_z: float = 0.2, recover_hold_s: float = 30.0, cooldown_s: float = 30.0):
        self.peak_window_s, self.drop_z, self.band_low = peak_window_s, drop_z, band_low
        self.calm_hold_s, self.recover_z, self.recover_hold_s = calm_hold_s, recover_z, recover_hold_s
        self.cooldown_s = cooldown_s
        self.drop_timer = HoldTimer(drop_hold_s)
        self.calm_timer = HoldTimer(calm_hold_s)
        self.recover_timer = HoldTimer(recover_hold_s)
        self.refs: list[float] = []  # engagement before each step taken
        self._hist: list[tuple[float, float]] = []
        self._last: float | None = None

    def push(self, peak: float) -> None:
        """A difficulty step was taken: remember where engagement was before it.

        Never below an earlier step's level, and at least out of the
        absolute "bored" band.
        """
        self.refs.append(max([peak, self.band_low + self.recover_z, *self.refs]))

    def pop(self) -> None:
        if self.refs:
            self.refs.pop()

    def reset_timers(self) -> None:
        for t in (self.drop_timer, self.calm_timer, self.recover_timer):
            t.reset()

    def update(self, z: float, now: float, ok: bool) -> Fall | None:
        if ok:
            self._hist.append((now, z))
        self._hist = [(t, v) for t, v in self._hist if now - t <= self.peak_window_s]
        peak = max((v for _, v in self._hist), default=z)
        drop = peak - z
        dropping = self.drop_timer.update(drop >= self.drop_z, now, ok)
        calm = self.calm_timer.update(z < self.band_low, now, ok)
        ref = self.refs[-1] if self.refs else None
        recovered = self.recover_timer.update(
            ref is not None and z >= ref - self.recover_z, now, ok and ref is not None)
        if not ok:
            return None
        cooled = self._last is None or now - self._last >= self.cooldown_s
        if (dropping or calm) and cooled:
            self.reset_timers()
            self._last = now
            if dropping:
                why = (f"engagement fell {drop:.1f} below its {self.peak_window_s / 60:g}-min "
                       f"peak ({peak:+.1f} → {z:+.1f})")
            else:
                why = f"engagement_z < {self.band_low:g} for {self.calm_hold_s:g} s (now {z:+.1f})"
            self._hist = [(now, z)]  # the next step needs a fresh drop from here
            return Fall("falling", why, drop, peak, z)
        if recovered and ref is not None:
            self.recover_timer.reset()
            return Fall("recovered", (f"engagement back to {z:+.1f} (≥ {ref - self.recover_z:+.1f}) "
                                      f"for {self.recover_hold_s:g} s"), drop, peak, z)
        return None
