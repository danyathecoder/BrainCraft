"""EEG part (P1): synthetic signal, features, artifacts, baseline, pipeline."""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

from contracts import BrainState
from eeg import features as F
from eeg.calibrate import Baseline, compute_baseline, run_calibration
from eeg.layout import CH, FS, Layout, parse_cols
from eeg.stream import EEGStream
from eeg.synthetic import SyntheticEEG, apply_command

EEG = slice(0, 8)


def eeg_seconds(gen: SyntheticEEG, seconds: float) -> np.ndarray:
    return gen.generate(int(seconds * FS))[:, EEG]


def mean_index(gen: SyntheticEEG, n: int = 6) -> dict[str, float]:
    vals = [F.indices(F.band_powers(eeg_seconds(gen, 4))) for _ in range(n)]
    return {k: float(np.mean([v[k] for v in vals])) for k in F.INDEX_NAMES}


# --- synthetic + features ---------------------------------------------------


def test_synthetic_layout():
    gen = SyntheticEEG(seed=0)
    x = gen.generate(500)
    assert x.shape == (500, 17)
    assert np.all(np.diff(x[:, 15]) == 1)  # counter
    assert np.abs(x[:, 11:14]).max() < 5  # gyro quiet
    assert SyntheticEEG(seed=0, n_channels=8).generate(10).shape == (10, 8)


def test_alpha_rises_with_eyes_closed():
    gen = SyntheticEEG(seed=1)
    open_ = F.band_powers(eeg_seconds(gen, 4))["alpha"]
    gen.eyes_closed = True
    eeg_seconds(gen, 1)
    closed = F.band_powers(eeg_seconds(gen, 4))["alpha"]
    for ch in ("Oz", "PO7", "PO8"):
        assert closed[CH[ch]] > 4 * open_[CH[ch]], ch
    assert closed[CH["Oz"]] / open_[CH["Oz"]] > closed[CH["Fz"]] / open_[CH["Fz"]]


def test_engagement_follows_level():
    gen = SyntheticEEG(seed=2)
    by_level = {}
    for e in (-1.0, 0.0, 1.0):
        gen.engagement = e
        eeg_seconds(gen, 1)
        by_level[e] = mean_index(gen)
    eng = [by_level[e]["engagement"] for e in (-1.0, 0.0, 1.0)]
    fat = [by_level[e]["fatigue"] for e in (-1.0, 0.0, 1.0)]
    assert eng[0] < eng[1] < eng[2]
    assert fat[0] > fat[1] > fat[2]
    assert eng[2] - eng[0] > 0.3  # log10 units: about x2 in the ratio


def test_fatigue_drift_lowers_engagement():
    gen = SyntheticEEG(seed=3, fatigue_rate=30.0)  # fast drift: 0.5 per second
    early = F.engagement(F.band_powers(eeg_seconds(gen, 4)))
    eeg_seconds(gen, 4)
    late = F.engagement(F.band_powers(eeg_seconds(gen, 4)))
    assert gen.effective_engagement < -2
    assert late < early


def test_ali_is_negative_when_right_alpha_is_higher():
    even = np.ones(8)
    assert F.ali({"alpha": even}) == pytest.approx(0.0)
    right_heavy = even.copy()
    right_heavy[CH["PO8"]] = 3.0
    assert F.ali({"alpha": right_heavy}) < 0
    left_heavy = even.copy()
    left_heavy[CH["PO7"]] = 4.0
    assert F.ali({"alpha": left_heavy}) > 0


def test_band_powers_shapes_and_notch():
    gen = SyntheticEEG(seed=4, line_noise_uv=50.0)
    raw = eeg_seconds(gen, 4)
    bp = F.band_powers(raw)
    assert set(bp) == set(F.BANDS)
    assert all(v.shape == (8,) for v in bp.values())
    f, p = F.psd(F.preprocess(raw))
    assert p[np.argmin(np.abs(f - 50))].max() < 0.05 * p[(f > 8) & (f < 12)].mean(axis=0).max()


# --- artifacts ----------------------------------------------------------------


def test_clean_windows_are_none():
    gen = SyntheticEEG(seed=5)
    labels = []
    for _ in range(20):
        x = gen.generate(FS)
        labels.append(F.artifact(x[:, EEG], x[:, 11:14]))
    assert labels == ["none"] * 20
    gen.eyes_closed = True  # big alpha must not look like a blink
    gen.generate(FS)
    assert F.artifact(gen.generate(FS)[:, EEG]) == "none"


@pytest.mark.parametrize("event,expected", [("blink", "blink"), ("jaw", "jaw"), ("motion", "motion")])
def test_artifacts_detected(event, expected):
    gen = SyntheticEEG(seed=6)
    detector = F.ArtifactDetector()
    for _ in range(10):  # learn the typical HF level
        x = gen.generate(FS)
        assert detector(x[:, EEG], x[:, 11:14]) == "none"
    getattr(gen, event)()
    x = gen.generate(FS)
    assert detector(x[:, EEG], x[:, 11:14]) == expected


def test_analyzer_keeps_window_usable_around_jaw():
    """Segment-wise: a jaw second is left out, the rest of the window counts."""
    gen = SyntheticEEG(seed=7)
    an = F.Analyzer()
    hist = gen.generate(4 * FS)
    assert an.step(hist[:, EEG], hist[:, 11:14]).ok
    gen.jaw()
    hist = np.vstack([hist, gen.generate(FS)])[-4 * FS :]
    res = an.step(hist[:, EEG], hist[:, 11:14])
    assert res.artifact == "jaw" and res.ok
    assert res.segment_labels[-1] == "jaw" and res.clean_segments == 3
    # three bad seconds out of four: not enough clean data
    for _ in range(2):
        gen.jaw()
        hist = np.vstack([hist, gen.generate(FS)])[-4 * FS :]
        res = an.step(hist[:, EEG], hist[:, 11:14])
    assert res.clean_segments == 1 and not res.ok


def test_analyzer_contact_and_bad_channels():
    gen = SyntheticEEG(seed=8)
    an = F.Analyzer()
    hist = gen.generate(4 * FS)[:, EEG]
    clipped = hist.copy()
    clipped[:, F.CH["Cz"]] = 750_000.0  # electrode at the amplifier limit
    res = an.step(clipped)
    assert set(res.segment_labels) == {"contact"} and not res.ok
    noisy = hist.copy()
    noisy[:, F.CH["C4"]] += 400 * np.sin(np.arange(len(noisy)) * 2 * np.pi * 6 / FS)
    res = an.step(noisy)
    assert res.ok and res.channels[F.CH["C4"]] == "noisy"  # C4 feeds no index
    noisy[:, F.CH["Cz"]] += 400 * np.sin(np.arange(len(noisy)) * 2 * np.pi * 6 / FS)
    res = an.step(noisy)
    assert res.ok  # engagement falls back to Fz alone
    assert F.indices(res.bp, {"Fz"}) is not None  # fatigue falls back to Cz
    assert F.indices(res.bp, {"Fz", "Cz", "C3", "C4"}) is None  # nothing left


def test_analyzer_legacy_holds_after_jaw():
    gen = SyntheticEEG(seed=7)
    an = F.Analyzer(legacy=True)
    hist = gen.generate(4 * FS)
    assert an.step(hist[:, EEG], hist[:, 11:14]).ok
    gen.jaw()
    hist = np.vstack([hist, gen.generate(FS)])[-4 * FS :]
    res = an.step(hist[:, EEG], hist[:, 11:14])
    assert res.artifact == "jaw" and not res.ok
    hist = np.vstack([hist, gen.generate(FS)])[-4 * FS :]
    res = an.step(hist[:, EEG], hist[:, 11:14])
    assert res.artifact == "none" and res.contaminated  # jaw still in window
    hist = np.vstack([hist, gen.generate(4 * FS)])[-4 * FS :]
    assert an.step(hist[:, EEG], hist[:, 11:14]).ok


def test_commands():
    gen = SyntheticEEG(seed=0)
    apply_command(gen, "e")
    assert gen.eyes_closed
    apply_command(gen, "f+")
    assert gen.engagement < 0
    assert apply_command(gen, "q") is None


def test_layout_parsing(monkeypatch):
    assert parse_cols("0-7") == tuple(range(8))
    assert parse_cols("11,12,13") == (11, 12, 13)
    assert parse_cols("none") is None
    assert Layout.for_stream(17).gyro == (11, 12, 13)
    assert Layout.for_stream(8).gyro is None
    monkeypatch.setenv("BRAINCRAFT_GYRO_COLS", "none")
    assert Layout.for_stream(17).gyro is None
    with pytest.raises(ValueError):
        Layout.for_stream(6)


# --- stream -------------------------------------------------------------------


def test_synthetic_stream_window():
    with EEGStream("synthetic", seconds=6, seed=0, speed=20) as s:
        eeg, gyro = s.windows(4)
        assert eeg.shape == (1000, 8) and gyro.shape == (1000, 3)
        n0 = s.total_samples
        assert s.wait_for_samples(n0 + FS, timeout=2)
        assert s.window(10).shape == (6 * FS, 8)  # capped at buffer size
        s.generator.eyes_closed = True


# --- baseline -----------------------------------------------------------------


def test_baseline_math_and_json(tmp_path):
    base = compute_baseline({"engagement": [1.0, 2.0, 3.0], "fatigue": [0.0, 0.0, 0.0, float("nan")]})
    assert base.mean["engagement"] == pytest.approx(2.0)
    assert base.std["engagement"] == pytest.approx(1.0)
    assert base.z("engagement", 4.0) == pytest.approx(2.0)
    assert base.z("fatigue", 0.5) == pytest.approx(0.5)  # zero std -> 1
    assert base.n["fatigue"] == 3
    path = base.save(tmp_path / "sub" / "baseline.json")
    assert json.loads(path.read_text())["mean"]["engagement"] == 2.0
    again = Baseline.load(path)
    assert again.mean == base.mean and again.std == base.std
    with pytest.raises(ValueError):
        compute_baseline({"engagement": [1.0]})


def test_run_calibration_synthetic():
    msgs = []
    with EEGStream("synthetic", seed=8, speed=60) as s:
        base = run_calibration(s, prompt=msgs.append, durations=(12, 10, 12))
    assert base is not None
    assert set(base.mean) == set(F.INDEX_NAMES) | {"ali"}  # ALI mean: the player's resting balance
    assert abs(base.mean["ali"]) < 0.5
    assert base.meta["alpha_check"]["seen"]
    assert base.meta["alpha_check"]["ratio"] > 3
    assert any("alpha jump OK" in m for m in msgs)


# --- pipeline -----------------------------------------------------------------


def _baseline_for(seed: int) -> Baseline:
    gen = SyntheticEEG(seed=seed)
    vals = [F.indices(F.band_powers(eeg_seconds(gen, 4))) for _ in range(10)]
    return compute_baseline({k: [v[k] for v in vals] for k in F.INDEX_NAMES})


def _run_pipeline(tmp_path, seconds: float, **kwargs):
    from eeg.pipeline import run

    states: list[BrainState] = []
    stop = threading.Event()
    th = threading.Thread(
        target=run, args=(states.append, stop),
        kwargs={"source": "synthetic", "period": 0.05, "ema_s": 0.5, "seed": 9,
                "baseline_path": tmp_path / "baseline.json", **kwargs},
        daemon=True,
    )
    th.start()
    time.sleep(seconds)
    stop.set()
    th.join(timeout=3)
    return states, th


def test_pipeline_smoke(tmp_path):
    _baseline_for(9).save(tmp_path / "baseline.json")
    states, th = _run_pipeline(tmp_path, 1.2)
    assert not th.is_alive()
    assert len(states) >= 5
    for st in states:
        assert isinstance(st, BrainState)
        assert st.quality in ("ok", "bad")
        assert st.artifact in ("none", "blink", "jaw", "motion")
        assert isinstance(st.jaw_clench, bool)
        for v in (st.engagement_z, st.fatigue_z, st.alpha_low_z, st.ali, st.t):
            assert isinstance(v, float) and np.isfinite(v)
        if st.quality == "ok":
            assert -1.0 <= st.ali <= 1.0
    ok = [st for st in states if st.quality == "ok"]
    assert ok and abs(ok[-1].engagement_z) < 4


def test_pipeline_calibrates_without_baseline(tmp_path):
    states, th = _run_pipeline(tmp_path, 1.5, speed=60, calibration_durations=(12, 10, 12),
                               prompt=lambda m: None)
    assert not th.is_alive()
    assert (tmp_path / "baseline.json").exists()
    assert states[0].quality == "bad"
    assert any(st.quality == "ok" for st in states)


def test_quick_baseline_without_protocol():
    from eeg.calibrate import quick_baseline

    msgs = []
    with EEGStream("synthetic", seed=4, speed=60) as s:
        base = quick_baseline(s, seconds=10, prompt=msgs.append)
    assert base.meta["quick"] is True
    assert all(base.n[k] == 10 and base.std[k] > 0 for k in base.mean)
    assert any("skipped" in m for m in msgs)


def test_pipeline_quick_calibration_is_not_saved(tmp_path):
    states, th = _run_pipeline(tmp_path, 1.5, speed=60, calibration="quick",
                               prompt=lambda m: None)
    assert not th.is_alive()
    assert not (tmp_path / "baseline.json").exists()
    assert states[0].quality == "bad"
    assert any(st.quality == "ok" for st in states)


def test_pipeline_survives_bad_windows(tmp_path):
    _baseline_for(9).save(tmp_path / "baseline.json")
    stream = EEGStream("synthetic", seed=9).start()
    stream.windows = lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        states, th = _run_pipeline(tmp_path, 0.5, stream=stream)
    finally:
        stream.stop()
    assert not th.is_alive()
    assert states and all(st.quality == "bad" for st in states)


def test_pipeline_ali_is_relative_to_the_baseline(tmp_path):
    base = _baseline_for(9)
    base.mean["ali"], base.std["ali"] = 0.5, 0.1  # pretend this player rests at +0.5
    base.save(tmp_path / "baseline.json")
    states, th = _run_pipeline(tmp_path, 1.2)
    ok = [s for s in states if s.quality == "ok"]
    assert ok and -0.8 < ok[-1].ali < -0.2  # synthetic is ~symmetric: 0 - 0.5


def test_ali_needs_both_po_channels():
    res = F.WindowResult({}, "none", False, channels=["ok"] * 8)
    assert F.ali_ok(res)
    res.channels[F.CH["PO8"]] = "noisy"
    assert not F.ali_ok(res)
    assert F.ali_ok(F.WindowResult({}, "none", False))  # legacy: no channel info
