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
