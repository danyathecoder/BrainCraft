"""LSL inlet + ring buffer for the Unicorn Hybrid Black (P1).

Unicorn Suite -> "Unicorn LSL" app -> this module. The stream carries
more than EEG (accelerometer, gyroscope, ...): check which 8 channels
are EEG with list_streams() and a first raw plot.
"""

from __future__ import annotations

import numpy as np

FS = 250  # Hz
EEG_CHANNELS = ["Fz", "C3", "Cz", "C4", "Pz", "PO7", "Oz", "PO8"]


def list_streams() -> None:
    """Print every LSL stream on the network (step 1: read any signal)."""
    from pylsl import resolve_streams

    for info in resolve_streams():
        print(info.name(), info.type(), info.channel_count(), info.nominal_srate())


class EEGStream:
    """Keeps the last `seconds` of EEG in a ring buffer."""

    def __init__(self, name: str | None = None, seconds: float = 10.0):
        self.buffer = np.zeros((int(seconds * FS), len(EEG_CHANNELS)))
        # TODO(P1): resolve the stream (by name/type), open a StreamInlet,
        # pull chunks in a thread, keep only the EEG columns, fill buffer.
        raise NotImplementedError

    def window(self, seconds: float = 4.0) -> np.ndarray:
        """Last `seconds` of EEG, shape (samples, 8), microvolts."""
        raise NotImplementedError
