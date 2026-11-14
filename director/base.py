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
    kind: str = "target"  # "target" (still zombie), "mob" (walking zombie), "sheep"
    lifetime_s: float | None = None  # None -> TrialRunner default


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
