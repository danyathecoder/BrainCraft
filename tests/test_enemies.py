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
    d.boost, d._boost_ref = 2, [1.0, 1.0]  # falling engagement: two tiers early
    out = d.on_state(BrainState(t=96, engagement_z=1.0), 96)
    assert d.enemy_tier == 3 and out[-1].params["enemy"] == "pillager"
    d.boost, d._boost_ref = 0, []
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


def test_bow_commands_per_mode():
    fun = effects.bow_cmds("fun", "@p")
    assert fun[0] == "clear @p minecraft:bow"
    assert fun[1].startswith('give @p minecraft:bow[minecraft:enchantments={"minecraft:power":3')
    assert '"minecraft:infinity":1' in fun[1] and "levels" not in fun[1]
    assert effects.bow_cmds("rehab", "@p") == ["clear @p minecraft:bow", "give @p minecraft:bow 1"]


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
