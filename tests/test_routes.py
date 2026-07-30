"""HTTP routes: /health only.
Uses Starlette's TestClient against the FastMCP streamable-http app."""
import pytest
from starlette.testclient import TestClient

import ticktick_mcp.src.server as s


@pytest.fixture
def client():
    app = s.mcp.streamable_http_app()
    return TestClient(app)


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "ticktick_connected" in body


def test_health_reports_version_and_commit(client):
    """PLAN_retrofit.md §16.11 / references/testing-deploy.md §14: /health
    must distinguish "feature doesn't work" from "build never landed" — so
    it needs a version/commit field, not just a bare status ping."""
    r = client.get("/health")
    body = r.json()
    assert "version" in body and body["version"]
    assert "commit" in body and body["commit"]


def test_health_commit_falls_back_to_unknown_off_railway(client, monkeypatch):
    monkeypatch.setattr(s, "_APP_COMMIT", "unknown")
    r = client.get("/health")
    assert r.json()["commit"] == "unknown"
