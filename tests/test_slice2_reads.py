"""Slice 2 (read-only tools): get_all_tasks and its siblings under package 2
of the STANDARD.md retrofit (docs/PLAN_retrofit.md §1 item 1 — confirmed
decision to translate the whole read layer to Russian). Locks in the
Russian wrapper text for get_all_tasks's v2 branch; task content itself
(titles etc.) is untouched — only the wrapper text changed language."""
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


async def test_get_all_tasks_v2_branch_is_russian(monkeypatch):
    tasks = [
        {"id": "t1", "title": "Позвонить в банк", "projectId": "p1"},
        {"id": "t2", "title": "Купить билеты", "projectId": "p1"},
    ]
    monkeypatch.setattr(
        s, "ticktick_v2",
        FakeV2(tasks, project_profiles=[{"id": "p1", "name": "Работа"}]),
    )

    out = await s.get_all_tasks()

    assert "Все открытые задачи (2):" in out
    assert "Работа (2 задач) ──" in out
    # regression guard: the old hardcoded English strings must be gone
    assert "All open tasks" not in out
    assert "tasks) ──" not in out
    # task content itself is untouched (only the wrapper text was translated)
    assert "Позвонить в банк" in out
    assert "Купить билеты" in out


async def test_get_all_tasks_v2_branch_empty_is_russian(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))

    out = await s.get_all_tasks()

    assert out == "Задачи не найдены."
