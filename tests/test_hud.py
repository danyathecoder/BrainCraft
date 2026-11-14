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
