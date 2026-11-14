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

# Block the player stands on, facing north (yaw 180). Feet at GROUND_Y,
# centered on the block. Targets sit on an arc around this spot.
STAND_X, STAND_Z = 0, 0

# Targets appear on an arc in front of the player.
TARGET_DISTANCE = 24  # blocks from where the player stands
TARGET_LIFETIME_S = 6.0
# Mob used as a target. Skeletons burn in daylight, and the rest phase
# sets dawn: a mob that burns to death is logged as a hit. Head armor
# suppresses that. The helmet is unbreakable because a normal one loses
# durability in sunlight and would break during a long rest.
# The bow is explicit: a summon that sets equipment replaces the
# skeleton's default gear, and without a bow it never shoots.
# Tested command shape on 26.3: equipment NBT, summon/exists/kill.
TARGET_ENTITY = os.environ.get("MC_TARGET_ENTITY", "minecraft:skeleton")
TARGET_GEAR_NBT = (
    'equipment:{mainhand:{id:"minecraft:bow",count:1},'
    'head:{id:"minecraft:leather_helmet",count:1,'
    'components:{"minecraft:unbreakable":{}}}},'
    'drop_chances:{mainhand:0.0f,head:0.0f}'
)
# Default skeleton aggro is 16 blocks. Spawns sit at TARGET_DISTANCE,
# so a shorter range means they never draw.
TARGET_FOLLOW_RANGE = TARGET_DISTANCE + 24
TARGET_ATTRIBUTES_NBT = (
    f'attributes:[{{id:"minecraft:follow_range",base:{TARGET_FOLLOW_RANGE}.0d}}]'
)
# Same aggro, but movement speed 0: the scan mark stays put while it shoots.
TARGET_HOLD_ATTRIBUTES_NBT = (
    f'attributes:[{{id:"minecraft:follow_range",base:{TARGET_FOLLOW_RANGE}.0d}},'
    '{id:"minecraft:movement_speed",base:0.0d}]'
)

# Client field of view the eccentricities assume: FOV 70 ("Normal") in
# Options. Minecraft applies that number as the vertical angle. On a 16:9
# screen the horizontal view is about ±51°. Every spawn angle stays at or
# under 45°, a few degrees inside that edge, so the whole mob is on screen
# in the direction the player is looking, without a further turn of the head.
CLIENT_FOV_DEG = 70
ECCENTRICITIES = (15, 25, 35, 45)  # degrees from straight ahead

# Chunks kept loaded with /forceload so targets exist, and can be
# detected, even before the player has looked that way. Block
# coordinates; the margin covers the target hitbox and some slack.
ARENA_MARGIN = 8
ARENA_RADIUS = TARGET_DISTANCE + ARENA_MARGIN
ARENA_MIN_X = STAND_X - ARENA_RADIUS
ARENA_MAX_X = STAND_X + ARENA_RADIUS
ARENA_MIN_Z = STAND_Z - ARENA_RADIUS
ARENA_MAX_Z = STAND_Z + ARENA_RADIUS
