"""Per-person baseline -> z-scores (P1).

Protocol (2.5 min):
  60 s eyes open, rest, looking at the screen
  30 s eyes closed (sanity check: alpha at Oz/PO7/PO8 must jump)
  60 s easy game
Baseline = mean/std of each index (engagement, fatigue, alpha_low; log10
values, see eeg.features) over the eyes-open + easy-game parts, one
value per second, artifact seconds excluded.

    python -m eeg.calibrate [--source lsl|synthetic] [--name STREAM]
                            [--out sessions/baseline.json] [--fast]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from eeg.features import ANALYZER_VERSION, INDEX_NAMES, Analyzer

EMA_SECONDS = 15.0  # smoothing of z-scores
DEFAULT_BASELINE = Path(__file__).resolve().parent.parent / "sessions" / "baseline.json"
DURATIONS = (60, 30, 60)
FAST_DURATIONS = (12, 10, 12)
ALPHA_JUMP_MIN = 2.0  # eyes-closed / eyes-open posterior alpha power ratio
STD_FLOOR = 1e-6

log = logging.getLogger("eeg.calibrate")


def default_baseline_path() -> Path:
    env = os.environ.get("BRAINCRAFT_BASELINE")
    return Path(env) if env else DEFAULT_BASELINE


@dataclass
class Baseline:
    mean: dict[str, float]
    std: dict[str, float]
    n: dict[str, int] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)  # alpha check, source, time

    def z(self, name: str, value: float) -> float:
        sd = self.std.get(name) or 1.0
        if not math.isfinite(sd) or sd < STD_FLOOR:
            sd = 1.0
        return (value - self.mean[name]) / sd

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Baseline:
        return cls(
            mean={k: float(v) for k, v in d["mean"].items()},
            std={k: float(v) for k, v in d["std"].items()},
            n={k: int(v) for k, v in d.get("n", {}).items()},
            meta=dict(d.get("meta", {})),
        )

    def save(self, path: str | Path | None = None) -> Path:
        path = Path(path) if path else default_baseline_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path | None = None) -> Baseline:
        path = Path(path) if path else default_baseline_path()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def compute_baseline(samples: dict[str, list[float]], meta: dict | None = None) -> Baseline:
    """Mean / std (ddof=1) per index; non-finite values are dropped.

    Raises ValueError if an index has fewer than 2 usable values.
    """
    mean, std, n = {}, {}, {}
    for name, values in samples.items():
        v = np.asarray([x for x in values if x is not None and math.isfinite(x)], dtype=float)
        if len(v) < 2:
            raise ValueError(f"not enough clean values for {name!r} ({len(v)}); recalibrate")
        mean[name] = float(v.mean())
        std[name] = float(v.std(ddof=1))  # z() treats ~0 as 1
        n[name] = len(v)
    return Baseline(mean, std, n, dict(meta or {}))


class CalibrationError(RuntimeError):
    pass


PHASES = (
    ("rest", "Eyes OPEN, relax, look at the screen"),
    ("eyes_closed", "Close your EYES and relax"),
    ("game", "Play the easy game"),
)


def run_calibration(
    stream,
    prompt: Callable[[str], None] = print,
    durations: tuple[float, float, float] = DURATIONS,
    period: float = 1.0,
    window_s: float = 4.0,
    stop: threading.Event | None = None,
    on_tick: Callable[[str, int], None] | None = None,
    simulate: bool = True,
) -> Baseline | None:
    """Run the 3-phase protocol on a started EEGStream; returns a Baseline.

    Paced by the stream's samples (not wall time), so an accelerated
    synthetic stream calibrates faster. One index value per `period`
    from the last `window_s`; seconds with artifacts are skipped, as are
    the first seconds of each phase (window still holds the previous one).
    With a synthetic stream and simulate=True the generator's eyes are
    closed during the eyes-closed phase. Returns None if `stop` is set.
    Raises CalibrationError if no data arrives or too few clean seconds.
    Baseline.meta["alpha_check"] says whether the eyes-closed alpha jump
    was seen (ratio >= ALPHA_JUMP_MIN).
    """
    stop = stop or threading.Event()
    from eeg.artifact_model import default_detector

    analyzer = Analyzer(fs=stream.fs, detector=default_detector(stream.fs))
    fs = stream.fs
    step = max(int(round(period * fs)), 1)
    gen = getattr(stream, "generator", None) if simulate else None

    # make sure the window is full before starting
    need = int(window_s * fs)
    if not stream.wait_for_samples(need, timeout=window_s / max(getattr(stream, "speed", 1), 1) + 10):
        if stop.is_set():
            return None
        raise CalibrationError("no EEG data arriving; check the stream")

    samples: dict[str, list[float]] = {k: [] for k in INDEX_NAMES}
    alpha: dict[str, list[float]] = {"rest": [], "eyes_closed": []}
    skipped = 0
    for (phase, text), duration in zip(PHASES, durations):
        n_ticks = int(round(duration / period))
        settle = min(int(math.ceil(window_s / period)) + 1, n_ticks // 3)
        prompt(f"[calibration] {text} ({duration:g} s)")
        if gen is not None:
            gen.eyes_closed = phase == "eyes_closed"
        start = stream.total_samples
        for k in range(n_ticks):
            target = start + (k + 1) * step
            got = stream.wait_for_samples(target, timeout=period * 5 + 5)
            if stop.is_set():
                if gen is not None:
                    gen.eyes_closed = False
                return None
            if not got:
                raise CalibrationError("EEG data stopped during calibration")
            if on_tick:
                on_tick(phase, k)
            if k < settle:
                continue
            eeg, gyro = stream.windows(window_s)
            try:
                res = analyzer.step(eeg, gyro)
            except Exception:
                log.exception("bad calibration window, skipped")
                skipped += 1
                continue
            if not res.ok:
                skipped += 1
                continue
            if phase in alpha:
                alpha[phase].append(res.posterior_alpha)
            if phase != "eyes_closed":
                for name in INDEX_NAMES:
                    samples[name].append(res.indices[name])
    if gen is not None:
        gen.eyes_closed = False

    open_a = float(np.median(alpha["rest"])) if alpha["rest"] else float("nan")
    closed_a = float(np.median(alpha["eyes_closed"])) if alpha["eyes_closed"] else float("nan")
    ratio = closed_a / open_a if open_a > 0 else float("nan")
    seen = bool(math.isfinite(ratio) and ratio >= ALPHA_JUMP_MIN)
    check = {"open": open_a, "closed": closed_a, "ratio": ratio, "seen": seen}
    if seen:
        prompt(f"[calibration] eyes-closed alpha jump OK: x{ratio:.1f} at PO7/Oz/PO8")
    else:
        prompt(f"[calibration] WARNING: no clear eyes-closed alpha jump (x{ratio:.2f}, "
               f"want >= x{ALPHA_JUMP_MIN:g}); check electrodes / channel layout")
    try:
        base = compute_baseline(samples, meta={
            "alpha_check": check,
            "skipped_seconds": skipped,
            "source": getattr(stream, "source", "?"),
            "created": time.time(),
            "index_units": "log10", "analyzer": ANALYZER_VERSION,
        })
    except ValueError as e:
        raise CalibrationError(str(e)) from e
    prompt("[calibration] done: " + ", ".join(
        f"{k} {base.mean[k]:+.3f} +- {base.std[k]:.3f} (n={base.n[k]})" for k in INDEX_NAMES))
    return base


QUICK_SECONDS = 30  # clean seconds for a quick baseline
QUICK_TIMEOUT_S = 120


def quick_baseline(
    stream,
    seconds: int = QUICK_SECONDS,
    prompt: Callable[[str], None] = print,
    period: float = 1.0,
    window_s: float = 4.0,
    stop: threading.Event | None = None,
    on_tick: Callable[[int], None] | None = None,
) -> Baseline | None:
    """Baseline from the first `seconds` clean seconds, no protocol.

    For skipping calibration: the player just sits or starts playing.
    Less reliable than run_calibration (no eyes-closed check, the state
    while it is recorded becomes "normal"), so it is never saved.
    Returns None if `stop` is set; raises CalibrationError without data.
    """
    stop = stop or threading.Event()
    from eeg.artifact_model import default_detector

    analyzer = Analyzer(fs=stream.fs, detector=default_detector(stream.fs))
    step = max(int(round(period * stream.fs)), 1)
    if not stream.wait_for_samples(int(window_s * stream.fs), timeout=window_s + 10):
        if stop.is_set():
            return None
        raise CalibrationError("no EEG data arriving; check the stream")
    prompt(f"[calibration] skipped: quick baseline from the next {seconds} clean seconds "
           "(sit still or play normally)")
    samples: dict[str, list[float]] = {k: [] for k in INDEX_NAMES}
    start, tick, skipped = stream.total_samples, 0, 0
    deadline = time.monotonic() + QUICK_TIMEOUT_S / max(getattr(stream, "speed", 1), 1)
    while len(samples[INDEX_NAMES[0]]) < seconds:
        tick += 1
        got = stream.wait_for_samples(start + tick * step, timeout=period * 5 + 5)
        if stop.is_set():
            return None
        if not got or time.monotonic() > deadline:
            raise CalibrationError(f"only {len(samples[INDEX_NAMES[0]])} clean seconds "
                                   f"({skipped} with artifacts); check electrodes")
        if on_tick:
            on_tick(tick)
        try:
            res = analyzer.step(*stream.windows(window_s))
        except Exception:
            log.exception("bad window, skipped")
            skipped += 1
            continue
        if not res.ok:
            skipped += 1
            continue
        for name in INDEX_NAMES:
            samples[name].append(res.indices[name])
    try:
        base = compute_baseline(samples, meta={
            "quick": True, "skipped_seconds": skipped, "created": time.time(),
            "source": getattr(stream, "source", "?"), "index_units": "log10", "analyzer": ANALYZER_VERSION,
        })
    except ValueError as e:
        raise CalibrationError(str(e)) from e
    prompt(f"[calibration] quick baseline ready ({seconds} s, {skipped} artifact seconds skipped)")
    return base


def main(argv: list[str] | None = None) -> None:
    from eeg.stream import EEGStream

    p = argparse.ArgumentParser(description="BrainCraft EEG calibration (baseline for z-scores)")
    p.add_argument("--source", choices=("lsl", "synthetic"), default="lsl")
    p.add_argument("--name", default=os.environ.get("BRAINCRAFT_LSL_NAME"),
                   help="LSL stream name (default: first stream of type EEG)")
    p.add_argument("--out", default=None, help=f"baseline JSON (default {DEFAULT_BASELINE})")
    p.add_argument("--fast", action="store_true",
                   help=f"short phases {FAST_DURATIONS} s; synthetic also runs 10x faster")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    speed = 10.0 if (args.fast and args.source == "synthetic") else 1.0
    stream = EEGStream(args.source, name=args.name, speed=speed).start()
    try:
        base = run_calibration(stream, durations=FAST_DURATIONS if args.fast else DURATIONS)
    except KeyboardInterrupt:
        print("calibration aborted")
        return
    finally:
        stream.stop()
    if base is not None:
        print(f"saved {base.save(args.out)}")


if __name__ == "__main__":
    main()
