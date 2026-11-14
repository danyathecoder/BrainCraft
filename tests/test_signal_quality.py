"""Protocol recorder, replay and the learned artifact detector (synthetic data)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from eeg import artifact_model as AM
from eeg.protocol import load_protocol, run_protocol, save_protocol
from eeg.replay import Config, analyse, load_recording
from eeg.stream import EEGStream


def _record(tmp_path: Path, seed: int, scale: float = 0.4) -> Path:
    with EEGStream("synthetic", seed=seed, speed=40, seconds=10) as st:
        p = run_protocol(st, f"syn{seed}", scale=scale, say=lambda m: None, seed=seed)
    return save_protocol(p, tmp_path / f"artifacts-syn{seed}.npz")


@pytest.fixture(scope="module")
def recordings(tmp_path_factory):
    d = tmp_path_factory.mktemp("proto")
    return [_record(d, s) for s in (1, 2)]


def test_protocol_roundtrip(recordings):
    p = load_protocol(recordings[0])
    names = [n for n, _, _ in p.phases]
    assert names == ["still", "eyes_closed", "blink", "jaw", "head", "talk", "play"]
    assert p.eeg.shape[1] == 8 and p.gyro is not None and p.gyro.shape[1] == 3
    assert {lab for _, lab in p.cues} == {"blink", "jaw", "motion"}
    assert all(a <= b for _, a, b in p.phases) and p.phases[-1][2] <= len(p.eeg)
    assert p.meta["player"] == "syn1" and p.meta["oscar"] is False


def test_replay_segments_beat_legacy(recordings):
    p = load_recording(recordings[0])
    new, old = analyse(p, Config()), analyse(p, Config("legacy", legacy=True))
    assert new["usable %"] > old["usable %"]
    assert new["usable % jaw"] > old["usable % jaw"]
    assert new["cue recall blink %"] >= 50 and new["cue recall motion %"] >= 50
    assert new["false alarms quiet %"] <= 10
    assert new["eyes-closed alpha x"] > 2  # alpha check survives


def test_replay_reads_bridge_csv(tmp_path):
    rng = np.random.default_rng(0)
    rows = np.hstack([np.arange(2500)[:, None] / 250.0, rng.normal(0, 10, (2500, 17))])
    f = tmp_path / "raw-headset-x.csv"
    with f.open("w") as fh:
        fh.write("# NON-COMMERCIAL USE\nTime, " + ", ".join(f"Ch{i:02d}" for i in range(1, 18)) + "\n")
        np.savetxt(fh, rows, delimiter=",")
    p = load_recording(f)
    assert p.eeg.shape == (2500, 8) and p.fs == 250 and p.phases == [("all", 0, 2500)]
    assert analyse(p, Config())["seconds"] == 10


def test_model_trains_and_plugs_into_detector(recordings, tmp_path, monkeypatch):
    out = tmp_path / "m.joblib"
    model, report = AM.train(recordings, out, force=True, say=lambda m: None)
    assert out.exists() and set(report) == {"model", "thresholds"}
    loaded = AM.ArtifactModel.load(out)
    p = load_protocol(recordings[0])
    i, lab = next(c for c in p.cues if c[1] == "motion")
    s = i + int(0.3 * p.fs)
    assert loaded.predict_label(p.eeg[s:s + 250], p.gyro[s:s + 250], 3.0) in AM.CLASSES
    monkeypatch.setenv("BRAINCRAFT_ARTIFACT_MODEL", str(out))
    assert AM.default_detector().model is not None
    monkeypatch.setenv("BRAINCRAFT_ARTIFACT_MODEL", "off")
    assert AM.default_detector().model is None


def test_model_not_saved_without_evaluation(recordings, tmp_path):
    out = tmp_path / "m.joblib"
    model, report = AM.train(recordings[:1], out, say=lambda m: None)
    assert model is None and not out.exists() and report == {}
