"""BrainCraft launcher: set up, check and start the whole stack.

    python scripts/braincraft.py setup [--accept-eula] [--no-gpype]
    poetry run python scripts/braincraft.py doctor
    poetry run python scripts/braincraft.py start [--eeg gpype|unicorn|synthetic|mock]
                                               [--game rcon|sim|none] [--mode rehab|fun]
                                               [--player NAME] [--recalibrate]
    poetry run python scripts/braincraft.py check | calibrate | report

`start` brings the services up in order, checks each one before the
next, prefixes their output ([bridge], [service], ...) and stops all of
them on Ctrl+C. Logs: sessions/logs/<time>-<service>.log. The Makefile
wraps these commands (make start, make dev, ...).

Stdlib only at import time, so `setup` runs before `poetry install`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "sessions"
LOGS = SESSIONS / "logs"
SERVER_DIR = ROOT / "server"
WINDOWS = os.name == "nt"
PY = sys.executable
BRIDGE_STREAM = "BrainCraft-Unicorn"
# Lines kept in the log files but not printed: liblsl's own log, and the
# Minecraft server noting every RCON connection (the launcher polls).
NOISE = re.compile(r"\s(INFO|WARN)\|\s|Thread RCON Client|RCON Listener")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def say(msg: str) -> None:
    print(f"[braincraft] {msg}", flush=True)


def die(msg: str) -> None:
    say(f"ERROR: {msg}")
    raise SystemExit(1)


# --- child processes ---------------------------------------------------------


class Child:
    """A background service with prefixed, logged output."""

    def __init__(self, tag: str, cmd: list[str] | str, *, cwd: Path = ROOT,
                 env: dict | None = None, shell: bool = False, stamp: str = ""):
        self.tag = tag
        self.lines: list[str] = []
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log_path = LOGS / f"{stamp}-{tag}.log"
        self._log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        kw: dict = {}
        if WINDOWS:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True  # Ctrl+C reaches only the launcher
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})},
            shell=shell, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kw,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip()
            self._log.write(line + "\n")
            self._log.flush()
            self.lines.append(line)
            del self.lines[:-200]
            if not NOISE.search(line):
                print(f"[{self.tag}] {line}", flush=True)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def saw(self, text: str) -> bool:
        return any(text in line for line in self.lines)

    def stop(self, timeout: float = 10.0) -> None:
        if self.alive():
            try:
                if WINDOWS:
                    self.proc.terminate()
                else:
                    self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                say(f"{self.tag} did not stop in {timeout:g} s, killing it")
                self.proc.kill()
                self.proc.wait(5)
            except OSError:
                pass
        self._log.close()


def wait_until(what: str, ok, timeout: float, child: Child | None = None, every: float = 1.0) -> bool:
    say(f"waiting for {what} (up to {timeout:g} s)...")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if child is not None and not child.alive():
            die(f"{child.tag} exited (code {child.proc.returncode}); see {child.log_path}")
        if ok():
            return True
        time.sleep(every)
    return False


# --- probes ------------------------------------------------------------------


def http_json(url: str, timeout: float = 1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def lsl_streams(wait: float = 1.0) -> list:
    try:
        from pylsl import resolve_streams
    except Exception:
        return []
    return list(resolve_streams(wait_time=wait))


def find_stream(name: str | None) -> bool:
    return any((s.name() == name if name else s.type() == "EEG") for s in lsl_streams())


def rcon_ok() -> bool:
    try:
        from mc.rcon import Rcon

        with Rcon(timeout=2) as r:
            r.cmd("list")
        return True
    except Exception:
        return False


def players_online() -> list[str]:
    try:
        from mc.rcon import Rcon

        with Rcon(timeout=2) as r:
            return r.players_online()
    except Exception:
        return []


def server_command() -> str:
    """The java command written by scripts/setup_mc_server.py."""
    if WINDOWS:
        bat = SERVER_DIR / "start_server.bat"
        if bat.exists():
            for line in bat.read_text().splitlines():
                if "-jar server.jar" in line:
                    return line.strip()
    else:
        sh = SERVER_DIR / "start_server.sh"
        if sh.exists():
            for line in sh.read_text().splitlines():
                if "-jar server.jar" in line:
                    return re.sub(r"^exec\s+", "", line.strip())
    die("no Minecraft server set up: run `make server` "
        "(python scripts/setup_mc_server.py --accept-eula)")
    return ""


def whitelist(names: list[str]) -> None:
    """Minecraft 26.3 servers start with the whitelist on: add the players.

    Kept on (rather than disabled) so nobody else on the venue network can
    join the demo server. Needs internet once per name (Mojang lookup).
    """
    if not names:
        return
    from mc.rcon import Rcon

    with Rcon(timeout=15) as r:
        failed = []
        for name in names:
            reply = r.cmd(f"whitelist add {name}").strip()
            say(f"whitelist {name}: {reply or 'ok'}")
            if "does not exist" in reply:  # typo, or Mojang lookup failed (no internet)
                failed.append(name)
        if failed:
            say(f"could not whitelist {', '.join(failed)} (check the spelling; the lookup "
                "needs internet). Turning the whitelist OFF so you can join.")
            say(r.cmd("whitelist off").strip())


def baseline_path(player: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", player)
    return SESSIONS / f"baseline-{safe}.json"


def latest_session() -> Path | None:
    csvs = [p for p in SESSIONS.glob("*.csv")
            if not p.stem.endswith("-state") and not p.name.startswith(("raw", "baseline"))]
    return max(csvs, key=lambda p: p.stat().st_mtime) if csvs else None


# --- commands ----------------------------------------------------------------


def cmd_setup(args) -> int:
    poetry = shutil.which("poetry")
    if not poetry:
        die("Poetry not found. Install it (docs/setup.md section 2), open a new terminal, retry.")
    groups = [] if args.no_gpype else ["--with", "gpype"]
    say(f"poetry install {' '.join(groups)}")
    if subprocess.call([poetry, "install", *groups], cwd=ROOT) != 0:
        die("poetry install failed")
    if (SERVER_DIR / "server.jar").exists() and not args.accept_eula:
        say("Minecraft server already set up in server/")
    elif args.accept_eula:
        cmd = [PY, "scripts/setup_mc_server.py", "--accept-eula"]
        if args.java:
            cmd += ["--java", args.java]
        say("setting up the Minecraft server (you accepted the EULA: https://aka.ms/MinecraftEULA)")
        if subprocess.call([poetry, "run", *cmd], cwd=ROOT) != 0:
            die("Minecraft server setup failed")
    else:
        say("Minecraft server not set up. To accept the Minecraft EULA "
            "(https://aka.ms/MinecraftEULA) and set it up: make setup ACCEPT_EULA=1")
    say("done. Next: make doctor")
    return 0


def cmd_doctor(args) -> int:
    rows: list[tuple[str, bool, str]] = []

    def row(name, ok, detail=""):
        rows.append((name, ok, detail))

    row("Python 3.10-3.14", (3, 10) <= sys.version_info[:2] <= (3, 14), sys.version.split()[0])
    row("Poetry", bool(shutil.which("poetry")), shutil.which("poetry") or "not on PATH")
    for mod in ("numpy", "fastapi", "pylsl"):
        try:
            __import__(mod)
            row(f"package {mod}", True)
        except Exception as e:
            row(f"package {mod}", False, f"{e.__class__.__name__}: run `make setup`")
    try:
        import gpype

        row("g.Pype (headset bridge)", True, gpype.__version__)
    except Exception:
        row("g.Pype (headset bridge)", False, "poetry install --with gpype (only for --eeg gpype)")
    try:
        from scripts.setup_mc_server import _run_java, default_java, parse_java_major

        candidates = [default_java(), "/opt/homebrew/opt/openjdk@25/bin/java",
                      "/usr/local/opt/openjdk@25/bin/java"]
        if (SERVER_DIR / "server.jar").exists():  # the java the server really uses
            candidates.insert(0, server_command().split(" -X")[0].strip().strip('"'))
        java, major = "java", None
        for java in candidates:
            try:
                major = parse_java_major(_run_java(java))
            except Exception:
                major = None
            if major:
                break
        row("Java 25 (Minecraft 26.3)", (major or 0) >= 25, f"{java}: {major}")
    except Exception as e:
        row("Java 25 (Minecraft 26.3)", False, str(e)[:80])
    jar = SERVER_DIR / "server.jar"
    row("Minecraft server set up", jar.exists(), "server/" if jar.exists() else "make setup ACCEPT_EULA=1")
    eula = SERVER_DIR / "eula.txt"
    row("EULA accepted", eula.exists() and "eula=true" in eula.read_text(), "")
    row("Minecraft server running (RCON)", rcon_ok(), "make start starts it" if not port_open(25575) else "")
    st = http_json("http://127.0.0.1:8000/state")
    row("state service :8000", st is not None, f"source={st.get('source')}" if st else "not running")
    streams = lsl_streams(2.0)
    row("LSL EEG stream", any(s.type() == "EEG" for s in streams),
        ", ".join(f"{s.name()} ({s.channel_count()} ch)" for s in streams) or "none")
    baselines = sorted(p.name for p in SESSIONS.glob("baseline*.json"))
    row("baselines", bool(baselines), ", ".join(baselines) or "none yet (start calibrates)")

    width = max(len(r[0]) for r in rows)
    for name, ok, detail in rows:
        print(f"  {'OK ' if ok else '-- '} {name:<{width}}  {detail}")
    return 0


def cmd_check(args) -> int:
    cmd = [PY, "-m", "eeg.check"]
    if args.name:
        cmd += ["--name", args.name]
    return subprocess.call(cmd, cwd=ROOT)


def run_calibration(eeg: str, out: Path, name: str | None) -> None:
    cmd = [PY, "-m", "eeg.calibrate", "--out", str(out)]
    if eeg == "synthetic":
        cmd += ["--source", "synthetic", "--fast"]
    elif name:
        cmd += ["--name", name]
    say(f"calibrating -> {out.relative_to(ROOT)} (follow the instructions; ~2.5 min)")
    if subprocess.call(cmd, cwd=ROOT) != 0 or not out.exists():
        die("calibration failed or was aborted")


def cmd_calibrate(args) -> int:
    run_calibration(args.eeg, baseline_path(args.player), args.name)
    return 0


def cmd_whitelist(args) -> int:
    if not rcon_ok():
        die("Minecraft server not running (or RCON not reachable)")
    whitelist(args.names)
    return 0


def cmd_report(args) -> int:
    path = Path(args.session) if args.session else latest_session()
    if not path:
        die("no session CSV in sessions/")
    return subprocess.call([PY, "-m", "report.make_report", str(path), "--open"], cwd=ROOT)


def _interrupt(*_):
    raise KeyboardInterrupt


def cmd_start(args) -> int:
    # Ctrl+C and termination both shut everything down in order, even when
    # the launcher was started in the background (where SIGINT is ignored).
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _interrupt)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = args.name or f"{args.player}-{args.mode}-{stamp}"
    children: list[Child] = []
    server: Child | None = None
    lsl_name = args.lsl_name or (BRIDGE_STREAM if args.eeg == "gpype" else None)

    if port_open(args.port):
        st = http_json(f"http://127.0.0.1:{args.port}/state")
        die(f"port {args.port} is busy (a state service with source="
            f"{st.get('source') if st else '?'} is already running?). Stop it first.")

    def stop_all() -> None:
        for c in reversed(children):
            if c is server:
                stop_server(c)
            else:
                c.stop()

    try:
        # 1. EEG reader
        service_env = {"BRAINCRAFT_SOURCE": "mock" if args.eeg == "mock" else "eeg"}
        if args.eeg == "gpype":
            if find_stream(BRIDGE_STREAM):
                say(f"LSL stream {BRIDGE_STREAM!r} already running; not starting another bridge")
            else:
                cmd = [PY, "-m", "eeg.gpype_bridge"]
                if args.record:
                    cmd += ["--record", str(SESSIONS / f"raw-{name}")]
                if args.oscar:
                    cmd.append("--oscar")
                if args.fake_headset:
                    cmd.append("--fake")
                bridge = Child("bridge", cmd, stamp=stamp)
                children.append(bridge)
                if not wait_until(f"LSL stream {BRIDGE_STREAM!r}", lambda: find_stream(BRIDGE_STREAM),
                                  60, bridge, every=0):
                    die("the bridge started but no stream appeared: headset on, paired, in range?")
        elif args.eeg == "unicorn":
            if not wait_until("an EEG stream from the Unicorn LSL app (Open + Start it now)",
                              lambda: find_stream(lsl_name), 120, every=0):
                die("no EEG stream")
        if args.eeg in ("gpype", "unicorn"):
            service_env["BRAINCRAFT_EEG_INPUT"] = "lsl"
            if lsl_name:
                service_env["BRAINCRAFT_LSL_NAME"] = lsl_name
        elif args.eeg == "synthetic":
            service_env["BRAINCRAFT_EEG_INPUT"] = "synthetic"

        # 2. baseline for this player
        if args.eeg != "mock":
            base = baseline_path(args.player)
            if args.recalibrate or not base.exists():
                run_calibration(args.eeg, base, lsl_name)
            else:
                say(f"using baseline {base.relative_to(ROOT)} (--recalibrate for a new one)")
            service_env["BRAINCRAFT_BASELINE"] = str(base)

        # 3. Minecraft
        if args.game == "rcon":
            if rcon_ok():
                say("Minecraft server already running")
            else:
                server = Child("minecraft", server_command(), cwd=SERVER_DIR, shell=True, stamp=stamp)
                children.append(server)
                if not wait_until("Minecraft server RCON", rcon_ok, 180, server, every=2):
                    die("Minecraft server did not open RCON")
            if args.mc_name:
                whitelist(args.mc_name)
            elif args.wait_player:
                say("tip: if Minecraft says you're not whitelisted, add MC_NAME=<username> "
                    "(make) or --mc-name <username>")
            subprocess.call([PY, "-m", "mc.world"], cwd=ROOT)
            if args.wait_player:
                say("join the server now: Minecraft 26.3 -> Multiplayer -> Direct Connection -> localhost")
                last_hint = [time.monotonic()]

                def joined() -> bool:
                    if time.monotonic() - last_hint[0] > 30:
                        last_hint[0] = time.monotonic()
                        say("still waiting: join localhost in Minecraft 26.3 "
                            "(or restart with ARGS=--no-wait-player)")
                    return bool(players_online())

                wait_until("a player to join", joined, 600, server, every=2)
                players = players_online()
                if players:
                    subprocess.call([PY, "-m", "mc.world", "--player", players[0]], cwd=ROOT)

        # 4. state service
        service = Child("service", [PY, "-m", "uvicorn", "state_service:app",
                                    "--host", "127.0.0.1", "--port", str(args.port),
                                    "--no-access-log"],
                        env=service_env, stamp=stamp)
        children.append(service)
        url = f"http://127.0.0.1:{args.port}"
        if not wait_until("state service", lambda: http_json(f"{url}/state") is not None, 30, service):
            die("state service did not answer")
        source = http_json(f"{url}/state").get("source")
        say(f"state service up, source={source}")
        if source != service_env["BRAINCRAFT_SOURCE"]:
            die(f"service reports source={source}, expected {service_env['BRAINCRAFT_SOURCE']}")
        hud = f"http://localhost:{args.port}"
        say(f"HUD: {hud}")
        if not args.no_browser:
            webbrowser.open(hud)

        # 5. game loop
        game = None
        if args.game != "none":
            cmd = [PY, "game_loop.py", "--mode", args.mode, f"--{args.game}", "--name", name,
                   "--duration", str(args.duration), "--state-url", url]
            game = Child("game", cmd, stamp=stamp)
            children.append(game)

        say("running. Ctrl+C stops everything.")
        while True:
            for c in children:
                if c.tag != "game" and not c.alive():
                    say(f"{c.tag} exited (code {c.proc.returncode}); see {c.log_path}")
                    raise KeyboardInterrupt
            if game is not None and not game.alive():
                say("game session finished")
                events = SESSIONS / f"{name}.csv"
                if events.exists() and not args.no_report:
                    subprocess.call([PY, "-m", "report.make_report", str(events), "--open"], cwd=ROOT)
                game = None
                say("services keep running (HUD stays live). Ctrl+C stops everything.")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        say("stopping...")
    finally:
        stop_all()
        say(f"stopped. Logs: {LOGS.relative_to(ROOT)}/{stamp}-*.log")
    return 0


def stop_server(server: Child) -> None:
    """Save the world and stop via RCON; kill only as a last resort."""
    if not server.alive():
        return
    try:
        from mc.rcon import Rcon

        with Rcon(timeout=3) as r:
            r.cmd("stop")
        server.proc.wait(30)
    except Exception:
        server.stop(timeout=30)
    server.stop()


# --- CLI ---------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="braincraft", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup", help="poetry install (+ g.Pype) and the Minecraft server")
    s.add_argument("--accept-eula", action="store_true",
                   help="accept the Minecraft EULA (https://aka.ms/MinecraftEULA) and set up server/")
    s.add_argument("--no-gpype", action="store_true", help="skip the g.Pype headset group")
    s.add_argument("--java", help="java executable for the server")

    sub.add_parser("doctor", help="check prerequisites and what is running")

    c = sub.add_parser("check", help="signal sanity check (eyes-closed alpha)")
    c.add_argument("--name", help="LSL stream name")

    k = sub.add_parser("calibrate", help="record a baseline for --player")
    k.add_argument("--player", default="player")
    k.add_argument("--eeg", choices=("gpype", "unicorn", "synthetic"), default="gpype")
    k.add_argument("--name", help="LSL stream name")

    w = sub.add_parser("whitelist", help="allow Minecraft usernames to join the running server")
    w.add_argument("names", nargs="+")

    r = sub.add_parser("report", help="HTML report of the latest (or given) session")
    r.add_argument("session", nargs="?")

    st = sub.add_parser("start", help="start the stack; Ctrl+C stops it")
    st.add_argument("--eeg", choices=("gpype", "unicorn", "synthetic", "mock"), default="gpype",
                    help="gpype: start the headset bridge; unicorn: Unicorn LSL app is running; "
                         "synthetic: generated EEG; mock: HUD sliders (default gpype)")
    st.add_argument("--game", choices=("rcon", "sim", "none"), default="rcon",
                    help="rcon: real Minecraft (started if needed); sim: simulated; none: no game loop")
    st.add_argument("--mode", choices=("rehab", "fun"), default="rehab")
    st.add_argument("--player", default="player", help="whose baseline to use (sessions/baseline-<player>.json)")
    st.add_argument("--recalibrate", action="store_true", help="record a new baseline first")
    st.add_argument("--mc-name", action="append", default=[], metavar="USERNAME",
                    help="Minecraft username to whitelist (repeatable); 26.3 servers whitelist by default")
    st.add_argument("--name", help="session name (default <player>-<mode>-<time>)")
    st.add_argument("--duration", type=float, default=600.0, help="game session length, s")
    st.add_argument("--port", type=int, default=8000)
    st.add_argument("--lsl-name", help="LSL stream name (default: the bridge's, or any EEG stream)")
    st.add_argument("--no-record", dest="record", action="store_false", help="don't save raw EEG")
    st.add_argument("--oscar", action="store_true", help="bridge: OSCAR artifact removal")
    st.add_argument("--no-wait-player", dest="wait_player", action="store_false",
                    help="rcon: don't wait for a player to join before starting")
    st.add_argument("--no-browser", action="store_true", help="don't open the HUD")
    st.add_argument("--fake-headset", action="store_true", help=argparse.SUPPRESS)  # launcher tests
    st.add_argument("--no-report", action="store_true", help="no report when the session ends")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:  # Windows consoles may not be UTF-8
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    return {
        "setup": cmd_setup, "doctor": cmd_doctor, "check": cmd_check,
        "calibrate": cmd_calibrate, "report": cmd_report, "start": cmd_start,
        "whitelist": cmd_whitelist,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
