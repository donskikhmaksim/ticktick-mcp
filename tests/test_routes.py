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


def test_health_tells_the_truth_about_the_ticktick_connection(client, monkeypatch):
    """Раньше проверялось только НАЛИЧИЕ ключа `ticktick_connected`, а его
    значение — никогда. То есть health мог рапортовать «подключено» при
    мёртвом токене, и тест бы это подтвердил: единственный смысловой бит
    эндпоинта не проверялся. А ради него сервер и остаётся живым, когда
    клиент TickTick поднять не удалось."""
    monkeypatch.setattr(s, "ticktick", None)
    assert client.get("/health").json()["ticktick_connected"] is False

    monkeypatch.setattr(s, "ticktick", object())
    assert client.get("/health").json()["ticktick_connected"] is True
