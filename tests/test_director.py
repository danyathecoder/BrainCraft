"""Director logic: staircase, cues, fatigue pacing, challenge, fun mode."""

from __future__ import annotations

import pytest

from contracts import BrainState
from director import make_director
from director.base import Decision, HoldTimer
from director.fun import FunDirector
from director.rehab import RehabDirector, RehabParams


def st(eng=0.0, fat=0.0, quality="ok", jaw=False) -> BrainState:
    return BrainState(t=0, engagement_z=eng, fatigue_z=fat, quality=quality, jaw_clench=jaw)


def feed(d, results, side="L", t0=0.0):
    out = []
    for i, hit in enumerate(results):
        out += d.on_trial_result(side, hit, t0 + i)
    return out


def all_have_reasons(decisions):
    return all(isinstance(x, Decision) and x.reason.strip() for x in decisions)


# --- base -------------------------------------------------------------------


def test_decision_needs_reason():
    with pytest.raises(ValueError):
        Decision("rest_start", {}, reason="")
    assert Decision("spawn_interval", {"old": 4.0, "new": 3.5}, "x").value == "spawn_interval old=4 new=3.5"


def test_hold_timer_ignores_bad_quality_and_resets():
    h = HoldTimer(5)
    for t in range(4):
        assert not h.update(True, t)
    assert not h.update(True, 4, valid=False)  # bad second: no time added, no reset
    assert h.elapsed == 4
    assert h.update(True, 5)
    assert not h.update(False, 6)  # good-quality false sample resets
    assert h.elapsed == 0


def test_make_director():
    assert make_director("rehab").mode == "rehab"
    assert make_director("fun").mode == "fun"
    with pytest.raises(ValueError):
        make_director("nope")


# --- staircase ----------------------------------------------------------------


def test_staircase_up_and_window_reset():
    d = RehabDirector(seed=1)
    assert d.left_ecc_deg == 15 and d.p_left == 0.1
    out = feed(d, [True, True, False, True, True])
    assert [x.action for x in out] == ["staircase_up"]
    assert d.left_ecc_deg == 25 and d.p_left == 0.1  # the staircase moves the angle only
    assert "4/5" in out[0].reason and "15° → 25°" in out[0].reason
    # window was reset: four more hits are not enough for another step
    assert feed(d, [True] * 4, t0=10) == []
    assert feed(d, [True], t0=20)[0].action == "staircase_up"
    assert d.left_ecc_deg == 35
    feed(d, [True] * 5, t0=30)
    assert d.left_ecc_deg == 45
    assert feed(d, [True] * 5, t0=40) == []  # top of the staircase: nothing changes
    assert d.left_ecc_deg == 45


def test_staircase_down_turns_cues_on_then_off():
    d = RehabDirector(params=RehabParams(warmup_s=0), seed=2)
    feed(d, [True] * 5)  # -> 25 deg
    out = feed(d, [False, True, False, True, False], t0=10)
    assert [x.action for x in out] == ["staircase_down"]
    assert d.left_ecc_deg == 15 and d.cues_on
    assert "cues on" in out[0].reason
    # force left spawns and check they are cued, then cues_off after 5
    d.p.p_left_max = d.p_left = 1.0
    d.start(100)
    t, cued, decisions = 102.0, [], []
    while len(cued) < 6:
        specs = d.due_spawns(t, 0)
        if specs:
            cued.append(specs[0].cued)
            decisions += d.on_trial_result("L", True, t + 0.5)
        t += 0.2
    assert cued == [True] * 5 + [False]
    assert [x.action for x in decisions if x.action != "left_chance"] == ["cues_off", "staircase_up"]


def test_staircase_down_at_min_still_cues_and_middle_band_no_change():
    d = RehabDirector()
    assert feed(d, [True, True, True, False, False]) == []  # 3/5: no change
    out = feed(d, [False, False, False, False, False], t0=10)
    assert out[0].action == "staircase_down" and d.left_ecc_deg == 15 and d.cues_on


def test_right_results_do_not_move_staircase():
    d = RehabDirector()
    assert feed(d, [False] * 10, side="R") == []
    assert d.left_ecc_deg == 15


def test_step_up_turns_cues_off():
    d = RehabDirector()
    feed(d, [False] * 5)
    assert d.cues_on
    out = feed(d, [True] * 5, t0=10)
    assert out[0].action == "staircase_up" and not d.cues_on and "cues off" in out[0].reason


# --- fatigue pacing -------------------------------------------------------------


def run_states(d, states, t0=0.0):
    out = []
    for i, s in enumerate(states):
        out += d.on_state(s, t0 + i)
    return out


def test_fatigue_hold_20s_starts_rest():
    d = RehabDirector()
    assert run_states(d, [st(fat=2.0)] * 19) == []
    out = d.on_state(st(fat=2.0), 19)
    assert [x.action for x in out] == ["rest_start"]
    assert "fatigue_z > 1.5 for 20 s" in out[0].reason and d.resting


def test_fatigue_interrupted_resets():
    d = RehabDirector()
    out = run_states(d, [st(fat=2.0)] * 15 + [st(fat=0.5)] + [st(fat=2.0)] * 15)
    assert out == []


def test_bad_quality_seconds_ignored():
    d = RehabDirector()
    seq = []
    for i in range(20):
        seq += [st(fat=2.0), st(fat=-3.0, quality="bad")]  # bad samples neither count nor reset
    out = run_states(d, seq)
    assert [x.action for x in out] == ["rest_start"]
    d2 = RehabDirector()
    assert run_states(d2, [st(fat=3.0, quality="bad")] * 60) == []


def test_low_engagement_30s_starts_rest():
    d = RehabDirector()
    assert run_states(d, [st(eng=-2.0)] * 29) == []
    out = d.on_state(st(eng=-2.0), 29)
    assert out[0].action == "rest_start" and "engagement_z < -1.5 for 30 s" in out[0].reason


def test_rest_duration_no_spawns_slower_after_and_refractory():
    d = RehabDirector(params=RehabParams(warmup_s=0), seed=0)
    d.start(0)
    run_states(d, [st(fat=2.0)] * 20)  # rest starts at t=19
    assert d.resting and d.status()["resting"]
    assert d.due_spawns(30, 0) == []
    assert d.on_state(st(), 63.9) == []  # still resting
    out = d.on_state(st(), 64)  # 19 + 45
    assert [x.action for x in out] == ["rest_end", "spawn_interval"]
    assert not d.resting and d.spawn_interval_s == 4.5
    assert all_have_reasons(out)
    # refractory: sustained fatigue right after the rest does not trigger a new rest
    out = run_states(d, [st(fat=2.5)] * 100, t0=65)
    assert out == []
    # once 120 s after the rest end have passed, it may trigger again
    out = d.on_state(st(fat=2.5), 64 + 120)
    assert [x.action for x in out] == ["rest_start"]


# --- challenge ---------------------------------------------------------------


def test_challenge_speeds_up_when_engaged_and_accurate():
    d = RehabDirector()
    feed(d, [True] * 10, side="R")
    assert d.on_state(st(eng=1.5), 20) == []  # engagement out of band
    out = d.on_state(st(eng=0.2), 21)
    assert [x.action for x in out] == ["spawn_interval"]
    assert d.spawn_interval_s == 3.5 and "hit rate 100%" in out[0].reason
    assert d.on_state(st(eng=0.2), 22) == []  # window cleared, needs 10 new trials


def test_challenge_needs_more_than_85_percent_and_has_floor():
    d = RehabDirector()
    feed(d, [True] * 8 + [False] * 2, side="R")  # 80%
    assert d.on_state(st(), 20) == []
    d.spawn_interval_s = 2.0
    feed(d, [True] * 10, side="R", t0=30)
    assert d.on_state(st(), 50) == []
    assert d.spawn_interval_s == 2.0


# --- scheduling ----------------------------------------------------------------


def test_one_target_unhit_gap_and_hit_gap():
    d = RehabDirector(params=RehabParams(warmup_s=0), seed=3)
    d.start(0)
    assert d.due_spawns(1.0, 0) == []  # lead-in
    first = d.due_spawns(2.0, 0)
    assert len(first) == 1 and first[0].side in "LR" and first[0].lifetime_s == 60
    if first[0].side == "R":
        assert first[0].ecc_deg == 35
    # one is already up, so nothing else appears even after the 4 s gap
    assert d.due_spawns(6.0, 1) == []
    assert d.due_spawns(30.0, 1) == []
    # a hit brings the next target 1 s later
    d.on_trial_result(first[0].side, True, 12.0)
    assert d.due_spawns(12.9, 0) == []
    assert len(d.due_spawns(13.0, 0)) == 1
    # a miss waits 4 s, and still only one target
    d.on_trial_result("R", False, 20.0)
    assert d.due_spawns(23.9, 0) == []
    assert len(d.due_spawns(24.0, 0)) == 1
    assert d.due_spawns(24.0, 1) == []


def test_status_fields():
    s = RehabDirector().status()
    assert s == {"mode": "rehab", "resting": False, "spawn_interval_s": 4.0, "p_left": 0.0,
                 "left_ecc_deg": 15.0, "warmup": True, "ali": None, "ali_rewards": 0,
                 "enemy_tier": 0, "enemy_boost": 0}


# --- fun mode -----------------------------------------------------------------


def test_fun_progresses_over_time_even_when_engaged():
    f = FunDirector(seed=1)
    f.start(0)
    levels, actions = [], []
    for t in range(0, 400):
        actions += [x.action for x in f.on_state(st(eng=1.5), t)]  # high engagement all along
        levels.append(f.level)
    assert [levels[t] for t in (0, 89, 90, 180, 270, 360)] == [0, 0, 1, 2, 3, 4]
    assert actions.count("escalate") == 4 and "deescalate" not in actions
    assert f.max_active == 3 and f.status()["base_level"] == 4 and f.boost == 0


def test_fun_falling_engagement_boosts_difficulty():
    f = FunDirector(seed=1)
    f.start(0)
    out = run_states(f, [st(eng=1.0)] * 60)  # stable, high
    assert f.level == 0 and not any(x.action == "boost_up" for x in out)
    out = run_states(f, [st(eng=0.4)] * 10, t0=60)  # 0.6 below its peak for 10 s
    acts = [x.action for x in out]
    assert acts[:2] == ["boost_up", "escalate"] and f.boost == 1 and f.level == 1
    assert "fell 0.6" in out[0].reason and all_have_reasons(out)
    # falls further after the 30 s cooldown -> +2, two levels above the time schedule
    out = run_states(f, [st(eng=0.4)] * 20, t0=70) + run_states(f, [st(eng=-0.2)] * 12, t0=90)
    assert f.boost == 2 and f.base_level == 1 and f.level == 3  # time 1 + boost 2
    # staying low is not a recovery
    assert not any(x.action == "boost_down" for x in run_states(f, [st(eng=0.0)] * 40, t0=102))
    # back to where it was before the drops (1.0) for 30 s -> boost -1, then -1 again
    out = run_states(f, [st(eng=0.9)] * 31, t0=142)
    assert [x.action for x in out][:2] == ["boost_down", "deescalate"] and f.boost == 1
    run_states(f, [st(eng=0.9)] * 31, t0=173)
    assert f.boost == 0 and f.level == f.base_level


def test_fun_short_or_noisy_drops_do_nothing():
    f = FunDirector(seed=1)
    f.start(0)
    run_states(f, [st(eng=1.0)] * 30)
    run_states(f, [st(eng=0.2)] * 8, t0=30)  # only 8 s
    run_states(f, [st(eng=1.0)] * 5, t0=38)
    run_states(f, [st(eng=0.2, quality="bad")] * 20, t0=43)  # bad seconds don't count
    assert f.boost == 0 and f.level == 0


def test_fun_breather_then_wave():
    f = FunDirector(seed=1)
    f.start(0)
    out = []
    for i in range(3):
        out += f.on_trial_result("L", False, 10 + i)
    assert [x.action for x in out] == ["breather_start"] and f.resting
    assert f.due_spawns(15, 0) == []
    out = f.on_state(st(), 33)  # breather started at 12, 20 s
    assert [x.action for x in out] == ["breather_end", "wave"]
    wave = f.due_spawns(33, 0)
    assert len(wave) == 5 and all(s.kind == "mob" for s in wave)


def test_fun_breather_eases_boost_not_below_time_schedule():
    f = FunDirector(seed=1)
    f.start(0)
    f.boost, f.drop.refs = 1, [1.0]
    f.on_state(st(eng=1.0), 100)  # base 1 + boost 1
    assert f.level == 2
    out = []
    for i in range(3):
        out += f.on_trial_result("L", False, 101 + i)
    assert out[0].action == "breather_start" and "boost 1 → 0" in out[0].reason
    assert f.boost == 0 and f.level == 1  # back to the time schedule, not below


def test_fun_flare_cooldown_and_fatigue_overload():
    f = FunDirector()
    assert [x.action for x in f.on_state(st(jaw=True), 0)] == ["flare"]
    assert f.on_state(st(jaw=True), 10) == []
    assert [x.action for x in f.on_state(st(jaw=True), 30)] == ["flare"]
    out = run_states(f, [st(fat=2.0)] * 5, t0=40)
    assert [x.action for x in out] == ["breather_start"]


# --- rehab: warm-up and ALI ------------------------------------------------------


def _drive(d, ali=0.0, quality="ok", t0=0.0, until=30.0, step=1.0):
    """Step the director once per `step` s; return (spawn specs, decisions)."""
    specs, decisions = [], []
    t = t0
    while t <= until + 1e-9:
        decisions += d.on_state(BrainState(t=t, ali=ali, quality=quality), t)
        for spec in d.due_spawns(t, 0):
            specs.append((t, spec))
            d.on_trial_result(spec.side, True, t + 0.1)  # hit: next one 1 s later
        t += step
    return specs, decisions


def test_warmup_is_right_only_even_if_ali_is_negative():
    d = RehabDirector(params=RehabParams(warmup_s=20, lead_in_s=0), seed=1)
    d.start(0)
    specs, decisions = _drive(d, ali=-0.8, until=19)
    assert specs and all(spec.side == "R" for _, spec in specs)
    assert {spec.ecc_deg for _, spec in specs} <= set(d.ecc)
    assert d.status()["warmup"] and d.status()["p_left"] == 0.0
    assert not any(x.action.startswith("ali_bias") for x in decisions)
    _, later = _drive(d, ali=-0.8, t0=20, until=21)
    assert [x.action for x in later][:1] == ["warmup_end"] and not d.warming_up


def test_after_warmup_mostly_right_then_hits_shift_left():
    import collections

    d = RehabDirector(params=RehabParams(warmup_s=10, lead_in_s=0), seed=4)
    d.start(0)
    d.on_trial_result("R", True, 5)  # hits in the warm-up don't move the side
    assert d.p_left == 0.1
    d.on_state(BrainState(t=11), 11)
    assert not d.warming_up and d.p_left_effective == 0.1
    sides = collections.Counter(d._spec("L" if d.rng.random() < d.p_left else "R", 15).side
                                for _ in range(2000))
    assert 0.07 < sides["L"] / 2000 < 0.13  # 10% left
    out = []
    for i in range(4):
        out += d.on_trial_result("R", True, 20 + i)  # 4 hits: 0.10 -> 0.30
    assert d.p_left == 0.3 and [x.action for x in out] == ["left_chance", "left_chance"]
    assert "10% → 20%" in out[0].reason and "20% → 30%" in out[1].reason
    d.on_trial_result("R", False, 25)
    assert d.on_trial_result("R", True, 26) == []  # wobbling 0.25 / 0.3: not logged
    for i in range(20):
        d.on_trial_result("L", True, 30 + i)
    assert d.p_left == 0.7  # capped
    for i in range(20):
        d.on_trial_result("R", False, 60 + i)
    assert d.p_left == 0.1  # floor


def _ali_states(d, values, t0=0.0, quality="ok"):
    out = []
    for i, a in enumerate(values):
        out += d.on_state(BrainState(t=t0 + i, ali=a, quality=quality), t0 + i)
    return [x for x in out if x.action == "ali_reward"]


def test_rising_ali_heals_with_cooldown():
    d = RehabDirector(params=RehabParams(lead_in_s=0), seed=5)
    d.start(0)
    rewards = _ali_states(d, [0.0, 0.03])  # +0.03 in 1 s
    assert len(rewards) == 1 and rewards[0].params["hp"] == 2
    assert "attention moving left" in rewards[0].reason and "+1 heart" in rewards[0].reason
    assert _ali_states(d, [0.06, 0.09, 0.12], t0=2) == []  # inside the 5 s cooldown
    assert len(_ali_states(d, [0.12, 0.15], t0=6)) == 1  # cooldown over
    assert _ali_states(d, [0.15, 0.16], t0=20) == []  # +0.01: too small
    assert _ali_states(d, [0.0, 0.5], t0=30, quality="bad") == []  # bad quality
    d.on_state(BrainState(t=40, ali=0.0), 40)
    assert _ali_states(d, [0.5], t0=45) == []  # 5 s gap: not "in one second"
    assert d.status()["ali_rewards"] == 2


def test_no_reward_while_resting():
    d = RehabDirector(params=RehabParams(warmup_s=0), seed=6)
    d.start(0)
    d.rest_until = 100.0
    assert _ali_states(d, [0.0, 0.5, 1.0], t0=10) == []


def test_rehab_enemies_slow_schedule_and_engagement_boost():
    d = RehabDirector(params=RehabParams(warmup_s=0, lead_in_s=0), seed=7)
    d.start(0)
    for t in range(0, 400):  # engaged, steady: only the slow schedule
        d.on_state(BrainState(t=t, engagement_z=1.0), t)
        if t == 299:
            assert d.enemy_tier == 0
    assert d.enemy_tier == 1 and d.enemy_boost == 0  # stray from 5 min
    out = []
    for t in range(400, 421):  # engagement 0.6 below its peak for 20 s
        out += d.on_state(BrainState(t=t, engagement_z=0.4), t)
    acts = [x.action for x in out]
    assert acts == ["enemy_boost_up", "enemy_unlock"] and d.enemy_boost == 1 and d.enemy_tier == 2
    assert out[1].params["enemy"] == "bogged" and "harder enemies" in out[0].reason
    for t in range(421, 500):  # stays low: no second boost (max +1)
        d.on_state(BrainState(t=t, engagement_z=-0.4), t)
    assert d.enemy_boost == 1
    specs = [d.due_spawns(500 + 10 * i, 0) for i in range(5)]
    assert {s[0].enemy for s in specs if s} <= {"skeleton", "stray", "bogged"}
    for t in range(550, 612):  # recovers to its earlier level for 60 s
        d.on_state(BrainState(t=t, engagement_z=1.0), t)
    assert d.enemy_boost == 0 and d.enemy_tier == 2  # 10 min in: the schedule reached bogged too


def test_rest_rule_still_fires_for_strongly_low_engagement():
    d = RehabDirector(params=RehabParams(warmup_s=0), seed=8)
    d.start(0)
    out = run_states(d, [st(eng=-2.0)] * 30)
    assert "rest_start" in [x.action for x in out]


def test_fun_spawns_several_at_higher_levels():
    f = FunDirector(seed=5)
    f.start(-400)  # 400 s into the session: level 4
    kinds = set()
    t = 2.0
    for _ in range(200):
        for s in f.due_spawns(t, 0):
            kinds.add(s.kind)
        t += 0.2
    assert kinds == {"target", "mob", "sheep"}
    assert f.due_spawns(t + 10, 3) == []  # max_active reached
