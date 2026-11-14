"""The HUD page is served at / and talks to the service endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

import state_service


def test_hud_served_and_uses_endpoints() -> None:
    client = TestClient(state_service.app)
    r = client.get("/")
    assert r.status_code == 200
    page = r.text
    assert "BrainCraft HUD" in page
    for endpoint in ("/state", "/status", "/decisions?limit=15", "/mode", "/mock"):
        assert endpoint in page
    # offline: no external scripts, stylesheets or fonts
    assert "http://" not in page and "https://" not in page
    assert "<canvas" in page and "REST PHASE" in page and "STALE" in page


def test_raw_unavailable_in_mock_mode(monkeypatch) -> None:
    monkeypatch.setattr(state_service, "SOURCE", "mock")
    r = TestClient(state_service.app).get("/raw").json()
    assert r["available"] is False and "eeg" in r["reason"].lower()
    assert "/raw?seconds=5" in TestClient(state_service.app).get("/").text


def test_raw_serves_filtered_eeg(monkeypatch) -> None:
    from eeg.stream import EEGStream

    stream = EEGStream("synthetic", seed=1).start()
    try:
        assert stream.wait_for_samples(int(6 * stream.fs), timeout=5)
        monkeypatch.setattr(state_service, "SOURCE", "eeg")
        monkeypatch.setattr(state_service, "_stream", stream)
        r = TestClient(state_service.app).get("/raw?seconds=4").json()
    finally:
        stream.stop()
    assert r["available"] is True
    assert r["channels"][0] == "Fz" and len(r["data"]) == 8
    assert len(r["data"][0]) == int(4 * r["fs"])  # 125 Hz after decimation
    assert all(f in ("ok", "flat", "noisy") for f in r["flags"])
    assert 0 < max(r["rms"]) < 1000
