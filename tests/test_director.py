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
    assert make_director("rehab_01").mode == "rehab_01"
    assert make_director("fun").mode == "fun"
    with pytest.raises(ValueError):
        make_director("nope")


# --- staircase ----------------------------------------------------------------


def test_staircase_up_and_window_reset():
    d = RehabDirector(seed=1)
    assert d.left_ecc_deg == 15 and d.p_left == 0.5
    out = feed(d, [True, True, False, True, True])
    assert [x.action for x in out] == ["staircase_up"]
    assert d.left_ecc_deg == 25 and d.p_left == pytest.approx(0.6)
    assert "4/5" in out[0].reason and "15° → 25°" in out[0].reason
    # window was reset: four more hits are not enough for another step
    assert feed(d, [True] * 4, t0=10) == []
    assert feed(d, [True], t0=20)[0].action == "staircase_up"
    assert d.left_ecc_deg == 35 and d.p_left == pytest.approx(0.7)
    feed(d, [True] * 5, t0=30)
    assert d.left_ecc_deg == 45 and d.p_left == pytest.approx(0.7)  # p_left capped
    assert feed(d, [True] * 5, t0=40) == []  # top of the staircase: nothing changes
    assert d.left_ecc_deg == 45


def test_staircase_down_turns_cues_on_then_off():
    d = RehabDirector(seed=2)
    feed(d, [True] * 5)  # -> 25 deg
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


def test_one_target_unhit_gap_and_hit_gap():
    d = RehabDirector(seed=3)
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


# --- rehab 01 ----------------------------------------------------------------


def _rehab01(**kwargs):
    from director.rehab_01 import Rehab01Director, Rehab01Params

    params = kwargs.pop("params", None) or Rehab01Params(
        lead_in_s=0.0, spawn_interval_s=1.0, right_only_s=10.0, ali_after_spawns=3,
    )
    return Rehab01Director(params=params, **kwargs)


def _drive(d, ali=0.0, quality="ok", until=30.0, step=1.0):
    """Step the director; return (spawn specs, decisions)."""
    specs, decisions = [], []
    t = 0.0
    while t <= until + 1e-9:
        decisions += d.on_state(BrainState(t=t, ali=ali, quality=quality), t)
        for spec in d.due_spawns(t, 0):
            specs.append((t, spec))
        t += step
    return specs, decisions


def test_rehab01_first_window_is_right_only_even_if_ali_is_negative():
    d = _rehab01(seed=1)
    d.start(0)
    specs, decisions = _drive(d, ali=-0.8, until=9)
    assert specs and all(spec.side == "R" for _, spec in specs)
    assert {spec.ecc_deg for _, spec in specs} <= {15, 25, 35, 45}
    assert d.status()["zone"] == "right" and d.status()["p_left"] == 0.0
    assert any(x.action == "ali_watch" for x in decisions)
    assert all(x.action != "spawn_zone" for x in decisions)
    later, later_dec = _drive_from(d, t0=10, until=14, ali=-0.8)
    assert later and all(spec.side == "L" for _, spec in later)
    assert any("first 10 s over and ALI" in x.reason for x in later_dec)


def test_rehab01_whole_view_then_left_when_ali_drops():
    d = _rehab01(seed=2)
    d.start(0)
    early, _ = _drive(d, ali=0.4, until=9)
    assert early and all(spec.side == "R" for _, spec in early)
    # minute is over, three targets already spawned, ALI still positive: both sides
    mid, mid_dec = _drive_from(d, t0=10, until=25, ali=0.4)
    assert d.status()["zone"] == "full" and d.status()["p_left"] == 0.5
    assert {spec.side for _, spec in mid} == {"L", "R"}
    assert any("whole field of view" in x.reason for x in mid_dec)
    late, late_dec = _drive_from(d, t0=26, until=40, ali=-0.25)
    assert d.status()["zone"] == "left" and d.status()["p_left"] == 1.0
    assert late and all(spec.side == "L" for _, spec in late)
    assert any("targets only on the left" in x.reason for x in late_dec)
    back, back_dec = _drive_from(d, t0=41, until=55, ali=0.0)
    assert d.status()["zone"] == "full"
    assert {spec.side for _, spec in back} == {"L", "R"}
    assert any("≥ 0" in x.reason for x in back_dec)
    assert all_have_reasons(mid_dec + late_dec + back_dec)


def _drive_from(d, t0, until, ali=0.0, quality="ok", step=1.0):
    specs, decisions = [], []
    t = t0
    while t <= until + 1e-9:
        decisions += d.on_state(BrainState(t=t, ali=ali, quality=quality), t)
        for spec in d.due_spawns(t, 0):
            specs.append((t, spec))
        t += step
    return specs, decisions


def test_rehab01_bad_quality_does_not_move_ali():
    d = _rehab01(seed=3)
    d.start(0)
    _drive(d, ali=0.2, until=12)
    assert d.status()["zone"] == "full"
    decisions = d.on_state(BrainState(t=13, ali=-0.9, quality="bad"), 13)
    assert d.status()["zone"] == "full" and decisions == []
    decisions = d.on_state(BrainState(t=14, ali=-0.9, quality="ok"), 14)
    assert d.status()["zone"] == "left"
    assert decisions and decisions[0].action == "spawn_zone"


def test_rehab01_ignores_negative_ali_until_ten_targets():
    from director.rehab_01 import Rehab01Params

    d = _rehab01(seed=4, params=Rehab01Params(
        lead_in_s=0, spawn_interval_s=5, right_only_s=10, ali_after_spawns=10,
    ))
    d.start(0)
    specs, _ = _drive(d, ali=-0.5, until=40, step=5)
    # 0 and 5 are still inside the right-only window; 10..40 are the whole view
    assert [spec.side for _, spec in specs[:2]] == ["R", "R"]
    assert d.n_spawned == 9 and d.status()["zone"] == "full"
    tenth = d.due_spawns(45, 0)
    assert d.n_spawned == 10 and tenth and tenth[0].side in "LR"
    out = d.on_state(BrainState(t=46, ali=-0.5), 46)
    assert d.status()["zone"] == "left"
    assert [x.action for x in out] == ["ali_watch", "spawn_zone"]
    assert d.due_spawns(50, 0)[0].side == "L"


def test_rehab01_one_target_unhit_gap_and_hit_gap():
    from director.rehab_01 import Rehab01Director

    d = Rehab01Director(seed=1)
    d.start(0)
    assert d.due_spawns(1.0, 0) == []
    first = d.due_spawns(2.0, 0)
    assert len(first) == 1 and first[0].lifetime_s == 60 and first[0].side == "R"
    assert d.due_spawns(6.0, 1) == []
    assert d.due_spawns(30.0, 1) == []
    d.on_trial_result(first[0].side, True, 12.0)
    assert d.due_spawns(12.9, 0) == []
    assert len(d.due_spawns(13.0, 0)) == 1
    d.on_trial_result("R", False, 20.0)
    assert d.due_spawns(23.9, 0) == []
    assert len(d.due_spawns(24.0, 0)) == 1
    assert d.due_spawns(24.0, 1) == []


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
