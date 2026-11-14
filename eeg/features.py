"""Filters, band powers, indices and artifact flags (P1).

Every 1 s on the last 4 s window:
  notch 50 Hz + bandpass 1-40 Hz (zero-phase on the window),
  Welch PSD (fs=250, nperseg=500), mean PSD per band and channel.

Indices are LOG10 values (power ratios are roughly log-normal, so their
logs give symmetric, better-behaved z-scores; the ordering is the same):

  engagement = mean over Fz, Cz of log10(beta / (alpha + theta))
  fatigue    = log10((theta + alpha) / beta) at Fz
  alpha_low  = log10(mean 8-10 Hz power at Pz and Oz)
  ali        = (alpha PO7 - alpha PO8) / (alpha PO7 + alpha PO8)
               raw, in about [-1, 1], not z-scored and not part of the baseline
  ali        = (alpha PO7 - alpha PO8) / (alpha PO7 + alpha PO8)

Artifacts (checked on the RAW, unfiltered signal, newest 1 s by default):
  motion  95th percentile of |gyro| > MOTION_DEG_S
  jaw     RMS of the 20-120 Hz band (50/100 Hz mains bins excluded),
          median over channels, > JAW_RATIO x the typical level
          (running median of recent values; JAW_DEFAULT_REF_UV until known)
  blink   peak-to-peak of Fz (detrended, low-passed 10 Hz) > BLINK_UV
          and > BLINK_FRONTAL_RATIO x the same at Oz (blinks are frontal)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy import signal

from eeg.layout import CH, FS

BANDS = {
    "theta": (4, 8),
    "alpha_low": (8, 10),
    "alpha": (8, 12),
    "beta": (13, 30),
}
INDEX_NAMES = ("engagement", "fatigue", "alpha_low")
POSTERIOR = ("PO7", "Oz", "PO8")  # eyes-closed sanity check channels

MOTION_DEG_S = 30.0
JAW_RATIO = 4.0
JAW_MIN_UV = 6.0  # never call 'jaw' below this HF RMS
JAW_DEFAULT_REF_UV = 3.0
BLINK_UV = 100.0
BLINK_FRONTAL_RATIO = 2.0
_EPS = 1e-12


# --- filtering and PSD ----------------------------------------------------


@lru_cache(maxsize=8)
def _clean_sos(fs: float) -> np.ndarray:
    b, a = signal.iirnotch(50.0, 30.0, fs)
    notch = signal.tf2sos(b, a)
    band = signal.butter(4, [1.0, 40.0], "bandpass", fs=fs, output="sos")
    return np.vstack([notch, band])


@lru_cache(maxsize=8)
def _lowpass_sos(fs: float, hz: float) -> np.ndarray:
    return signal.butter(4, hz, "lowpass", fs=fs, output="sos")


def preprocess(window: np.ndarray, fs: float = FS) -> np.ndarray:
    """Detrend, notch 50 Hz and bandpass 1-40 Hz, zero-phase, along axis 0."""
    x = signal.detrend(np.asarray(window, dtype=float), axis=0, type="linear")
    return signal.sosfiltfilt(_clean_sos(float(fs)), x, axis=0)


def psd(window: np.ndarray, fs: float = FS, nperseg: int = 500) -> tuple[np.ndarray, np.ndarray]:
    nper = min(nperseg, len(window))
    return signal.welch(window, fs=fs, nperseg=nper, axis=0)


def band_powers(
    window: np.ndarray, fs: float = FS, filtered: bool = False
) -> dict[str, np.ndarray]:
    """Mean PSD (uV^2/Hz) per band and channel, after filtering.

    window: (samples, channels) raw EEG in microvolts. Bands are [lo, hi).
    """
    x = window if filtered else preprocess(window, fs)
    f, p = psd(x, fs)
    out = {}
    for name, (lo, hi) in BANDS.items():
        mask = (f >= lo) & (f < hi)
        out[name] = p[mask].mean(axis=0)
    return out


# --- indices ----------------------------------------------------------------


def _ch(bp: dict[str, np.ndarray], band: str, *names: str) -> np.ndarray:
    return np.array([bp[band][CH[n]] for n in names])


def engagement(bp: dict[str, np.ndarray]) -> float:
    """log10 beta / (alpha + theta), mean of Fz and Cz (Pope et al. 1995)."""
    chans = ("Fz", "Cz")
    ratio = _ch(bp, "beta", *chans) / (_ch(bp, "alpha", *chans) + _ch(bp, "theta", *chans) + _EPS)
    return float(np.mean(np.log10(ratio + _EPS)))


def fatigue(bp: dict[str, np.ndarray]) -> float:
    """log10 (theta + alpha) / beta at Fz. Roughly -engagement."""
    i = CH["Fz"]
    ratio = (bp["theta"][i] + bp["alpha"][i]) / (bp["beta"][i] + _EPS)
    return float(np.log10(ratio + _EPS))


def alpha_low(bp: dict[str, np.ndarray]) -> float:
    """log10 lower alpha 8-10 Hz power, mean of Pz and Oz (vigilance axis)."""
    return float(np.log10(np.mean(_ch(bp, "alpha_low", "Pz", "Oz")) + _EPS))


def ali(bp: dict[str, np.ndarray]) -> float:
    """Alpha Lateralization Index: (alpha PO7 - alpha PO8) / (alpha PO7 + alpha PO8).

    Band powers are 8-12 Hz. Positive when left (PO7) alpha exceeds right (PO8).
    """
    po7, po8 = _ch(bp, "alpha", "PO7", "PO8")
    return float((po7 - po8) / (po7 + po8 + _EPS))


def posterior_alpha(bp: dict[str, np.ndarray]) -> float:
    """Mean 8-12 Hz power at PO7/Oz/PO8 (uV^2/Hz), for the eyes-closed check."""
    return float(np.mean(_ch(bp, "alpha", *POSTERIOR)))


def indices(bp: dict[str, np.ndarray]) -> dict[str, float]:
    return {"engagement": engagement(bp), "fatigue": fatigue(bp), "alpha_low": alpha_low(bp)}


# --- artifacts ----------------------------------------------------------------


def hf_rms(window_raw: np.ndarray, fs: float = FS) -> float:
    """RMS (uV) of 20-120 Hz content without mains bins, median over channels."""
    x = np.asarray(window_raw, dtype=float)
    f, p = signal.welch(x, fs=fs, nperseg=min(len(x), int(fs)), detrend="linear", axis=0)
    df = f[1] - f[0]
    hi = min(120.0, fs / 2 - 1)
    mask = (f >= 20) & (f <= hi) & (np.abs(f - 50) > 4) & (np.abs(f - 100) > 4)
    rms = np.sqrt(p[mask].sum(axis=0) * df)
    return float(np.median(rms))


def blink_p2p(window_raw: np.ndarray, fs: float = FS) -> tuple[float, float]:
    """Peak-to-peak (uV) of detrended, 10 Hz low-passed Fz and Oz."""
    x = np.asarray(window_raw, dtype=float)[:, [CH["Fz"], CH["Oz"]]]
    x = signal.detrend(x, axis=0, type="linear")
    if len(x) > 30:
        x = signal.sosfiltfilt(_lowpass_sos(float(fs), 10.0), x, axis=0)
    p2p = x.max(axis=0) - x.min(axis=0)
    return float(p2p[0]), float(p2p[1])


def motion_level(gyro: np.ndarray | None) -> float:
    if gyro is None or len(gyro) == 0:
        return 0.0
    mag = np.linalg.norm(np.asarray(gyro, dtype=float), axis=1)
    return float(np.percentile(mag, 95))


def artifact(
    window_raw: np.ndarray,
    gyro: np.ndarray | None = None,
    fs: float = FS,
    hf_ref: float | None = None,
    motion_deg_s: float = MOTION_DEG_S,
    jaw_ratio: float = JAW_RATIO,
    blink_uv: float = BLINK_UV,
) -> str:
    """'motion', 'jaw', 'blink' or 'none' for one raw window (priority order).

    window_raw: (samples, 8) unfiltered EEG in uV (use ~1 s);
    gyro: (samples, 3) deg/s or None; hf_ref: typical HF RMS of this
    person/headset (see ArtifactDetector), default JAW_DEFAULT_REF_UV.
    """
    if motion_level(gyro) > motion_deg_s:
        return "motion"
    ref = JAW_DEFAULT_REF_UV if hf_ref is None else hf_ref
    hf = hf_rms(window_raw, fs)
    if hf > max(jaw_ratio * ref, JAW_MIN_UV):
        return "jaw"
    fz, oz = blink_p2p(window_raw, fs)
    if fz > blink_uv and fz > BLINK_FRONTAL_RATIO * oz:
        return "blink"
    return "none"


@dataclass
class ArtifactDetector:
    """artifact() with a learned 'typical' HF level (running median)."""

    fs: float = FS
    history_len: int = 60
    min_history: int = 5
    motion_deg_s: float = MOTION_DEG_S
    jaw_ratio: float = JAW_RATIO
    blink_uv: float = BLINK_UV
    _hf: deque = field(default_factory=deque)

    @property
    def hf_ref(self) -> float | None:
        if len(self._hf) < self.min_history:
            return None
        return float(np.median(self._hf))

    def __call__(self, window_raw: np.ndarray, gyro: np.ndarray | None = None,
                 learn: bool = True) -> str:
        label = artifact(window_raw, gyro, self.fs, self.hf_ref,
                         self.motion_deg_s, self.jaw_ratio, self.blink_uv)
        if learn and label != "motion":
            # jaw seconds are included on purpose: they are brief, the median
            # ignores them, and this way a noisy headset can't lock us in 'jaw'.
            self._hf.append(hf_rms(window_raw, self.fs))
            while len(self._hf) > self.history_len:
                self._hf.popleft()
        return label


@dataclass
class WindowResult:
    indices: dict[str, float] | None  # None if the window was too short
    artifact: str  # artifact in the newest `artifact_s` seconds
    contaminated: bool  # jaw/motion anywhere in the window, or artifact now
    posterior_alpha: float = float("nan")
    bp: dict[str, np.ndarray] | None = None

    @property
    def ok(self) -> bool:
        return self.indices is not None and not self.contaminated


class Analyzer:
    """One step = indices of the window + artifact flags (pipeline, calibration).

    `artifact` is judged on the newest `artifact_s` seconds. The window is
    also 'contaminated' while a jaw clench or motion is anywhere in it
    (EMG leaks into beta), so z-scores are held up to `window_s` after one.
    """

    def __init__(self, fs: float = FS, artifact_s: float = 1.0,
                 detector: ArtifactDetector | None = None):
        self.fs = fs
        self.artifact_s = artifact_s
        self.detector = detector or ArtifactDetector(fs=fs)

    def step(self, eeg_raw: np.ndarray, gyro: np.ndarray | None = None,
             min_seconds: float = 2.0) -> WindowResult:
        n1 = int(self.artifact_s * self.fs)
        if len(eeg_raw) < max(n1, int(min_seconds * self.fs)):
            return WindowResult(None, "none", True)
        g_new = gyro[-n1:] if gyro is not None and len(gyro) else None
        label = self.detector(eeg_raw[-n1:], g_new)
        contaminated = label != "none"
        if not contaminated:
            for end in range(len(eeg_raw) - n1, n1 - 1, -n1):
                g = gyro[end - n1 : end] if gyro is not None and len(gyro) >= end else None
                if self.detector(eeg_raw[end - n1 : end], g, learn=False) in ("jaw", "motion"):
                    contaminated = True
                    break
        bp = band_powers(eeg_raw, self.fs)
        return WindowResult(indices(bp), label, contaminated, posterior_alpha(bp), bp)
