"""Minecraft connection and world layout. Adjust on the demo laptop."""

import os

RCON_HOST = os.environ.get("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.environ.get("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "braincraft")

PLAYER = os.environ.get("MC_PLAYER", "@p")

# Superflat ground: players stand at y = -60 in recent versions.
# Check with F3 on day one and update GROUND_Y if it differs.
GROUND_Y = -60

# Tower top where the player stands, facing north (yaw 180).
TOWER_X, TOWER_Z = 0, 0
TOWER_HEIGHT = 6

# Targets appear on an arc in front of the tower.
TARGET_DISTANCE = 24  # blocks from the tower
ECCENTRICITIES = (15, 35, 55, 75)  # degrees from straight ahead
TARGET_LIFETIME_S = 6.0
