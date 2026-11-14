"""Set up a vanilla Minecraft Java server for BrainCraft (stdlib only).

    python scripts/setup_mc_server.py --accept-eula
    python scripts/setup_mc_server.py --jar C:\\Downloads\\server.jar --accept-eula   # offline

Steps: Mojang version manifest -> version JSON -> server.jar (sha1
verified, skipped if already there) -> Java version check -> merged
server.properties (RCON on, superflat) -> eula.txt (only with
--accept-eula) -> start_server.bat / start_server.sh.

Works on Windows and macOS. Network and Java detection are injected
(`http_get`, `run_java`) so the tests run offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
EULA_URL = "https://aka.ms/MinecraftEULA"
SERVER_PAGE = "https://www.minecraft.net/en-us/download/server"
DEFAULT_VERSION = "26.3"
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "server"
META_CACHE = ".braincraft-version.json"  # version JSON saved for offline reruns
HTTP_TIMEOUT_S = 60

# Fallback when the version JSON is unavailable (offline --jar without a
# cache and without version.json in the jar). The script prefers the JSON.
KNOWN_JAVA = [("26.", 25), ("1.21", 21), ("1.20.5", 21), ("1.20.6", 21)]

HttpGet = Callable[[str], bytes]
RunJava = Callable[[str], str]


class SetupError(RuntimeError):
    pass


# --- network ---------------------------------------------------------------


def _is_cert_error(e: BaseException) -> bool:
    if isinstance(e, ssl.SSLCertVerificationError):
        return True
    reason = getattr(e, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e)


def _urllib_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
        return resp.read()


def _curl_get(url: str) -> bytes:
    curl = shutil.which("curl")
    if not curl:
        raise SetupError("curl not found")
    proc = subprocess.run(
        [curl, "-fsSL", "--retry", "2", "--max-time", "600", url],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SetupError(f"curl failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def http_get(
    url: str,
    urlopen_get: HttpGet = _urllib_get,
    curl_get: HttpGet = _curl_get,
    log: Callable[[str], None] = print,
) -> bytes:
    """GET with urllib (verified TLS); on a certificate error retry with curl.

    Certificate verification is never disabled. The python.org build on
    macOS ships without a CA bundle until 'Install Certificates.command'
    is run; curl uses the system store instead.
    """
    try:
        return urlopen_get(url)
    except Exception as e:  # noqa: BLE001 - classify below
        if not _is_cert_error(e):
            raise SetupError(f"Download failed: {url}: {e}") from e
        log(
            "  TLS certificate check failed in Python; retrying with curl. "
            "(macOS python.org Python: run 'Install Certificates.command' from the "
            "Python folder in Applications; or use --jar.)"
        )
        try:
            return curl_get(url)
        except Exception as e2:  # noqa: BLE001
            raise SetupError(f"Download failed with urllib ({e}) and curl ({e2}): {url}") from e2


def get_json(url: str, get: HttpGet) -> dict:
    return json.loads(get(url).decode("utf-8"))


def find_version(manifest: dict, version_id: str) -> dict:
    for entry in manifest.get("versions", []):
        if entry.get("id") == version_id:
            return entry
    latest = manifest.get("latest", {}).get("release")
    raise SetupError(f"Version {version_id!r} not in the Mojang manifest (latest release: {latest}).")


def server_download(version_json: dict) -> tuple[str, str, int | None]:
    try:
        server = version_json["downloads"]["server"]
    except KeyError as e:
        raise SetupError(f"Version {version_json.get('id')} has no server download.") from e
    return server["url"], server["sha1"], server.get("size")


def required_java(version_json: dict | None) -> int | None:
    if not version_json:
        return None
    major = version_json.get("javaVersion", {}).get("majorVersion")
    return int(major) if major else None


def guess_java(version_id: str) -> int | None:
    for prefix, major in KNOWN_JAVA:
        if version_id.startswith(prefix):
            return major
    return None


# --- jar -------------------------------------------------------------------


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def jar_version_info(path: Path) -> dict:
    """version.json embedded in server.jar ({'id', 'java_version', ...}), or {}."""
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("version.json").decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {}


def ensure_jar(
    dest: Path,
    url: str | None,
    sha1: str | None,
    get: HttpGet | None,
    jar: Path | None = None,
    log: Callable[[str], None] = print,
) -> str:
    """Make dest (server.jar) exist and match sha1. Returns what was done."""
    if jar is not None:
        jar = Path(jar)
        if not jar.is_file():
            raise SetupError(f"--jar {jar} does not exist.")
        actual = sha1_file(jar)
        if sha1 and actual != sha1:
            raise SetupError(
                f"--jar {jar} has sha1 {actual}, expected {sha1} for this version. "
                "Wrong version or a broken download."
            )
        if not sha1:
            log("  (no expected sha1 known offline; cannot verify the --jar file)")
        if not (dest.exists() and dest.resolve() == jar.resolve()):
            shutil.copyfile(jar, dest)
        return "copied"

    if sha1 and dest.is_file() and sha1_file(dest) == sha1:
        return "present"
    if not url or not sha1 or get is None:
        if dest.is_file():
            log("  server.jar exists but cannot be verified (no version metadata).")
            return "unverified"
        raise SetupError("No server.jar and no download URL (offline?). Use --jar PATH.")

    log(f"  downloading {url} ...")
    data = get(url)
    actual = hashlib.sha1(data).hexdigest()
    if actual != sha1:
        raise SetupError(f"Downloaded server.jar has sha1 {actual}, expected {sha1}. Try again.")
    tmp = dest.with_suffix(".jar.part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return "downloaded"


# --- java ------------------------------------------------------------------


def default_java() -> str:
    home = os.environ.get("JAVA_HOME")
    if home:
        exe = Path(home) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if exe.is_file():
            return str(exe)
    return "java"


def _run_java(java: str) -> str:
    proc = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30)
    return (proc.stderr or "") + (proc.stdout or "")


def parse_java_major(text: str) -> int | None:
    """'openjdk version "25.0.1" 2025-10-21' -> 25, 'java version "1.8.0_391"' -> 8."""
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text) or re.search(
        r"(?:openjdk|java)\s+(\d+)(?:\.(\d+))?", text
    )
    if not m:
        return None
    major = int(m.group(1))
    if major == 1 and m.group(2):
        major = int(m.group(2))
    return major


def java_install_hint(major: int) -> str:
    return (
        f"  Install Java {major} (a JDK or JRE, 64-bit):\n"
        f"    Windows: Eclipse Temurin {major} MSI from https://adoptium.net (tick 'Set JAVA_HOME'),\n"
        f"             or: winget install EclipseAdoptium.Temurin.{major}.JDK\n"
        f"    macOS:   brew install openjdk@{major}\n"
        f"             (keg-only: /opt/homebrew/opt/openjdk@{major}/bin/java, pass it with --java)\n"
        "  Then open a new terminal and run this script again (or pass --java PATH)."
    )


def check_java(java: str, need: int | None, run_java: RunJava = _run_java) -> tuple[bool, int | None, str]:
    try:
        out = run_java(java)
    except (OSError, subprocess.SubprocessError):
        out = None
    if out is None:
        msg = f"Java not found ('{java}')."
        return False, None, msg + ("\n" + java_install_hint(need) if need else "")
    major = parse_java_major(out)
    if major is None:
        return False, None, f"Could not read the Java version from: {out.strip()[:200]}"
    if need and major < need:
        return False, major, f"Java {major} is too old; Minecraft needs Java {need}.\n" + java_install_hint(need)
    return True, major, f"Java {major} OK" + (f" (needs {need})" if need else "")


# --- config files ----------------------------------------------------------


def server_properties(rcon_password: str, rcon_port: int) -> dict[str, str]:
    """Managed keys, values as written in the file (':' escaped)."""
    return {
        "enable-rcon": "true",
        "rcon.port": str(rcon_port),
        "rcon.password": rcon_password,
        "broadcast-rcon-to-ops": "false",
        "level-type": "minecraft\\:flat",
        "generate-structures": "false",
        "difficulty": "easy",
        "spawn-protection": "0",
        "view-distance": "8",
        "simulation-distance": "6",
        "online-mode": "true",
        "motd": "BrainCraft",
        "spawn-monsters": "false",
        "allow-flight": "true",
    }


def merge_properties(path: Path, updates: dict[str, str], defaults: dict[str, str] | None = None) -> dict[str, str]:
    """Set `updates` keys in a .properties file, keep every other line.

    `defaults` are only added if the key is missing. Returns the keys
    whose previous value changed ({key: old}).
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    changed: dict[str, str] = {}
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith(("#", "!")) and "=" in s:
            key, _, old = s.partition("=")
            key = key.strip()
            if key in updates:
                seen.add(key)
                if old != updates[key]:
                    changed[key] = old
                out.append(f"{key}={updates[key]}")
                continue
            if defaults and key in defaults:
                seen.add(key)
        out.append(line)
    if not lines:
        out.append("#Minecraft server properties (written by BrainCraft setup_mc_server.py)")
    for key, value in {**(defaults or {}), **updates}.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return changed


def write_eula(path: Path, accept: bool) -> bool:
    """Write eula=true only when accepted. Returns whether the EULA is accepted."""
    if path.exists() and re.search(r"^\s*eula\s*=\s*true\s*$", path.read_text(encoding="utf-8"), re.M | re.I):
        return True
    if not accept:
        return False
    path.write_text(f"#By changing the setting below to TRUE you are indicating your agreement to our EULA ({EULA_URL}).\neula=true\n", encoding="utf-8")
    return True


def _xms(memory: str) -> str:
    """-Xms1G unless -Xmx is smaller."""
    n, unit = int(memory[:-1]), memory[-1].upper()
    mib = n * (1024 if unit == "G" else 1)
    return "1G" if mib >= 1024 else memory


def start_command(java: str, memory: str) -> str:
    j = f'"{java}"' if " " in java else java
    return f"{j} -Xms{_xms(memory)} -Xmx{memory} -jar server.jar nogui"


def write_start_scripts(dest: Path, java: str, memory: str) -> tuple[Path, Path]:
    cmd = start_command(java, memory)
    bat = dest / "start_server.bat"
    sh = dest / "start_server.sh"
    bat_lines = ["@echo off", 'cd /d "%~dp0"', cmd, "pause"]
    sh_lines = ["#!/bin/sh", 'cd "$(dirname "$0")" || exit 1', f"exec {cmd}"]
    with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(bat_lines) + "\n")
    with open(sh, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sh_lines) + "\n")
    try:
        sh.chmod(0o755)
    except OSError:
        pass
    return bat, sh


# --- main ------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Set up the BrainCraft Minecraft server.")
    ap.add_argument("--version", default=DEFAULT_VERSION, help="Minecraft version (default %(default)s)")
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="server folder (default: <repo>/server)")
    ap.add_argument("--accept-eula", action="store_true", help=f"accept the Minecraft EULA ({EULA_URL})")
    ap.add_argument("--rcon-password", default="braincraft")
    ap.add_argument("--rcon-port", type=int, default=25575)
    ap.add_argument("--memory", default="2G", help="max heap, e.g. 2G or 1536M (default %(default)s)")
    ap.add_argument("--jar", type=Path, help="use a manually downloaded server.jar (offline)")
    ap.add_argument("--java", default=None, help="java executable (default: JAVA_HOME/bin/java, else java on PATH)")
    ap.add_argument("--offline", action="store_true", help="do not contact Mojang (use with --jar)")
    ap.add_argument("--skip-java-check", action="store_true")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"\d+[MmGg]", args.memory):
        ap.error("--memory must look like 2G or 1536M")
    return args


def load_metadata(
    version: str, dest: Path, get: HttpGet | None, offline_ok: bool, log: Callable[[str], None]
) -> dict | None:
    """Version JSON from Mojang (cached in dest), or the cache, or None if offline_ok."""
    cache = dest / META_CACHE
    try:
        if get is None:
            raise SetupError("network disabled")
        entry = find_version(get_json(MANIFEST_URL, get), version)
        meta = get_json(entry["url"], get)
        cache.write_text(json.dumps(meta), encoding="utf-8")
        return meta
    except (SetupError, OSError, ValueError, KeyError) as e:
        if "not in the Mojang manifest" in str(e):
            raise
        if cache.is_file():
            meta = json.loads(cache.read_text(encoding="utf-8"))
            if meta.get("id") == version:
                log(f"  Mojang metadata not reachable ({e}); using cached {cache.name}.")
                return meta
        if offline_ok:
            log(f"  Mojang metadata not reachable ({e}); continuing offline with --jar.")
            return None
        raise SetupError(
            f"{e}\n  Cannot reach Mojang. Download server.jar for {version} elsewhere "
            f"(e.g. on a phone hotspot, from {SERVER_PAGE}) and rerun with --jar PATH."
        ) from e


def main(
    argv: list[str] | None = None,
    *,
    get: HttpGet | None = None,
    run_java: RunJava = _run_java,
    log: Callable[[str], None] = print,
) -> int:
    args = parse_args(argv)
    if get is None:
        get = lambda url: http_get(url, log=log)  # noqa: E731
    dest = Path(args.dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    java = args.java or default_java()
    problems: list[str] = []

    try:
        log(f"Minecraft {args.version} server in {dest}")
        meta = load_metadata(
            args.version, dest, None if args.offline else get, offline_ok=args.jar is not None, log=log
        )
        url = sha1 = None
        if meta:
            url, sha1, _ = server_download(meta)
        jar_path = dest / "server.jar"
        status = ensure_jar(jar_path, url, sha1, get, jar=args.jar, log=log)
        log(f"  server.jar: {status}" + (f" (sha1 {sha1})" if sha1 else ""))
    except SetupError as e:
        log(f"ERROR: {e}")
        return 2

    info = jar_version_info(jar_path)
    if info.get("id") and info["id"] != args.version:
        problems.append(f"server.jar is version {info['id']}, not {args.version}. Client and server must match.")
    need = required_java(meta) or info.get("java_version") or guess_java(args.version)
    if not args.skip_java_check:
        ok, _, msg = check_java(java, need, run_java)
        log(f"  {msg}")
        if not ok:
            problems.append(msg.splitlines()[0])

    props = dest / "server.properties"
    changed = merge_properties(
        props, server_properties(args.rcon_password, args.rcon_port), defaults={"server-ip": ""}
    )
    log(f"  server.properties: RCON on port {args.rcon_port}, superflat")
    if "level-type" in changed and (dest / "world").exists():
        problems.append(
            "level-type changed but a world/ folder already exists; delete server/world to get a superflat world."
        )

    eula_ok = write_eula(dest / "eula.txt", args.accept_eula)
    if eula_ok:
        log("  eula.txt: eula=true")
    else:
        log(
            f"  eula.txt: NOT accepted. The server will not start until you agree to the\n"
            f"  Minecraft EULA ({EULA_URL}). Read it, then rerun with --accept-eula\n"
            f"  (or set eula=true in {dest / 'eula.txt'})."
        )
        problems.append("EULA not accepted yet")

    bat, sh = write_start_scripts(dest, java, args.memory)
    log(f"  start scripts: {bat.name}, {sh.name}")

    start = f"{bat}" if os.name == "nt" else f"{sh}"
    log(
        "\nNext steps:\n"
        f"  1. Start the server:  {start}\n"
        f"     Wait for 'RCON running on 0.0.0.0:{args.rcon_port}' and 'Done (...)!'.\n"
        "     Windows Defender firewall prompt: allow Java on private networks only.\n"
        "  2. Set up the world:  poetry run python -m mc.world\n"
        f"  3. Minecraft Launcher -> installation {args.version} -> Multiplayer -> Direct Connection -> localhost\n"
        "  4. Run poetry run python -m mc.world again once you are in (tp, bow, adventure mode).\n"
        "  Stop the server by typing 'stop' in its console."
    )
    if args.rcon_password != "braincraft" or args.rcon_port != 25575:
        log("  Non-default RCON settings: set RCON_PASSWORD / RCON_PORT env vars for the game (mc/config.py).")
    if problems:
        log("\nTo fix before starting:\n" + "\n".join(f"  - {p}" for p in problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
