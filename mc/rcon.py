"""Thin RCON wrapper: every game change is a plain Minecraft command.

Command syntax is for Java 1.20+; check it against the server version.
"""

from __future__ import annotations

import re
import threading

from mcrcon import MCRcon

from mc import config


class Rcon:
    def __init__(
        self,
        host: str = config.RCON_HOST,
        password: str = config.RCON_PASSWORD,
        port: int = config.RCON_PORT,
    ):
        self._conn = MCRcon(host, password, port=port)
        self._lock = threading.Lock()  # one command at a time on the socket

    def __enter__(self) -> "Rcon":
        self._conn.connect()
        return self

    def __exit__(self, *exc) -> None:
        self._conn.disconnect()

    def cmd(self, command: str) -> str:
        with self._lock:
            return self._conn.command(command)

    # --- targets -------------------------------------------------------

    def summon_target(
        self, target_id: str, x: float, y: float, z: float, side: str, glowing: bool = False
    ) -> str:
        """Still zombie that dies from one arrow, tagged for tracking."""
        nbt = (
            "{NoAI:1b,Silent:1b,PersistenceRequired:1b,IsBaby:0b,Health:1f,"
            f'Glowing:{int(glowing)}b,Tags:["target","{target_id}","{side}"]}}'
        )
        return self.cmd(f"summon minecraft:zombie {x:.1f} {y:.1f} {z:.1f} {nbt}")

    def exists(self, tag: str) -> bool:
        """True while an entity with this tag is alive."""
        return "passed" in self.cmd(f"execute if entity @e[tag={tag}]").lower()

    def kill(self, tag: str) -> str:
        return self.cmd(f"kill @e[tag={tag}]")

    # --- feedback and effects -----------------------------------------

    def title(self, text: str, player: str = config.PLAYER) -> str:
        return self.cmd(f'title {player} title {{"text":"{text}"}}')

    def playsound(self, sound: str, x: float, y: float, z: float, player: str = config.PLAYER) -> str:
        """Positional sound, e.g. a cue from the target's side."""
        return self.cmd(f"playsound {sound} master {player} {x:.1f} {y:.1f} {z:.1f} 1 1")

    # --- player --------------------------------------------------------

    def yaw(self, player: str = config.PLAYER) -> float | None:
        """Player's horizontal look direction in degrees (180 = north)."""
        reply = self.cmd(f"data get entity {player} Rotation")
        match = re.search(r"\[(-?[\d.]+)f", reply)
        return float(match.group(1)) if match else None
