"""2026-08-09 (П9 пакет ТЗ, пункт 7): get_completed_tasks()'s default upper
bound (`to_str=None` → "now") used `datetime.now()` — the PROCESS clock
(UTC on Railway) — while the rest of this file carefully resolves "now"/
"today" through `_USER_TZ` (see its docstring above in
ticktick_v2_client.py). For 7-8 hours of every day (owner is
America/Los_Angeles) this made the upper bound of "recently completed"
disagree with what the owner considers "now".

Same no-hard-coded-date pattern as tests/test_filter_due_timezone.py: two
zones far enough apart (Pacific/Kiritimati UTC+14, Etc/GMT+12 UTC-12) that
their local clocks/dates always differ, so a process-clock implementation
would answer identically for both zones and at least one assertion below
would fail no matter when the suite runs."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import ticktick_mcp.src.ticktick_v2_client as c

EAST = "Pacific/Kiritimati"  # UTC+14
WEST = "Etc/GMT+12"          # UTC-12


def _make_client(monkeypatch, captured):
    client = c.TickTickV2Client(token="fake-token-for-test")

    def _fake_request(method, path, params=None, **kwargs):
        captured["params"] = params
        return {"tasks": []}

    monkeypatch.setattr(client, "_request", _fake_request)
    return client


def test_default_to_bound_follows_user_timezone_not_process_clock(monkeypatch):
    captured = {}
    client = _make_client(monkeypatch, captured)

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(EAST))
    expected_east = datetime.now(ZoneInfo(EAST)).strftime("%Y-%m-%d %H:%M:%S")
    client.get_completed_tasks()
    got_east = captured["params"]["to"]

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(WEST))
    expected_west = datetime.now(ZoneInfo(WEST)).strftime("%Y-%m-%d %H:%M:%S")
    client.get_completed_tasks()
    got_west = captured["params"]["to"]

    # Sanity: the two zones must actually disagree right now, otherwise this
    # test would pass even against a process-clock implementation.
    assert expected_east != expected_west

    # Same call, only _USER_TZ changed — the "to" bound must move with it,
    # each within a couple of seconds of the zone-local wall clock (a small
    # margin absorbs the real time elapsed between the two calls above).
    assert got_east[:16] == expected_east[:16]
    assert got_west[:16] == expected_west[:16]
    assert got_east != got_west


def test_explicit_to_str_is_passed_through_untouched(monkeypatch):
    """Not about the bug — a caller-supplied bound must never be overridden
    by the timezone default."""
    captured = {}
    client = _make_client(monkeypatch, captured)
    fixed = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    client.get_completed_tasks(to_str=fixed)

    assert captured["params"]["to"] == fixed
