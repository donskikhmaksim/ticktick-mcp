"""Package 7 — `operation_report` / `_build_operation_report` / `_verify_item`.

This is the single independent post-verify mechanism for every write tool: a
defect here silently degrades trust for the whole server. Covers plan items
7.1 (warn counter always printed), 7.2 (fail-closed ⚠️ instead of the dead
ASCII `✓` fallback for uncovered op types), and 7.4 (a mock that reports
"not what was asked" must yield ❌, never ✅)."""
import json

import ticktick_mcp.src.server as s


def _write_journal(tmp_path, record):
    path = tmp_path / "deletion_journal.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---- 7.1: three-number summary, always printed, even with warn/bad == 0 ----

def test_summary_always_prints_three_numbers_even_at_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_v2_project_names", dict)
    # One "complete" item, verified ok — no warn, no bad. The summary line
    # must still spell out all three counters, not omit warn because it's 0.
    live = {}  # task not in open map => complete verified as done
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: live)
    _write_journal(tmp_path, {
        "record": "complete-aaaa1111",
        "op": "complete",
        "ts": "2026-07-28T10:00:00-07:00",
        "items": [{"taskId": "t1", "title": "Задача 1"}],
    })
    report = s._build_operation_report("complete-aaaa1111")
    assert "Итог: ✅ 1 подтверждено, ⚠️ 0 не проверено, ❌ 0 расхождений." in report


def test_warn_outcomes_are_counted_in_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_v2_project_names", dict)
    # The item's id must be present in the live map (not None) so the flow
    # falls through past the generic "not found among open" branch and
    # reaches the op-fallback at the bottom of _verify_item.
    monkeypatch.setattr(s, "_open_by_id",
                         lambda fresh=False: {"t1": {"taskId": "t1"}})
    # "some_future_op" isn't one of the ops _verify_item special-cases, so it
    # must fall into the fail-closed ⚠️ branch (7.2) — and that ⚠️ must be
    # reflected in the summary counters (7.1), not silently dropped.
    _write_journal(tmp_path, {
        "record": "attach-bbbb2222",
        "op": "some_future_op",
        "ts": "2026-07-28T10:00:00-07:00",
        "items": [{"taskId": "t1", "title": "Файл"}],
    })
    report = s._build_operation_report("attach-bbbb2222")
    assert "⚠️" in report
    assert "тип операции `some_future_op` не покрыт независимой проверкой" in report
    assert "Итог: ✅ 0 подтверждено, ⚠️ 1 не проверено, ❌ 0 расхождений." in report


# ---- 7.2: no dead ASCII fallback, and it must be fail-closed (⚠️, not ✅) ----

def test_uncovered_op_type_is_fail_closed_not_silently_ok():
    # live_map must contain the id (non-None) so the flow reaches the bottom
    # op-fallback instead of the generic "not found among open" branch.
    live_map = {"t1": {"taskId": "t1"}}
    line = s._verify_item("some_future_op", {"taskId": "t1", "title": "X"},
                           live_map, {})
    assert "✓" not in line  # the banned bare-checkmark symbol (§7.2) is gone
    assert line.startswith("- ⚠️ ")
    assert "не покрыт независимой проверкой" in line


# ---- 7.4: a mock that returns "not what was asked" must yield ❌, not ✅ ----

async def test_mismatched_live_state_yields_bad_not_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа", "p2": "Личное"})
    # The mock "server" reports the task landed in p2, but the journal
    # recorded that the caller expected p1 — this must surface as ❌.
    live = {"t1": {"taskId": "t1", "projectId": "p2", "tags": []}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: live)
    _write_journal(tmp_path, {
        "record": "move-cccc3333",
        "op": "move",
        "ts": "2026-07-28T10:00:00-07:00",
        "items": [{"taskId": "t1", "title": "Задача",
                    "expect": {"projectId": "p1"}}],
    })
    report = s._build_operation_report("move-cccc3333")
    assert "❌" in report
    assert "Итог: ✅ 0 подтверждено, ⚠️ 0 не проверено, ❌ 1 расхождений." in report
