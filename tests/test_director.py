"""Director logic: staircase, cues, fatigue pacing, challenge, fun mode."""

from __future__ import annotations

import pytest

from contracts import BrainState
from director import make_director
from director.base import Decision, HoldTimer
from director.fun import FunDirector
from director.rehab import RehabDirector


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
    assert d.left_ecc_deg == 15 and d.p_left == 0.5
    out = feed(d, [True, True, False, True, True])
    assert [x.action for x in out] == ["staircase_up"]
    assert d.left_ecc_deg == 35 and d.p_left == pytest.approx(0.6)
    assert "4/5" in out[0].reason and "15° → 35°" in out[0].reason
    # window was reset: four more hits are not enough for another step
    assert feed(d, [True] * 4, t0=10) == []
    assert feed(d, [True], t0=20)[0].action == "staircase_up"
    assert d.left_ecc_deg == 55 and d.p_left == pytest.approx(0.7)
    feed(d, [True] * 5, t0=30)
    assert d.left_ecc_deg == 75 and d.p_left == pytest.approx(0.7)  # p_left capped
    assert feed(d, [True] * 5, t0=40) == []  # top of the staircase: nothing changes
    assert d.left_ecc_deg == 75


def test_staircase_down_turns_cues_on_then_off():
    d = RehabDirector(seed=2)
    feed(d, [True] * 5)  # -> 35 deg
    out = feed(d, [False, True, False, True, False], t0=10)
    assert [x.action for x in out] == ["staircase_down"]
    assert d.left_ecc_deg == 15 and d.cues_on
    assert "cues on" in out[0].reason
    # force left spawns and check they are cued, then cues_off after 5
    d.p_left = 1.0
    d.start(100)
    t, cued, decisions = 102.0, [], []
    while len(cued) < 6:
        specs = d.due_spawns(t, 0)
        if specs:
            cued.append(specs[0].cued)
            decisions += d.on_trial_result("L", True, t + 0.5)
        t += 0.2
    assert cued == [True] * 5 + [False]
    assert [x.action for x in decisions] == ["cues_off", "staircase_up"]  # 5 hits also step up


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
    d = RehabDirector(seed=0)
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


def test_one_target_at_a_time_and_interval():
    d = RehabDirector(seed=3)
    d.start(0)
    assert d.due_spawns(1.0, 0) == []  # lead-in
    first = d.due_spawns(2.0, 0)
    assert len(first) == 1 and first[0].side in "LR"
    if first[0].side == "R":
        assert first[0].ecc_deg == 35
    assert d.due_spawns(7.0, 1) == []  # one already active
    d.on_trial_result(first[0].side, True, 3.0)
    assert d.due_spawns(5.9, 0) == []  # N = 4 s after the previous spawn
    assert len(d.due_spawns(6.0, 0)) == 1


def test_status_fields():
    s = RehabDirector().status()
    assert s == {"mode": "rehab", "resting": False, "spawn_interval_s": 4.0, "p_left": 0.5,
                 "left_ecc_deg": 15.0}


# --- fun mode -----------------------------------------------------------------


def test_fun_escalates_when_calm():
    f = FunDirector(seed=1)
    f.start(0)
    out = run_states(f, [st(eng=-1.0)] * 15)
    assert "escalate" in [x.action for x in out] and f.level == 1
    assert all_have_reasons(out)
    for i in range(3):
        run_states(f, [st(eng=-1.0)] * 15, t0=100 + 20 * i)
    assert f.level == 4 and f.max_active == 3
    out = run_states(f, [st(eng=-1.0)] * 15, t0=300)
    assert {x.action for x in out} >= {"cave_sound", "darkness"}


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


def test_fun_flare_cooldown_and_fatigue_overload():
    f = FunDirector()
    assert [x.action for x in f.on_state(st(jaw=True), 0)] == ["flare"]
    assert f.on_state(st(jaw=True), 10) == []
    assert [x.action for x in f.on_state(st(jaw=True), 30)] == ["flare"]
    out = run_states(f, [st(fat=2.0)] * 5, t0=40)
    assert [x.action for x in out] == ["breather_start"]


def test_fun_spawns_several_at_higher_levels():
    f = FunDirector(seed=5)
    f.start(0)
    f.level = 4
    kinds = set()
    t = 2.0
    for _ in range(200):
        for s in f.due_spawns(t, 0):
            kinds.add(s.kind)
        t += 0.2
    assert kinds == {"target", "mob", "sheep"}
    assert f.due_spawns(t + 10, 3) == []  # max_active reached
