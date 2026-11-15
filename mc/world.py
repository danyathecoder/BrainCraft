"""One-off world setup: chunks, rules, night, ground, player. Run once per session.

    python -m mc.world [--player NAME]

Safe to run again. Works without a player online (player steps are
skipped with a message; run it again after joining, or let the game
loop place the player).
"""

from __future__ import annotations

import argparse
import math
import sys

from mc import config
from mc.rcon import Rcon, RconError, looks_like_error

# Canonical (pre-1.21.11 camelCase) name -> value. Kept for reference
# and backward compatibility; GAMERULE_NAMES lists the names to try.
GAMERULES = {
    "doDaylightCycle": "false",
    "doMobSpawning": "false",
    "doWeatherCycle": "false",
    "doImmediateRespawn": "true",
    "doMobLoot": "false",
    "mobGriefing": "false",
    "showDeathMessages": "false",
    "announceAdvancements": "false",
    "keepInventory": "true",
    "doInsomnia": "false",
    "doPatrolSpawning": "false",
    "doTraderSpawning": "false",
    "spawn_monsters": "false",  # 26.x only; older servers report it as unknown
}

# Names to try in order. Minecraft 1.21.11 / 26.x renamed game rules to
# snake_case (camelCase fails with "Incorrect argument ... <--[HERE]");
# older servers only know the camelCase names. naturalRegeneration is
# left at its default.
GAMERULE_NAMES = {
    "doDaylightCycle": ("advance_time", "doDaylightCycle"),
    "doMobSpawning": ("spawn_mobs", "doMobSpawning"),
    "doWeatherCycle": ("advance_weather", "doWeatherCycle"),
    "doImmediateRespawn": ("immediate_respawn", "doImmediateRespawn"),
    "doMobLoot": ("mob_drops", "doMobLoot"),
    "mobGriefing": ("mob_griefing", "mobGriefing"),
    "showDeathMessages": ("show_death_messages", "showDeathMessages"),
    "announceAdvancements": ("show_advancement_messages", "announceAdvancements"),
    "keepInventory": ("keep_inventory", "keepInventory"),
    "doInsomnia": ("spawn_phantoms", "doInsomnia"),
    "doPatrolSpawning": ("spawn_patrols", "doPatrolSpawning"),
    "doTraderSpawning": ("spawn_wandering_traders", "doTraderSpawning"),
    "spawn_monsters": ("spawn_monsters",),
}

NO_PLAYER = "no player was found"


def _say(msg: str) -> None:
    print(msg, flush=True)


def set_gamerule(rcon: Rcon, canonical: str, value: str) -> str | None:
    """Set a game rule, trying each known name. Returns the name that worked."""
    names = GAMERULE_NAMES.get(canonical, (canonical,))
    last = ""
    for name in names:
        reply = rcon.cmd(f"gamerule {name} {value}")
        if not looks_like_error(reply):
            _say(f"  gamerule {name} = {value}  ({reply.strip() or 'ok'})")
            return name
        last = reply
    _say(f"  gamerule {canonical}: no known name accepted ({', '.join(names)}); last reply: {last.strip()}")
    return None


def forceload_arena(rcon: Rcon) -> str:
    """Keep the arena chunks loaded so targets exist with nobody online.

    Without this, summon/execute fail with "That position is not loaded"
    until a player is nearby.
    """
    reply = rcon.cmd(
        f"forceload add {config.ARENA_MIN_X} {config.ARENA_MIN_Z} {config.ARENA_MAX_X} {config.ARENA_MAX_Z}"
    )
    # "No chunks were marked for force loading" = already loaded, fine.
    _say(f"  forceload: {reply.strip()}")
    return reply


def forceload_active(rcon: Rcon) -> bool:
    """True if the chunk the player stands in is force loaded."""
    reply = rcon.cmd(f"forceload query {config.STAND_X} {config.STAND_Z}").lower()
    return "is marked" in reply or "are marked" in reply


# The stone tower this used to build: a 5×5 column, 6 blocks tall, a rim
# on top, and 3 blocks of headroom. Setup replaces that volume with air
# so an existing world loses the tower on the next run.
_OLD_TOWER_ABOVE = 9


def clear_tower_cmd() -> str:
    """Air where the old tower stood. Does not dig the grass under the player's feet."""
    x, z, y = config.STAND_X, config.STAND_Z, config.GROUND_Y
    return f"fill {x - 2} {y} {z - 2} {x + 2} {y + _OLD_TOWER_ABOVE} {z + 2} minecraft:air"


def player_anchor_cmd(player: str) -> str:
    """Feet on the grass, facing north, looking level."""
    return f"tp {player} {config.STAND_X + 0.5} {config.GROUND_Y} {config.STAND_Z + 0.5} 180 0"


# Vanilla player walking speed. Movement speed also scales the client's
# field of view, by (speed / 0.1 + 1) / 2 (the reason Slowness narrows
# the view). Speed 0 halved the FOV, so on a 16:9 screen only about
# +-29 deg was visible and 35/45 deg targets spawned off screen. The speed
# stays at the default; the leash below keeps the player on the spot.
PLAYER_WALK_SPEED = 0.1
LEASH_BLOCKS = 0.3  # farther than this from the standing spot -> back


def lock_movement_cmds(player: str) -> list[str]:
    """Jumping and arrow knockback off, view untouched.

    Also resets a zero movement speed left in the player data by older
    versions, which would still narrow the view. Walking is undone by
    `leash_cmd` (the game loop checks the position 5 times a second).
    """
    return [
        f"attribute {player} minecraft:movement_speed base set {PLAYER_WALK_SPEED}",
        f"attribute {player} minecraft:jump_strength base set 0",
        f"attribute {player} minecraft:knockback_resistance base set 1",
    ]


def leash_cmd(player: str) -> str:
    """Back to the standing spot. No rotation given, so the view stays."""
    return f"tp {player} {config.STAND_X + 0.5} {config.GROUND_Y} {config.STAND_Z + 0.5}"


def off_spot(pos: tuple[float, float, float] | None) -> bool:
    """True if a player position has drifted off the standing spot."""
    if pos is None:
        return False
    x, y, z = pos
    dx, dz = x - (config.STAND_X + 0.5), z - (config.STAND_Z + 0.5)
    return math.hypot(dx, dz) > LEASH_BLOCKS or abs(y - config.GROUND_Y) > 1.0


def setup(rcon: Rcon, player: str = config.PLAYER) -> None:
    _say("Setting up the BrainCraft world...")
    forceload_arena(rcon)
    for rule, value in GAMERULES.items():
        set_gamerule(rcon, rule, value)
    for c in ("time set midnight", "weather clear", "difficulty normal"):  # peaceful despawns zombies
        _say(f"  {c}: {rcon.cmd(c).strip()}")
    rcon.cmd("kill @e[tag=target]")  # leftovers from a previous session
    reply = rcon.cmd(clear_tower_cmd())
    _say(f"  ground: {reply.strip()}")
    rcon.cmd(f"setworldspawn {config.STAND_X} {config.GROUND_Y} {config.STAND_Z}")
    place_player(rcon, player)


def place_player(rcon: Rcon, player: str = config.PLAYER) -> bool:
    """Put the player on the grass facing north, ready to shoot.

    Looking around stays free. Jumping and knockback are off and the game
    loop leashes the player to this spot, so the spawn angles stay exact. Returns False (and does nothing) if
    no matching player is online.
    """
    if not rcon.player_online(player):
        _say(f"  player {player}: nobody online, skipping tp/gamemode/bow. Join, then run again.")
        return False
    x, z, y = config.STAND_X, config.STAND_Z, config.GROUND_Y
    p = player
    steps = [
        f"gamemode adventure {p}",
        player_anchor_cmd(p),
        f"spawnpoint {p} {x} {y} {z}",
        f"clear {p}",
        f"give {p} minecraft:bow",
        f"give {p} minecraft:arrow 192",
        f"effect clear {p} minecraft:saturation",  # it healed too fast to ever die
        *lock_movement_cmds(p),
    ]
    for c in steps:
        reply = rcon.cmd(c)
        if NO_PLAYER in reply.lower():
            _say(f"  player {p}: not found, skipping the rest ({c!r}).")
            return False
        _say(f"  {c}: {reply.strip()}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Set up the BrainCraft world over RCON.")
    ap.add_argument("--player", default=config.PLAYER, help="player name or selector (default %(default)s)")
    args = ap.parse_args(argv)
    try:
        with Rcon() as r:
            setup(r, args.player)
    except RconError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("world ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
