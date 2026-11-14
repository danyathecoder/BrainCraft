"""Target spawning and tracking: hit / miss / reaction time (P3).

Each target gets a unique tag (t1, t2, ...). Poll rcon.exists(tag) at
~5 Hz: if it disappears before the lifetime ends it was hit (RT = time
since spawn), otherwise it's a miss and gets killed. Every spawn, hit
and miss goes to the EventLogger.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc import config


@dataclass
class Target:
    target_id: str
    side: str
    ecc_deg: float
    spawned_at: float
    cued: bool = False


class TrialRunner:
    def __init__(self, rcon, logger, lifetime_s: float = config.TARGET_LIFETIME_S):
        self.rcon = rcon
        self.log = logger
        self.lifetime_s = lifetime_s
        self.active: dict[str, Target] = {}

    def spawn(self, side: str, ecc_deg: float, cued: bool = False) -> Target:
        # TODO(P3): geometry.target_position -> rcon.summon_target
        # (+ playsound at the target if cued), log "spawn".
        raise NotImplementedError

    def poll(self) -> list[tuple[Target, bool]]:
        """Check active targets; return (target, hit) for finished ones."""
        # TODO(P3): exists() per active target, log "hit"/"miss" + rt_ms.
        raise NotImplementedError
