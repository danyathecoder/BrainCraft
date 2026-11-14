"""Replay recordings through the EEG analysis and compare (P1).

    python -m eeg.replay sessions/artifacts-diuna-*.npz            # labelled protocol
    python -m eeg.replay sessions/raw-headset-*.csv               # raw bridge recording
    python -m eeg.replay A.npz B.npz                              # e.g. OSCAR off vs on
    python -m eeg.replay A.npz --vs-legacy                        # old vs new analysis
    python -m eeg.replay A.npz --motion-deg-s 60 --jaw-ratio 6    # try thresholds
    python -m eeg.replay A.npz --model sessions/artifact_model.joblib

Per recording (and config) it prints:
- usable % of 1 s steps (4 s windows, as the live pipeline), overall and per phase;
- share of seconds per artifact label (contact, motion, jaw, blink);
- labelled protocols only: cue recall (did the detector see the cued blink /
  jaw / head turn?), false alarms while still / eyes closed, and the
  eyes-closed alpha ratio (should stay >= 2).
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from eeg.features import Analyzer, ArtifactDetector
from eeg.layout import DEFAULT_EEG_COLS, DEFAULT_GYRO_COLS, FS
from eeg.protocol import CUE_WINDOW_S, Protocol, load_protocol

QUIET_PHASES = ("still", "eyes_closed")
ARTIFACTS = ("contact", "motion", "jaw", "blink")


def load_recording(path: str | Path) -> Protocol:
    """A protocol .npz, or a raw CSV written by the bridge (--record)."""
    path = Path(path)
    if path.suffix == ".npz":
        return load_protocol(path)
    d = pd.read_csv(path, comment="#", skipinitialspace=True)
    x = d.iloc[:, 1:].to_numpy(float)  # first column is time
    eeg = x[:, list(DEFAULT_EEG_COLS)]
    gyro = x[:, list(DEFAULT_GYRO_COLS)] if x.shape[1] > max(DEFAULT_GYRO_COLS) else None
    t = d.iloc[:, 0].to_numpy(float)
    fs = (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else FS
    fs = FS if abs(fs - FS) < 5 else fs
    return Protocol(eeg, gyro, fs, [("all", 0, len(eeg))], [], {"source": "raw csv"}, path)


@dataclass
class Config:
    name: str = "current"
    legacy: bool = False
    detector_kwargs: dict = field(default_factory=dict)
    model: object | None = None

    def detector(self, fs: float) -> ArtifactDetector:
        return ArtifactDetector(fs=fs, model=self.model, **self.detector_kwargs)


def second_labels(p: Protocol, cfg: Config) -> list[str]:
    """Artifact label of every whole second, in order (detector learns as live)."""
    det = cfg.detector(p.fs)
    n1 = int(p.fs)
    out = []
    for k in range(len(p.eeg) // n1):
        s = slice(k * n1, (k + 1) * n1)
        out.append(det(p.eeg[s], None if p.gyro is None else p.gyro[s]))
    return out


def window_steps(p: Protocol, cfg: Config, window_s: float = 4.0) -> list[tuple[int, object]]:
    """(end sample, WindowResult) every 1 s, like the live pipeline."""
    an = Analyzer(fs=p.fs, detector=cfg.detector(p.fs), legacy=cfg.legacy)
    w, step = int(window_s * p.fs), int(p.fs)
    out = []
    for end in range(w, len(p.eeg) + 1, step):
        g = None if p.gyro is None else p.gyro[end - w:end]
        out.append((end, an.step(p.eeg[end - w:end], g)))
    return out


def analyse(p: Protocol, cfg: Config) -> dict:
    steps = window_steps(p, cfg)
    labels = second_labels(p, cfg)
    n1 = int(p.fs)
    m: dict = {"seconds": round(p.seconds)}
    m["usable %"] = 100 * np.mean([r.ok for _, r in steps]) if steps else math.nan
    for name, a, b in p.phases:
        if len(p.phases) > 1:
            sel = [r.ok for end, r in steps if a < end <= b and end - a >= 2 * p.fs]
            m[f"usable % {name}"] = 100 * np.mean(sel) if sel else math.nan
    for art in ARTIFACTS:
        m[f"{art} % of s"] = 100 * np.mean([lab == art for lab in labels]) if labels else math.nan
    if p.cues:
        for art in ("blink", "jaw", "motion"):
            hits = []
            for i, lab in p.cues:
                if lab != art:
                    continue
                k0, k1 = i // n1, int((i + CUE_WINDOW_S[lab] * p.fs) // n1)
                hits.append(any(labels[k] == art for k in range(k0, min(k1 + 1, len(labels)))))
            if hits:
                m[f"cue recall {art} %"] = 100 * np.mean(hits)
    quiet = [labels[k] != "none" for k in range(len(labels))
             if p.phase_at(k * n1) in QUIET_PHASES and p.phase_at((k + 1) * n1 - 1) in QUIET_PHASES]
    if quiet and len(p.phases) > 1:
        m["false alarms quiet %"] = 100 * np.mean(quiet)
    alpha = {ph: [r.posterior_alpha for end, r in steps if r.ok and p.phase_at(end - 1) == ph
                  and p.phase_at(end - int(4 * p.fs)) == ph] for ph in QUIET_PHASES}
    if all(alpha.values()):
        m["eyes-closed alpha x"] = float(np.median(alpha["eyes_closed"]) / np.median(alpha["still"]))
    return m


def table(results: dict[str, dict]) -> str:
    rows = list(dict.fromkeys(k for m in results.values() for k in m))
    cols = list(results)
    width = max(14, *(len(c) for c in cols))
    lines = [f"{'':24s}" + "".join(f"{c:>{width + 2}s}" for c in cols)]
    for r in rows:
        cells = []
        for c in cols:
            v = results[c].get(r)
            cells.append("—" if v is None or (isinstance(v, float) and math.isnan(v))
                         else f"{v:.0f}" if r == "seconds" else f"{v:.1f}")
        lines.append(f"{r:24s}" + "".join(f"{x:>{width + 2}s}" for x in cells))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> dict[str, dict]:
    ap = argparse.ArgumentParser(description="Replay recordings through the EEG analysis")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--vs-legacy", action="store_true", help="also run the old whole-window rule")
    ap.add_argument("--motion-deg-s", type=float)
    ap.add_argument("--jaw-ratio", type=float)
    ap.add_argument("--blink-uv", type=float)
    ap.add_argument("--model", type=Path, help="artifact model (eeg.artifact_model) instead of thresholds")
    args = ap.parse_args(argv)

    kw = {k: v for k, v in (("motion_deg_s", args.motion_deg_s), ("jaw_ratio", args.jaw_ratio),
                            ("blink_uv", args.blink_uv)) if v is not None}
    model = None
    if args.model:
        from eeg.artifact_model import ArtifactModel

        model = ArtifactModel.load(args.model)
    configs = [Config("legacy", legacy=True)] if args.vs_legacy else []
    configs.append(Config("model" if model else ("tuned" if kw else "current"), False, kw, model))
    results = {}
    for f in args.files:
        p = load_recording(f)
        tag = f.stem.replace("artifacts-", "").replace("raw-headset-", "raw-")[:22]
        if p.meta.get("oscar"):
            tag += " [oscar]"
        for cfg in configs:
            results[f"{tag} {cfg.name}" if len(configs) > 1 else tag] = analyse(p, cfg)
    print(table(results))
    return results


if __name__ == "__main__":
    main()
