"""Filters, band powers, indices and artifact flags (P1).

Every 1 s on the last 4 s window:
  notch 50 Hz + bandpass 1-40 Hz, Welch PSD (fs=250, nperseg=500).
"""

from __future__ import annotations

import numpy as np

BANDS = {
    "theta": (4, 8),
    "alpha_low": (8, 10),
    "alpha": (8, 12),
    "beta": (13, 30),
}


def band_powers(window: np.ndarray, fs: int = 250) -> dict[str, np.ndarray]:
    """Mean PSD per band and channel, after filtering."""
    # TODO(P1): scipy.signal iirnotch(50) + butter bandpass(1, 40),
    # welch(window, fs, nperseg=500, axis=0), average bins per band.
    raise NotImplementedError


def engagement(bp: dict[str, np.ndarray]) -> float:
    """beta / (alpha + theta), mean of Fz and Cz (Pope et al. 1995)."""
    raise NotImplementedError


def fatigue(bp: dict[str, np.ndarray]) -> float:
    """(theta + alpha) / beta at Fz. Roughly 1 / engagement."""
    raise NotImplementedError


def alpha_low(bp: dict[str, np.ndarray]) -> float:
    """Lower alpha 8-10 Hz at Pz / Oz (second axis, vigilance)."""
    raise NotImplementedError


def artifact(window_raw: np.ndarray, gyro: np.ndarray | None = None) -> str:
    """'blink' (big low-freq deflection on Fz), 'jaw' (>20 Hz burst on
    all channels, raw signal), 'motion' (gyro magnitude) or 'none'."""
    raise NotImplementedError
