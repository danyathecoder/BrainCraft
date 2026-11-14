"""Channel layout of the Unicorn Hybrid Black LSL stream.

The stream carries more than EEG. Layout, 0-based column indices (the
g.Pype bridge publishes exactly this, checked against g.Pype 4.2.0's
HybridBlack; for the Unicorn LSL app it is assumed, unverified):

    0-7    EEG  Fz, C3, Cz, C4, Pz, PO7, Oz, PO8   (microvolts)
    8-10   accelerometer x, y, z                   (g)
    11-13  gyroscope x, y, z                       (deg/s)
    14     battery level
    15     counter
    16     validation indicator
    17     g.Pype arrival stamp (bridge only; ignored)

If the real stream differs, override the columns without touching code:

    BRAINCRAFT_EEG_COLS="0-7"         (or "0,1,2,3,4,5,6,7")
    BRAINCRAFT_GYRO_COLS="11-13"      (or "none" to disable motion checks)

or pass eeg_cols= / gyro_cols= to EEGStream.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

FS = 250  # Hz
EEG_CHANNELS = ["Fz", "C3", "Cz", "C4", "Pz", "PO7", "Oz", "PO8"]
CH = {name: i for i, name in enumerate(EEG_CHANNELS)}

UNICORN_CHANNELS = EEG_CHANNELS + [
    "AccX", "AccY", "AccZ",
    "GyroX", "GyroY", "GyroZ",
    "Battery", "Counter", "Validation",
]  # fmt: skip
N_UNICORN = len(UNICORN_CHANNELS)  # 17
DEFAULT_EEG_COLS = tuple(range(8))
DEFAULT_ACC_COLS = (8, 9, 10)
DEFAULT_GYRO_COLS = (11, 12, 13)
BATTERY_COL, COUNTER_COL, VALIDATION_COL = 14, 15, 16


def parse_cols(text: str | None) -> tuple[int, ...] | None:
    """'0-7' / '0,1,2' / '11-13' / 'none' -> tuple of ints (None for 'none')."""
    if text is None:
        return None
    text = text.strip().lower()
    if text in ("", "none", "off", "-"):
        return None
    cols: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            cols.extend(range(int(lo), int(hi) + 1))
        elif part:
            cols.append(int(part))
    return tuple(cols)


_UNSET = object()


@dataclass(frozen=True)
class Layout:
    """Which stream columns are EEG and which are gyroscope."""

    eeg: tuple[int, ...] = DEFAULT_EEG_COLS
    gyro: tuple[int, ...] | None = DEFAULT_GYRO_COLS

    @classmethod
    def for_stream(
        cls,
        n_channels: int,
        eeg_cols: Sequence[int] | None = None,
        gyro_cols: Sequence[int] | None | object = _UNSET,
    ) -> Layout:
        """Explicit args > env vars > defaults (gyro only if >= 14 channels)."""
        if eeg_cols is None:
            env = os.environ.get("BRAINCRAFT_EEG_COLS")
            eeg_cols = parse_cols(env) if env else DEFAULT_EEG_COLS
        if gyro_cols is _UNSET:
            env = os.environ.get("BRAINCRAFT_GYRO_COLS")
            if env is not None:
                gyro_cols = parse_cols(env)
            else:
                gyro_cols = DEFAULT_GYRO_COLS if n_channels >= 14 else None
        eeg = tuple(int(c) for c in eeg_cols)  # type: ignore[union-attr]
        gyro = None if gyro_cols is None else tuple(int(c) for c in gyro_cols)  # type: ignore[union-attr]
        if len(eeg) != len(EEG_CHANNELS):
            raise ValueError(f"need {len(EEG_CHANNELS)} EEG columns, got {eeg}")
        used = eeg + (gyro or ())
        if max(used) >= n_channels or min(used) < 0:
            raise ValueError(
                f"stream has {n_channels} channels but layout uses columns {used}; "
                "set BRAINCRAFT_EEG_COLS / BRAINCRAFT_GYRO_COLS"
            )
        return cls(eeg=eeg, gyro=gyro)
