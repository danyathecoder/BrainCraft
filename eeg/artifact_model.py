"""Learned artifact detector, trained on labelled protocol recordings (P1).

    python -m eeg.artifact_model train sessions/artifacts-*.npz   # evaluate + save if better
    python -m eeg.artifact_model train ... --force                 # save anyway

Each 1 s segment becomes a feature vector (muscle/high-frequency power,
blink-like frontal deflection, low-frequency power, kurtosis, gyroscope,
mains). A gradient-boosting classifier predicts none / blink / jaw / motion.

Training labels come from eeg.protocol recordings:
- "none":   whole seconds of the still and eyes-closed phases;
- cued 1 s after each BLINK / CLENCH / TURN cue: "blink" / "jaw" / "motion";
- "jaw":    whole seconds of the talk phase (facial muscle activity).

With >= 2 recordings it reports leave-one-recording-out results next to
the threshold detector and saves sessions/artifact_model.joblib only if the
model is better (higher mean recall, false alarms not worse by > 2 points),
or with --force. The live pipeline and calibration then use it
automatically (BRAINCRAFT_ARTIFACT_MODEL=path picks another file, "off"
disables it). Bad contact (amplifier limit) stays a fixed rule.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import numpy as np
from scipy import signal, stats

from eeg.features import (
    FS,
    JAW_DEFAULT_REF_UV,
    ArtifactDetector,
    artifact,
    blink_p2p,
    motion_level,
)
from eeg.protocol import Protocol, load_protocol

log = logging.getLogger("eeg.artifact_model")

DEFAULT_MODEL = Path(__file__).resolve().parent.parent / "sessions" / "artifact_model.joblib"
FEATURES_VERSION = 1
FEATURE_NAMES = (
    "log_hf_median", "log_hf_max", "log_hf_ratio", "log_fz_p2p", "log_oz_p2p",
    "log_fz_oz", "log_lf_median", "log_mid_rms_median", "log_mid_rms_max",
    "kurtosis_max", "gyro_p95", "gyro_mean", "log_mains",
)
CLASSES = ("none", "blink", "jaw", "motion")
CUE_OFFSET_S = {"blink": 0.1, "jaw": 0.2, "motion": 0.3}  # reaction time before the event
QUIET = ("still", "eyes_closed")
_EPS = 1e-9


def segment_features(raw: np.ndarray, gyro: np.ndarray | None, hf_ref: float | None,
                     fs: float = FS) -> np.ndarray:
    """Feature vector of one ~1 s raw segment (uV, deg/s)."""
    x = signal.detrend(np.asarray(raw, dtype=float), axis=0)
    f, p = signal.welch(x, fs=fs, nperseg=min(len(x), int(fs)), axis=0)
    df = f[1] - f[0]
    hi = min(120.0, fs / 2 - 1)
    hfm = (f >= 20) & (f <= hi) & (np.abs(f - 50) > 4) & (np.abs(f - 100) > 4)
    hf = np.sqrt(p[hfm].sum(axis=0) * df)
    lf = p[(f >= 0.5) & (f < 4)].sum(axis=0) * df
    mid = np.sqrt(p[(f >= 4) & (f < 20)].sum(axis=0) * df)
    mains = p[(f >= 48) & (f <= 52)].sum(axis=0) * df
    fz, oz = blink_p2p(raw, fs)
    ref = hf_ref if hf_ref else JAW_DEFAULT_REF_UV
    g = np.linalg.norm(np.asarray(gyro, float), axis=1) if gyro is not None and len(gyro) else None
    return np.array([
        np.log10(np.median(hf) + _EPS), np.log10(hf.max() + _EPS),
        np.log10(np.median(hf) / ref + _EPS),
        np.log10(fz + 1), np.log10(oz + 1), np.log10((fz + 1) / (oz + 1)),
        np.log10(np.median(lf) + _EPS), np.log10(np.median(mid) + _EPS), np.log10(mid.max() + _EPS),
        float(np.nan_to_num(stats.kurtosis(x, axis=0)).max()),
        motion_level(gyro), float(g.mean()) if g is not None else 0.0,
        np.log10(np.median(mains) + _EPS),
    ])


def _hf_refs(p: Protocol) -> np.ndarray:
    """Running-median HF reference per whole second, like ArtifactDetector learns it."""
    det = ArtifactDetector(fs=p.fs)
    n1, refs = int(p.fs), []
    for k in range(len(p.eeg) // n1):
        refs.append(det.hf_ref or JAW_DEFAULT_REF_UV)
        s = slice(k * n1, (k + 1) * n1)
        det(p.eeg[s], None if p.gyro is None else p.gyro[s])
    return np.array(refs)


def labelled_segments(p: Protocol) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """(X features, y labels, threshold-detector labels) for one recording."""
    n1 = int(p.fs)
    refs = _hf_refs(p)
    segs: list[tuple[int, str]] = []
    for name, a, b in p.phases:
        if name in QUIET or name == "talk":
            lab = "none" if name in QUIET else "jaw"
            start = -(-a // n1) * n1  # whole seconds inside the phase
            segs += [(s, lab) for s in range(start, b - n1 + 1, n1)]
    for i, lab in p.cues:
        s = i + int(CUE_OFFSET_S[lab] * p.fs)
        if s + n1 <= len(p.eeg):
            segs.append((s, lab))
    X, y, thr = [], [], []
    for s, lab in segs:
        raw = p.eeg[s:s + n1]
        g = None if p.gyro is None else p.gyro[s:s + n1]
        ref = float(refs[min(s // n1, len(refs) - 1)])
        X.append(segment_features(raw, g, ref, p.fs))
        y.append(lab)
        thr.append(artifact(raw, g, p.fs, ref))
    return np.array(X), np.array(y), thr


def scores(y_true, y_pred) -> dict[str, float]:
    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    out = {}
    for c in CLASSES[1:]:
        m = y_true == c
        if m.any():
            out[f"recall {c} %"] = 100 * float(np.mean(y_pred[m] == c))
    quiet = y_true == "none"
    if quiet.any():
        out["false alarms %"] = 100 * float(np.mean(y_pred[quiet] != "none"))
    recalls = [v for k, v in out.items() if k.startswith("recall")]
    out["mean recall %"] = float(np.mean(recalls)) if recalls else float("nan")
    return out


def _classifier():
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1,
                                          class_weight="balanced", random_state=0)


class ArtifactModel:
    """Wraps the classifier for ArtifactDetector(model=...)."""

    def __init__(self, clf, meta: dict | None = None):
        self.clf = clf
        self.meta = meta or {}

    def predict_label(self, raw, gyro, hf_ref, fs: float = FS) -> str:
        if len(raw) < fs * 0.5:
            return "none"
        return str(self.clf.predict(segment_features(raw, gyro, hf_ref, fs)[None, :])[0])

    def save(self, path: Path = DEFAULT_MODEL) -> Path:
        import joblib

        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"clf": self.clf, "version": FEATURES_VERSION, "meta": self.meta}, path)
        return path

    @classmethod
    def load(cls, path: Path = DEFAULT_MODEL) -> ArtifactModel:
        import joblib

        d = joblib.load(path)
        if d.get("version") != FEATURES_VERSION:
            raise ValueError(f"{path}: features version {d.get('version')}, "
                             f"need {FEATURES_VERSION}; retrain")
        return cls(d["clf"], d.get("meta"))


def default_detector(fs: float = FS, prompt=None) -> ArtifactDetector:
    """ArtifactDetector with the trained model if there is one (live + calibration)."""
    choice = os.environ.get("BRAINCRAFT_ARTIFACT_MODEL", "")
    path = Path(choice) if choice and choice != "off" else DEFAULT_MODEL
    if choice == "off" or not path.exists():
        return ArtifactDetector(fs=fs)
    try:
        model = ArtifactModel.load(path)
    except Exception as e:  # never block the pipeline on a bad model file
        log.warning("artifact model %s not used: %s", path, e)
        return ArtifactDetector(fs=fs)
    if prompt:
        prompt(f"[eeg] artifact detection: trained model {path.name}")
    return ArtifactDetector(fs=fs, model=model)


def train(files: list[Path], out: Path = DEFAULT_MODEL, force: bool = False,
          say=print) -> tuple[ArtifactModel | None, dict]:
    data = [(f, *labelled_segments(load_protocol(f))) for f in files]
    data = [d for d in data if len(d[2])]
    if not data:
        raise SystemExit("no labelled segments in these files")
    report = {}
    if len(data) >= 2:
        y_all, pred_all, thr_all = [], [], []
        for i, (f, X, y, thr) in enumerate(data):
            Xtr = np.vstack([d[1] for j, d in enumerate(data) if j != i])
            ytr = np.concatenate([d[2] for j, d in enumerate(data) if j != i])
            if len(set(ytr)) < 2:
                continue
            clf = _classifier().fit(Xtr, ytr)
            y_all += list(y)
            pred_all += list(clf.predict(X))
            thr_all += thr
        report = {"model": scores(y_all, pred_all), "thresholds": scores(y_all, thr_all)}
        say("leave-one-recording-out:")
        for k in report["model"]:
            say(f"  {k:16s} model {report['model'][k]:6.1f}   thresholds "
                f"{report['thresholds'].get(k, float('nan')):6.1f}")
    else:
        say("only one recording: no held-out evaluation (record more people/runs)")
    better = bool(report) and (
        report["model"]["mean recall %"] > report["thresholds"]["mean recall %"]
        and report["model"].get("false alarms %", 0) <= report["thresholds"].get("false alarms %", 0) + 2
    )
    X = np.vstack([d[1] for d in data])
    y = np.concatenate([d[2] for d in data])
    model = ArtifactModel(_classifier().fit(X, y), {
        "files": [Path(d[0]).name for d in data], "segments": int(len(y)),
        "classes": sorted(set(y)), "report": report})
    if better or force:
        say(f"saved {model.save(out)} ({len(y)} segments from {len(data)} recordings)"
            + ("" if better else " [--force: not better than thresholds]"))
        return model, report
    say("model NOT saved: not better than the thresholds on held-out recordings "
        "(use --force to save anyway)")
    return None, report


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Train the learned artifact detector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("files", nargs="+", type=Path)
    t.add_argument("--out", type=Path, default=DEFAULT_MODEL)
    t.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    train(args.files, args.out, args.force)


if __name__ == "__main__":
    main()
