"""EEG input + ring buffer for the Unicorn Hybrid Black (P1).

Headset -> eeg.gpype_bridge (or Unicorn Suite's "Unicorn LSL" app) ->
LSL -> this module. The stream carries
more than EEG (accelerometer, gyroscope, ...); which columns are EEG and
gyro is configurable, see eeg/layout.py (BRAINCRAFT_EEG_COLS,
BRAINCRAFT_GYRO_COLS).

    python -m eeg.stream            # list LSL streams on the network

    s = EEGStream("synthetic", seed=0).start()   # or EEGStream("lsl")
    eeg = s.window(4.0)             # (samples, 8) uV, oldest first
    eeg, gyro = s.windows(4.0)      # same instant; gyro (samples, 3) or None
    s.generator.eyes_closed = True  # synthetic only
    s.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence

import numpy as np

from eeg.layout import EEG_CHANNELS, FS, Layout

__all__ = ["FS", "EEG_CHANNELS", "EEGStream", "StreamNotFound", "list_streams"]

log = logging.getLogger("eeg.stream")


class StreamNotFound(RuntimeError):
    pass


def list_streams(wait_time: float = 2.0) -> list[dict]:
    """Print every LSL stream on the network (step 1: read any signal)."""
    from pylsl import resolve_streams

    found = []
    for info in resolve_streams(wait_time=wait_time):
        d = {
            "name": info.name(),
            "type": info.type(),
            "channels": info.channel_count(),
            "srate": info.nominal_srate(),
            "source_id": info.source_id(),
        }
        found.append(d)
        print(f"{d['name']!r:30} type={d['type']!r:8} channels={d['channels']:3} "
              f"srate={d['srate']:g} source_id={d['source_id']!r}")
    if not found:
        print("no LSL streams found")
    return found


class EEGStream:
    """Keeps the last `seconds` of EEG (and gyro) in a ring buffer.

    source="lsl": resolve an LSL stream by `name` if given, else by type
    "EEG" (within `timeout` s). source="synthetic": in-process
    SyntheticEEG, no LSL; `speed` > 1 runs faster than real time (tests),
    `prefill` fills the buffer at start so windows are usable at once.
    Extra kwargs go to SyntheticEEG (seed, engagement, eyes_closed, ...).
    """

    def __init__(
        self,
        source: str = "lsl",
        name: str | None = None,
        seconds: float = 10.0,
        timeout: float = 5.0,
        eeg_cols: Sequence[int] | None = None,
        gyro_cols: Sequence[int] | None | object = None,
        scale: float = 1.0,
        speed: float = 1.0,
        prefill: bool = True,
        **synthetic_kwargs,
    ):
        if source not in ("lsl", "synthetic"):
            raise ValueError(f"source must be 'lsl' or 'synthetic', got {source!r}")
        self.source = source
        self.name = name
        self.seconds = seconds
        self.timeout = timeout
        self.scale = scale
        self.speed = speed
        self.prefill = prefill
        self._eeg_cols = eeg_cols
        # gyro_cols=None means "auto" (env var / default); pass () to disable.
        self._gyro_cols = gyro_cols
        self._synthetic_kwargs = synthetic_kwargs
        self.fs: float = FS
        self.layout: Layout | None = None
        self.generator = None  # SyntheticEEG for source="synthetic"
        self.info = None  # pylsl StreamInfo for source="lsl"
        self._lock = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.total_samples = 0
        self.last_data_time = 0.0  # wall clock of the last chunk
        self._rec: list[tuple[np.ndarray, np.ndarray | None]] | None = None
        self._alloc(FS, 3)

    # --- buffer ------------------------------------------------------------

    def _alloc(self, fs: float, n_gyro: int) -> None:
        self.fs = fs
        self._n = int(round(self.seconds * fs))
        self._eeg = np.zeros((self._n, len(EEG_CHANNELS)))
        self._gyro = np.zeros((self._n, max(n_gyro, 1)))
        self._has_gyro = n_gyro > 0
        self._pos = 0
        self._filled = 0

    def _push(self, chunk: np.ndarray) -> None:
        """chunk: (n, stream_channels) in stream units."""
        if chunk.size == 0:
            return
        assert self.layout is not None
        eeg = chunk[:, self.layout.eeg] * self.scale
        gyro = chunk[:, self.layout.gyro] if self.layout.gyro else None
        with self._lock:
            if self._rec is not None:
                self._rec.append((eeg.copy(), None if gyro is None else gyro.copy()))
            n = len(eeg)
            if n >= self._n:
                eeg, n_keep = eeg[-self._n :], self._n
                gyro = gyro[-self._n :] if gyro is not None else None
            else:
                n_keep = n
            idx = (self._pos + np.arange(n_keep)) % self._n
            self._eeg[idx] = eeg
            if gyro is not None:
                self._gyro[idx] = gyro
            self._pos = (self._pos + n_keep) % self._n
            self._filled = min(self._n, self._filled + n_keep)
            self.total_samples += n
            self.last_data_time = time.time()
            self._lock.notify_all()

    def windows(self, seconds: float = 4.0) -> tuple[np.ndarray, np.ndarray | None]:
        """(eeg (samples, 8) uV, gyro (samples, 3) or None), oldest first.

        Fewer samples than asked if the buffer is not full yet.
        """
        with self._lock:
            k = min(int(round(seconds * self.fs)), self._filled)
            idx = (self._pos - k + np.arange(k)) % self._n
            eeg = self._eeg[idx].copy()
            gyro = self._gyro[idx].copy() if self._has_gyro else None
        return eeg, gyro

    def window(self, seconds: float = 4.0) -> np.ndarray:
        """Last `seconds` of EEG, shape (samples, 8), microvolts."""
        return self.windows(seconds)[0]

    def gyro_window(self, seconds: float = 4.0) -> np.ndarray | None:
        return self.windows(seconds)[1]

    def wait_for_samples(self, total: int, timeout: float | None = None) -> bool:
        """Block until total_samples >= total (or stop/timeout). True if reached."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while self.total_samples < total and not self._stop.is_set():
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._lock.wait(0.1 if left is None else min(0.1, left))
            return self.total_samples >= total

    # --- full recording (beyond the ring buffer) -----------------------------

    def start_recording(self) -> int:
        """Keep every sample from now on; returns total_samples at the start."""
        with self._lock:
            self._rec = []
            return self.total_samples

    def stop_recording(self) -> tuple[np.ndarray, np.ndarray | None]:
        """(eeg (n, 8) uV, gyro (n, 3) or None) since start_recording()."""
        with self._lock:
            rec, self._rec = self._rec or [], None
        if not rec:
            return np.zeros((0, len(EEG_CHANNELS))), None
        eeg = np.concatenate([e for e, _ in rec])
        gyros = [g for _, g in rec]
        gyro = np.concatenate(gyros) if all(g is not None for g in gyros) else None
        return eeg, gyro

    @property
    def seconds_since_data(self) -> float:
        return time.time() - self.last_data_time if self.last_data_time else float("inf")

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> EEGStream:
        if self._thread is not None:
            return self
        self._stop.clear()
        if self.source == "synthetic":
            self._start_synthetic()
        else:
            self._start_lsl()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._lock.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> EEGStream:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # --- synthetic ---------------------------------------------------------

    def _start_synthetic(self) -> None:
        from eeg.synthetic import SyntheticEEG

        gen = self._synthetic_kwargs.pop("generator", None)
        if gen is None:
            gen = SyntheticEEG(**self._synthetic_kwargs)
        self.generator = gen
        gyro_cols = None if gen.gyro_cols is None else tuple(int(c) for c in gen.gyro_cols)
        self.layout = Layout(eeg=tuple(int(c) for c in gen.eeg_cols), gyro=gyro_cols)
        self._alloc(gen.fs, len(gyro_cols or ()))
        if self.prefill:
            self._push(gen.generate(self._n))
        self._thread = threading.Thread(target=self._run_synthetic, name="eeg-synthetic", daemon=True)
        self._thread.start()

    def _run_synthetic(self) -> None:
        gen = self.generator
        t0 = time.perf_counter()
        made = 0
        rate = gen.fs * self.speed
        while not self._stop.is_set():
            due = int((time.perf_counter() - t0) * rate) - made
            if due > 0:
                self._push(gen.generate(due))
                made += due
            self._stop.wait(0.02)

    # --- LSL ---------------------------------------------------------------

    def _resolve(self):
        from pylsl import resolve_byprop

        prop, value = ("name", self.name) if self.name else ("type", "EEG")
        found = resolve_byprop(prop, value, minimum=1, timeout=self.timeout)
        if not found:
            from pylsl import resolve_streams

            others = [f"{i.name()!r} (type {i.type()!r}, {i.channel_count()} ch)"
                      for i in resolve_streams(wait_time=1.0)]
            seen = ", ".join(others) if others else "none"
            raise StreamNotFound(
                f"no LSL stream with {prop}={value!r} found within {self.timeout:g} s. "
                f"Streams on the network: {seen}. Is `python -m eeg.gpype_bridge` running "
                f"(or the Unicorn LSL app streaming, or `python -m eeg.synthetic --lsl`)? "
                f"Select the stream with "
                f"name=... / BRAINCRAFT_LSL_NAME."
            )
        if len(found) > 1:
            log.warning("several LSL streams match %s=%r, using %r", prop, value, found[0].name())
        return found[0]

    def _start_lsl(self) -> None:
        from pylsl import StreamInlet

        info = self._resolve()
        n_ch = info.channel_count()
        srate = info.nominal_srate() or FS
        if abs(srate - FS) > 1e-6:
            log.warning("stream %r runs at %g Hz, expected %d Hz", info.name(), srate, FS)
        gyro_arg = self._gyro_cols
        if gyro_arg is None:
            from eeg.layout import _UNSET

            gyro_arg = _UNSET
        elif len(gyro_arg) == 0:  # type: ignore[arg-type]
            gyro_arg = None
        self.layout = Layout.for_stream(n_ch, self._eeg_cols, gyro_arg)
        self._alloc(srate, len(self.layout.gyro or ()))
        self.info = info
        log.info("LSL stream %r: %d channels at %g Hz, EEG cols %s, gyro cols %s",
                 info.name(), n_ch, srate, self.layout.eeg, self.layout.gyro)
        inlet = StreamInlet(info, max_buflen=int(max(self.seconds, 30)), recover=True)
        self._thread = threading.Thread(target=self._run_lsl, args=(inlet,), name="eeg-lsl", daemon=True)
        self._thread.start()

    def _run_lsl(self, inlet) -> None:
        while not self._stop.is_set():
            try:
                samples, _ts = inlet.pull_chunk(timeout=0.2, max_samples=1024)
                if samples:
                    self._push(np.asarray(samples, dtype=float))
            except Exception:  # lost stream etc.: keep trying, never die
                log.exception("LSL read failed, retrying")
                self._stop.wait(1.0)
        try:
            inlet.close_stream()
        except Exception:
            pass


if __name__ == "__main__":
    list_streams()
