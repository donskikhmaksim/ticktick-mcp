"""_normalize_date: bare YYYY-MM-DD becomes local-midnight all-day; datetimes
pass through untouched."""
from ticktick_mcp.src.ticktick_client import _normalize_date


def test_date_only_becomes_all_day_local_midnight():
    value, is_all_day = _normalize_date("2026-07-08")
    assert is_all_day is True
    assert value.startswith("2026-07-08T00:00:00")
    # UTC test env -> +0000
    assert value.endswith("+0000")


def test_datetime_passes_through():
    value, is_all_day = _normalize_date("2026-07-08T15:30:00+0300")
    assert is_all_day is False
    assert value == "2026-07-08T15:30:00+0300"


def test_none_passes_through():
    value, is_all_day = _normalize_date(None)
    assert value is None
    assert is_all_day is False
