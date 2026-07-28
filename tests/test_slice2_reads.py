"""Slice 2 (read-only tools) audit: get_all_tasks was the sole English-vs-
Russian outlier among get_projects / get_project / get_project_tasks /
get_task / get_all_tasks / get_tasks_by_priority / get_tasks_due_today /
get_overdue_tasks — every sibling (and the shared _get_project_tasks_by_filter
helper they funnel through) renders in English, but get_all_tasks's v2 branch
was hardcoded in Russian. Locks in the fix: English output, same content."""
import pytest

import ticktick_mcp.src.server as s


class FakeV2:
    """Minimal stand-in for TickTickV2Client: only what get_all_tasks touches."""

    def __init__(self, tasks, project_profiles=None, inbox_id="inbox1"):
        self._tasks = tasks
        self._project_profiles = project_profiles or []
        self._inbox_id = inbox_id

    def get_open_tasks(self):
        return self._tasks

    def get_state(self):
        return {"projectProfiles": self._project_profiles, "inboxId": self._inbox_id}


@pytest.fixture(autouse=True)
def _fake_official_client(monkeypatch):
    # _ensure_official() only needs a truthy `ticktick`; no real network calls
    # happen on the v2 path exercised here.
    monkeypatch.setattr(s, "ticktick", object())


async def test_get_all_tasks_v2_branch_is_english_not_russian(monkeypatch):
    tasks = [
        {"id": "t1", "title": "Позвонить в банк", "projectId": "p1"},
        {"id": "t2", "title": "Купить билеты", "projectId": "p1"},
    ]
    monkeypatch.setattr(
        s, "ticktick_v2",
        FakeV2(tasks, project_profiles=[{"id": "p1", "name": "Работа"}]),
    )

    out = await s.get_all_tasks()

    assert "All open tasks (2):" in out
    assert "Работа (2 tasks)" in out
    # regression guard: the old hardcoded Russian strings must be gone
    assert "Задач не найдено" not in out
    assert "Все открытые задачи" not in out
    assert "задач) ──" not in out
    # task content itself is untouched (only the wrapper text was translated)
    assert "Позвонить в банк" in out
    assert "Купить билеты" in out


async def test_get_all_tasks_v2_branch_empty_is_english(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))

    out = await s.get_all_tasks()

    assert out == "No tasks found."
