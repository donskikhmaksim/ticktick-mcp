"""Self-service OAuth routes: /health, /setup/{secret}, /auth/accept.
Uses Starlette's TestClient against the FastMCP streamable-http app."""
import pytest
from starlette.testclient import TestClient

import ticktick_mcp.src.server as s

SECRET = "test-secret"  # matches conftest MCP_SECRET


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


def test_setup_wrong_secret_403(client):
    r = client.get("/setup/wrong-secret", follow_redirects=False)
    assert r.status_code == 403


def test_setup_correct_secret_redirects_to_proxy(client):
    r = client.get(f"/setup/{SECRET}", follow_redirects=False)
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert "/start?" in loc
    assert "return_to=" in loc
    assert "secret=" in loc


def test_auth_accept_wrong_secret_403(client):
    r = client.post("/auth/accept", data={"secret": "nope", "access_token": "a"})
    assert r.status_code == 403


def test_auth_accept_missing_token_400(client):
    r = client.post("/auth/accept", data={"secret": SECRET, "access_token": ""})
    assert r.status_code == 400


def test_auth_accept_success_hotswaps(client, monkeypatch):
    # avoid a real TickTick round-trip during initialize_client()
    monkeypatch.setattr(s, "initialize_client", lambda: True)
    r = client.post(
        "/auth/accept",
        data={"secret": SECRET, "access_token": "AT", "refresh_token": "RT"},
    )
    assert r.status_code == 200
    import os
    assert os.environ["TICKTICK_ACCESS_TOKEN"] == "AT"
    assert os.environ["TICKTICK_REFRESH_TOKEN"] == "RT"
