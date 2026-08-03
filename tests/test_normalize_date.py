"""_normalize_date: a bare YYYY-MM-DD is a ZONE-INDEPENDENT all-day date, pinned
to UTC NOON so the calendar date part survives regardless of the account's
offset sign — for BOTH directions, not just positive offsets (#36 fixed
midnight for positive-offset accounts; noon additionally protects
negative-offset accounts against a third-party renderer, e.g. TickTick's own
app, converting the stored instant into the account's own timezone before
display). Datetimes pass through untouched."""
import pytest

from ticktick_mcp.src.ticktick_client import _normalize_date


def test_date_only_becomes_all_day_utc_noon():
    value, is_all_day = _normalize_date("2026-07-08")
    assert is_all_day is True
    assert value == "2026-07-08T12:00:00.000+0000"


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "Europe/Moscow", "UTC"])
def test_date_only_is_zone_independent(zone, monkeypatch):
    # The write no longer depends on USER_TIMEZONE at all: whatever the env,
    # a bare date normalizes to the SAME UTC-noon value (no ±1 by zone).
    monkeypatch.setenv("USER_TIMEZONE", zone)
    value, is_all_day = _normalize_date("2026-07-08")
    assert is_all_day is True
    assert value == "2026-07-08T12:00:00.000+0000"


@pytest.mark.parametrize("offset_hours", [-12, -11, -8, -7, -3, 0, 3, 5, 9, 11])
def test_noon_utc_never_crosses_a_calendar_day_in_any_real_offset(offset_hours):
    """The actual point of anchoring at noon: whichever way a THIRD PARTY
    (e.g. TickTick's own app, using the account's own timezone setting —
    outside our control) converts this instant, it must land on the SAME
    calendar date. Noon UTC is exact for -12..+11 (Baker Island through just
    short of New Zealand) — every populated zone that plausibly matters here
    (Moscow +03, US Pacific -07/-08). It is NOT airtight at the extreme
    eastern edge (+12..+14: NZ/Fiji/Kiribati) — no single anchor can survive
    both -12 and +14 at once (a 26h span > 24h) — but that's a strict
    improvement over the old midnight anchor, which failed at ANY positive
    offset at all, including Moscow's own +03."""
    from datetime import datetime, timedelta, timezone
    value, _ = _normalize_date("2026-07-08")
    utc_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    shifted = utc_dt.astimezone(timezone(timedelta(hours=offset_hours)))
    assert shifted.date().isoformat() == "2026-07-08"


def test_datetime_passes_through():
    value, is_all_day = _normalize_date("2026-07-08T15:30:00+0300")
    assert is_all_day is False
    assert value == "2026-07-08T15:30:00+0300"


def test_none_passes_through():
    value, is_all_day = _normalize_date(None)
    assert value is None
    assert is_all_day is False
