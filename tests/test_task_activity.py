"""get_task_activity: the server-level formatting/fallback logic, mocked at
the ticktick_v2 client boundary (the client's own HTTP behaviour for the
real /api/v1/task/activity/{taskId} endpoint is covered by
test_client_v2_activity.py). These tests lock in the fallback behaviour:
when the real endpoint genuinely returns nothing for a task (empty list or
a 404), fall back to the created/modified/completed stamps already on the
task record."""
from zoneinfo import ZoneInfo

import pytest

import ticktick_mcp.src.server as s


class FakeV2:
    """Minimal stand-in for TickTickV2Client, just the bits get_task_activity
    and its fallback touch."""

    def __init__(self, activity_result=None, activity_error=None, task=None,
                 trash=None, inbox_id="inbox1"):
        self._activity_result = activity_result
        self._activity_error = activity_error
        self._task = task
        self._trash = trash or []
        self._inbox_id = inbox_id

    def get_task_activity(self, project_id, task_id):
        if self._activity_error:
            raise self._activity_error
        return self._activity_result or []

    def get_state(self):
        return {
            "inboxId": self._inbox_id,
            "syncTaskBean": {"update": [self._task] if self._task else []},
        }

    def get_trash(self, limit=50):
        return self._trash


def _http_404():
    return RuntimeError(
        "404 Client Error:  for url: https://api.ticktick.com/api/v1/"
        "task/activity/task1")


@pytest.mark.asyncio
async def test_404_falls_back_to_task_stamps(monkeypatch):
    # The stamps are now rendered in the OWNER's zone with the zone named
    # (see tests/test_detail_views_local_date.py for why), so the assertion
    # is on that rendering rather than on the raw stored string. _USER_TZ is
    # pinned to UTC explicitly here so the expected text stays exact and does
    # not silently depend on conftest's default.
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("UTC"))
    task = {"id": "task1", "createdTime": "2026-07-01T10:00:00+0000",
            "modifiedTime": "2026-07-02T11:00:00+0000", "creator": "me"}
    fake = FakeV2(activity_error=_http_404(), task=task, inbox_id="inbox_me")
    monkeypatch.setattr(s, "ticktick_v2", fake)

    out = await s.get_task_activity(task_id="task1", project_id="proj1")

    assert "404" in out
    assert "falling back" in out
    assert "2026-07-01 10:00 (UTC)" in out
    assert "2026-07-02 11:00 (UTC)" in out


@pytest.mark.asyncio
async def test_404_with_unknown_task_has_no_fallback_but_says_so(monkeypatch):
    fake = FakeV2(activity_error=_http_404(), task=None, trash=[])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    out = await s.get_task_activity(task_id="ghost", project_id="proj1")

    assert "Error fetching task activity" in out
    assert "falling back" not in out


@pytest.mark.asyncio
async def test_empty_events_also_offers_fallback(monkeypatch):
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("UTC"))
    task = {"id": "task1", "createdTime": "2026-07-01T10:00:00+0000",
            "creator": "me"}
    fake = FakeV2(activity_result=[], task=task, inbox_id="inbox_me")
    monkeypatch.setattr(s, "ticktick_v2", fake)

    out = await s.get_task_activity(task_id="task1", project_id="proj1")

    assert "No activity found" in out
    assert "What we do know from the task itself" in out
    assert "2026-07-01 10:00 (UTC)" in out


@pytest.mark.asyncio
async def test_successful_events_still_formatted_normally(monkeypatch):
    events = [{"action": "T_TITLE", "when": "2026-07-01T10:00:00+0000",
               "whoProfile": {"isMyself": True}, "title": "New name"}]
    fake = FakeV2(activity_result=events)
    monkeypatch.setattr(s, "ticktick_v2", fake)

    out = await s.get_task_activity(task_id="task1", project_id="proj1")

    assert "renamed" in out
    assert "New name" in out
    assert "404" not in out
