"""Game side: yaw math, trials with SimRcon + fake clock, effects, full sim session."""

from __future__ import annotations

import csv
import math

import pytest

import game_loop
from contracts import EVENT_FIELDS, STATE_FIELDS, BrainState
from game import effects
from game.geometry import target_position
from director.rehab import RehabParams
from game.trials import TrialRunner, bearing_yaw, normalize_yaw, yaw_diff, yaw_towards
from mc import config
from mc.sim import NeglectProfile, SimRcon


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


class ListLogger:
    def __init__(self):
        self.rows = []

    def event(self, event, **fields):
        self.rows.append({"event": event, **fields})

    def of(self, event):
        return [r for r in self.rows if r["event"] == event]


def perfect_player(**kw) -> NeglectProfile:
    base = dict(p_right=1.0, p_left_by_ecc=((0, 1.0), (90, 1.0)), p_max=1.0, search_sigma=0.0, aim_sigma=0.0)
    base.update(kw)
    return NeglectProfile(**base)


def blind_player() -> NeglectProfile:
    return NeglectProfile(p_right=0.0, p_left_by_ecc=((0, 0.0), (90, 0.0)), cue_boost=0.0, sheep_shoot_p=0.0)


def run_until_result(runner, clock, max_s=10.0, dt=0.2):
    end = clock.t + max_s
    while clock.t < end:
        clock.t += dt
        res = runner.poll(clock.t)
        if res:
            return res
    return []


# --- yaw math -----------------------------------------------------------------


@pytest.mark.parametrize("a,expected", [(180, -180), (-180, -180), (540, -180), (-190, 170),
                                        (190, -170), (360, 0), (-90, -90), (270, -90), (720.5, 0.5)])
def test_normalize_yaw(a, expected):
    assert normalize_yaw(a) == pytest.approx(expected)


def test_yaw_diff_wraps():
    assert yaw_diff(179, -179) == pytest.approx(-2)
    assert yaw_diff(-179, 179) == pytest.approx(2)
    assert abs(yaw_diff(540, 180)) == pytest.approx(0)


def test_bearing_convention():
    assert bearing_yaw("L", 15) == pytest.approx(165)
    assert bearing_yaw("R", 35) == pytest.approx(-145)  # 215 normalized
    assert bearing_yaw("L", 90) == pytest.approx(90)  # west = -X
    assert bearing_yaw("R", 90) == pytest.approx(-90)  # east = +X
    assert abs(yaw_diff(bearing_yaw("R", 0), 180)) < 1e-9
    with pytest.raises(ValueError):
        bearing_yaw("X", 10)


@pytest.mark.parametrize("side", ["L", "R"])
@pytest.mark.parametrize("ecc", config.ECCENTRICITIES)
@pytest.mark.parametrize("facing", [180, -180, 0, 90, -90, 40])
def test_bearing_matches_geometry(side, ecc, facing):
    x, _, z = target_position(side, ecc, facing_yaw=facing)
    ox, oz = config.STAND_X + 0.5, config.STAND_Z + 0.5
    assert abs(yaw_diff(yaw_towards(x - ox, z - oz), bearing_yaw(side, ecc, facing))) < 1e-6
    assert abs(yaw_diff(bearing_yaw(side, ecc, facing), facing)) == pytest.approx(ecc)


def test_spawn_angles_fit_in_the_fov():
    # FOV 70 is vertical. Half the horizontal view on 16:9, minus a few
    # degrees so the mob body is inside the frame, not only its center.
    half = math.degrees(math.atan(math.tan(math.radians(config.CLIENT_FOV_DEG / 2)) * 16 / 9))
    limit = half - 5
    angles = (*config.ECCENTRICITIES, RehabParams().right_ecc_deg)
    assert angles
    assert max(angles) <= limit


# --- SimRcon replies ---------------------------------------------------------------


def test_sim_replies_match_real_server():
    clock = FakeClock()
    r = SimRcon(clock=clock.now, profile=blind_player())
    assert r.summon_target("t1", 1, -60, -20, "L", entity="minecraft:zombie") == "Summoned new Zombie"
    assert r.cmd("execute if entity @e[tag=t1]") == "Test passed. Count: 1"
    assert r.exists("t1")
    assert r.kill("t1") == "Killed Zombie"
    assert r.cmd("execute if entity @e[tag=t1]") == "Test failed"
    assert not r.exists("t1")
    assert r.yaw() == pytest.approx(-180.0)
    assert r.cmd("time set 23000") == "Set minecraft:overworld to 23000 tick(s)"
    assert r.cmd("weather clear") == "Set the weather to clear"
    off = SimRcon(clock=clock.now, player_online=False)
    assert off.yaw() is None
    assert off.title("hi") == "No player was found"
    assert r.commands[0].startswith("summon minecraft:zombie")
    assert r.summon_target("t2", 1, -60, -20, "L").startswith("Summoned new ")
    assert r.players_online() == ["SimPlayer"] and off.players_online() == []


# --- trials ----------------------------------------------------------------------


def test_spawn_is_in_front_of_where_the_player_looks():
    clock = FakeClock()
    rcon = SimRcon(clock=clock.now, profile=blind_player())
    rcon.yaw_deg = 0.0  # south, +Z
    runner = TrialRunner(rcon, ListLogger(), clock=clock.now)
    t = runner.spawn("R", 0)
    ox, oz = config.STAND_X + 0.5, config.STAND_Z + 0.5
    x, _, z = t.pos
    assert t.bearing == pytest.approx(0)
    assert z == pytest.approx(oz + config.TARGET_DISTANCE)
    assert x == pytest.approx(ox)
    rcon.yaw_deg = -90.0  # east, +X
    left = runner.spawn("L", 30)
    assert left.bearing == pytest.approx(-120)
    assert abs(yaw_diff(left.bearing, -90.0)) == pytest.approx(30)


def test_hit_found_and_rt():
    clock = FakeClock()
    rcon = SimRcon(clock=clock.now, seed=1, profile=perfect_player())
    log = ListLogger()
    runner = TrialRunner(rcon, log, clock=clock.now)
    t = runner.spawn("R", 35)
    assert t.target_id == "t1" and t.bearing == pytest.approx(-145)
    assert log.of("spawn")[0]["side"] == "R" and log.of("spawn")[0]["ecc_deg"] == "35"
    res = run_until_result(runner, clock)
    assert len(res) == 1 and res[0].outcome == "hit"
    found, hit = log.of("found")[0], log.of("hit")[0]
    assert 0 < found["rt_ms"] < hit["rt_ms"] < 6000
    # perfect player: search 0.45 s + 35 deg at 160 deg/s + aim 0.7 s ~ 1.4 s
    assert 1000 < hit["rt_ms"] < 2000
    assert runner.stats["hits_R"] == 1 and runner.score == 1 and not runner.active


def test_miss_after_lifetime_kills_target():
    clock = FakeClock()
    rcon = SimRcon(clock=clock.now, seed=1, profile=blind_player())
    log = ListLogger()
    runner = TrialRunner(rcon, log, clock=clock.now)
    runner.spawn("L", 75)
    res = run_until_result(runner, clock)
    assert res[0].outcome == "miss" and not res[0].hit
    assert clock.t - 1000 == pytest.approx(6.0, abs=0.21)
    assert "kill @e[tag=t1]" in rcon.commands
    assert not rcon.exists("t1")
    assert log.of("found") == [] and log.of("miss")[0]["side"] == "L"
    assert runner.stats["misses_L"] == 1


def test_yaw_none_no_found_but_hits_work():
    clock = FakeClock()
    rcon = SimRcon(clock=clock.now, profile=perfect_player(), player_online=False)
    log = ListLogger()
    runner = TrialRunner(rcon, log, clock=clock.now)
    runner.spawn("L", 15)
    res = run_until_result(runner, clock)
    assert res[0].outcome == "hit" and log.of("found") == []


def test_cued_spawn_glows_and_plays_sound():
    clock = FakeClock()
    rcon = SimRcon(clock=clock.now, profile=blind_player())
    runner = TrialRunner(rcon, ListLogger(), clock=clock.now)
    runner.spawn("L", 55, cued=True)
    summon = next(c for c in rcon.commands if c.startswith("summon"))
    assert "Glowing:1b" in summon
    assert "NoAI" not in summon
    assert "minecraft:bow" in summon and "minecraft:movement_speed" in summon
    assert any(c.startswith(f"playsound {effects.CUE_SOUND} master {config.PLAYER} ") for c in rcon.commands)
    clock.t += 2.2
    runner.poll(clock.t)
    assert sum(c.startswith(f"playsound {effects.CUE_SOUND}") for c in rcon.commands) == 2  # repeated once


def test_sheep_false_tag_and_mob():
    clock = FakeClock()
    rcon = SimRcon(clock=clock.now, profile=perfect_player(sheep_shoot_p=1.0))
    log = ListLogger()
    runner = TrialRunner(rcon, log, clock=clock.now)
    s = runner.spawn("R", 15, kind="sheep", lifetime_s=8)
    assert s.target_id == "d1" and any("minecraft:sheep" in c for c in rcon.commands)
    assert runner.n_active() == 0 and runner.n_active(include_sheep=True) == 1
    res = run_until_result(runner, clock)
    assert res[0].outcome == "false_tag" and runner.stats["false_tags"] == 1
    m = runner.spawn("L", 35, kind="mob", lifetime_s=14)
    assert m.target_id == "t1" and "NoAI" not in rcon.commands[-1] and '"mob"' in rcon.commands[-1]
    assert "minecraft:skeleton" in rcon.commands[-1] and config.TARGET_GEAR_NBT in rcon.commands[-1]
    assert "minecraft:bow" in rcon.commands[-1] and "movement_speed" not in rcon.commands[-1]
    assert run_until_result(runner, clock)[0].outcome == "hit"


# --- effects -----------------------------------------------------------------------


def test_effect_commands_syntax():
    assert effects.actionbar_cmd('say "hi" → ok') == 'title @p actionbar {"text": "say \\"hi\\" → ok"}'
    assert effects.smoke_cmd(1, -60, -20).startswith("particle minecraft:large_smoke 1.0 -59.0 -20.0 ")
    clock = FakeClock()
    rcon = SimRcon(clock=clock.now)
    fx = effects.Effects(rcon)
    fx.rest_start(45)
    fx.rest_end()
    assert "time set 23000" in rcon.commands and "time set midnight" in rcon.commands
    assert any("Take a breath" in c for c in rcon.commands)
    assert any("Back to the watch" in c for c in rcon.commands)
    fx.flare(5)
    assert "effect give @e[tag=target] minecraft:glowing 5 0 true" in rcon.commands
    fx.darkness(30)
    assert f"effect give {config.PLAYER} minecraft:darkness 30 0 true" in rcon.commands


def test_send_warns_once_on_syntax_error(capsys):
    class Bad:
        def cmd(self, c):
            return "Unknown or incomplete command, see below for error\nfoo 1<--[HERE]"

    effects._warned.clear()
    effects.send(Bad(), "foo 1")
    effects.send(Bad(), "foo 2")
    assert capsys.readouterr().out.count("WARNING") == 1


# --- full sessions -----------------------------------------------------------------


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_two_minute_rehab_session(tmp_path):
    from log.events import EventLogger

    clock = game_loop.SimClock(start=1_800_000_000.0, speed=0)
    rcon = SimRcon(clock=clock.now, seed=4)
    logger = EventLogger("test-rehab", directory=tmp_path)
    brain = game_loop.FakeBrain(seed=1, fatigue_at=20)
    summary = game_loop.run_session(mode="rehab", rcon=rcon, logger=logger, clock=clock,
                                    duration_s=120, sim=True, brain=brain, out=lambda *_: None)
    logger.close()
    events = read_csv(logger.path)
    states = read_csv(logger.state_path)
    assert list(events[0]) == EVENT_FIELDS and list(states[0]) == STATE_FIELDS
    kinds = [e["event"] for e in events]
    assert kinds.count("spawn") >= 10
    assert kinds.count("hit") >= 5
    assert "rest_start" in kinds and "rest_end" in kinds
    decisions = [e for e in events if e["event"] == "decision"]
    assert decisions and all(d["reason"].strip() for d in decisions)
    assert any("fatigue_z > 1.5 for 20 s" in d["reason"] for d in decisions)
    hits = [e for e in events if e["event"] == "hit"]
    assert all(0 < int(h["rt_ms"]) < 6000 and h["side"] in "LR" and h["target_id"] for h in hits)
    ts = [float(e["t"]) for e in events]
    assert ts == sorted(ts) and 1_800_000_000 <= ts[0] and ts[-1] <= 1_800_000_000 + 121
    assert 115 <= len(states) <= 121
    assert summary["stats"]["spawns"] == kinds.count("spawn")
    assert "time set 23000" in rcon.commands


def test_rehab_warmup_right_then_ali_bias_left(tmp_path):
    """Warm-up minute is right-only; a sustained negative ALI then favours the left."""
    from log.events import EventLogger

    class Brain:
        def __init__(self):
            self.t0 = None

        def get(self, now):
            if self.t0 is None:
                self.t0 = now
            ali = 0.1 if now - self.t0 < 100 else -0.3
            return BrainState(t=now, ali=ali, quality="ok")

    t0 = 1_800_000_000.0
    clock = game_loop.SimClock(start=t0, speed=0)
    rcon = SimRcon(clock=clock.now, seed=3)
    logger = EventLogger("test-rehab-ali", directory=tmp_path)
    game_loop.run_session(mode="rehab", rcon=rcon, logger=logger, clock=clock,
                          duration_s=300, sim=True, brain=Brain(), out=lambda *_: None)
    logger.close()
    events = read_csv(logger.path)
    spawns = [e for e in events if e["event"] == "spawn"]
    assert all(e["side"] == "R" for e in spawns if float(e["t"]) - t0 < 60)
    late = [e["side"] for e in spawns if float(e["t"]) - t0 >= 115]
    assert late and late.count("L") / len(late) > 0.6
    reasons = [e["reason"] for e in events if e["event"] == "decision"]
    assert any("warm-up on the right over" in r for r in reasons)
    assert sum("attention biased right" in r for r in reasons) == 1  # no flicker
    assert "ali" in read_csv(logger.state_path)[0]


def test_session_with_misses(tmp_path):
    from log.events import EventLogger

    clock = game_loop.SimClock(start=1_800_000_000.0, speed=0)
    rcon = SimRcon(clock=clock.now, seed=7, profile=NeglectProfile(p_left_by_ecc=((0, 0.3), (90, 0.1))))
    logger = EventLogger("test-miss", directory=tmp_path)
    # Unhit targets live 60 s, only one is up, and the first minute is a
    # right-side warm-up, so a left staircase step needs a long session.
    game_loop.run_session(mode="rehab", rcon=rcon, logger=logger, clock=clock, duration_s=1200,
                          sim=True, brain=game_loop.FakeBrain(seed=2), out=lambda *_: None)
    events = read_csv(logger.path)
    kinds = [e["event"] for e in events]
    assert kinds.count("miss") >= 3
    assert any(e["event"] == "decision" and e["value"].startswith("staircase_down") for e in events)
    assert any(e["event"] == "spawn" and e["value"] == "cued" for e in events)


class FakeService:
    """Stands in for state_service: switches to fun mode after 30 s."""

    url = "fake"

    def __init__(self, clock, t0):
        self.clock, self.t0 = clock, t0
        self.statuses, self.decisions = [], []

    def get_state(self):
        import time

        return BrainState(t=time.time(), engagement_z=-1.0)

    def get_mode(self):
        return "fun" if self.clock.now() - self.t0 > 30 else "rehab"

    def post_status(self, s):
        self.statuses.append(s)

    def post_decision(self, d, mode, t):
        self.decisions.append((d, mode))


def test_fun_mode_and_live_switch(tmp_path):
    from log.events import EventLogger

    clock = game_loop.SimClock(start=1_800_000_000.0, speed=0)
    svc = FakeService(clock, clock.now())
    rcon = SimRcon(clock=clock.now, seed=5)
    logger = EventLogger("test-fun", directory=tmp_path)
    summary = game_loop.run_session(mode="rehab", rcon=rcon, logger=logger, clock=clock,
                                    duration_s=90, sim=True, service=svc, out=lambda *_: None)
    assert summary["mode"] == "fun"
    actions = [d.action for d, _ in svc.decisions]
    assert "mode" in actions and "escalate" in actions
    assert any(m == "fun" for _, m in svc.decisions)
    assert 85 <= len(svc.statuses) <= 91 and svc.statuses[-1].mode == "fun"
    events = read_csv(logger.path)
    assert any(e["event"] == "decision" and "HUD switched mode rehab → fun" in e["reason"] for e in events)


def test_cli_sim_no_service(tmp_path, capsys):
    rc = game_loop.main(["--mode", "fun", "--sim", "--no-service", "--duration", "20", "--speed", "0",
                         "--name", "cli", "--log-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "events:" in out and "state:" in out
    assert len(list(tmp_path.glob("*-cli.csv"))) == 1 and len(list(tmp_path.glob("*-cli-state.csv"))) == 1
