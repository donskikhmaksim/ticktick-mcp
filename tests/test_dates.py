"""Tests for the robust date parsing and timezone-aware filters — the audit
found the old parser silently dropped tasks whose dueDate wasn't in the one
hard-coded format, and compared against UTC (all-day off-by-one)."""
from datetime import datetime, timedelta, timezone

import ticktick_mcp.src.server as s


class TestParseDatetime:
    def test_full_millis_offset(self):
        dt = s._parse_ticktick_datetime("2026-07-08T10:00:00.000+0000")
        assert dt is not None and dt.tzinfo is not None

    def test_no_millis(self):
        assert s._parse_ticktick_datetime("2026-07-08T10:00:00+0000") is not None

    def test_z_suffix(self):
        dt = s._parse_ticktick_datetime("2026-07-08T10:00:00Z")
        assert dt is not None
        assert dt.utcoffset() == timedelta(0)

    def test_date_only(self):
        assert s._parse_ticktick_datetime("2026-07-08") is not None

    def test_naive_assumed_utc(self):
        dt = s._parse_ticktick_datetime("2026-07-08T10:00:00")
        assert dt is not None and dt.tzinfo is not None

    def test_garbage_returns_none(self):
        assert s._parse_ticktick_datetime("not a date") is None

    def test_empty_and_nonstring(self):
        assert s._parse_ticktick_datetime("") is None
        assert s._parse_ticktick_datetime(None) is None


class TestDueFilters:
    def _task(self, due):
        return {"id": "t1", "title": "x", "dueDate": due}

    def test_due_today(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00+0000")
        assert s._is_task_due_today(self._task(today)) is True

    def test_not_due_today_when_tomorrow(self):
        tm = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT12:00:00+0000")
        assert s._is_task_due_today(self._task(tm)) is False

    def test_due_in_exactly_n_days(self):
        d3 = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT12:00:00+0000")
        assert s._is_task_due_in_days(self._task(d3), 3) is True
        assert s._is_task_due_in_days(self._task(d3), 2) is False

    def test_overdue_past(self):
        past = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT12:00:00+0000")
        assert s._is_task_overdue(self._task(past)) is True

    def test_overdue_future_false(self):
        fut = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT12:00:00+0000")
        assert s._is_task_overdue(self._task(fut)) is False

    def test_no_duedate_never_matches(self):
        assert s._is_task_due_today({"id": "t"}) is False
        assert s._is_task_overdue({"id": "t"}) is False

    def test_unparseable_duedate_does_not_raise(self):
        # The whole point of the fix: a weird format must not throw / drop loudly.
        assert s._is_task_due_today(self._task("2026/07/08 weird")) is False
