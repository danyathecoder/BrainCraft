"""Where targets go. Absolute coordinates on an arc around the player.

The arc is centered on the direction the player is looking (`facing_yaw`,
Minecraft yaw: 180 or -180 = north). Their left is ecc degrees one way,
their right the other. The default facing is north, which is how a
session starts. Verify on day one by looking north, spawning a target
on the left at 60 degrees, and checking it is on the left.
"""

from __future__ import annotations

import math

from mc import config


def target_position(
    side: str,
    ecc_deg: float,
    distance: float = config.TARGET_DISTANCE,
    origin: tuple[float, float] = (config.STAND_X + 0.5, config.STAND_Z + 0.5),
    ground_y: float = config.GROUND_Y,
    facing_yaw: float = 180.0,
) -> tuple[float, float, float]:
    """Block position of a target at ecc_deg to one side of facing_yaw.

    Minecraft yaw 0 looks south (+Z), 90 west (-X), 180 north (-Z).
    Straight ahead of `facing_yaw` is ecc 0; left is a smaller yaw.
    """
    if side not in ("L", "R"):
        raise ValueError(f"side must be 'L' or 'R', got {side!r}")
    bearing = facing_yaw - ecc_deg if side == "L" else facing_yaw + ecc_deg
    theta = math.radians(bearing)
    x0, z0 = origin
    x = x0 - distance * math.sin(theta)
    z = z0 + distance * math.cos(theta)
    return x, ground_y, z
