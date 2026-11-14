"""Minecraft RCON tests.

Two parts:
- Offline (always run): the RCON client against an in-process fake RCON
  server, and mc.world logic against a fake Rcon.
- Live integration (skipped unless BRAINCRAFT_MC_IT=1): against a real
  local server, e.g.

      BRAINCRAFT_MC_IT=1 poetry run pytest tests/test_mc_integration.py -q
      $env:BRAINCRAFT_MC_IT="1"; poetry run pytest tests/test_mc_integration.py -q   # PowerShell

  Uses RCON_HOST / RCON_PORT / RCON_PASSWORD from mc/config.py.
"""

from __future__ import annotations

import os
import socket
import struct
import threading

import pytest

from game.geometry import target_position
from mc import config, world
from mc.rcon import Rcon, RconAuthError, RconConnectionError, RconTimeout, looks_like_error

# --- fake RCON server ------------------------------------------------------


class FakeServer:
    """Minimal Minecraft-style RCON server on 127.0.0.1:<random port>."""

    def __init__(self, password="pw", reply=lambda c: f"ran {c}", drop_after=None, silent=False):
        self.password, self.reply = password, reply
        self.drop_after, self.silent = drop_after, silent
        self.commands: list[str] = []
        self.connections = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    @staticmethod
    def _send(conn, pid, ptype, body):
        payload = struct.pack("<ii", pid, ptype) + body.encode() + b"\x00\x00"
        conn.sendall(struct.pack("<i", len(payload)) + payload)

    def _handle(self, conn):
        handled = 0
        try:
            while True:
                (length,) = struct.unpack("<i", self._recv(conn, 4))
                payload = self._recv(conn, length)
                pid, ptype = struct.unpack("<ii", payload[:8])
                body = payload[8:-2].decode()
                if ptype == 3:
                    self._send(conn, pid if body == self.password else -1, 2, "")
                    continue
                if self.drop_after is not None and handled >= self.drop_after:
                    self.drop_after = None  # drop once
                    conn.close()
                    return
                self.commands.append(body)
                handled += 1
                if self.silent:
                    continue
                text = self.reply(body)
                for i in range(0, max(len(text), 1), 4096):
                    self._send(conn, pid, 0, text[i : i + 4096])
        except (ConnectionError, OSError, struct.error):
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


@pytest.fixture
def server():
    servers = []

    def make(**kw):
        s = FakeServer(**kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def test_cmd_roundtrip(server):
    s = server()
    with Rcon("127.0.0.1", "pw", s.port, timeout=2) as r:
        assert r.cmd("list") == "ran list"
        assert r.cmd("time set midnight") == "ran time set midnight"
    assert s.commands == ["list", "time set midnight"]


def test_long_reply_is_reassembled(server):
    long = "x" * 9000
    s = server(reply=lambda c: long)
    with Rcon("127.0.0.1", "pw", s.port, timeout=2) as r:
        assert r.cmd("help") == long


def test_wrong_password(server):
    s = server()
    with pytest.raises(RconAuthError, match="wrong password"):
        Rcon("127.0.0.1", "nope", s.port, timeout=2).connect()


def test_server_not_running():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(RconConnectionError, match="refused|Cannot connect"):
        Rcon("127.0.0.1", "pw", port, timeout=1).connect()


def test_reconnects_once_after_broken_connection(server):
    s = server(drop_after=1)
    with Rcon("127.0.0.1", "pw", s.port, timeout=2) as r:
        assert r.cmd("a") == "ran a"
        assert r.cmd("b") == "ran b"  # first try hits the dropped socket
    assert s.connections == 2
    assert s.commands == ["a", "b"]


def test_timeout_raises_and_resets(server):
    s = server(silent=True)
    r = Rcon("127.0.0.1", "pw", s.port, timeout=0.3)
    with pytest.raises(RconTimeout):
        r.cmd("list")
    assert not r.connected
    r.close()


def test_helpers_parse_replies(server):
    replies = {
        "list": "There are 2 of a max of 20 players online: Alice, Bob",
        "data get entity @p Rotation": "Alice has the following entity data: [-90.5f, 3.0f]",
        "execute if entity @e[tag=t1]": "Test passed. Count: 1",
        "execute if entity @e[tag=t2]": "Test failed",
    }
    s = server(reply=lambda c: replies.get(c, ""))
    with Rcon("127.0.0.1", "pw", s.port, timeout=2) as r:
        assert r.players_online() == ["Alice", "Bob"]
        assert r.player_online("bob") and r.player_online("@p") and not r.player_online("Carol")
        assert r.yaw() == -90.5
        assert r.exists("t1") and not r.exists("t2")
        r.title('say "hi"', "@a")
        r.summon_target("t9", 1, -60, -20, "L", glowing=True)
    assert s.commands[-2] == 'title @a title {"text": "say \\"hi\\""}'
    assert s.commands[-1].startswith("summon minecraft:husk 1.0 -60.0 -20.0 {NoAI:1b")
    assert 'Glowing:1b,Tags:["target","t9","L"]' in s.commands[-1]


def test_players_online_empty(server):
    s = server(reply=lambda c: "There are 0 of a max of 20 players online: ")
    with Rcon("127.0.0.1", "pw", s.port, timeout=2) as r:
        assert r.players_online() == []
        assert not r.player_online("@p")


# --- mc.world logic with a fake Rcon ------------------------------------------


class FakeRcon:
    """Answers like a 26.3 server: snake_case game rules only, nobody online."""

    def __init__(self, players=()):
        self.players = list(players)
        self.sent: list[str] = []

    def cmd(self, c):
        self.sent.append(c)
        if c.startswith("gamerule "):
            name = c.split()[1]
            if any(ch.isupper() for ch in name):
                return f"Incorrect argument for command\n...gamerule {name}<--[HERE]"
            return f"Gamerule {name} is now set to: {c.split()[2]}"
        if c == "list":
            return f"There are {len(self.players)} of a max of 20 players online: " + ", ".join(self.players)
        if not self.players and ("@p" in c or "@a" in c):
            return "No player was found"
        return "ok"

    def player_online(self, player="@p"):
        return Rcon.player_online(self, player)  # type: ignore[arg-type]

    def players_online(self):
        return Rcon.players_online(self)  # type: ignore[arg-type]


def test_gamerule_tries_snake_case_first():
    r = FakeRcon()
    assert world.set_gamerule(r, "doDaylightCycle", "false") == "advance_time"
    assert r.sent == ["gamerule advance_time false"]


def test_gamerule_falls_back_to_camel_case():
    class Old(FakeRcon):
        def cmd(self, c):
            self.sent.append(c)
            if "_" in c.split()[1]:
                return "Unknown or incomplete command, see below for error<--[HERE]"
            return "Gamerule set"

    r = Old()
    assert world.set_gamerule(r, "keepInventory", "true") == "keepInventory"
    assert r.sent == ["gamerule keep_inventory true", "gamerule keepInventory true"]


def test_gamerule_unknown_does_not_crash():
    class Nothing(FakeRcon):
        def cmd(self, c):
            return "Incorrect argument for command<--[HERE]"

    assert world.set_gamerule(Nothing(), "doMobLoot", "false") is None


def test_setup_without_player_skips_player_steps(capsys):
    r = FakeRcon()
    world.setup(r)
    assert r.sent[0].startswith("forceload add ")
    assert not any(c.startswith(("tp ", "give ", "gamemode ")) for c in r.sent)
    assert "nobody online" in capsys.readouterr().out


def test_setup_with_player_places_them():
    r = FakeRcon(players=["Steve"])
    world.setup(r, "Steve")
    top = config.GROUND_Y + config.TOWER_HEIGHT
    assert f"tp Steve {config.TOWER_X + 0.5} {top} {config.TOWER_Z + 0.5} 180 0" in r.sent
    assert "gamemode adventure Steve" in r.sent


def test_forceload_covers_all_targets():
    for side in ("L", "R"):
        for ecc in (0, *config.ECCENTRICITIES, 90):
            x, _, z = target_position(side, ecc)
            assert config.ARENA_MIN_X <= x <= config.ARENA_MAX_X
            assert config.ARENA_MIN_Z <= z <= config.ARENA_MAX_Z
    chunks = ((config.ARENA_MAX_X // 16) - (config.ARENA_MIN_X // 16) + 1) * (
        (config.ARENA_MAX_Z // 16) - (config.ARENA_MIN_Z // 16) + 1
    )
    assert chunks <= 256  # forceload limit per command


def test_looks_like_error():
    assert looks_like_error("Incorrect argument for command\n...<--[HERE]")
    assert looks_like_error("Unknown or incomplete command, see below for error")
    assert not looks_like_error("Summoned new Zombie")
    assert not looks_like_error("No player was found")


# --- live integration ------------------------------------------------------

live = pytest.mark.skipif(
    os.environ.get("BRAINCRAFT_MC_IT") != "1", reason="set BRAINCRAFT_MC_IT=1 with a running server"
)


@pytest.fixture(scope="module")
def mc():
    with Rcon() as r:
        world.setup(r)
        yield r


@live
def test_live_forceload_active(mc):
    assert world.forceload_active(mc)


@live
def test_live_summon_exists_kill(mc):
    x, y, z = target_position("L", 35)
    reply = mc.summon_target("it_t1", x, y, z, "L", glowing=True)
    assert not looks_like_error(reply) and "summoned" in reply.lower(), reply
    assert mc.exists("it_t1")
    assert "killed" in mc.kill("it_t1").lower()
    assert not mc.exists("it_t1")


@live
def test_live_feedback_commands_parse(mc):
    x, y, z = target_position("R", 55)
    for reply in (
        mc.title("BrainCraft test", "@a"),
        mc.actionbar("+1", "@a"),
        mc.playsound("minecraft:entity.experience_orb.pickup", x, y, z, "@a"),
        mc.cmd("time set midnight"),
    ):
        assert not looks_like_error(reply), reply


@live
def test_live_yaw(mc):
    if not mc.players_online():
        pytest.skip("no player online")
    assert mc.yaw() is not None
