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


# --- Positive-offset self-hoster hardening (adversarial-review findings) ---
#
# FINDING 1: `_all_day_date` used to read dueDate[:10] verbatim with no regard
# for the task's own `timeZone` field. That's safe for the owner's negative-
# offset America/Los_Angeles account (verbatim already agrees with every
# plausible stored form), but NOT for a positive-offset self-hoster: a Moscow
# (+03) all-day task can be stored as local-midnight-expressed-in-UTC, i.e.
# one calendar day EARLIER than the intended date, and [:10] alone reads that
# wrong day. FINDING 2: the vendored OpenAPI example itself pairs an all-day
# dueDate with an explicit `timeZone`, and a verbatim read disagrees with a
# zone-aware read of that exact documented shape — proof the "verbatim is
# always safe" assumption is offset-sign-dependent, not a hypothetical.
#
# The fix: when a task carries its own `timeZone`, `_all_day_date` treats
# dueDate as a UTC instant and converts it into THAT zone (not `_USER_TZ`)
# before taking the date. Absent (or unrecognized) `timeZone`, it still falls
# back to the verbatim read, so nothing changes for the owner's LA tasks
# (which are echoed back with no `timeZone` field on the observed shapes,
# per the fixtures above).

def test_allday_moscow_positive_offset_reads_via_task_timezone(monkeypatch):
    """FINDING 1, closed: a Moscow (+03) all-day task stored as local-midnight
    expressed in UTC (day D in Moscow -> "<D-1>T21:00:00.000+0000") must read
    back as day D when the task carries timeZone=Europe/Moscow — NOT day D-1,
    which is what dueDate[:10] verbatim gives. USER_TIMEZONE (_USER_TZ) is
    irrelevant here: each task carries its own zone. Uses a dynamic "tomorrow"
    (rather than a fixed date) so the overdue check is unambiguous regardless
    of when the suite runs."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    target = s._today_local() + timedelta(days=1)
    prev_day = target - timedelta(days=1)
    due = f"{prev_day.isoformat()}T21:00:00.000+0000"
    task = {"dueDate": due, "isAllDay": True, "timeZone": "Europe/Moscow"}
    # Before the fix: naive verbatim reads the wrong (previous) day.
    assert s._all_day_date(due) == prev_day
    # After the fix, consulting the task's own timeZone gives the right day.
    assert s._task_due_local_date(task) == target
    assert s._is_task_overdue(task) is False


def test_allday_moscow_missing_timezone_falls_back_to_verbatim(monkeypatch):
    """If `timeZone` is absent, the same stored value has no zone to convert
    with, so we fall back to the (documented-safe-for-that-case) verbatim
    read rather than guessing a zone."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    task = {"dueDate": "2026-07-21T21:00:00.000+0000", "isAllDay": True}
    assert s._task_due_local_date(task) == date(2026, 7, 21)


def test_allday_unrecognized_timezone_falls_back_to_verbatim(monkeypatch):
    """A garbage/unknown IANA name must not raise — fall back to verbatim."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    task = {
        "dueDate": "2026-07-21T21:00:00.000+0000",
        "isAllDay": True,
        "timeZone": "Not/ARealZone",
    }
    assert s._task_due_local_date(task) == date(2026, 7, 21)


def test_allday_openapi_documented_shape_verbatim_vs_zone_aware_disagree():
    """FINDING 2, closed: the vendored OpenAPI example (ticktick-openapi.md)
    documents isAllDay=true with dueDate="2019-11-14T03:00:00+0000" and
    timeZone="America/Los_Angeles" together. Verbatim [:10] and a timeZone-
    aware read of this exact documented shape give DIFFERENT calendar dates —
    proof that "take dueDate[:10] verbatim" is not a universally safe
    principle, only an offset-sign-dependent one. The fixed `_all_day_date`
    now takes the zone-aware date whenever `timeZone` is present."""
    due = "2019-11-14T03:00:00+0000"
    verbatim = date.fromisoformat(due[:10])
    assert verbatim == date(2019, 11, 14)
    zone_aware = s._all_day_date(due, "America/Los_Angeles")
    assert zone_aware == date(2019, 11, 13)
    assert zone_aware != verbatim

    task = {"dueDate": due, "isAllDay": True, "timeZone": "America/Los_Angeles"}
    assert s._task_due_local_date(task) == zone_aware


def test_allday_write_side_ignores_positive_offset_user_timezone(monkeypatch):
    """The WRITE side (`_normalize_date`) is already zone-agnostic: it anchors
    a bare all-day date at UTC midnight regardless of USER_TIMEZONE, so it
    needs no further change for this finding. Confirm with a positive-offset
    USER_TIMEZONE that the produced value is identical to the negative-offset
    (owner) case — the function doesn't even read the env var."""
    monkeypatch.setenv("USER_TIMEZONE", "Europe/Moscow")
    value, all_day = _normalize_date("2026-07-22")
    assert all_day is True
    assert value == "2026-07-22T00:00:00.000+0000"
