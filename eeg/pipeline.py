"""EEG -> BrainState loop, run by state_service in a daemon thread (P1).

    run(publish, stop, source="lsl")

1. starts an EEGStream ("lsl": Unicorn / synthetic LSL outlet;
   "synthetic": in-process generator); if the LSL stream isn't there
   yet it publishes quality="bad" and retries every few seconds;
2. loads the baseline (sessions/baseline.json, or BRAINCRAFT_BASELINE);
   if missing, runs the calibration protocol first (quality="bad"
   meanwhile; instructions are printed on the service console) and saves it;
3. every `period` s: indices on the last `window_s` s -> z-scores ->
   EMA (time constant `ema_s`) -> publish(BrainState).

BrainState semantics:
- *_z: smoothed z-scores vs the baseline (log10 indices, see eeg.features);
- artifact: artifact in the newest second ("none"|"blink"|"jaw"|"motion");
- jaw_clench: artifact == "jaw" (usable as a deliberate trigger);
- quality "bad": artifact now, a jaw/motion still inside the 4 s window,
  no/stale data, or calibrating. While bad the *_z values are HELD
  (last good values), not updated.

Env vars: BRAINCRAFT_LSL_NAME (stream name), BRAINCRAFT_BASELINE (path),
BRAINCRAFT_EEG_COLS / BRAINCRAFT_GYRO_COLS (see eeg/layout.py).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from contracts import BrainState
from eeg.calibrate import (
    DURATIONS,
    EMA_SECONDS,
    QUICK_SECONDS,
    Baseline,
    CalibrationError,
    default_baseline_path,
    quick_baseline,
    run_calibration,
)
from eeg.features import Analyzer
from eeg.stream import EEGStream, StreamNotFound

log = logging.getLogger("eeg.pipeline")

Z_FIELDS = {"engagement": "engagement_z", "fatigue": "fatigue_z", "alpha_low": "alpha_low_z"}
STALE_S = 2.0  # no new samples for this long -> quality bad
RETRY_S = 3.0


def _bad(last: dict[str, float], artifact: str = "none") -> BrainState:
    return BrainState(t=time.time(), quality="bad", artifact=artifact,
                      jaw_clench=artifact == "jaw", **last)


def run(
    publish: Callable[[BrainState], None],
    stop: threading.Event,
    source: str = "lsl",
    baseline_path: str | Path | None = None,
    period: float = 1.0,
    window_s: float = 4.0,
    ema_s: float = EMA_SECONDS,
    name: str | None = None,
    stream: EEGStream | None = None,
    calibration_durations: tuple[float, float, float] = DURATIONS,
    prompt: Callable[[str], None] | None = None,
    on_stream: Callable[[EEGStream], None] | None = None,
    calibration: str | None = None,
    **stream_kwargs,
) -> None:
    """Publish BrainState every `period` s until `stop` is set. Never raises.

    `on_stream(stream)` is called once the EEG stream is running (the state
    service uses it to serve the raw signal to the HUD).

    `calibration` (or BRAINCRAFT_CALIBRATION) applies only when there is no
    baseline file: "full" runs the 2.5 min protocol and saves it (default),
    "quick" skips it and uses the first QUICK_SECONDS clean seconds, unsaved.
    """
    calibration = calibration or os.environ.get("BRAINCRAFT_CALIBRATION", "full")
    baseline_path = Path(baseline_path) if baseline_path else default_baseline_path()
    name = name or os.environ.get("BRAINCRAFT_LSL_NAME") or None
    prompt = prompt or (lambda msg: (print(msg, flush=True), log.info(msg)))
    zero = {f: 0.0 for f in Z_FIELDS.values()}
    own_stream = stream is None

    try:
        # 1. stream
        while stream is None:
            try:
                stream = EEGStream(source, name=name, seconds=max(10.0, window_s * 2),
                                   **stream_kwargs).start()
            except StreamNotFound as e:
                log.warning("%s (retrying in %g s)", e, RETRY_S)
                publish(_bad(zero))
                if stop.wait(RETRY_S):
                    return
            except Exception:
                log.exception("could not start EEG stream (retrying)")
                publish(_bad(zero))
                if stop.wait(RETRY_S):
                    return

        if on_stream is not None:
            try:
                on_stream(stream)
            except Exception:
                log.exception("on_stream callback failed")

        # 2. baseline
        baseline: Baseline | None = None
        while baseline is None and not stop.is_set():
            if baseline_path.exists():
                try:
                    baseline = Baseline.load(baseline_path)
                    log.info("baseline loaded from %s", baseline_path)
                    break
                except Exception:
                    log.exception("baseline %s unreadable, recalibrating", baseline_path)
            quick = calibration == "quick"
            prompt(f"[eeg] no baseline at {baseline_path}: "
                   f"{'quick baseline, calibration skipped' if quick else 'calibrating'}")
            publish(_bad(zero))
            try:
                if quick:
                    baseline = quick_baseline(
                        stream, seconds=QUICK_SECONDS, prompt=prompt, window_s=window_s,
                        stop=stop, on_tick=lambda *_: publish(_bad(zero)),
                    )
                else:
                    baseline = run_calibration(
                        stream, prompt=prompt, durations=calibration_durations, window_s=window_s,
                        stop=stop, on_tick=lambda *_: publish(_bad(zero)),
                    )
            except CalibrationError as e:
                log.warning("calibration failed: %s (retrying)", e)
                if stop.wait(RETRY_S):
                    return
                continue
            except Exception:
                log.exception("calibration crashed (retrying)")
                if stop.wait(RETRY_S):
                    return
                continue
            if baseline is not None and not baseline.meta.get("quick"):
                try:
                    baseline.save(baseline_path)
                    prompt(f"[eeg] baseline saved to {baseline_path}")
                except OSError:
                    log.exception("could not save baseline to %s", baseline_path)
        if baseline is None:
            return

        # 3. loop
        analyzer = Analyzer(fs=stream.fs)
        alpha = 1.0 - math.exp(-period / ema_s) if ema_s > 0 else 1.0
        smooth: dict[str, float] | None = None
        n_ok = 0
        while not stop.wait(period):
            try:
                if stream.seconds_since_data > STALE_S:
                    publish(_bad(smooth or zero))
                    continue
                eeg, gyro = stream.windows(window_s)
                res = analyzer.step(eeg, gyro)
                if res.ok:
                    z = {Z_FIELDS[k]: baseline.z(k, v) for k, v in res.indices.items()}
                    if not all(math.isfinite(v) for v in z.values()):
                        raise ValueError(f"non-finite z-scores {z}")
                    # EMA; the first ~ema_s seconds use the running mean
                    # instead, so the very first window doesn't dominate.
                    n_ok += 1
                    a = max(alpha, 1.0 / n_ok)
                    smooth = z if smooth is None else {
                        k: smooth[k] + a * (z[k] - smooth[k]) for k in z}
                    publish(BrainState(t=time.time(), quality="ok", artifact="none",
                                       jaw_clench=False, **smooth))
                else:
                    publish(_bad(smooth or zero, res.artifact))
            except Exception:
                log.exception("bad EEG window, skipped")
                try:
                    publish(_bad(smooth or zero))
                except Exception:
                    log.exception("publish failed")
    except Exception:  # last resort: never take the service down
        log.exception("EEG pipeline crashed")
    finally:
        if own_stream and stream is not None:
            stream.stop()
