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


def test_create_task_504_is_NOT_retried_and_surfaces_error(client, monkeypatch):
    """Д4 (2026-08-09): create_task hits the official v1 /task endpoint,
    where the SERVER mints the task id — there is no idempotency key. A 504
    means the client never SAW a response; it does not mean the write never
    landed. If the first attempt actually created the task and only the ack
    was lost, blindly retrying (as П9's "unify the retry set" change did)
    resends the same "create a task" request and TickTick creates a SECOND,
    orphaned task — the ghost-duplicate defect. So a create must fail fast
    on a 504, not retry: exactly one POST is issued, and the caller gets an
    error dict back (not the second response's success)."""
    calls = {"n": 0}
    responses = iter([
        FakeResp(504, text="gateway timeout"),  # first attempt: ack lost
        FakeResp(200, {"id": "second-task-would-be-a-duplicate"}),  # would-be retry
    ])

    def _post(*_a, **_k):
        calls["n"] += 1
        return next(responses)

    monkeypatch.setattr(
        "ticktick_mcp.src.ticktick_client.time.sleep",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("create_task must not sleep/retry on a 504")))
    sess = types.SimpleNamespace(post=_post, get=_post, delete=_post)
    client.session = sess

    result = client.create_task(title="ghost task check", project_id="proj1")

    assert calls["n"] == 1, "create_task retried the write — this is how ghost duplicates happen"
    assert isinstance(result, dict) and "error" in result
    assert result.get("id") != "second-task-would-be-a-duplicate"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_get_retries_on_every_documented_transient_status(client, monkeypatch, status):
    """2026-08-09 (независимый аудит): test_retries_on_502_and_504_before_succeeding
    выше проверяет ТОЛЬКО добавленную пару (502, 504) — сужение всего набора
    ретраев до РОВНО этих двух кодов (`response.status_code not in (502,
    504)` вместо `(429, 500, 502, 503, 504)`) оставило бы тот тест зелёным
    (502 и 504 по-прежнему в множестве), а 429/500/503 молча ушли бы в
    жёсткие ошибки без единой попытки повтора. Здесь — весь документированный
    набор, один код за один прогон, чтобы прогонка любого из них поодиночке
    не могла спрятаться за успехом соседних."""
    monkeypatch.setattr("ticktick_mcp.src.ticktick_client.time.sleep", lambda *_: None)
    responses = iter([FakeResp(status, text="transient"), FakeResp(200, {"ok": 1})])
    sess = types.SimpleNamespace()
    sess.get = lambda *a, **k: next(responses)
    sess.post = lambda *a, **k: next(responses)
    sess.delete = lambda *a, **k: next(responses)
    client.session = sess

    assert client._make_request("GET", "/project") == {"ok": 1}


def test_get_project_still_retries_on_504_like_before(client, monkeypatch):
    """Mirror of the above: reads have no side effect, so the pre-existing
    retry-on-504 behaviour (test_retries_on_502_and_504_before_succeeding)
    must be untouched by the Д4 fix."""
    monkeypatch.setattr("ticktick_mcp.src.ticktick_client.time.sleep", lambda *_: None)
    responses = iter([
        FakeResp(504, text="gateway timeout"),
        FakeResp(200, {"ok": 1}),
    ])
    sess = types.SimpleNamespace()
    sess.get = lambda *a, **k: next(responses)
    sess.post = lambda *a, **k: next(responses)
    sess.delete = lambda *a, **k: next(responses)
    client.session = sess

    assert client.get_projects() == {"ok": 1}


def test_update_task_504_is_retried_since_it_targets_a_known_id(client, monkeypatch):
    """The flip side of the create test: update_task addresses an existing
    task by id, so re-applying the same change on retry is a no-op, not a
    duplicate — it must keep retrying on a 504 exactly like a GET does."""
    monkeypatch.setattr("ticktick_mcp.src.ticktick_client.time.sleep", lambda *_: None)
    calls = {"n": 0}
    responses = iter([
        FakeResp(504, text="gateway timeout"),
        FakeResp(200, {"id": "task1", "title": "renamed"}),
    ])

    def _post(*_a, **_k):
        calls["n"] += 1
        return next(responses)

    sess = types.SimpleNamespace(post=_post, get=_post, delete=_post)
    client.session = sess

    result = client.update_task(task_id="task1", project_id="proj1", title="renamed")

    assert calls["n"] == 2
    assert result == {"id": "task1", "title": "renamed"}
