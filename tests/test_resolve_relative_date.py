"""Bug (reported repeatedly, off by exactly -1 day on many tasks at once):
in a long-running chat the calling model's own sense of "today" can drift a
day behind the real wall clock (stale context) — it then computes "today" as
yesterday's date and silently writes that into every task for the rest of
that conversation. Nothing server-side could detect this after the fact,
because the server just stored whatever literal date string it was given.

Fix: the server itself resolves "today"/"tomorrow"/"yesterday" (RU too) off
its OWN real clock (_today_local(), USER_TIMEZONE-aware) instead of trusting
the caller to have already computed the right date. An already-literal date
passes through unchanged (idempotent)."""
from datetime import date

import ticktick_mcp.src.server as s


class TestResolveRelativeDate:
    def _fix_today(self, monkeypatch, d: date):
        monkeypatch.setattr(s, "_today_local", lambda: d)

    def test_today_variants(self, monkeypatch):
        self._fix_today(monkeypatch, date(2026, 7, 13))
        assert s._resolve_relative_date("today") == "2026-07-13"
        assert s._resolve_relative_date("Today") == "2026-07-13"
        assert s._resolve_relative_date("  TODAY  ") == "2026-07-13"
        assert s._resolve_relative_date("сегодня") == "2026-07-13"
        assert s._resolve_relative_date("Сегодня") == "2026-07-13"

    def test_tomorrow_and_yesterday(self, monkeypatch):
        self._fix_today(monkeypatch, date(2026, 7, 13))
        assert s._resolve_relative_date("tomorrow") == "2026-07-14"
        assert s._resolve_relative_date("завтра") == "2026-07-14"
        assert s._resolve_relative_date("yesterday") == "2026-07-12"
        assert s._resolve_relative_date("вчера") == "2026-07-12"

    def test_month_boundary(self, monkeypatch):
        # Guards against a naive "just decrement the day number" bug.
        self._fix_today(monkeypatch, date(2026, 8, 1))
        assert s._resolve_relative_date("yesterday") == "2026-07-31"

    def test_literal_date_passes_through_unchanged(self, monkeypatch):
        self._fix_today(monkeypatch, date(2026, 7, 13))
        assert s._resolve_relative_date("2026-07-01") == "2026-07-01"

    def test_full_iso_timestamp_passes_through_unchanged(self, monkeypatch):
        self._fix_today(monkeypatch, date(2026, 7, 13))
        v = "2026-07-01T09:00:00.000+0000"
        assert s._resolve_relative_date(v) == v

    def test_none_and_non_string_pass_through(self, monkeypatch):
        self._fix_today(monkeypatch, date(2026, 7, 13))
        assert s._resolve_relative_date(None) is None
        assert s._resolve_relative_date(42) == 42

    def test_idempotent_on_already_resolved_value(self, monkeypatch):
        self._fix_today(monkeypatch, date(2026, 7, 13))
        once = s._resolve_relative_date("today")
        twice = s._resolve_relative_date(once)
        assert once == twice == "2026-07-13"


class TestResolveDatesInTaskTree:
    def _fix_today(self, monkeypatch, d: date):
        monkeypatch.setattr(s, "_today_local", lambda: d)

    def test_resolves_root_due_and_start_date(self, monkeypatch):
        self._fix_today(monkeypatch, date(2026, 7, 13))
        node = {"title": "X", "due_date": "today", "start_date": "tomorrow"}
        s._resolve_dates_in_task_tree(node)
        assert node["due_date"] == "2026-07-13"
        assert node["start_date"] == "2026-07-14"

    def test_recurses_into_nested_subtasks(self, monkeypatch):
        self._fix_today(monkeypatch, date(2026, 7, 13))
        node = {
            "title": "Epic",
            "subtasks": [
                {"title": "Step 1", "due_date": "today"},
                {"title": "Step 2", "due_date": "2026-07-20",
                 "subtasks": [{"title": "Sub-step", "due_date": "tomorrow"}]},
                "Plain string subtask — must not crash",
            ],
        }
        s._resolve_dates_in_task_tree(node)
        assert node["subtasks"][0]["due_date"] == "2026-07-13"
        assert node["subtasks"][1]["due_date"] == "2026-07-20"  # unchanged
        assert node["subtasks"][1]["subtasks"][0]["due_date"] == "2026-07-14"

    def test_node_without_date_fields_is_untouched(self, monkeypatch):
        self._fix_today(monkeypatch, date(2026, 7, 13))
        node = {"title": "No dates here"}
        s._resolve_dates_in_task_tree(node)
        assert node == {"title": "No dates here"}
