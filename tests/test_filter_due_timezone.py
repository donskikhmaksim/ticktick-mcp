"""Filter date tokens ("today", "overdue", …) must be resolved in the USER's
timezone, not in the process timezone.

`_due_token_matches()` compared against `date.today()` — the zone the server
process happens to run in (UTC on Railway) — while the rest of the codebase
resolves "today" through `USER_TIMEZONE` (server.py's `_USER_TZ` /
`_today_local()`). The owner is America/Los_Angeles, so for 7-8 hours of every
day the two disagree and a filter with a `dueDate` condition silently answers
for the wrong calendar day.

The test is deliberately built WITHOUT any hard-coded calendar date (a repo
rule: such fixtures rot silently). It pins two zones 26 hours apart —
Pacific/Kiritimati (UTC+14) and Etc/GMT+12 (UTC-12) — whose local dates
therefore differ at EVERY instant, and asserts the answer follows the
configured zone. A process-clock implementation gives the same answer for both
zones, so at least one assertion fails no matter what time the suite runs.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import ticktick_mcp.src.ticktick_v2_client as c

EAST = "Pacific/Kiritimati"  # UTC+14
WEST = "Etc/GMT+12"          # UTC-12  (26 hours behind EAST → always another date)


def _today_in(zone: str):
    return datetime.now(ZoneInfo(zone)).date()


def _task_due(day):
    """An all-day task due on `day` (its date is read verbatim from dueDate)."""
    return {"id": "t1", "title": "x", "dueDate": f"{day.isoformat()}T00:00:00.000+0000"}


def test_zone_fixture_is_self_checking():
    """Guard for the premise: the two zones must never share a calendar date."""
    assert _today_in(EAST) != _today_in(WEST)


def test_today_token_follows_user_timezone(monkeypatch):
    task_east = _task_due(_today_in(EAST))

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(EAST))
    assert c._due_token_matches(task_east, "today") is True

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(WEST))
    assert c._due_token_matches(task_east, "today") is False


def test_today_token_follows_user_timezone_the_other_way(monkeypatch):
    task_west = _task_due(_today_in(WEST))

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(WEST))
    assert c._due_token_matches(task_west, "today") is True

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(EAST))
    assert c._due_token_matches(task_west, "today") is False


def test_overdue_token_follows_user_timezone(monkeypatch):
    """WEST is always at least a day behind EAST: a task due on WEST's today is
    already in the past for EAST, and not yet due for WEST."""
    task_west = _task_due(_today_in(WEST))
    task_west["status"] = 0

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(EAST))
    assert c._due_token_matches(task_west, "overdue") is True

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(WEST))
    assert c._due_token_matches(task_west, "overdue") is False


def test_user_timezone_default_matches_the_server_module(monkeypatch):
    """Both halves of the app must read the same env var, or a filter and a
    due-date tool would disagree about what day it is."""
    import ticktick_mcp.src.server as s

    assert c._USER_TZ.key == s._USER_TZ.key
