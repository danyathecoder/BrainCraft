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

Windows are analysed in 1 s segments (SEGMENT_S): each segment gets an
artifact label, and band powers come from the clean segments only, so
one blink or jaw clench no longer throws away the whole 4 s window. A
window is usable with >= MIN_CLEAN_SEGMENTS clean segments. Channels
that are saturated, flat or noisy in the clean segments are left out of
the indices (within each index's own channel group).

Artifacts (checked on the RAW, unfiltered signal, per 1 s segment):
  contact a key channel (Fz, Cz, Pz, Oz) at the amplifier limit
          (|raw| >= SATURATION_UV: electrode off, reference loose)
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
SATURATION_UV = 740_000.0  # Unicorn input range is +-750 mV
FLAT_UV, NOISY_UV = 0.5, 100.0  # channel RMS after 1-40 Hz filtering
SEGMENT_S = 1.0
MIN_CLEAN_SEGMENTS = 2
KEY_CHANNELS = ("Fz", "Cz", "Pz", "Oz")
# Channel groups of each index; bad channels are dropped within the group,
# and if none is left the (nearby) fallback group is used instead.
INDEX_CHANNELS = {"engagement": ("Fz", "Cz"), "fatigue": ("Fz",), "alpha_low": ("Pz", "Oz")}
INDEX_FALLBACK = {"engagement": ("C3", "C4"), "fatigue": ("Cz", "C3", "C4"),
                  "alpha_low": ("PO7", "PO8")}
ANALYZER_VERSION = "segments-1s"  # stored in baselines; recalibrate on change
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


def engagement(bp: dict[str, np.ndarray], chans: tuple[str, ...] = ("Fz", "Cz")) -> float:
    """log10 beta / (alpha + theta), mean of Fz and Cz (Pope et al. 1995)."""
    ratio = _ch(bp, "beta", *chans) / (_ch(bp, "alpha", *chans) + _ch(bp, "theta", *chans) + _EPS)
    return float(np.mean(np.log10(ratio + _EPS)))


def fatigue(bp: dict[str, np.ndarray], chans: tuple[str, ...] = ("Fz",)) -> float:
    """log10 (theta + alpha) / beta at Fz. Roughly -engagement."""
    ratio = (_ch(bp, "theta", *chans) + _ch(bp, "alpha", *chans)) / (_ch(bp, "beta", *chans) + _EPS)
    return float(np.mean(np.log10(ratio + _EPS)))


def alpha_low(bp: dict[str, np.ndarray], chans: tuple[str, ...] = ("Pz", "Oz")) -> float:
    """log10 lower alpha 8-10 Hz power, mean of Pz and Oz (vigilance axis)."""
    return float(np.log10(np.mean(_ch(bp, "alpha_low", *chans)) + _EPS))


def ali(bp: dict[str, np.ndarray]) -> float:
    """Alpha Lateralization Index: (alpha PO7 - alpha PO8) / (alpha PO7 + alpha PO8).

    Band powers are 8-12 Hz. Positive when left (PO7) alpha exceeds right (PO8).
    """
    po7, po8 = _ch(bp, "alpha", "PO7", "PO8")
    return float((po7 - po8) / (po7 + po8 + _EPS))


def posterior_alpha(bp: dict[str, np.ndarray], bad: frozenset[str] | set[str] = frozenset()) -> float:
    """Mean 8-12 Hz power at PO7/Oz/PO8 (uV^2/Hz), for the eyes-closed check."""
    chans = [c for c in POSTERIOR if c not in bad] or list(POSTERIOR)
    return float(np.mean(_ch(bp, "alpha", *chans)))


def indices(bp: dict[str, np.ndarray],
            bad: frozenset[str] | set[str] = frozenset()) -> dict[str, float] | None:
    """The three indices; channels in `bad` are left out of their group.

    A group with no good channel left falls back to INDEX_FALLBACK; None if
    that has no good channel either.
    """
    groups = {}
    for k, chans in INDEX_CHANNELS.items():
        groups[k] = (tuple(c for c in chans if c not in bad)
                     or tuple(c for c in INDEX_FALLBACK[k] if c not in bad))
    if not all(groups.values()):
        return None
    return {"engagement": engagement(bp, groups["engagement"]),
            "fatigue": fatigue(bp, groups["fatigue"]),
            "alpha_low": alpha_low(bp, groups["alpha_low"])}


def band_powers_segments(filtered: np.ndarray, segments: list[slice],
                         fs: float = FS) -> dict[str, np.ndarray]:
    """Band powers averaged over the given segments of a filtered window.

    One periodogram (Hann, 1 Hz resolution for 1 s) per segment, averaged.
    """
    ps, f = [], None
    for sl in segments:
        x = filtered[sl]
        f, p = signal.welch(x, fs=fs, nperseg=len(x), axis=0)
        ps.append(p)
    p = np.mean(ps, axis=0)
    out = {}
    for name, (lo, hi) in BANDS.items():
        mask = (f >= lo) & (f < hi)
        out[name] = p[mask].mean(axis=0)
    return out


def channel_status(raw: np.ndarray, filtered: np.ndarray) -> list[str]:
    """Per channel: 'saturated' (amplifier limit), 'flat', 'noisy' or 'ok'.

    raw: unfiltered uV (for saturation); filtered: 1-40 Hz uV (for RMS).
    """
    raw = np.asarray(raw, dtype=float)
    sat = np.abs(raw).max(axis=0) >= SATURATION_UV if len(raw) else np.zeros(raw.shape[1], bool)
    rms = np.sqrt((np.asarray(filtered, dtype=float) ** 2).mean(axis=0)) if len(filtered) else None
    out = []
    for i in range(raw.shape[1]):
        if sat[i]:
            out.append("saturated")
        elif rms is not None and rms[i] < FLAT_UV:
            out.append("flat")
        elif rms is not None and rms[i] > NOISY_UV:
            out.append("noisy")
        else:
            out.append("ok")
    return out


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
    """'contact', 'motion', 'jaw', 'blink' or 'none' for one raw window (priority order).

    window_raw: (samples, 8) unfiltered EEG in uV (use ~1 s);
    gyro: (samples, 3) deg/s or None; hf_ref: typical HF RMS of this
    person/headset (see ArtifactDetector), default JAW_DEFAULT_REF_UV.
    """
    raw = np.asarray(window_raw, dtype=float)
    if len(raw) and (np.abs(raw[:, [CH[c] for c in KEY_CHANNELS]]) >= SATURATION_UV).any():
        return "contact"
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
    model: object | None = None  # eeg.artifact_model.ArtifactModel; None = thresholds
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
        if self.model is not None and label != "contact":  # contact stays a hard rule
            label = self.model.predict_label(window_raw, gyro, self.hf_ref, self.fs)
        if learn and label not in ("motion", "contact"):
            # jaw seconds are included on purpose: they are brief, the median
            # ignores them, and this way a noisy headset can't lock us in 'jaw'.
            self._hf.append(hf_rms(window_raw, self.fs))
            while len(self._hf) > self.history_len:
                self._hf.popleft()
        return label


@dataclass
class WindowResult:
    indices: dict[str, float] | None  # None if too short or nothing usable
    artifact: str  # artifact in the newest `artifact_s` seconds
    contaminated: bool  # too few clean segments (legacy: jaw/motion anywhere)
    posterior_alpha: float = float("nan")
    bp: dict[str, np.ndarray] | None = None
    segment_labels: list[str] = field(default_factory=list)  # oldest first
    channels: list[str] = field(default_factory=list)  # channel_status()

    @property
    def ok(self) -> bool:
        return self.indices is not None and not self.contaminated

    @property
    def clean_segments(self) -> int:
        return sum(1 for s in self.segment_labels if s == "none")


class Analyzer:
    """One step = indices of the window + artifact flags (pipeline, calibration).

    The window is cut into `artifact_s` segments, each labelled by the
    detector. Band powers come from the clean segments; the window is
    usable with >= `min_clean` of them. `artifact` is the newest
    segment's label (HUD, jaw flare). `legacy=True` restores the old
    rule: the whole window is held while a jaw/motion second is in it.
    """

    def __init__(self, fs: float = FS, artifact_s: float = SEGMENT_S,
                 detector: ArtifactDetector | None = None,
                 min_clean: int = MIN_CLEAN_SEGMENTS, legacy: bool = False):
        self.fs = fs
        self.artifact_s = artifact_s
        self.detector = detector or ArtifactDetector(fs=fs)
        self.min_clean = min_clean
        self.legacy = legacy

    def step(self, eeg_raw: np.ndarray, gyro: np.ndarray | None = None,
             min_seconds: float = 2.0) -> WindowResult:
        if self.legacy:
            return self._step_legacy(eeg_raw, gyro, min_seconds)
        fs, n1 = self.fs, int(self.artifact_s * self.fs)
        eeg_raw = np.asarray(eeg_raw, dtype=float)
        if len(eeg_raw) < max(n1, int(min_seconds * fs)):
            return WindowResult(None, "none", True)
        nseg = len(eeg_raw) // n1
        eeg_raw = eeg_raw[len(eeg_raw) - nseg * n1:]
        has_gyro = gyro is not None and len(gyro) >= len(eeg_raw)
        if has_gyro:
            gyro = np.asarray(gyro)[len(gyro) - len(eeg_raw):]
        segs = [slice(k * n1, (k + 1) * n1) for k in range(nseg)]
        labels = [self.detector(eeg_raw[s], gyro[s] if has_gyro else None,
                                learn=(k == nseg - 1)) for k, s in enumerate(segs)]
        filt = preprocess(eeg_raw, fs)
        clean = [s for s, lab in zip(segs, labels) if lab == "none"]
        use = clean if clean else segs
        status = channel_status(np.concatenate([eeg_raw[s] for s in use]),
                                np.concatenate([filt[s] for s in use]))
        bad = {name for name, st in zip(CH, status) if st != "ok"}
        bp = band_powers_segments(filt, use, fs)
        idx = indices(bp, bad) if len(clean) >= self.min_clean else None
        return WindowResult(idx, labels[-1], idx is None, posterior_alpha(bp, bad), bp,
                            labels, status)

    def _step_legacy(self, eeg_raw: np.ndarray, gyro: np.ndarray | None,
                     min_seconds: float) -> WindowResult:
        n1 = int(self.artifact_s * self.fs)
        if len(eeg_raw) < max(n1, int(min_seconds * self.fs)):
            return WindowResult(None, "none", True)
        g_new = gyro[-n1:] if gyro is not None and len(gyro) else None
        label = self.detector(eeg_raw[-n1:], g_new)
        contaminated = label != "none"
        if not contaminated:
            for end in range(len(eeg_raw) - n1, n1 - 1, -n1):
                g = gyro[end - n1 : end] if gyro is not None and len(gyro) >= end else None
                if self.detector(eeg_raw[end - n1 : end], g, learn=False) in ("jaw", "motion", "contact"):
                    contaminated = True
                    break
        bp = band_powers(eeg_raw, self.fs)
        return WindowResult(indices(bp), label, contaminated, posterior_alpha(bp), bp)
