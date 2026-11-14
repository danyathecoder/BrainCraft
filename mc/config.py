"""Minecraft connection and world layout. Adjust on the demo laptop."""

import os

# Target game version: server.jar and client must match exactly.
MC_VERSION = os.environ.get("MC_VERSION", "26.3")

RCON_HOST = os.environ.get("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.environ.get("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "braincraft")
RCON_TIMEOUT_S = float(os.environ.get("RCON_TIMEOUT_S", "5"))

PLAYER = os.environ.get("MC_PLAYER", "@p")

# Superflat ground surface. The default superflat preset is bedrock
# (y=-64), 2 dirt, grass (top face at y=-61), so the first air block,
# where a player's feet are, is y = -60. Check with F3 on day one
# ("XYZ" line while standing on the grass) and update if it differs.
GROUND_Y = -60

# Tower top where the player stands, facing north (yaw 180).
TOWER_X, TOWER_Z = 0, 0
TOWER_HEIGHT = 6

# Targets appear on an arc in front of the tower.
TARGET_DISTANCE = 24  # blocks from the tower
ECCENTRICITIES = (15, 35, 55, 75)  # degrees from straight ahead
TARGET_LIFETIME_S = 6.0
# Mob used as a target. Husks look like zombies but don't burn in
# daylight; the rest phase sets dawn, and a burning zombie would be
# logged as a hit. Tested on 26.3: summon/exists/kill work for husks.
TARGET_ENTITY = os.environ.get("MC_TARGET_ENTITY", "minecraft:husk")

# Client field of view the eccentricities assume: FOV 70 ("Normal") in
# Options. Wider FOV shows more periphery and makes the task easier.
CLIENT_FOV_DEG = 70

# Chunks kept loaded with /forceload so targets exist, and can be
# detected, even before the player has looked that way. Block
# coordinates; the margin covers the target hitbox and some slack.
ARENA_MARGIN = 8
ARENA_RADIUS = TARGET_DISTANCE + ARENA_MARGIN
ARENA_MIN_X = TOWER_X - ARENA_RADIUS
ARENA_MAX_X = TOWER_X + ARENA_RADIUS
ARENA_MIN_Z = TOWER_Z - ARENA_RADIUS
ARENA_MAX_Z = TOWER_Z + ARENA_RADIUS
