"""Thin RCON wrapper: every game change is a plain Minecraft command.

Command syntax checked for Java 1.20.5+ / 26.x. The client speaks the
RCON protocol directly over a socket (stdlib only) so that it has real
socket timeouts on every platform, notices when the server goes away,
and reconnects once instead of hanging.

    with Rcon() as r:
        print(r.cmd("list"))
"""

from __future__ import annotations

import json
import re
import select
import socket
import struct
import threading

from mc import config

# Packet types (Source RCON protocol, as implemented by Minecraft).
_LOGIN = 3
_COMMAND = 2
_MAX_PACKET = 1 << 20  # sanity bound; Minecraft sends at most ~4 KiB chunks
_CHUNK_CHARS = 4096  # Minecraft splits long replies into 4096-char packets


class RconError(RuntimeError):
    """Any RCON failure."""


class RconConnectionError(RconError):
    """Server not reachable, or the connection broke."""


class RconTimeout(RconConnectionError):
    """No reply within the timeout."""


class RconAuthError(RconError):
    """Wrong rcon.password."""


# Markers of a command the server could not parse or run.
_ERROR_MARKERS = (
    "unknown or incomplete command",
    "incorrect argument",
    "<--[here]",
    "unknown game rule",
    "unknown command",
)


def looks_like_error(reply: str) -> bool:
    """True if the reply is a command syntax/argument error."""
    low = reply.lower()
    return any(m in low for m in _ERROR_MARKERS)


class Rcon:
    def __init__(
        self,
        host: str = config.RCON_HOST,
        password: str = config.RCON_PASSWORD,
        port: int = config.RCON_PORT,
        timeout: float = config.RCON_TIMEOUT_S,
    ):
        self.host = host
        self.password = password
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._next_id = 0
        self._lock = threading.RLock()  # one command at a time on the socket

    # --- connection ----------------------------------------------------

    def __enter__(self) -> "Rcon":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        """Connect and log in. Raises RconConnectionError / RconAuthError."""
        with self._lock:
            if self._sock is None:
                self._connect()

    def close(self) -> None:
        with self._lock:
            self._drop()

    disconnect = close

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def _where(self) -> str:
        return f"{self.host}:{self.port}"

    def _connect(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except ConnectionRefusedError as e:
            raise RconConnectionError(
                f"Minecraft RCON at {self._where()} refused the connection. Is the "
                "server running (server/start_server.bat or start_server.sh) and does "
                "server.properties have enable-rcon=true and "
                f"rcon.port={self.port}? Wait for 'RCON running on' in the server console."
            ) from e
        except socket.timeout as e:
            raise RconTimeout(f"Timed out connecting to Minecraft RCON at {self._where()}.") from e
        except OSError as e:
            raise RconConnectionError(f"Cannot connect to Minecraft RCON at {self._where()}: {e}") from e
        sock.settimeout(self.timeout)
        self._sock = sock
        try:
            login_id = self._send(_LOGIN, self.password)
            reply_id, _, _ = self._read_packet()
        except RconError:
            self._drop()
            raise
        if reply_id == -1 or reply_id != login_id:
            self._drop()
            raise RconAuthError(
                f"RCON login to {self._where()} failed: wrong password. It must match "
                "rcon.password in server/server.properties (set RCON_PASSWORD to override "
                "mc/config.py)."
            )

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # --- protocol --------------------------------------------------------

    def _send(self, ptype: int, body: str) -> int:
        assert self._sock is not None
        self._next_id = self._next_id % 2_000_000_000 + 1
        payload = struct.pack("<ii", self._next_id, ptype) + body.encode("utf-8") + b"\x00\x00"
        try:
            self._sock.sendall(struct.pack("<i", len(payload)) + payload)
        except socket.timeout as e:
            raise RconTimeout(f"Timed out sending to RCON at {self._where()}.") from e
        except OSError as e:
            raise RconConnectionError(f"RCON connection to {self._where()} broke: {e}") from e
        return self._next_id

    def _read_exact(self, n: int) -> bytes:
        assert self._sock is not None
        data = b""
        while len(data) < n:
            try:
                chunk = self._sock.recv(n - len(data))
            except socket.timeout as e:
                raise RconTimeout(
                    f"No reply from RCON at {self._where()} within {self.timeout:.0f} s."
                ) from e
            except OSError as e:
                raise RconConnectionError(f"RCON connection to {self._where()} broke: {e}") from e
            if not chunk:
                raise RconConnectionError(
                    f"Minecraft server at {self._where()} closed the RCON connection "
                    "(server stopped or restarted?)."
                )
            data += chunk
        return data

    def _read_packet(self) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._read_exact(4))
        if not 10 <= length <= _MAX_PACKET:
            raise RconConnectionError(f"Bad RCON packet length {length} from {self._where()}.")
        payload = self._read_exact(length)
        pid, ptype = struct.unpack("<ii", payload[:8])
        return pid, ptype, payload[8:-2].decode("utf-8", errors="replace")

    def _exchange(self, command: str) -> str:
        self._send(_COMMAND, command)
        _, _, body = self._read_packet()
        reply = body
        # Long replies arrive as several 4096-char packets back to back.
        while len(body) >= _CHUNK_CHARS and self._readable(0.1):
            _, _, body = self._read_packet()
            reply += body
        return reply

    def _readable(self, wait: float) -> bool:
        assert self._sock is not None
        return bool(select.select([self._sock], [], [], wait)[0])

    def cmd(self, command: str) -> str:
        """Run one command, return the server's reply text.

        Connects lazily. If the connection turns out to be broken, it
        reconnects and resends once. A timeout is not resent (the command
        may have run); the socket is reset for the next call.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    return self._exchange(command)
                except RconTimeout:
                    self._drop()
                    raise
                except RconConnectionError:
                    self._drop()
                    if attempt == 2:
                        raise
            raise AssertionError("unreachable")

    # --- targets -------------------------------------------------------

    def summon_target(
        self,
        target_id: str,
        x: float,
        y: float,
        z: float,
        side: str,
        glowing: bool = False,
        entity: str = config.TARGET_ENTITY,
    ) -> str:
        """Skeleton that holds its mark and shoots the player. One arrow kills it.

        NoAI would also freeze the bow attack. Movement speed 0 keeps the
        eccentricity fixed; follow range covers the spawn distance.
        Entity NBT on /summon is still valid in 1.20.5+ / 26.x (only item
        data moved to components).
        """
        nbt = (
            "{PersistenceRequired:1b,IsBaby:0b,CanPickUpLoot:0b,Health:1f,"
            f"{config.TARGET_GEAR_NBT},"
            f"{config.TARGET_HOLD_ATTRIBUTES_NBT},"
            f'Glowing:{int(glowing)}b,Tags:["target","{target_id}","{side}"]}}'
        )
        return self.cmd(f"summon {entity} {x:.1f} {y:.1f} {z:.1f} {nbt}")

    def exists(self, tag: str) -> bool:
        """True while an entity with this tag is alive (and its chunk loaded)."""
        return "passed" in self.cmd(f"execute if entity @e[tag={tag}]").lower()

    def kill(self, tag: str) -> str:
        return self.cmd(f"kill @e[tag={tag}]")

    # --- feedback and effects -----------------------------------------

    @staticmethod
    def _text(text: str) -> str:
        # JSON text component; also valid SNBT, which 1.21.5+ parses.
        return json.dumps({"text": text}, ensure_ascii=False)

    def title(self, text: str, player: str = config.PLAYER) -> str:
        return self.cmd(f"title {player} title {self._text(text)}")

    def actionbar(self, text: str, player: str = config.PLAYER) -> str:
        """Small text above the hotbar, e.g. the director's reason."""
        return self.cmd(f"title {player} actionbar {self._text(text)}")

    def playsound(self, sound: str, x: float, y: float, z: float, player: str = config.PLAYER) -> str:
        """Positional sound, e.g. a cue from the target's side."""
        return self.cmd(f"playsound {sound} master {player} {x:.1f} {y:.1f} {z:.1f} 1 1")

    # --- player --------------------------------------------------------

    def yaw(self, player: str = config.PLAYER) -> float | None:
        """Player's horizontal look direction in degrees (180 = north)."""
        reply = self.cmd(f"data get entity {player} Rotation")
        match = re.search(r"\[\s*(-?[\d.]+(?:[eE]-?\d+)?)f", reply)
        return float(match.group(1)) if match else None

    def players_online(self) -> list[str]:
        """Names of online players, from 'There are N of a max of M players online: a, b'."""
        reply = self.cmd("list")
        _, sep, names = reply.partition(":")
        if not sep:
            return []
        return [n.strip() for n in names.split(",") if n.strip()]

    def player_online(self, player: str = config.PLAYER) -> bool:
        """True if `player` (a name, or a selector like @p) matches someone online."""
        online = self.players_online()
        if player.startswith("@"):
            return bool(online)
        return player.lower() in (n.lower() for n in online)
