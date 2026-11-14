"""SimRcon: a fake Minecraft server + simulated player, no server needed.

Same public interface as mc.rcon.Rcon (it subclasses it, so the real
command strings and reply parsing of summon_target / exists / kill /
title / playsound / yaw are exercised). Only cmd() is replaced: it
records every command (self.commands) and answers like a real 26.3
server ("Test passed. Count: 1", "Test failed", "Summoned new Zombie",
"Killed Zombie", "<name> has the following entity data: [..f, ..f]").

The simulated player has a left spatial-neglect profile: right targets
are noticed ~92% of the time, left targets less and less with
eccentricity; cues (Glowing + sound) help. It notices a target after a
log-normal search delay (longer on the left), turns its yaw toward it at
a finite speed, aims for a log-normal time and shoots it (the entity
disappears). Targets it does not notice, or reach too late, time out as
misses. Everything runs on the injected clock, so a sim session can run
faster than real time.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from dataclasses import dataclass, field

from mc import config
from mc.rcon import Rcon

_SUMMON = re.compile(
    r"^summon\s+(?:minecraft:)?(\w+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(\{.*\}))?\s*$")
_TAG_SELECTOR = re.compile(r"@e\[tag=([\w.+-]+)\]")


@dataclass
class NeglectProfile:
    p_right: float = 0.92
    p_left_by_ecc: tuple = ((0, 0.9), (15, 0.85), (35, 0.65), (55, 0.45), (75, 0.25), (90, 0.15))
    cue_boost: float = 0.25
    p_max: float = 0.97
    search_median_s: float = 0.45
    left_search_s_per_deg: float = 0.012  # extra search time per degree on the left
    cue_search_factor: float = 0.7
    search_sigma: float = 0.45
    aim_median_s: float = 0.7
    aim_sigma: float = 0.35
    turn_speed_dps: float = 160.0
    idle_speed_dps: float = 60.0
    sheep_shoot_p: float = 0.12

    def p_notice(self, side: str, ecc: float, cued: bool) -> float:
        if side == "R":
            p = self.p_right
        else:
            pts = self.p_left_by_ecc
            p = pts[-1][1]
            for (e0, p0), (e1, p1) in zip(pts, pts[1:]):
                if e0 <= ecc <= e1:
                    p = p0 + (p1 - p0) * (ecc - e0) / (e1 - e0)
                    break
        if cued:
            p += self.cue_boost
        return min(p, self.p_max)


@dataclass
class SimEntity:
    eid: int
    kind: str
    tags: list
    x: float
    y: float
    z: float
    spawned_at: float
    glowing: bool = False
    bearing: float = 180.0
    side: str = ""
    attended: bool = False
    search_s: float = 0.0
    turn_at: float | None = None
    shoot_at: float | None = None
    alive: bool = True
    extra: dict = field(default_factory=dict)


class SimRcon(Rcon):
    def __init__(self, clock=time.time, seed: int | None = 0, profile: NeglectProfile | None = None,
                 player_name: str = "SimPlayer", player_online: bool = True,
                 origin: tuple[float, float] = (config.STAND_X + 0.5, config.STAND_Z + 0.5),
                 step_s: float = 0.05):
        # deliberately no super().__init__(): there is no socket
        self._lock = threading.RLock()
        self._sock = None
        self.host, self.port, self.timeout = "sim", 0, 0.0
        self.clock = clock
        self.rng = random.Random(seed)
        self.profile = profile or NeglectProfile()
        self.player_name = player_name
        self.player_online = player_online
        self.origin = origin
        self.step_s = step_s
        self.commands: list[str] = []
        self.entities: dict[int, SimEntity] = {}
        self._next_id = 1
        self.yaw_deg = 180.0
        self.pitch_deg = 0.0
        self._queue: list[int] = []  # attended entities, in order
        self._head_since: float | None = None
        self._t: float | None = None
        self.shots: list[tuple[float, list]] = []  # (time, tags) of entities the player shot

    def __enter__(self) -> "SimRcon":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    disconnect = close

    @property
    def connected(self) -> bool:
        return True

    # --- command handling -----------------------------------------------

    def cmd(self, command: str) -> str:
        with self._lock:
            self.commands.append(command)
            self._advance(self.clock())
            return self._handle(command.strip())

    def _handle(self, c: str) -> str:
        m = _SUMMON.match(c)
        if m:
            return self._summon(m)
        if c.startswith("execute if entity"):
            sel = _TAG_SELECTOR.search(c)
            n = len(self._select(sel.group(1))) if sel else 0
            return f"Test passed. Count: {n}" if n else "Test failed"
        if c.startswith("kill"):
            sel = _TAG_SELECTOR.search(c)
            found = self._select(sel.group(1)) if sel else []
            for e in found:
                e.alive = False
            if not found:
                return "No entity was found"
            if len(found) == 1:
                return f"Killed {found[0].kind.capitalize()}"
            return f"Killed {len(found)} entities"
        if c.startswith("data get entity") and "Rotation" in c:
            if not self.player_online:
                return "No entity was found"
            yaw = normalize(self.yaw_deg)
            return f"{self.player_name} has the following entity data: [{yaw:.5f}f, {self.pitch_deg:.5f}f]"
        if c.startswith("effect give @e[tag=target]") and "glowing" in c:
            hits = self._select("target")
            for e in hits:
                e.glowing = True
                self._maybe_attend(e, second_chance=True)
            return f"Applied effect Glowing to {len(hits)} entities" if hits else "No entity was found"
        if c == "list":
            names = self.player_name if self.player_online else ""
            return f"There are {int(self.player_online)} of a max of 20 players online: {names}"
        if c.startswith("time set"):
            v = c.split()[-1]
            if v.lstrip("-").isdigit():
                return f"Set minecraft:overworld to {v} tick(s)"
            return f"Set minecraft:overworld to time marker minecraft:{v}"
        if c.startswith("weather"):
            return f"Set the weather to {c.split()[-1]}"
        if c.startswith("particle"):
            return "The particle was not visible for anybody" if not self.player_online else "Displaying particle"
        if not self.player_online:
            if any(c.startswith(k) for k in ("title", "playsound", "give")):
                return "No player was found"
            if any(c.startswith(k) for k in ("tp", "effect")):
                return "No entity was found"
        return ""

    def _select(self, tag: str) -> list[SimEntity]:
        return [e for e in self.entities.values() if e.alive and tag in e.tags]

    def _summon(self, m: re.Match) -> str:
        kind, xs, ys, zs, nbt = m.groups()
        try:
            x, y, z = float(xs), float(ys), float(zs)
        except ValueError:
            return f"Incorrect argument for command\n{m.group(0)}<--[HERE]"
        tags: list[str] = []
        glowing = False
        if nbt:
            tm = re.search(r"Tags:\[([^\]]*)\]", nbt)
            if tm:
                tags = [s.strip().strip('"') for s in tm.group(1).split(",") if s.strip()]
            glowing = "Glowing:1b" in nbt
        now = self._t if self._t is not None else self.clock()
        side = "L" if "L" in tags else ("R" if "R" in tags else "")
        e = SimEntity(self._next_id, kind, tags, x, y, z, now, glowing=glowing,
                      bearing=normalize(math.degrees(math.atan2(-(x - self.origin[0]), z - self.origin[1]))),
                      side=side)
        self._next_id += 1
        self.entities[e.eid] = e
        self._maybe_attend(e)
        return f"Summoned new {kind.capitalize()}"

    # --- simulated player ---------------------------------------------------

    def _maybe_attend(self, e: SimEntity, second_chance: bool = False) -> None:
        if e.attended or not e.alive:
            return
        pr = self.profile
        # Eccentricity from the direction the player is looking. Targets are
        # spawned in that view, so "left" is left of the current yaw.
        ecc = abs(normalize(e.bearing - self.yaw_deg))
        if "distractor" in e.tags:
            p = pr.sheep_shoot_p
        elif "target" in e.tags:
            p = pr.p_notice(e.side or "R", ecc, e.glowing)
            if second_chance:
                p *= 0.5
        else:
            return
        if self.rng.random() >= p:
            return
        e.attended = True
        median = pr.search_median_s
        if e.side == "L":
            median += pr.left_search_s_per_deg * ecc
        if e.glowing:
            median *= pr.cue_search_factor
        e.search_s = self._lognormal(median, pr.search_sigma)
        self._queue.append(e.eid)

    def _lognormal(self, median: float, sigma: float) -> float:
        return median * math.exp(self.rng.gauss(0.0, sigma))

    def _advance(self, now: float) -> None:
        if self._t is None:
            self._t = now
            return
        while self._t < now:
            dt = min(self.step_s, now - self._t)
            self._t += dt
            self._step(self._t, dt)

    def _step(self, t: float, dt: float) -> None:
        pr = self.profile
        # drop attended entities that are gone (killed by the game loop)
        while self._queue and not self.entities[self._queue[0]].alive:
            self._queue.pop(0)
            self._head_since = t
        head = self.entities[self._queue[0]] if self._queue else None
        # Hold the current look. The next target is placed in this view,
        # so drifting back to north would turn the player away from it.
        goal = self.yaw_deg
        speed = pr.idle_speed_dps
        if head is not None:
            if head.turn_at is None:
                start = max(head.spawned_at, self._head_since or head.spawned_at)
                head.turn_at = start + head.search_s
            if t >= head.turn_at:
                goal, speed = head.bearing, pr.turn_speed_dps
        diff = normalize(goal - self.yaw_deg)
        move = max(-speed * dt, min(speed * dt, diff))
        self.yaw_deg = normalize(self.yaw_deg + move)
        if head is not None and t >= (head.turn_at or math.inf):
            if head.shoot_at is None and abs(normalize(head.bearing - self.yaw_deg)) < 2.0:
                head.shoot_at = t + self._lognormal(pr.aim_median_s, pr.aim_sigma)
            if head.shoot_at is not None and t >= head.shoot_at:
                head.alive = False
                self.shots.append((t, list(head.tags)))
                self._queue.pop(0)
                self._head_since = t


def normalize(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0
