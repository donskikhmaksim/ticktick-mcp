"""TickTickV2Client.get_task_activity: hits the real (DevTools-confirmed)
endpoint GET /api/v1/task/activity/{taskId} — v1, singular "activity", no
projectId in the path. No real network: session.get is stubbed so we can
assert the URL shape, response-shape parsing, pagination via skip/lastId,
and auth/404 handling."""
import pytest

from ticktick_mcp.src.ticktick_v2_client import TickTickAuthError, TickTickV2Client

TASK_ID = "task1"
PROJECT_ID = "proj1"


@pytest.fixture
def client():
    return TickTickV2Client(token="tok")


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        if text is not None:
            self.text = text
        else:
            self.text = "" if json_data is None else "non-empty"
        self._json_data = json_data

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code not in (401, 403, 404):
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


def test_hits_v1_task_activity_path_no_project_id(client, monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return FakeResponse(200, json_data=[])

    monkeypatch.setattr(client.session, "get", fake_get)
    client.get_task_activity(PROJECT_ID, TASK_ID)

    assert len(calls) == 1
    url, params = calls[0]
    assert url == "https://api.ticktick.com/api/v1/task/activity/task1"
    assert "proj1" not in url


def _one_page_then_empty(page):
    """Realistic pagination stub: first call (no params yet) returns `page`,
    every subsequent call (now carrying skip/lastId) returns an empty page
    so the auto-walk loop terminates after one page."""
    def fake_get(url, params=None, timeout=None):
        if not params:
            return FakeResponse(200, json_data=page)
        return FakeResponse(200, json_data=[])
    return fake_get


def test_plain_list_response_returned_as_is(client, monkeypatch):
    events = [{"id": "e1", "action": "T_TITLE"}]
    monkeypatch.setattr(client.session, "get", _one_page_then_empty(events))
    assert client.get_task_activity(PROJECT_ID, TASK_ID) == events


@pytest.mark.parametrize("key", ["items", "activities", "data"])
def test_wrapped_dict_response_unwrapped(client, monkeypatch, key):
    events = [{"id": "e1", "action": "T_TITLE"}]
    monkeypatch.setattr(client.session, "get", _one_page_then_empty({key: events}))
    assert client.get_task_activity(PROJECT_ID, TASK_ID) == events


def test_empty_list_returns_empty(client, monkeypatch):
    monkeypatch.setattr(client.session, "get",
                        lambda url, params=None, timeout=None: FakeResponse(200, json_data=[]))
    assert client.get_task_activity(PROJECT_ID, TASK_ID) == []


def test_404_returns_empty_list_not_raise(client, monkeypatch):
    monkeypatch.setattr(client.session, "get",
                        lambda url, params=None, timeout=None: FakeResponse(404))
    assert client.get_task_activity(PROJECT_ID, TASK_ID) == []


def test_401_raises_auth_error(client, monkeypatch):
    monkeypatch.setattr(client.session, "get",
                        lambda url, params=None, timeout=None: FakeResponse(401))
    with pytest.raises(TickTickAuthError):
        client.get_task_activity(PROJECT_ID, TASK_ID)


def test_automatically_walks_pages_via_skip_and_last_id(client, monkeypatch):
    """First page has 2 events; second page (fetched with skip=2,
    lastId=<id of last event>) has 1 more; third page is empty -> stop."""
    page1 = [{"id": "e1"}, {"id": "e2"}]
    page2 = [{"id": "e3"}]
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        if len(calls) == 1:
            return FakeResponse(200, json_data=page1)
        if len(calls) == 2:
            return FakeResponse(200, json_data=page2)
        return FakeResponse(200, json_data=[])

    monkeypatch.setattr(client.session, "get", fake_get)
    result = client.get_task_activity(PROJECT_ID, TASK_ID)

    assert result == page1 + page2
    assert calls[0] == {}  # first page: no skip/lastId yet
    assert calls[1] == {"skip": 2, "lastId": "e2"}
    assert calls[2] == {"skip": 3, "lastId": "e3"}


def test_explicit_single_page_when_skip_or_last_id_passed(client, monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        return FakeResponse(200, json_data=[{"id": "e5"}])

    monkeypatch.setattr(client.session, "get", fake_get)
    result = client.get_task_activity(PROJECT_ID, TASK_ID, skip=10, last_id="e4")

    assert result == [{"id": "e5"}]
    assert len(calls) == 1  # caller drives pagination themselves
    assert calls[0] == {"skip": 10, "lastId": "e4"}


def test_missing_task_id_raises(client):
    with pytest.raises(ValueError):
        client.get_task_activity(PROJECT_ID, None)
