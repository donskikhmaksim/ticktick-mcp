"""Slice 3 (due-date / search / recurring / GTD reads) audit: get_tasks_due_tomorrow,
get_tasks_due_in_days, get_tasks_due_this_week, search_tasks, get_recurring_tasks,
get_engaged_tasks, get_next_tasks, get_completed_tasks.

Two findings fixed here:

1. Language: retrofit package 4 (docs/PLAN_retrofit.md §4.4) mandates Russian
   for every user-visible response of search_tasks/get_recurring_tasks/
   get_completed_tasks (STANDARD.md references/output-format.md §7.4 — "весь
   текст, видимый Максиму... по-русски. Без исключений."). This supersedes
   the earlier English-canon fix that used to live here; get_recurring_tasks,
   search_tasks and get_completed_tasks are now Russian. The other siblings
   in this file (get_tasks_due_tomorrow/_in_days/_this_week, get_engaged_tasks,
   get_next_tasks — and the shared _get_project_tasks_by_filter they funnel
   through) are out of package 4's scope and stay English until their own
   owning package translates them.

2. Docstring honesty: get_tasks_due_this_week's filter is
   `today <= d <= today + 7 days`, i.e. TODAY plus the following 7 calendar
   days — 8 days inclusive, not a strict "next 7 days" window. Docstring
   updated to describe the actual boundary; behavior unchanged.

Also locks in the #36 all-day-date fix's consistency: every due-date filter
in this group (_is_task_due_in_days, and the this-week range check) goes
through the same zone-safe `_task_due_local_date`/`_is_task_due_in_days`
helpers already covered end-to-end in test_allday_roundtrip.py — no
per-method duplicate/divergent date logic.
"""
from datetime import timedelta

import pytest

import ticktick_mcp.src.server as s


class FakeV2:
    """Minimal stand-in for TickTickV2Client covering what this slice touches."""

    def __init__(self, tasks):
        self._tasks = tasks

    def get_open_tasks(self):
        return self._tasks

    def get_state(self):
        return {"syncTaskBean": {"update": self._tasks}}

    def get_completed_tasks(self, limit=50):
        return self._tasks


@pytest.fixture(autouse=True)
def _fake_official_client(monkeypatch):
    # _ensure_official()/_ensure_ready() only need a truthy client; no real
    # network calls happen on the v2 branches exercised here.
    monkeypatch.setattr(s, "ticktick", object())


def _all_day_task(tid, title, offset_days, repeat=False):
    value, _ = __import__(
        "ticktick_mcp.src.ticktick_client", fromlist=["_normalize_date"]
    )._normalize_date((s._today_local() + timedelta(days=offset_days)).isoformat())
    task = {"id": tid, "title": title, "dueDate": value, "isAllDay": True,
            "projectId": "p1"}
    if repeat:
        task["repeatFlag"] = "RRULE:FREQ=DAILY"
    return task


class TestRecurringTasksIsRussian:
    """get_recurring_tasks is Russian per §4.4 of the retrofit plan (package 4)."""

    async def test_recurring_tasks_v2_branch_is_russian(self, monkeypatch):
        tasks = [
            {"id": "t1", "title": "Медитация", "repeatFlag": "RRULE:FREQ=DAILY",
             "projectId": "p1"},
            {"id": "t2", "title": "Разовая задача", "projectId": "p1"},
        ]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))

        out = await s.get_recurring_tasks()

        assert "Повторяющиеся задачи (1):" in out
        assert "Медитация" in out
        # regression guard: the old English strings must be gone
        assert "Recurring tasks" not in out

    async def test_recurring_tasks_empty_is_russian(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))

        out = await s.get_recurring_tasks()

        assert out == "Повторяющихся задач не найдено."

    async def test_recurring_tasks_search_term_empty_is_russian(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))

        out = await s.get_recurring_tasks(search_term="никогда")

        assert out == "Повторяющиеся задачи, соответствующие «никогда», не найдены."


class TestDueThisWeekBoundary:
    """The filter is inclusive of today AND today+7 (8 calendar days total).
    This is a real off-by-one relative to a literal "next 7 days" reading, so
    the docstring — not the behavior — was corrected to match the code."""

    async def test_boundary_today_and_plus7_included_plus8_excluded(self, monkeypatch):
        tasks = [
            _all_day_task("t0", "today", 0),
            _all_day_task("t7", "plus7", 7),
            _all_day_task("t8", "plus8", 8),
        ]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))

        out = await s.get_tasks_due_this_week()

        assert "today" in out
        assert "plus7" in out
        assert "plus8" not in out

    def test_docstring_describes_actual_8day_window(self):
        doc = s.get_tasks_due_this_week.__doc__ or ""
        assert "8" in doc or "through 7 days" in doc


class TestAllDayFixAppliedConsistently:
    """Every due-date filter in this slice must treat an all-day deadline as a
    zone-independent calendar date (fix #36) — not just one of them."""

    async def test_due_tomorrow_matches_allday_task_due_tomorrow(self, monkeypatch):
        tasks = [_all_day_task("t1", "AllDay tomorrow", 1)]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))
        out = await s.get_tasks_due_tomorrow()
        assert "AllDay tomorrow" in out

    async def test_due_in_days_matches_allday_task(self, monkeypatch):
        tasks = [_all_day_task("t1", "AllDay in 3", 3)]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))
        out = await s.get_tasks_due_in_days(3)
        assert "AllDay in 3" in out

    async def test_engaged_tasks_allday_due_today_counts_and_not_double_overdue(self, monkeypatch):
        tasks = [_all_day_task("t1", "AllDay today", 0)]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))
        out = await s.get_engaged_tasks()
        assert "AllDay today" in out

    async def test_next_tasks_allday_due_tomorrow(self, monkeypatch):
        tasks = [_all_day_task("t1", "AllDay next", 1)]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))
        out = await s.get_next_tasks()
        assert "AllDay next" in out


class TestSearchAndCompletedSmoke:
    """Cheap smoke coverage for the two methods in this slice that don't touch
    due-date logic at all (search by text, completed-tasks listing)."""

    async def test_search_tasks_v2_branch(self, monkeypatch):
        tasks = [{"id": "t1", "title": "Позвонить в банк", "projectId": "p1"}]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))
        out = await s.search_tasks("банк")
        assert "Задачи, соответствующие «банк» (1):" in out

    async def test_search_tasks_rejects_blank_term(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))
        out = await s.search_tasks("   ")
        assert out == "Строка поиска не может быть пустой."

    async def test_get_completed_tasks_v2(self, monkeypatch):
        tasks = [{"id": "t1", "title": "Done thing"}]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))
        out = await s.get_completed_tasks(limit=10)
        assert "Завершённые задачи (1):" in out
        assert "Done thing" in out
