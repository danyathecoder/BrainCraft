"""Per-person baseline -> z-scores (P1).

Protocol (2.5 min):
  60 s eyes open, rest, looking at the screen
  30 s eyes closed (sanity check: alpha at Oz/PO7/PO8 must jump)
  60 s easy game
Baseline = mean/std of each index over the eyes-open + easy-game parts.
"""

from __future__ import annotations

from dataclasses import dataclass

EMA_SECONDS = 15.0  # smoothing of z-scores


@dataclass
class Baseline:
    mean: dict[str, float]
    std: dict[str, float]

    def z(self, name: str, value: float) -> float:
        return (value - self.mean[name]) / (self.std[name] or 1.0)


def run_calibration(stream) -> Baseline:
    # TODO(P1): guide the player through the protocol (titles in-game or
    # on the HUD), collect index values per second, compute mean/std.
    raise NotImplementedError
