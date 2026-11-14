"""One-off world setup: chunks, rules, night, tower, player. Run once per session.

    python -m mc.world [--player NAME]

Safe to run again. Works without a player online (player steps are
skipped with a message; run it again after joining, or let the game
loop place the player).
"""

from __future__ import annotations

import argparse
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
    """True if the tower chunk is force loaded."""
    reply = rcon.cmd(f"forceload query {config.TOWER_X} {config.TOWER_Z}").lower()
    return "is marked" in reply or "are marked" in reply


def setup(rcon: Rcon, player: str = config.PLAYER) -> None:
    _say("Setting up the BrainCraft world...")
    forceload_arena(rcon)
    for rule, value in GAMERULES.items():
        set_gamerule(rcon, rule, value)
    for c in ("time set midnight", "weather clear", "difficulty easy"):  # peaceful despawns zombies
        _say(f"  {c}: {rcon.cmd(c).strip()}")
    rcon.cmd("kill @e[tag=target]")  # leftovers from a previous session
    build_tower(rcon)
    top = config.GROUND_Y + config.TOWER_HEIGHT
    rcon.cmd(f"setworldspawn {config.TOWER_X} {top} {config.TOWER_Z}")
    place_player(rcon, player)


def build_tower(rcon: Rcon) -> None:
    """5x5 stone tower; on top a 1-block-high rim around the 1x1 standing spot.

    The rim keeps the player in place (so eccentricities are exact) and
    is low enough that arrows aimed down at the targets clear it (a fence
    is 1.5 blocks tall and would catch them).
    """
    x, z, y0 = config.TOWER_X, config.TOWER_Z, config.GROUND_Y
    top = y0 + config.TOWER_HEIGHT
    # Clear the space above the tower (old railings, previous designs).
    rcon.cmd(f"fill {x - 2} {top} {z - 2} {x + 2} {top + 3} {z + 2} minecraft:air")
    reply = rcon.cmd(f"fill {x - 2} {y0} {z - 2} {x + 2} {top - 1} {z + 2} minecraft:stone_bricks")
    rcon.cmd(f"fill {x - 1} {top} {z - 1} {x + 1} {top} {z + 1} minecraft:stone_bricks")
    rcon.cmd(f"setblock {x} {top} {z} minecraft:air")
    _say(f"  tower: {reply.strip()}")
    # TODO(P3): walls to defend, lighting, nicer map


def place_player(rcon: Rcon, player: str = config.PLAYER) -> bool:
    """Put the player on the tower facing north, ready to shoot.

    Returns False (and does nothing) if no matching player is online.
    """
    if not rcon.player_online(player):
        _say(f"  player {player}: nobody online, skipping tp/gamemode/bow. Join, then run again.")
        return False
    x, z = config.TOWER_X, config.TOWER_Z
    top = config.GROUND_Y + config.TOWER_HEIGHT
    p = player
    steps = [
        f"gamemode adventure {p}",
        f"tp {p} {x + 0.5} {top} {z + 0.5} 180 0",
        f"spawnpoint {p} {x} {top} {z}",
        f"clear {p}",
        f"give {p} minecraft:bow",
        f"give {p} minecraft:arrow 192",
        f"effect give {p} minecraft:saturation infinite 0 true",
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
