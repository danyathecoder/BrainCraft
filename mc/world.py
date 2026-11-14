"""One-off world setup: rules, night, tower. Run once per session.

    python -m mc.world
"""

from __future__ import annotations

from mc import config
from mc.rcon import Rcon

GAMERULES = {
    "doDaylightCycle": "false",
    "doMobSpawning": "false",
    "doWeatherCycle": "false",
    "doImmediateRespawn": "true",
}


def setup(rcon: Rcon) -> None:
    for rule, value in GAMERULES.items():
        rcon.cmd(f"gamerule {rule} {value}")
    rcon.cmd("time set midnight")
    rcon.cmd("weather clear")
    rcon.cmd("difficulty easy")  # peaceful would despawn the zombie targets
    build_tower(rcon)
    place_player(rcon)


def build_tower(rcon: Rcon) -> None:
    x, z, y0 = config.TOWER_X, config.TOWER_Z, config.GROUND_Y
    top = y0 + config.TOWER_HEIGHT
    rcon.cmd(f"fill {x - 2} {y0} {z - 2} {x + 2} {top - 1} {z + 2} minecraft:stone_bricks")
    rcon.cmd(f"fill {x - 2} {top} {z - 2} {x + 2} {top} {z + 2} minecraft:oak_fence hollow")
    # TODO(P3): walls to defend, lighting, nicer map


def place_player(rcon: Rcon) -> None:
    """Put the player on the tower facing north, ready to shoot."""
    x, z = config.TOWER_X, config.TOWER_Z
    top = config.GROUND_Y + config.TOWER_HEIGHT
    p = config.PLAYER
    rcon.cmd(f"gamemode adventure {p}")
    rcon.cmd(f"tp {p} {x + 0.5} {top} {z + 0.5} 180 0")
    rcon.cmd(f"clear {p}")
    rcon.cmd(f"give {p} minecraft:bow")
    rcon.cmd(f"give {p} minecraft:arrow 64")


if __name__ == "__main__":
    with Rcon() as r:
        setup(r)
    print("world ready")
