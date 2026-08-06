"""Regression coverage for the operation_report tally bug (nightly QA, 2026-08):
`operation_report(record_id)` printed a "0 расхождений" verdict in the SAME
response that also listed several explicit ⚠️ discrepancy bullets — an
internal contradiction. Root cause: the old tally counted only lines whose
leading glyph matched "✅"/"❌" by re-parsing the printed markdown text, so
⚠️ verdicts (a mismatched-but-created task, an unreadable re-check, an
unhandled op type) were printed in the body but never counted anywhere.

The fix makes `_verify_item` return an explicit (status, line) pair — status
∈ {"ok", "warn", "bad"} — and makes `_build_operation_report`'s tally a
len()/count() over that SAME list of verdicts it prints, never a
separately-incremented counter. These tests assert that invariant holds:
whatever is printed as a bullet is exactly what is counted in "Итог", for a
mix of ok/warn/bad outcomes and for the all-clear case.

Pure-logic: no network, no real TickTick — `_open_by_id`/`_v2_project_names`
are monkeypatched to canned live state, same pattern as
tests/test_delete_identity_binding.py.
"""
import re

import ticktick_mcp.src.server as s


def _wire(monkeypatch, live, tmp_path, names=None):
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: names or {"p1": "Проект1", "p2": "Проект2"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))


def _count_body_bullets(report: str) -> dict:
    """Count verdict bullets by leading status glyph, straight from the
    printed markdown — this is the independent ground truth the "Итог" tally
    is checked against."""
    lines = [l for l in report.splitlines() if l.startswith("- ")]
    return {
        "ok": sum(1 for l in lines if l.startswith("- ✅")),
        "warn": sum(1 for l in lines if l.startswith("- ⚠️")),
        "bad": sum(1 for l in lines if l.startswith("- ❌")),
        "total": len(lines),
    }


def _parse_summary(report: str):
    m = re.search(
        r"Итог: ✅ (\d+) подтверждено, ⚠️ (\d+) не проверено, "
        r"❌ (\d+) расхождений",
        report)
    assert m, f"Итог-строка не найдена или не в ожидаемом формате:\n{report}"
    return {"ok": int(m.group(1)), "warn": int(m.group(2)),
            "bad": int(m.group(3))}


async def test_mixed_ok_warn_bad_tally_matches_printed_bullets(
        monkeypatch, tmp_path):
    """Reproduces the reported scenario: a batch with several ⚠️-flagged
    items (a create landing in the wrong project, an op type with no
    dedicated verifier) alongside a genuine ❌ and a genuine ✅. The printed
    "Итог" numbers must equal what's actually listed — no discrepancy can be
    invisible to the tally."""
    live = {
        # d1 absent from live -> delete confirmed (ok)
        "d2": {"id": "d2", "title": "Ещё живая"},           # delete NOT applied -> bad
        "u1": {"id": "u1", "title": "New U1"},               # update matches -> ok
        "c1": {"id": "c1", "title": "C1", "projectId": "p2"},  # create wrong project -> warn
        "h1": {"id": "h1", "title": "H1"},                   # unhandled op -> warn (fallback)
    }
    _wire(monkeypatch, live, tmp_path)

    rid = "mixed-test01"
    s._journal_write({"ts": "2026-08-05T10:00:00+00:00", "record": rid,
                      "op": "delete", "summary": "t",
                      "items": [{"taskId": "d1", "title": "Удалённая"}]})
    s._journal_write({"ts": "2026-08-05T10:00:01+00:00", "record": rid,
                      "op": "delete", "summary": "t",
                      "items": [{"taskId": "d2", "title": "Ещё живая"}]})
    s._journal_write({"ts": "2026-08-05T10:00:02+00:00", "record": rid,
                      "op": "update", "summary": "t",
                      "items": [{"taskId": "u1", "title": "U1",
                                "expect": {"changes": {"title": "New U1"}}}]})
    s._journal_write({"ts": "2026-08-05T10:00:03+00:00", "record": rid,
                      "op": "create", "summary": "t",
                      "items": [{"taskId": "c1", "title": "C1",
                                "expect": {"projectId": "p1"}}]})
    s._journal_write({"ts": "2026-08-05T10:00:04+00:00", "record": rid,
                      "op": "checkin_habit", "summary": "t",
                      "items": [{"taskId": "h1", "title": "H1"}]})

    report = s._build_operation_report(rid)

    bullets = _count_body_bullets(report)
    summary = _parse_summary(report)

    # The core regression: warn items (⚠️) are printed AND counted.
    assert bullets == {"ok": 2, "warn": 2, "bad": 1, "total": 5}
    assert summary == {"ok": 2, "warn": 2, "bad": 1}
    # Single source of truth, generically: tally == printed bullets, always.
    assert summary["ok"] == bullets["ok"]
    assert summary["warn"] == bullets["warn"]
    assert summary["bad"] == bullets["bad"]

    # Machine-readable verdict: any discrepancy (bad>0 here) must NOT read
    # as success.
    assert "Статус операции: ❌" in report
    assert "Статус операции: ✅" not in report

    # The frozen emoji legend bans ASCII "✓"/"✗" as status glyphs — the old
    # fallback line used "✓"; make sure it's gone for good.
    assert "✓" not in report
    assert "✗" not in report


async def test_warn_only_batch_is_not_reported_as_full_success(
        monkeypatch, tmp_path):
    """bad == 0 but warn > 0 must still not read as a clean ✅ — a headless
    consumer checking only the ❌ count would otherwise miss it."""
    live = {"c1": {"id": "c1", "title": "C1", "projectId": "p2"}}
    _wire(monkeypatch, live, tmp_path)

    rid = "warn-only-01"
    s._journal_write({"ts": "2026-08-05T11:00:00+00:00", "record": rid,
                      "op": "create", "summary": "t",
                      "items": [{"taskId": "c1", "title": "C1",
                                "expect": {"projectId": "p1"}}]})

    report = s._build_operation_report(rid)
    summary = _parse_summary(report)
    assert summary == {"ok": 0, "warn": 1, "bad": 0}
    assert "Статус операции: ⚠️" in report
    assert "Статус операции: ✅" not in report


async def test_zero_discrepancies_is_a_clean_and_honest_report(
        monkeypatch, tmp_path):
    """The counterpart case: nothing wrong at all. "0 расхождений" must only
    ever appear when there really is nothing to list."""
    live = {}  # the deleted task is genuinely gone
    _wire(monkeypatch, live, tmp_path)

    rid = "clean-test01"
    s._journal_write({"ts": "2026-08-05T12:00:00+00:00", "record": rid,
                      "op": "delete", "summary": "t",
                      "items": [{"taskId": "d1", "title": "Удалённая"}]})

    report = s._build_operation_report(rid)

    bullets = _count_body_bullets(report)
    summary = _parse_summary(report)

    assert bullets == {"ok": 1, "warn": 0, "bad": 0, "total": 1}
    assert summary == {"ok": 1, "warn": 0, "bad": 0}
    assert "Статус операции: ✅" in report
    # No stray discrepancy markers in the per-item body (the "Итог"/"Статус"
    # summary lines legitimately spell out "❌ 0"/no ⚠️ mention — only the
    # bullet body must be clean).
    body = "\n".join(l for l in report.splitlines() if l.startswith("- "))
    assert "❌" not in body
    assert "⚠️" not in body
