"""Tests for report.make_report with a synthetic but realistic session."""

from __future__ import annotations

import csv
import random
from pathlib import Path

from contracts import EVENT_FIELDS, STATE_FIELDS
from report.make_report import NO_EEG_TEXT, PANEL_TITLES, main, make_report

T0 = 1_760_000_000.0
DURATION_S = 6 * 60
REST = (200.0, 230.0)


def _write(path: Path, fields: list[str], rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def _events() -> list[dict]:
    rng = random.Random(7)
    rows: list[dict] = [
        {"t": f"{T0:.3f}", "event": "decision", "value": "mode rehab",
         "reason": "session start"}
    ]
    ecc = 15.0
    t = 2.0
    n = 0
    while t < DURATION_S - 5:
        if REST[0] <= t < REST[1]:
            t = REST[1] + 1
            continue
        n += 1
        side = "L" if n % 3 else "R"
        e = ecc if side == "L" else 20.0
        tid = f"t{n}"
        rows.append({"t": f"{T0 + t:.3f}", "event": "spawn", "target_id": tid,
                     "side": side, "ecc_deg": e})
        # left hit probability improves over the session: 0.3 -> 0.9
        p = 0.95 if side == "R" else 0.3 + 0.6 * t / DURATION_S
        found = 600 + rng.random() * (1500 if side == "L" else 500)
        if rng.random() < p:
            rows.append({"t": f"{T0 + t + found / 1000:.3f}", "event": "found",
                         "target_id": tid, "side": side, "ecc_deg": e, "rt_ms": round(found)})
            rt = found + 400 + rng.random() * 600
            rows.append({"t": f"{T0 + t + rt / 1000:.3f}", "event": "hit", "target_id": tid,
                         "side": side, "ecc_deg": e, "rt_ms": round(rt)})
            if side == "L":
                ecc = min(80.0, ecc + 5)
                rows.append({"t": f"{T0 + t + rt / 1000 + 0.01:.3f}", "event": "decision",
                             "value": f"left_ecc {ecc:.0f}", "reason": "left hit: staircase up"})
        else:
            rows.append({"t": f"{T0 + t + 3.5:.3f}", "event": "miss", "target_id": tid,
                         "side": side, "ecc_deg": e})
            if side == "L":
                ecc = max(15.0, ecc - 5)
        for k in range(4):
            yaw = 180 + (rng.gauss(-10, 30) if k % 2 else rng.gauss(10, 20))
            rows.append({"t": f"{T0 + t + k:.3f}", "event": "yaw", "value": f"{yaw:.1f}"})
        t += 4.0
    rows.append({"t": f"{T0 + REST[0]:.3f}", "event": "rest_start"})
    rows.append({"t": f"{T0 + REST[0]:.3f}", "event": "decision", "value": "rest phase",
                 "reason": "fatigue_z > 1.5 for 20 s"})
    rows.append({"t": f"{T0 + REST[1]:.3f}", "event": "rest_end"})
    rows.sort(key=lambda r: float(r["t"]))
    return rows


def _state() -> list[dict]:
    rows = []
    for s in range(DURATION_S):
        fat = 1.8 if REST[0] - 25 <= s < REST[0] else 0.2 * (s / DURATION_S)
        rows.append({
            "t": T0 + s, "engagement_z": round(0.5 + 0.3 * ((s % 30) / 30), 3),
            "fatigue_z": fat, "alpha_low_z": -0.2, "quality": "ok",
            "artifact": "blink" if s % 47 == 0 else "none", "jaw_clench": False,
        })
    return rows


def _session(tmp_path: Path, with_state: bool = True) -> Path:
    ev = _write(tmp_path / "20261114-120000-test.csv", EVENT_FIELDS, _events())
    if with_state:
        _write(tmp_path / "20261114-120000-test-state.csv", STATE_FIELDS, _state())
    return ev


def test_full_report(tmp_path: Path) -> None:
    ev = _session(tmp_path)
    out = make_report(ev)
    assert out == tmp_path / "20261114-120000-test-report.html"
    html = out.read_text(encoding="utf-8")
    assert len(html) > 1_000_000  # plotly.js inlined -> works offline
    for title in PANEL_TITLES.values():
        assert title in html
    assert "training session report" in html
    assert "6:00 min" in html or "5:5" in html  # session length
    assert "Asymmetry index" in html
    assert "Max left eccentricity" in html
    assert "fatigue_z &gt; 1.5 for 20 s" in html  # decision log, escaped
    assert NO_EEG_TEXT not in html
    assert "Rest phases" in html


def test_summary_numbers(tmp_path: Path) -> None:
    from report.make_report import _load_events, summarize

    ev = _load_events(_session(tmp_path, with_state=False))
    s = summarize(ev, ev["t"].min(), ev["t"].max())
    assert s["rest_phases"] == 1
    assert s["trials"] > 50
    assert s["L"]["rate"] < s["R"]["rate"]
    # left improves -> asymmetry shrinks from first to last third
    assert s["ai_first"] > s["ai_last"]
    assert s["max_left_ecc"] > 15
    assert s["L"]["found_rt"] > s["R"]["found_rt"]
    assert 0 < s["yaw_left_share"] < 1


def test_report_without_state(tmp_path: Path) -> None:
    ev = _session(tmp_path, with_state=False)
    out = make_report(ev, out=tmp_path / "custom.html")
    html = out.read_text(encoding="utf-8")
    assert out.name == "custom.html"
    assert NO_EEG_TEXT in html
    assert PANEL_TITLES["ecc"] in html


def test_almost_empty_sessions(tmp_path: Path) -> None:
    header_only = _write(tmp_path / "empty.csv", EVENT_FIELDS, [])
    assert make_report(header_only).exists()

    tiny = _write(tmp_path / "tiny.csv", EVENT_FIELDS, [
        {"t": T0, "event": "spawn", "target_id": "t1", "side": "L", "ecc_deg": 15},
        {"t": T0, "event": "rest_start"},  # rest never ended
    ])
    _write(tmp_path / "tiny-state.csv", STATE_FIELDS, [])
    html = make_report(tiny).read_text(encoding="utf-8")
    assert NO_EEG_TEXT in html

    zero_bytes = tmp_path / "zero.csv"
    zero_bytes.write_text("", encoding="utf-8")
    assert make_report(zero_bytes).exists()


def test_cli(tmp_path: Path) -> None:
    ev = _session(tmp_path)
    out = main([str(ev), "--out", str(tmp_path / "cli.html")])
    assert out.exists()


def test_signal_quality_section() -> None:
    import pandas as pd

    from report.make_report import signal_quality

    rows = []
    for s in range(100):  # 60 usable, 10 motion, 5 jaw, 5 blink, 20 held
        q, a = ("ok", "none") if s < 60 else ("bad", "none")
        if 60 <= s < 70:
            a = "motion"
        elif 70 <= s < 75:
            a = "jaw"
        elif 75 <= s < 80:
            a = "blink"
        rows.append({"t": T0 + s, "quality": q, "artifact": a})
    sq = signal_quality(pd.DataFrame(rows))
    assert sq["usable_share"] == 0.6
    assert sq["counts"] == {"usable": 60, "held": 20, "blink": 5, "jaw": 5, "motion": 10}
    assert sq["longest_bad_s"] == 40
    assert signal_quality(None) is None


def test_report_shows_signal_quality(tmp_path: Path) -> None:
    html = make_report(_session(tmp_path)).read_text(encoding="utf-8")
    assert PANEL_TITLES["quality"] in html and "Usable EEG" in html
    assert "Good: the director had fresh brain data" in html  # fixture is all "ok"


def test_game_loop_writes_report(tmp_path: Path) -> None:
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "game_loop.py", "--mode", "rehab", "--sim", "--no-service",
         "--duration", "30", "--speed", "0", "--name", "rep", "--log-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "report: " in out.stdout
    assert list(tmp_path.glob("*-rep-report.html"))
