"""Where targets go. Absolute coordinates, player faces north (-Z).

Facing north, the player's left is -X and right is +X. Verify on day
one by spawning a target on the left at 60 degrees and looking at it.
"""

from __future__ import annotations

import math

from mc import config


def target_position(
    side: str,
    ecc_deg: float,
    distance: float = config.TARGET_DISTANCE,
    origin: tuple[float, float] = (config.TOWER_X + 0.5, config.TOWER_Z + 0.5),
    ground_y: float = config.GROUND_Y,
) -> tuple[float, float, float]:
    """Block position of a target at ecc_deg to the given side."""
    if side not in ("L", "R"):
        raise ValueError(f"side must be 'L' or 'R', got {side!r}")
    sign = -1 if side == "L" else 1
    theta = math.radians(ecc_deg)
    x0, z0 = origin
    x = x0 + sign * distance * math.sin(theta)
    z = z0 - distance * math.cos(theta)
    return x, ground_y, z
