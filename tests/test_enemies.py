"""Fun-mode enemy roster, tier progression and the enchanted bow."""

from __future__ import annotations

import collections

from contracts import BrainState
from director.fun import FunDirector, FunParams
from game import effects
from mc import enemies as E


def test_roster_nbt_matches_each_enemy():
    by = E.BY_KEY
    assert [e.key for e in E.ROSTER] == ["skeleton", "stray", "bogged", "pillager", "blaze"]
    assert [e.health for e in E.ROSTER] == sorted(e.health for e in E.ROSTER)  # each tougher
    pill = E.summon_nbt(by["pillager"], ["target", "t1", "L"], hold=True)
    assert 'mainhand:{id:"minecraft:crossbow"' in pill and "leather_helmet" not in pill
    assert "Health:16f" in pill and 'max_health",base:16.0d' in pill
    assert 'movement_speed",base:0.0d' in pill  # still target holds its mark
    blaze = E.summon_nbt(by["blaze"], ["target", "t2", "R", "mob"], hold=False)
    assert "equipment" not in blaze and "IsBaby" not in blaze and "movement_speed" not in blaze
    for key in ("skeleton", "stray", "bogged"):  # undead: helmet against dawn
        assert "leather_helmet" in E.summon_nbt(by[key], ["t"], hold=True)
    cmd = E.summon_cmd(by["stray"], 1, -60, -20, ["target", "t3", "L"], hold=True, glowing=True)
    assert cmd.startswith("summon minecraft:stray 1.0 -60.0 -20.0 {") and "Glowing:1b" in cmd
    assert E.get("nope").key == "skeleton"


def _fun(**kw):
    return FunDirector(params=FunParams(lead_in_s=0, **kw), seed=1)


def test_enemy_tier_follows_level_time_and_boost():
    d = _fun()
    d.start(0)
    d.on_state(BrainState(t=60, engagement_z=1.0), 60)
    assert d.enemy_tier == 0
    out = d.on_state(BrainState(t=95, engagement_z=1.0), 95)  # 90 s: level 1 -> strays
    assert d.enemy_tier == 1 and "enemy_unlock" in [x.action for x in out]
    unlock = next(x for x in out if x.action == "enemy_unlock")
    assert "Strays" in unlock.reason and "slow you" in unlock.reason
    d.boost, d.drop.refs = 2, [1.0, 1.0]  # falling engagement: two tiers early
    out = d.on_state(BrainState(t=96, engagement_z=1.0), 96)
    assert d.enemy_tier == 3 and out[-1].params["enemy"] == "pillager"
    d.boost, d.drop.refs = 0, []
    d.on_state(BrainState(t=97, engagement_z=1.0), 97)
    assert d.enemy_tier == 1  # back down, no re-announce later
    assert not any(x.action == "enemy_unlock" for x in d.on_state(BrainState(t=200), 200))


def test_spawns_mix_newest_tier_with_lower_ones():
    d = _fun()
    d.start(0)
    d.on_state(BrainState(t=400), 400)  # 400 s: level 4, blazes
    kinds = collections.Counter()
    t = 400.0
    for _ in range(2000):
        for spec in d.due_spawns(t, 0):
            if spec.kind != "sheep":
                kinds[spec.enemy] += 1
                assert spec.lifetime_s >= E.get(spec.enemy).lifetime_s
        t += 2.0
    total = sum(kinds.values())
    assert 0.4 < kinds["blaze"] / total < 0.6
    assert {"skeleton", "stray", "bogged", "pillager"} <= set(kinds)


def test_engaged_session_still_meets_every_enemy():
    d = _fun()
    d.start(0)
    seen = []
    for t in range(0, 420, 2):
        d.on_state(BrainState(t=t, engagement_z=1.5), t)  # never bored
        for s in d.due_spawns(t, 0):
            if s.kind != "sheep" and s.enemy not in seen:
                seen.append(s.enemy)
    assert seen == ["skeleton", "stray", "bogged", "pillager", "blaze"]


def test_bow_tiers_and_commands():
    from game import bow

    assert [bow.tier_for(x) for x in (0, 49, 50, 149, 150, 299, 300, 499, 500, 9999)] == \
        [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]
    assert all('"minecraft:infinity":1' in bow.bow_item(i) for i in range(len(bow.TIERS)))
    assert '"minecraft:power":5' in bow.bow_item(4) and "flame" not in bow.bow_item(1)
    fun = effects.bow_cmds("fun", "@p", tier=2)
    assert fun[0] == "clear @p minecraft:bow" and '"minecraft:flame":1' in fun[1]
    assert "levels" not in fun[1]
    assert effects.bow_cmds("rehab", "@p", tier=4) == ["clear @p minecraft:bow", "give @p minecraft:bow 1"]


def test_xp_upgrades_the_fun_bow():
    import game_loop
    from game.trials import Target

    class R:
        def __init__(self):
            self.sent = []

        def cmd(self, c):
            self.sent.append(c)
            return ""

    fx = effects.Effects(R())
    fx.session_start("fun")
    assert game_loop.bow_upgrade_decision(fx) is None
    for _ in range(2):
        fx.hit(Target("t", "L", 35, 0.0, enemy="pillager"))  # 2 x 25 XP
    up = game_loop.bow_upgrade_decision(fx)
    assert up.action == "bow_upgrade" and up.params["tier"] == 1 and "50 XP" in up.reason
    fx.apply(up)
    assert fx.bow_tier == 1 and any("Bow upgraded!" in c for c in fx.rcon.sent)
    assert game_loop.bow_upgrade_decision(fx) is None  # no repeat
    fx.false_tag()
    fx.false_tag()
    assert fx.xp == 30 and fx.bow_tier == 1  # XP can drop, the bow does not


def test_effects_apply_enemy_unlock_and_mode_bow():
    from director.base import Decision

    class R:
        def __init__(self):
            self.sent = []

        def cmd(self, c):
            self.sent.append(c)
            return ""

    r = R()
    fx = effects.Effects(r)
    fx.apply(Decision("enemy_unlock", {"label": "Strays", "threat": "their arrows slow you"},
                      reason="new enemy"))
    assert any("New enemy!" in c for c in r.sent) and any("Strays" in c for c in r.sent)
    r.sent.clear()
    fx.apply(Decision("mode", {"mode": "fun"}, reason="HUD"))
    assert any("minecraft:enchantments" in c for c in r.sent)
    r.sent.clear()
    fx.apply(Decision("mode", {"mode": "rehab"}, reason="HUD"))
    assert "give @p minecraft:bow 1" in r.sent


def test_sim_fun_session_logs_enemy_progression(tmp_path):
    import game_loop
    from log.events import EventLogger
    from mc.sim import SimRcon
    from tests.test_game import read_csv

    class Bored:
        def get(self, now):
            return BrainState(t=now, engagement_z=-1.0, quality="ok")

    clock = game_loop.SimClock(start=1_800_000_000.0, speed=0)
    rcon = SimRcon(clock=clock.now, seed=2)
    logger = EventLogger("test-enemies", directory=tmp_path)
    game_loop.run_session(mode="fun", rcon=rcon, logger=logger, clock=clock, duration_s=420,
                          sim=True, brain=Bored(), out=lambda *_: None)
    logger.close()
    events = read_csv(logger.path)
    unlocks = [e for e in events if e["event"] == "decision" and e["value"].startswith("enemy_unlock")]
    order = [u["value"].split("enemy=")[1].split()[0] for u in unlocks]
    assert order == ["stray", "bogged", "pillager", "blaze"]
    first_unlock = float(unlocks[0]["t"]) - 1_800_000_000.0
    assert first_unlock < 90  # bored player: boosted ahead of the time schedule
    assert any(e["value"].startswith("boost_up") for e in events if e["event"] == "decision")
    assert any(e["event"] == "spawn" and e["value"].endswith(":blaze") for e in events)
    assert any(c.startswith("summon minecraft:pillager") for c in rcon.commands)


def test_kill_xp_and_effects():
    from game.trials import Target

    assert [E.kill_xp(e.key) for e in E.ROSTER] == [5, 10, 15, 25, 40]
    assert E.kill_xp("blaze", "mob") == 60 and E.kill_xp(None) == 5

    class R:
        def __init__(self):
            self.sent = []

        def cmd(self, c):
            self.sent.append(c)
            return ""

    r = R()
    fx = effects.Effects(r)
    fx.hit(Target("t1", "L", 35, 0.0, kind="target", enemy="pillager"))
    assert "xp add @p 25 points" in r.sent and any("+25 XP" in c for c in r.sent)
    fx.false_tag()
    assert "xp add @p -10 points" in r.sent
    r.sent.clear()
    fx.session_start("fun")
    assert "xp set @p 0 levels" in r.sent and "xp set @p 0 points" in r.sent


def test_fun_storm_and_lightning_stay_clear_of_targets(tmp_path):
    import math
    import re

    import game_loop
    from log.events import EventLogger
    from mc import config
    from mc.sim import SimRcon
    from tests.test_game import read_csv

    assert effects.weather_cmd("fun") == "weather thunder"
    assert effects.weather_cmd("rehab") == "weather clear"
    clock = game_loop.SimClock(start=1_800_000_000.0, speed=0)
    rcon = SimRcon(clock=clock.now, seed=4)
    logger = EventLogger("test-storm", directory=tmp_path)
    game_loop.run_session(mode="fun", rcon=rcon, logger=logger, clock=clock, duration_s=300,
                          sim=True, brain=game_loop.FakeBrain(seed=1), out=lambda *_: None)
    logger.close()
    assert "weather thunder" in rcon.commands
    bolts = [c for c in rcon.commands if c.startswith("summon minecraft:lightning_bolt")]
    assert 6 <= len(bolts) <= 21  # one every 15-40 s
    for c in bolts:
        x, _, z = map(float, re.findall(r"-?\d+\.\d", c)[:3])
        d = math.hypot(x - (config.STAND_X + 0.5), z - (config.STAND_Z + 0.5))
        assert d >= config.TARGET_DISTANCE + 10  # well behind the targets
    rehab = SimRcon(clock=clock.now, seed=4)
    logger = EventLogger("test-calm", directory=tmp_path)
    game_loop.run_session(mode="rehab", rcon=rehab, logger=logger, clock=clock, duration_s=120,
                          sim=True, brain=game_loop.FakeBrain(seed=1), out=lambda *_: None)
    logger.close()
    assert "weather clear" in rehab.commands and "weather thunder" not in rehab.commands
    assert not any("lightning_bolt" in c for c in rehab.commands)
