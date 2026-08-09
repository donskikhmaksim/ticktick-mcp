"""v1 client error conventions + the non-JSON guard the audit flagged.
No real network: the requests.Session is monkeypatched."""
import types

import pytest

from ticktick_mcp.src.ticktick_client import TickTickClient


class FakeResp:
    def __init__(self, status=200, json_data=None, text="{}", raise_json=False):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code}")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TICKTICK_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("TICKTICK_CLIENT_ID", "cid")
    monkeypatch.setenv("TICKTICK_CLIENT_SECRET", "sec")
    return TickTickClient()


def _session_returning(resp):
    sess = types.SimpleNamespace()
    sess.get = lambda *a, **k: resp
    sess.post = lambda *a, **k: resp
    sess.delete = lambda *a, **k: resp
    return sess


def test_success_returns_parsed_json(client):
    client.session = _session_returning(FakeResp(200, {"ok": 1}))
    assert client.get_projects() == {"ok": 1}


def test_non_json_200_returns_error_dict_not_exception(client):
    # The core audit fix: a 200 with an HTML body must NOT raise JSONDecodeError.
    client.session = _session_returning(FakeResp(200, text="<html>nope</html>", raise_json=True))
    result = client._make_request("GET", "/project")
    assert isinstance(result, dict) and "error" in result


def test_http_error_becomes_error_dict(client):
    client.session = _session_returning(FakeResp(500, text="boom"))
    result = client._make_request("GET", "/project")
    assert isinstance(result, dict) and "error" in result


def test_204_returns_empty_dict(client):
    client.session = _session_returning(FakeResp(204, text=""))
    assert client._make_request("DELETE", "/project/x") == {}


def test_bad_method_raises(client):
    with pytest.raises(ValueError):
        client._make_request("PATCH", "/project")


def test_retries_on_502_and_504_before_succeeding(client, monkeypatch):
    """2026-08-09 (П9 пакет ТЗ, пункт 8): this channel's retry loop only
    covered (429, 500, 503), while ticktick_v2_client.py's _request also
    retries 502/504 (transient Cloudflare-proxy errors — see its own
    comment) — the two channels had silently drifted apart. Unified here to
    the fuller set, so a 502/504 blip on THIS channel now also clears on
    retry instead of surfacing as a hard error."""
    monkeypatch.setattr("ticktick_mcp.src.ticktick_client.time.sleep", lambda *_: None)
    responses = iter([
        FakeResp(502, text="bad gateway"),
        FakeResp(504, text="gateway timeout"),
        FakeResp(200, {"ok": 1}),
    ])
    sess = types.SimpleNamespace()
    sess.get = lambda *a, **k: next(responses)
    sess.post = lambda *a, **k: next(responses)
    sess.delete = lambda *a, **k: next(responses)
    client.session = sess

    assert client._make_request("GET", "/project") == {"ok": 1}


def test_does_not_retry_on_plain_404(client, monkeypatch):
    """Not every error is retried — a client error like 404 must surface
    immediately as an error dict, not burn two retries first."""
    calls = {"n": 0}

    def _get(*_a, **_k):
        calls["n"] += 1
        return FakeResp(404, text="not found")

    monkeypatch.setattr("ticktick_mcp.src.ticktick_client.time.sleep",
                         lambda *_: (_ for _ in ()).throw(AssertionError("should not sleep/retry on 404")))
    sess = types.SimpleNamespace(get=_get, post=_get, delete=_get)
    client.session = sess

    result = client._make_request("GET", "/project")
    assert isinstance(result, dict) and "error" in result
    assert calls["n"] == 1
