"""_normalize_date: a bare YYYY-MM-DD is a ZONE-INDEPENDENT all-day date, pinned
to UTC midnight so the calendar date part survives regardless of the account's
offset sign (#36). Datetimes pass through untouched."""
import pytest

from ticktick_mcp.src.ticktick_client import _normalize_date


def test_date_only_becomes_all_day_utc_midnight():
    value, is_all_day = _normalize_date("2026-07-08")
    assert is_all_day is True
    # UTC midnight — date part verbatim, zero offset never rolls the day.
    assert value == "2026-07-08T00:00:00.000+0000"


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "Europe/Moscow", "UTC"])
def test_date_only_is_zone_independent(zone, monkeypatch):
    # The write no longer depends on USER_TIMEZONE at all: whatever the env,
    # a bare date normalizes to the SAME UTC-midnight value (no ±1 by zone).
    monkeypatch.setenv("USER_TIMEZONE", zone)
    value, is_all_day = _normalize_date("2026-07-08")
    assert is_all_day is True
    assert value == "2026-07-08T00:00:00.000+0000"


def test_datetime_passes_through():
    value, is_all_day = _normalize_date("2026-07-08T15:30:00+0300")
    assert is_all_day is False
    assert value == "2026-07-08T15:30:00+0300"


def test_none_passes_through():
    value, is_all_day = _normalize_date(None)
    assert value is None
    assert is_all_day is False
