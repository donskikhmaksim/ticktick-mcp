"""Package 3 (retrofit): shared filter dispatcher `_get_project_tasks_by_filter`
and the eight date/priority read methods that funnel through it.

Covers:
1. The fallback (no-v2, official API) branch is no longer unbounded — it now
   caps rendered task cards and reports an honest "показано N из M" note
   instead of silently building an ever-growing string (§10.1).
2. The fallback branch's blocking client calls (`get_projects`,
   `get_project_with_data`) run off the event loop via `_run_blocking` — the
   helper itself is now a coroutine, and callers must `await` it.
3. Russian-language wrapper text for the eight tool methods (translation
   requirement of package 3), while task titles/content pass through
   untouched.
"""
import asyncio

import pytest

import ticktick_mcp.src.server as s


class FakeOfficial:
    """Minimal stand-in for the synchronous official-API TickTick client."""

    def __init__(self, projects, tasks_by_project):
        self._projects = projects
        self._tasks_by_project = tasks_by_project

    def get_projects(self):
        return self._projects

    def get_project_with_data(self, project_id):
        return {"tasks": self._tasks_by_project.get(project_id, [])}


@pytest.fixture(autouse=True)
def _no_v2(monkeypatch):
    # Force the fallback (official-API) branch for every test in this file.
    monkeypatch.setattr(s, "ticktick_v2", None)


def _task(tid, title, priority=5):
    return {"id": tid, "title": title, "priority": priority, "projectId": "p1"}


async def test_fallback_branch_is_a_coroutine_and_awaited(monkeypatch):
    fake = FakeOfficial(
        projects=[{"id": "p1", "name": "Work"}],
        tasks_by_project={"p1": [_task("t1", "Позвонить в банк")]},
    )
    monkeypatch.setattr(s, "ticktick", fake)

    coro = s._get_project_tasks_by_filter(lambda t: True, "все")
    assert asyncio.iscoroutine(coro)
    out = await coro
    assert "Позвонить в банк" in out


async def test_fallback_honest_truncation_when_cap_exceeded(monkeypatch):
    tasks = [_task(f"t{i}", f"Пункт{i}") for i in range(5)]
    fake = FakeOfficial(
        projects=[{"id": "p1", "name": "Work"}],
        tasks_by_project={"p1": tasks},
    )
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_FILTER_FALLBACK_TASK_CAP", 2)

    out = await s._get_project_tasks_by_filter(lambda t: True, "все")

    assert "Пункт0" in out
    assert "Пункт1" in out
    assert "Пункт2" not in out
    assert "Показано 2 из 5" in out
    assert "⚠️" in out


async def test_fallback_no_truncation_note_under_cap(monkeypatch):
    tasks = [_task("t1", "Единственная задача")]
    fake = FakeOfficial(
        projects=[{"id": "p1", "name": "Work"}],
        tasks_by_project={"p1": tasks},
    )
    monkeypatch.setattr(s, "ticktick", fake)

    out = await s._get_project_tasks_by_filter(lambda t: True, "все")

    assert "Единственная задача" in out
    assert "Показано" not in out
    assert "⚠️" not in out


async def test_fallback_no_projects_is_russian(monkeypatch):
    fake = FakeOfficial(projects=[], tasks_by_project={})
    monkeypatch.setattr(s, "ticktick", fake)

    out = await s._get_project_tasks_by_filter(lambda t: True, "все")

    assert out == "Проекты не найдены."


class TestEightMethodsAreRussianAndAwaitTheHelper:
    """Smoke coverage for the eight tool methods package 3 owns: they must
    await the (now-async) shared helper and render Russian wrapper text."""

    async def test_get_tasks_by_priority(self, monkeypatch):
        fake = FakeOfficial(
            projects=[{"id": "p1", "name": "Work"}],
            tasks_by_project={"p1": [_task("t1", "Высокий приоритет", priority=5)]},
        )
        monkeypatch.setattr(s, "ticktick", fake)
        out = await s.get_tasks_by_priority(5)
        assert "Высокий приоритет" in out
        assert "приоритет" in out

    async def test_get_tasks_due_today_no_matches_is_russian(self, monkeypatch):
        # v2 branch: cleanest way to hit the "nothing matched" early return.
        class FakeV2:
            def get_state(self):
                return {"syncTaskBean": {"update": []}}

        monkeypatch.setattr(s, "ticktick", object())
        monkeypatch.setattr(s, "ticktick_v2", FakeV2())
        out = await s.get_tasks_due_today()
        assert "не найдены" in out

    async def test_get_tasks_due_in_days_rejects_negative(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick", FakeOfficial(projects=[], tasks_by_project={}))
        out = await s.get_tasks_due_in_days(-1)
        assert "неотрицательным" in out

    async def test_get_engaged_tasks_is_russian_wrapper(self, monkeypatch):
        fake = FakeOfficial(
            projects=[{"id": "p1", "name": "Work"}],
            tasks_by_project={"p1": [_task("t1", "Срочная задача", priority=5)]},
        )
        monkeypatch.setattr(s, "ticktick", fake)
        out = await s.get_engaged_tasks()
        assert "Срочная задача" in out
        assert "в работе" in out

    async def test_get_next_tasks_is_russian_wrapper(self, monkeypatch):
        fake = FakeOfficial(
            projects=[{"id": "p1", "name": "Work"}],
            tasks_by_project={"p1": [_task("t1", "Средний приоритет", priority=3)]},
        )
        monkeypatch.setattr(s, "ticktick", fake)
        out = await s.get_next_tasks()
        assert "Средний приоритет" in out
        assert "следующее" in out
