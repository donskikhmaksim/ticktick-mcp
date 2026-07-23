"""Regression tests for the all-day deadline off-by-one (#36).

Principle under test: an all-day / date-only deadline is a ZONE-INDEPENDENT
calendar date. Writing it (client `_normalize_date`) and reading it back
(server `_task_due_local_date` / overdue / due-in-N) must yield the IDENTICAL
calendar date regardless of USER_TIMEZONE's offset sign — no ±1 shift. Timed
deadlines must still convert into the user's local zone.

These tests would FAIL against the old code:
  - old WRITE pinned bare dates to local midnight, so USER_TIMEZONE=Europe/Moscow
    (+03) serialized 2026-07-22 to 2026-07-21T21:00Z → previous calendar day.
  - old READ assumed UTC then .astimezone(_USER_TZ), so a UTC-midnight all-day
    task read under America/Los_Angeles (−07) landed on the previous day.
"""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

import ticktick_mcp.src.server as s
from ticktick_mcp.src.ticktick_client import _normalize_date

ZONES = ["America/Los_Angeles", "Europe/Moscow", "UTC"]


@pytest.mark.parametrize("read_zone", ZONES)
def test_allday_write_then_read_no_shift(read_zone, monkeypatch):
    """Full round-trip: client writes the all-day value, TickTick echoes it back
    with isAllDay=True, server reads it — same calendar date in every zone."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(read_zone))
    value, all_day = _normalize_date("2026-07-22")
    assert all_day is True
    echoed = {"dueDate": value, "isAllDay": True}
    assert s._task_due_local_date(echoed) == date(2026, 7, 22)


@pytest.mark.parametrize("read_zone", ZONES)
def test_allday_stored_forms_read_verbatim(read_zone, monkeypatch):
    """Every shape TickTick is observed to store an all-day date in reads to the
    literal calendar date — never shifted by the reader's offset."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(read_zone))
    forms = [
        {"dueDate": "2026-07-22", "isAllDay": True},                    # bare date
        {"dueDate": "2026-07-22T00:00:00.000+0000", "isAllDay": True},  # UTC midnight
        {"dueDate": "2026-07-22"},                                      # bare, no flag
    ]
    for task in forms:
        assert s._task_due_local_date(task) == date(2026, 7, 22), task


def test_allday_due_today_is_today_not_overdue(monkeypatch):
    """Defect A: an all-day task dated *today* must read as due-today and NOT
    overdue (the old UTC-instant compare made it overdue for most of the day)."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    today = s._today_local()
    value, _ = _normalize_date(today.isoformat())
    task = {"dueDate": value, "isAllDay": True}
    assert s._is_task_due_today(task) is True
    assert s._is_task_overdue(task) is False


def test_allday_yesterday_is_overdue(monkeypatch):
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    yesterday = s._today_local() - timedelta(days=1)
    value, _ = _normalize_date(yesterday.isoformat())
    task = {"dueDate": value, "isAllDay": True}
    assert s._is_task_overdue(task) is True
    assert s._is_task_due_today(task) is False


def test_allday_due_in_exactly_n_days(monkeypatch):
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    target = s._today_local() + timedelta(days=3)
    value, _ = _normalize_date(target.isoformat())
    task = {"dueDate": value, "isAllDay": True}
    assert s._is_task_due_in_days(task, 3) is True
    assert s._is_task_due_in_days(task, 2) is False


def test_timed_deadline_still_converts_by_zone(monkeypatch):
    """Timed deadlines are NOT zone-independent — they carry a clock time and
    must convert into the user's local zone. 02:00Z on 07-22 is the 21st in LA
    (−07) but the 22nd in Moscow (+03)."""
    task = {"dueDate": "2026-07-22T02:00:00.000+0000"}  # no isAllDay
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    assert s._task_due_local_date(task) == date(2026, 7, 21)
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("Europe/Moscow"))
    assert s._task_due_local_date(task) == date(2026, 7, 22)


def test_timed_deadline_overdue_uses_instant(monkeypatch):
    """A timed deadline in the future is not overdue; in the past it is."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    from datetime import datetime, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000+0000")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000+0000")
    assert s._is_task_overdue({"dueDate": future}) is False
    assert s._is_task_overdue({"dueDate": past}) is True
