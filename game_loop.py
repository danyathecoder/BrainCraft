"""BrainCraft game loop: brain state + game events -> director -> RCON.

    python game_loop.py --mode rehab --sim --name demo --duration 120 --speed 20 --no-service
    python game_loop.py --mode rehab --rcon --name anna --duration 600

~5 Hz loop. Once per (game) second: read the brain state (GET /state)
and mode (GET /mode), feed the director, log a state row, POST /status.
Every poll: check targets (hit / miss / found), feed results to the
director, apply decisions (world effects + actionbar echo + CSV + POST
/decisions), spawn what the director asks for.

Brain state source:
- --no-service: built-in fake brain (neutral with slow random drift,
  optional scripted fatigue episode with --fake-fatigue-at), no HTTP.
- service reachable: GET /state.
- service unreachable: --sim falls back to the fake brain; --rcon uses
  a neutral state with quality "bad", so EEG rules pause (safe).

Ctrl+C stops cleanly and prints the session CSV paths.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import fields as dc_fields
from dataclasses import replace
from pathlib import Path

from contracts import MODES, BrainState, GameStatus
from director import make_director
from director.base import Decision
from game.effects import Effects
from game.trials import TrialRunner
from log.events import SESSIONS_DIR, EventLogger
from mc import config
from mc.rcon import RconError

LOOP_HZ = 5.0
STATE_FIELD_NAMES = {f.name for f in dc_fields(BrainState)}


# --- clocks ------------------------------------------------------------------


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, dt: float) -> None:
        if dt > 0:
            time.sleep(dt)


class SimClock:
    """Simulated time starting at `start` (default: now). sleep(dt) advances
    the clock by dt and sleeps dt / speed in real time (speed <= 0: no sleep)."""

    def __init__(self, start: float | None = None, speed: float = 1.0):
        self.t = time.time() if start is None else start
        self.speed = speed
        self._t0 = self.t
        self._real0 = time.perf_counter()

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        if dt <= 0:
            return
        self.t += dt
        if self.speed > 0 and math.isfinite(self.speed):
            # pace against a real-time anchor so loop overhead does not add up
            due = self._real0 + (self.t - self._t0) / self.speed
            wait = due - time.perf_counter()
            if wait > 0:
                time.sleep(wait)


# --- brain state sources ---------------------------------------------------


class FakeBrain:
    """Neutral brain state with slow random drift (Ornstein-Uhlenbeck), seeded.

    fatigue_at: seconds after the first call when a fatigue episode starts
    (fatigue_z ramps up to ~+2.3 and engagement drops for fatigue_len_s).
    calm_at: seconds after the first call when a calm (bored) episode
    starts (engagement_z ramps down to ~-1.2 for calm_len_s).
    """

    def __init__(self, seed: int | None = 0, fatigue_at: float | None = None,
                 fatigue_len_s: float = 40.0, bad_quality_p: float = 0.02,
                 tau_s: float = 30.0, sd: float = 0.35,
                 calm_at: float | None = None, calm_len_s: float = 60.0):
        self.rng = random.Random(seed)
        self.fatigue_at = fatigue_at
        self.fatigue_len_s = fatigue_len_s
        self.calm_at = calm_at
        self.calm_len_s = calm_len_s
        self.bad_quality_p = bad_quality_p
        self.tau, self.sd = tau_s, sd
        self.x = {"engagement_z": 0.0, "fatigue_z": 0.0, "alpha_low_z": 0.0}
        self._t0: float | None = None
        self._last: float | None = None

    def get(self, now: float) -> BrainState:
        if self._t0 is None:
            self._t0 = self._last = now
        dt = min(max(now - self._last, 0.0), 5.0)
        self._last = now
        for k in self.x:  # OU: mean 0, stationary sd = self.sd
            self.x[k] += -self.x[k] * dt / self.tau + self.sd * math.sqrt(2 * dt / self.tau) * self.rng.gauss(0, 1)
        eng, fat, alpha = self.x["engagement_z"], self.x["fatigue_z"], self.x["alpha_low_z"]
        if self.fatigue_at is not None:
            el = now - self._t0 - self.fatigue_at
            if 0 <= el < self.fatigue_len_s:
                ramp = min(el / 5.0, 1.0)
                fat += 2.3 * ramp
                eng -= 0.8 * ramp
                alpha += 1.0 * ramp
        if self.calm_at is not None:
            el = now - self._t0 - self.calm_at
            if 0 <= el < self.calm_len_s:
                eng -= 1.2 * min(el / 5.0, 1.0)
        bad = self.rng.random() < self.bad_quality_p
        artifact = "none"
        r = self.rng.random()
        if r < 0.03:
            artifact = "blink"
        elif r < 0.035:
            artifact = "jaw"
        return BrainState(t=now, engagement_z=round(eng, 3), fatigue_z=round(fat, 3),
                          alpha_low_z=round(alpha, 3), quality="bad" if bad else "ok",
                          artifact="motion" if bad else artifact, jaw_clench=artifact == "jaw")


class ServiceClient:
    """Best-effort HTTP client for state_service.py (stdlib only, never raises)."""

    def __init__(self, url: str = "http://localhost:8000", timeout_s: float = 0.5, retry_s: float = 5.0):
        self.url = url.rstrip("/")
        self.timeout = timeout_s
        self.retry_s = retry_s
        self.up: bool | None = None
        self._retry_at = 0.0

    def _req(self, method: str, path: str, body: dict | None = None):
        if self.up is False and time.monotonic() < self._retry_at:
            return None
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read().decode() or "null")
            self.up = True
            return out
        except (urllib.error.URLError, OSError, ValueError):
            self.up = False
            self._retry_at = time.monotonic() + self.retry_s
            return None

    def get_state(self) -> BrainState | None:
        d = self._req("GET", "/state")
        if not isinstance(d, dict):
            return None
        try:
            return BrainState(**{k: v for k, v in d.items() if k in STATE_FIELD_NAMES})
        except TypeError:
            return None

    def get_mode(self) -> str | None:
        d = self._req("GET", "/mode")
        mode = d.get("mode") if isinstance(d, dict) else None
        return mode if mode in MODES else None

    def post_status(self, status: GameStatus) -> None:
        self._req("POST", "/status", status.to_dict())

    def post_decision(self, d: Decision, mode: str, t: float) -> None:
        self._req("POST", "/decisions", {"value": d.value, "reason": d.reason, "mode": mode, "t": t})


# --- the loop ------------------------------------------------------------------


def run_session(*, mode: str, rcon, logger, clock, duration_s: float, sim: bool = False,
                brain: FakeBrain | None = None, service: ServiceClient | None = None,
                seed: int | None = 0, hz: float = LOOP_HZ, out=print) -> dict:
    """Run one session; returns a summary dict. Does not close the logger."""
    directors = {}

    def get_director(m: str):
        if m not in directors:
            directors[m] = make_director(m, seed=seed)
        return directors[m]

    effects = Effects(rcon)
    runner = TrialRunner(rcon, logger, clock=clock.now)
    t0 = clock.now()
    fallback_brain = brain
    warned = set()

    def say(key: str, msg: str) -> None:
        if key not in warned:
            warned.add(key)
            out(msg)

    def apply(d: Decision, now: float) -> None:
        logger.event("decision", t=f"{now:.3f}", value=d.value, reason=d.reason)
        if d.action == "rest_start":
            logger.event("rest_start", t=f"{now:.3f}", value=d.params.get("duration_s", ""), reason=d.reason)
        elif d.action == "rest_end":
            logger.event("rest_end", t=f"{now:.3f}", reason=d.reason)
        try:
            if d.action in ("rest_start", "breather_start"):
                runner.cancel_all()
            effects.apply(d)
        except RconError as exc:
            out(f"RCON error while applying {d.action}: {exc}")
        if service is not None:
            service.post_decision(d, current, now)
        out(f"[{now - t0:7.1f}s] {d.value}  ←  {d.reason}")

    def read_state(now: float) -> BrainState:
        nonlocal fallback_brain
        if service is not None:
            s = service.get_state()
            if s is not None:
                if time.time() - s.t > 5.0:
                    say("stale", "state service: brain state is stale (> 5 s old) → quality treated as bad")
                    s = replace(s, quality="bad")
                return replace(s, t=now)
            if sim:
                say("fake", f"state service at {service.url} unreachable → using the built-in fake brain state")
                if fallback_brain is None:
                    fallback_brain = FakeBrain(seed=seed)
                return fallback_brain.get(now)
            say("down", f"state service at {service.url} unreachable → neutral state with quality "
                        f"'bad' (EEG rules paused)")
            return BrainState(t=now, quality="bad")
        if fallback_brain is None:
            fallback_brain = FakeBrain(seed=seed)
        return fallback_brain.get(now)

    current = mode
    director = get_director(current)
    effects.session_start(current)
    for d in director.start(t0):
        apply(d, t0)
    next_second = t0
    dt = 1.0 / hz
    try:
        rcon_failures = 0
        while True:
            now = clock.now()
            if now - t0 >= duration_s:
                break
            try:
                decisions: list[Decision] = []
                tick = now >= next_second
                if tick:
                    next_second += 1.0
                    if service is not None:
                        m = service.get_mode()
                        if m and m != current:
                            old, current = current, m
                            director = get_director(current)
                            apply(Decision("mode", {"mode": current},
                                           reason=f"HUD switched mode {old} → {current}"), now)
                            decisions += director.start(now)
                    state = read_state(now)
                    decisions += director.on_state(state, now)
                    logger.state(state)
                    yaw = rcon.yaw()
                    if yaw is not None:
                        logger.event("yaw", t=f"{now:.3f}", value=f"{yaw:.1f}")
                for d in decisions:
                    apply(d, now)
                for r in runner.poll(now):
                    if r.outcome == "hit":
                        effects.hit(r.target)
                    elif r.outcome == "miss":
                        effects.miss(r.target)
                    elif r.outcome == "false_tag":
                        effects.false_tag(r.target)
                    owner = directors.get(r.target.owner, director)
                    for d in owner.on_trial_result(r.target.side, r.hit, now, kind=r.target.kind,
                                                   ecc_deg=r.target.ecc_deg):
                        apply(d, now)
                for spec in director.due_spawns(now, runner.n_active()):
                    runner.spawn(spec.side, spec.ecc_deg, spec.cued, now=now, kind=spec.kind,
                                 lifetime_s=spec.lifetime_s, owner=current)
                if tick and service is not None:
                    service.post_status(game_status(director, runner, now))
                rcon_failures = 0
            except RconError as exc:
                rcon_failures += 1
                out(f"RCON error ({rcon_failures} in a row): {exc}")
                if rcon_failures >= 10:
                    raise
                clock.sleep(1.0)
            clock.sleep(dt)
    finally:
        try:
            runner.cancel_all()
            effects.session_end()
        except Exception as exc:  # connection gone on Ctrl+C: nothing to clean up
            out(f"cleanup skipped: {exc}")
    return {"mode": current, "events_csv": str(logger.path), "state_csv": str(logger.state_path),
            "stats": dict(runner.stats), "score": runner.score, "status": game_status(director, runner, clock.now()).to_dict()}


def game_status(director, runner: TrialRunner, now: float) -> GameStatus:
    st = director.status()
    s = runner.stats
    return GameStatus(t=now, mode=st["mode"], resting=st["resting"], spawn_interval_s=st["spawn_interval_s"],
                      p_left=st["p_left"], left_ecc_deg=st["left_ecc_deg"], hits_L=s["hits_L"],
                      misses_L=s["misses_L"], hits_R=s["hits_R"], misses_R=s["misses_R"], score=runner.score)


# --- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BrainCraft game loop")
    ap.add_argument("--mode", choices=MODES, default="rehab")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sim", action="store_true", help="simulated server + player (no Minecraft)")
    g.add_argument("--rcon", action="store_true", help="real Minecraft server via RCON (mc/config.py)")
    ap.add_argument("--name", default=None, help="session name (CSV file name suffix)")
    ap.add_argument("--duration", type=float, default=600.0, help="session length in seconds")
    ap.add_argument("--speed", type=float, default=1.0, help="sim only: time acceleration (0 = as fast as possible)")
    ap.add_argument("--state-url", default="http://localhost:8000")
    ap.add_argument("--no-service", action="store_true", help="no state service: built-in fake brain state")
    ap.add_argument("--fake-fatigue-at", type=float, default=None,
                    help="fake brain only: start a 40 s fatigue episode at this second")
    ap.add_argument("--fake-calm-at", type=float, default=None,
                    help="fake brain only: start a 60 s low-engagement (bored) episode at this second")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--log-dir", type=Path, default=SESSIONS_DIR)
    args = ap.parse_args(argv)

    if args.rcon and args.speed != 1.0:
        ap.error("--speed only works with --sim")
    try:  # the demo laptop console may not be UTF-8
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    name = args.name or f"{args.mode}-{'sim' if args.sim else 'mc'}"
    logger = EventLogger(name, directory=args.log_dir)
    service = None if args.no_service else ServiceClient(args.state_url)
    brain = FakeBrain(seed=args.seed, fatigue_at=args.fake_fatigue_at, calm_at=args.fake_calm_at) if (args.no_service or args.sim) else None
    if args.no_service:
        print("no state service: using the built-in fake brain state (neutral, slow drift)")
    try:
        if args.sim:
            from mc.sim import SimRcon

            clock = SimClock(speed=args.speed)
            rcon = SimRcon(clock=clock.now, seed=args.seed)
        else:
            from mc.rcon import Rcon

            clock = RealClock()
            rcon = Rcon()
        print(f"{args.mode} session '{name}', {args.duration:g} s, "
              f"{'sim x' + format(args.speed, 'g') if args.sim else 'RCON ' + config.RCON_HOST}  (Ctrl+C stops)")
        with rcon:
            summary = run_session(mode=args.mode, rcon=rcon, logger=logger, clock=clock,
                                  duration_s=args.duration, sim=args.sim, brain=brain,
                                  service=service, seed=args.seed)
        print("stats:", summary["stats"], "score:", summary["score"])
    except KeyboardInterrupt:
        print("\nstopped (Ctrl+C)")
    except (RconError, ConnectionError) as exc:
        print(f"RCON failed: {exc}")
        return 1
    finally:
        logger.close()
        print(f"events: {logger.path}")
        print(f"state:  {logger.state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
