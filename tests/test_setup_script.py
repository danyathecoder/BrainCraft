"""Offline tests for scripts/setup_mc_server.py (fake Mojang, fake java)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import ssl
import zipfile
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "setup_mc_server.py"
_spec = importlib.util.spec_from_file_location("setup_mc_server", _PATH)
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

VERSION = "26.3"
VERSION_URL = "https://example.invalid/26.3.json"
JAR_URL = "https://example.invalid/server.jar"


def fake_jar(version: str = VERSION, java: int = 25) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("version.json", json.dumps({"id": version, "java_version": java}))
    return buf.getvalue()


JAR = fake_jar()
SHA1 = hashlib.sha1(JAR).hexdigest()
MANIFEST = {
    "latest": {"release": VERSION},
    "versions": [{"id": "26.2", "url": "https://example.invalid/26.2.json"}, {"id": VERSION, "url": VERSION_URL}],
}
VERSION_JSON = {
    "id": VERSION,
    "javaVersion": {"component": "java-runtime-epsilon", "majorVersion": 25},
    "downloads": {"server": {"url": JAR_URL, "sha1": SHA1, "size": len(JAR)}},
}


class FakeNet:
    def __init__(self, jar: bytes = JAR, fail: bool = False):
        self.jar, self.fail, self.calls = jar, fail, []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.fail:
            raise setup.SetupError("network unreachable")
        if url == setup.MANIFEST_URL:
            return json.dumps(MANIFEST).encode()
        if url == VERSION_URL:
            return json.dumps(VERSION_JSON).encode()
        if url == JAR_URL:
            return self.jar
        raise AssertionError(url)


def java25(_java: str) -> str:
    return 'openjdk version "25.0.1" 2025-10-21\nOpenJDK Runtime Environment Temurin-25.0.1+8'


def run(tmp_path, *extra, net=None, java=java25):
    logs: list[str] = []
    net = net or FakeNet()
    code = setup.main(["--dir", str(tmp_path), *extra], get=net, run_java=java, log=logs.append)
    return code, "\n".join(logs), net


# --- java -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, major",
    [
        ('openjdk version "25.0.1" 2025-10-21', 25),
        ('java version "1.8.0_391"\nJava(TM) SE Runtime Environment', 8),
        ('openjdk version "21" 2023-09-19', 21),
        ('openjdk version "17.0.9" 2023-10-17 LTS', 17),
        ("openjdk 25.0.1 2025-10-21", 25),
        ("something else", None),
    ],
)
def test_parse_java_major(text, major):
    assert setup.parse_java_major(text) == major


def test_check_java_too_old_and_missing():
    ok, major, msg = setup.check_java("java", 25, lambda j: 'java version "1.8.0_391"')
    assert not ok and major == 8 and "too old" in msg and "Temurin 25" in msg and "openjdk@25" in msg

    def missing(_):
        raise FileNotFoundError("java")

    ok, major, msg = setup.check_java("java", 25, missing)
    assert not ok and major is None and "not found" in msg

    assert setup.check_java("java", 25, java25)[0]


# --- metadata and jar ------------------------------------------------------


def test_find_version_and_downloads():
    assert setup.find_version(MANIFEST, VERSION)["url"] == VERSION_URL
    with pytest.raises(setup.SetupError, match="latest release: 26.3"):
        setup.find_version(MANIFEST, "99.9")
    assert setup.server_download(VERSION_JSON) == (JAR_URL, SHA1, len(JAR))
    assert setup.required_java(VERSION_JSON) == 25


def test_download_verifies_sha1_and_writes_everything(tmp_path):
    code, logs, net = run(tmp_path, "--accept-eula")
    assert code == 0, logs
    assert (tmp_path / "server.jar").read_bytes() == JAR
    assert JAR_URL in net.calls
    assert "server.jar: downloaded" in logs
    assert "Java 25 OK (needs 25)" in logs
    assert not (tmp_path / "server.jar.part").exists()


def test_skip_download_if_jar_present(tmp_path):
    (tmp_path / "server.jar").write_bytes(JAR)
    code, logs, net = run(tmp_path, "--accept-eula")
    assert code == 0
    assert JAR_URL not in net.calls
    assert "server.jar: present" in logs


def test_wrong_sha1_download_fails(tmp_path):
    code, logs, _ = run(tmp_path, net=FakeNet(jar=b"corrupt"))
    assert code == 2 and "sha1" in logs
    assert not (tmp_path / "server.jar").exists()


def test_stale_jar_is_replaced(tmp_path):
    (tmp_path / "server.jar").write_bytes(b"old version")
    code, logs, net = run(tmp_path, "--accept-eula")
    assert code == 0 and JAR_URL in net.calls
    assert (tmp_path / "server.jar").read_bytes() == JAR


def test_jar_option_copies_and_verifies(tmp_path):
    src = tmp_path / "dl" / "server.jar"
    src.parent.mkdir()
    src.write_bytes(JAR)
    out = tmp_path / "srv"
    code, logs, net = run(out, "--jar", str(src), "--accept-eula")
    assert code == 0, logs
    assert (out / "server.jar").read_bytes() == JAR
    assert JAR_URL not in net.calls

    bad = tmp_path / "dl" / "bad.jar"
    bad.write_bytes(b"nope")
    code, logs, _ = run(tmp_path / "srv2", "--jar", str(bad))
    assert code == 2 and "sha1" in logs


def test_jar_option_offline(tmp_path):
    src = tmp_path / "server.jar.download"
    src.write_bytes(JAR)
    out = tmp_path / "srv"
    code, logs, net = run(out, "--jar", str(src), "--accept-eula", net=FakeNet(fail=True))
    assert code == 0, logs
    assert "continuing offline" in logs
    assert "Java 25 OK (needs 25)" in logs  # java version from the jar's version.json

    code, logs, net = run(out, "--jar", str(src), "--accept-eula", "--offline", net=FakeNet())
    assert code == 0 and net.calls == []


def test_offline_jar_wrong_version_is_flagged(tmp_path):
    src = tmp_path / "old.jar"
    src.write_bytes(fake_jar("26.2"))
    code, logs, _ = run(tmp_path / "srv", "--jar", str(src), "--accept-eula", "--offline")
    assert code == 1 and "server.jar is version 26.2" in logs


def test_metadata_cache_used_offline(tmp_path):
    assert run(tmp_path, "--accept-eula")[0] == 0
    code, logs, _ = run(tmp_path, "--accept-eula", net=FakeNet(fail=True))
    assert code == 0 and "using cached" in logs and "server.jar: present" in logs


def test_no_network_no_jar_explains_hotspot(tmp_path):
    code, logs, _ = run(tmp_path, net=FakeNet(fail=True))
    assert code == 2 and "--jar" in logs


def test_ssl_cert_error_falls_back_to_curl():
    logs: list[str] = []

    def bad_urllib(url):
        raise ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate in certificate chain")

    curl_calls = []

    def fake_curl(url):
        curl_calls.append(url)
        return b"payload"

    assert setup.http_get("https://x.invalid/a", bad_urllib, fake_curl, logs.append) == b"payload"
    assert curl_calls == ["https://x.invalid/a"]
    assert "Install Certificates.command" in logs[0]


def test_ssl_cert_error_wrapped_in_urlerror_falls_back_to_curl():
    import urllib.error

    def bad_urllib(url):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))

    assert setup.http_get("u", bad_urllib, lambda u: b"ok", lambda m: None) == b"ok"


def test_curl_fallback_uses_subprocess(monkeypatch):
    seen = {}

    class Proc:
        returncode, stdout, stderr = 0, b"jar bytes", b""

    def fake_run(cmd, capture_output):
        seen["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(setup.subprocess, "run", fake_run)
    assert setup._curl_get("https://x.invalid/s.jar") == b"jar bytes"
    assert seen["cmd"][0] == "/usr/bin/curl" and "-fsSL" in seen["cmd"]


def test_other_network_errors_do_not_use_curl():
    def timeout(url):
        raise TimeoutError("timed out")

    def curl(url):
        raise AssertionError("curl must not be used")

    with pytest.raises(setup.SetupError, match="timed out"):
        setup.http_get("u", timeout, curl, lambda m: None)


# --- eula, properties, scripts --------------------------------------------


def test_eula_only_with_flag(tmp_path):
    code, logs, _ = run(tmp_path)
    assert code == 1
    assert not (tmp_path / "eula.txt").exists()
    assert setup.EULA_URL in logs and "--accept-eula" in logs

    code, _, _ = run(tmp_path, "--accept-eula")
    assert code == 0
    assert "eula=true" in (tmp_path / "eula.txt").read_text()


def test_existing_accepted_eula_kept(tmp_path):
    (tmp_path / "eula.txt").write_text("eula=TRUE\n")
    assert run(tmp_path)[0] == 0


def test_properties_written(tmp_path):
    run(tmp_path, "--rcon-password", "s3cret", "--rcon-port", "25576")
    text = (tmp_path / "server.properties").read_text()
    props = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    assert props["enable-rcon"] == "true"
    assert props["rcon.port"] == "25576"
    assert props["rcon.password"] == "s3cret"
    assert props["level-type"] == "minecraft\\:flat"
    assert props["broadcast-rcon-to-ops"] == "false"
    assert props["spawn-protection"] == "0"
    assert props["online-mode"] == "true"
    assert props["server-ip"] == ""


def test_properties_merge_keeps_unrelated_keys(tmp_path):
    (tmp_path / "server.properties").write_text(
        "#Minecraft server properties\nmax-players=5\nenable-rcon=false\nserver-ip=127.0.0.1\nmotd=Old\n"
    )
    run(tmp_path, "--accept-eula")
    lines = (tmp_path / "server.properties").read_text().splitlines()
    assert lines[0] == "#Minecraft server properties"
    assert "max-players=5" in lines
    assert "server-ip=127.0.0.1" in lines  # user's choice kept
    assert "enable-rcon=true" in lines and "enable-rcon=false" not in lines
    assert "motd=BrainCraft" in lines
    assert sum(line.startswith("enable-rcon=") for line in lines) == 1


def test_level_type_change_with_existing_world_warns(tmp_path):
    (tmp_path / "server.properties").write_text("level-type=minecraft\\:normal\n")
    (tmp_path / "world").mkdir()
    code, logs, _ = run(tmp_path, "--accept-eula")
    assert code == 1 and "delete server/world" in logs


def test_start_scripts(tmp_path):
    run(tmp_path, "--accept-eula", "--memory", "3G", "--java", "/opt/homebrew/opt/openjdk@25/bin/java")
    bat = (tmp_path / "start_server.bat").read_bytes()
    sh = (tmp_path / "start_server.sh").read_bytes()
    assert b"/opt/homebrew/opt/openjdk@25/bin/java -Xms1G -Xmx3G -jar server.jar nogui" in sh
    assert b"\r\n" in bat and b"\r\n" not in sh
    assert sh.startswith(b"#!/bin/sh\n")
    assert b'cd /d "%~dp0"' in bat and b"-Xmx3G -jar server.jar nogui\r\n" in bat


def test_start_command_quotes_and_small_memory():
    assert setup.start_command(r"C:\Program Files\Java\bin\java.exe", "2G") == (
        r'"C:\Program Files\Java\bin\java.exe" -Xms1G -Xmx2G -jar server.jar nogui'
    )
    assert setup.start_command("java", "512M") == "java -Xms512M -Xmx512M -jar server.jar nogui"


def test_java_too_old_returns_nonzero_but_writes_files(tmp_path):
    code, logs, _ = run(tmp_path, "--accept-eula", java=lambda j: 'java version "1.8.0_391"')
    assert code == 1 and "Java 8 is too old" in logs
    assert (tmp_path / "start_server.sh").exists()


def test_default_java_uses_java_home(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / ("java.exe" if setup.os.name == "nt" else "java")
    exe.parent.mkdir()
    exe.write_text("")
    monkeypatch.setenv("JAVA_HOME", str(tmp_path))
    assert setup.default_java() == str(exe)
    monkeypatch.delenv("JAVA_HOME")
    monkeypatch.setattr(setup, "BREW_JAVA", [])
    assert setup.default_java() == "java"


def test_default_java_finds_keg_only_homebrew(tmp_path, monkeypatch):
    exe = tmp_path / "opt" / "openjdk@25" / "bin" / "java"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(setup, "BREW_JAVA", [str(tmp_path / "missing" / "java"), str(exe)])
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    assert setup.default_java() == str(exe)


def test_bad_memory_rejected(tmp_path):
    with pytest.raises(SystemExit):
        setup.parse_args(["--memory", "lots"])


def test_ssl_context_trusts_certifi_bundle():
    # python.org's macOS Python has no CA certificates of its own; the
    # context must still trust public CAs (Mojang's) via certifi.
    ctx = setup._ssl_context()
    assert ctx.verify_mode == setup.ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.cert_store_stats()["x509_ca"] > 50
