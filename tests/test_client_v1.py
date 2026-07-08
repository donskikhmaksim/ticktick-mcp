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
