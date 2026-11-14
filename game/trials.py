"""Target spawning and tracking: hit / miss / found / reaction time.

Each target gets a unique tag (t1, t2, ... ; sheep distractors d1, d2,
...). poll(now) at ~5 Hz:
- the player's yaw is read once; the first time it is within +-10 deg
  of a target's bearing -> "found" (rt_ms = time to find)
- a target that disappears before its lifetime ended was shot -> "hit"
  (rt_ms = midpoint between the last poll that saw it and this one,
  minus the spawn time, so the error is at most half a poll period)
- lifetime over -> "miss", the target is killed
- a sheep that disappears early -> "false_tag"; one that lives out its
  lifetime is removed silently.
Every spawn / found / hit / miss / false_tag goes to the EventLogger
with t = the loop's clock (simulated time in --sim).

Yaw convention (Minecraft): 0 = south (+Z), 90 = west (-X),
180 = north (-Z), -90/270 = east (+X). The player faces north, so a
LEFT target at eccentricity theta has bearing 180 - theta and a RIGHT
target 180 + theta.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from game import effects
from game.geometry import target_position
from mc import config

FOUND_TOLERANCE_DEG = 10.0
CUE_REPEAT_S = 2.0  # repeat the cue sound once if the target is not found by then


def normalize_yaw(yaw: float) -> float:
    """Angle in degrees mapped to [-180, 180)."""
    return (yaw + 180.0) % 360.0 - 180.0


def yaw_diff(a: float, b: float) -> float:
    """Signed smallest difference a - b in degrees, in [-180, 180)."""
    return normalize_yaw(a - b)


def bearing_yaw(side: str, ecc_deg: float) -> float:
    """Yaw the player must face to look at a target (player faces north = 180)."""
    if side not in ("L", "R"):
        raise ValueError(f"side must be 'L' or 'R', got {side!r}")
    return normalize_yaw(180.0 - ecc_deg if side == "L" else 180.0 + ecc_deg)


def yaw_towards(dx: float, dz: float) -> float:
    """Yaw pointing along (dx, dz) in Minecraft's convention."""
    return normalize_yaw(math.degrees(math.atan2(-dx, dz)))


@dataclass
class Target:
    target_id: str
    side: str
    ecc_deg: float
    spawned_at: float
    cued: bool = False
    kind: str = "target"  # "target" | "mob" | "sheep"
    lifetime_s: float = config.TARGET_LIFETIME_S
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    bearing: float = 180.0
    owner: str = ""  # mode of the director that asked for it
    found_at: float | None = None
    last_seen: float = 0.0
    cue_repeated: bool = False


@dataclass
class TrialResult:
    target: Target
    outcome: str  # "hit" | "miss" | "false_tag" | "expired" (sheep left alone)
    rt_ms: int | None = None

    @property
    def hit(self) -> bool:
        return self.outcome in ("hit", "false_tag")


class TrialRunner:
    def __init__(self, rcon, logger, lifetime_s: float = config.TARGET_LIFETIME_S,
                 clock=time.time, found_tolerance_deg: float = FOUND_TOLERANCE_DEG):
        self.rcon = rcon
        self.log = logger
        self.lifetime_s = lifetime_s
        self.clock = clock
        self.tol = found_tolerance_deg
        self.active: dict[str, Target] = {}
        self._n_targets = 0
        self._n_sheep = 0
        self.last_yaw: float | None = None
        self.stats = {"hits_L": 0, "misses_L": 0, "hits_R": 0, "misses_R": 0,
                      "false_tags": 0, "spawns": 0, "found": 0}

    @property
    def score(self) -> int:
        return self.stats["hits_L"] + self.stats["hits_R"] - self.stats["false_tags"]

    def n_active(self, include_sheep: bool = False) -> int:
        return sum(1 for t in self.active.values() if include_sheep or t.kind != "sheep")

    def _event(self, event: str, now: float, target: Target | None = None, **fields) -> None:
        if target is not None:
            fields = {"target_id": target.target_id, "side": target.side,
                      "ecc_deg": f"{target.ecc_deg:g}", **fields}
        self.log.event(event, t=f"{now:.3f}", **fields)

    # --- spawning -------------------------------------------------------

    def spawn(self, side: str, ecc_deg: float, cued: bool = False, *, now: float | None = None,
              kind: str = "target", lifetime_s: float | None = None, owner: str = "") -> Target:
        now = self.clock() if now is None else now
        if kind == "sheep":
            self._n_sheep += 1
            tid = f"d{self._n_sheep}"
        else:
            self._n_targets += 1
            tid = f"t{self._n_targets}"
        x, y, z = target_position(side, ecc_deg)
        t = Target(tid, side, float(ecc_deg), now, cued=cued, kind=kind,
                   lifetime_s=self.lifetime_s if lifetime_s is None else lifetime_s,
                   pos=(x, y, z), bearing=bearing_yaw(side, ecc_deg), owner=owner, last_seen=now)
        if kind == "target":
            reply = self.rcon.summon_target(tid, x, y, z, side, glowing=cued)
            effects.check_reply("summon target", reply or "")
        elif kind == "mob":
            effects.send(self.rcon, effects.summon_mob_cmd(tid, x, y, z, side))
        elif kind == "sheep":
            effects.send(self.rcon, effects.summon_sheep_cmd(tid, x, y, z, side))
        else:
            raise ValueError(f"unknown target kind {kind!r}")
        if cued:
            effects.send(self.rcon, effects.cue_sound_cmd(x, y, z))
        self.active[tid] = t
        self.stats["spawns"] += kind != "sheep"
        value = "cued" if cued else ("" if kind == "target" else kind)
        self._event("spawn", now, t, value=value)
        return t

    # --- tracking -------------------------------------------------------

    def poll(self, now: float | None = None) -> list[TrialResult]:
        """Check active targets; return results for finished ones."""
        now = self.clock() if now is None else now
        if not self.active:
            return []
        self._check_found(now)
        results: list[TrialResult] = []
        for tid, t in list(self.active.items()):
            age = now - t.spawned_at
            alive = self.rcon.exists(tid)
            if not alive:
                del self.active[tid]
                rt = int(round(((t.last_seen + now) / 2 - t.spawned_at) * 1000))
                rt = max(rt, 0)
                if t.kind == "sheep":
                    self.stats["false_tags"] += 1
                    self._event("false_tag", now, t, rt_ms=rt, value="sheep")
                    results.append(TrialResult(t, "false_tag", rt))
                else:
                    self.stats[f"hits_{t.side}"] += 1
                    self._event("hit", now, t, rt_ms=rt, value="cued" if t.cued else t.kind)
                    results.append(TrialResult(t, "hit", rt))
                continue
            t.last_seen = now
            if age >= t.lifetime_s:
                del self.active[tid]
                effects.send(self.rcon, f"kill @e[tag={tid}]")
                if t.kind == "sheep":
                    results.append(TrialResult(t, "expired"))
                else:
                    self.stats[f"misses_{t.side}"] += 1
                    self._event("miss", now, t, value="cued" if t.cued else t.kind)
                    results.append(TrialResult(t, "miss"))
                continue
            if t.cued and not t.cue_repeated and t.found_at is None and age >= CUE_REPEAT_S:
                t.cue_repeated = True
                effects.send(self.rcon, effects.cue_sound_cmd(*t.pos))
        return results

    def _check_found(self, now: float) -> None:
        waiting = [t for t in self.active.values() if t.found_at is None and t.kind != "sheep"]
        if not waiting:
            return
        yaw = self.rcon.yaw()
        if yaw is None:
            return
        self.last_yaw = yaw
        for t in waiting:
            if abs(yaw_diff(yaw, t.bearing)) <= self.tol:
                t.found_at = now
                self.stats["found"] += 1
                self._event("found", now, t, rt_ms=int(round((now - t.spawned_at) * 1000)),
                            value=f"{normalize_yaw(yaw):.1f}")

    def cancel_all(self) -> list[Target]:
        """Remove every active target without a hit/miss (rest phase, breather)."""
        gone = list(self.active.values())
        for tid in list(self.active):
            effects.send(self.rcon, f"kill @e[tag={tid}]")
        self.active.clear()
        return gone
