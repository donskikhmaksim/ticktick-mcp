"""PLAN_retrofit.md §16.4 — post-verify mismatch coverage for `_verify_item`
op branches that weren't independently exercised yet.

`_verify_item` (server.py ~3597) is the single shared dispatcher every
gated write tool's journal record flows through via `operation_report` —
per STANDARD.md §13 point 3, a mock that reports "not what was asked" must
yield ❌ (never ✅) so a genuine post-mutation discrepancy is never
misreported as success. `test_package7_verify_report.py` already covers
this for op="move" and the unknown-op fallback (⚠️); `test_pkg17_security.py`
covers op="delete". This file adds the remaining branches actually reachable
from registered write tools: restore, complete/abandon, delete_project,
create, tags, parent, update.

Note on `create`: unlike most branches, a project/column mismatch on create
is NOT a hard ❌ in the shared dispatcher — the task WAS created (it exists),
just not exactly where asked, so the code deliberately downgrades that to
⚠️ ("создана, но: ..."), reserving ❌ for "doesn't exist at all". That's
existing server.py behavior (out of scope to change per package 16 — tests/
docs only) and is asserted here as-is, not silently strengthened to ❌.
"""
import ticktick_mcp.src.server as s


def test_restore_mismatch_task_not_back_among_open_is_bad():
    # Journal says it should be restored (back among open); live state says
    # it's still not there — must be ❌, not ✅.
    line = s._verify_item("restore", {"taskId": "t1", "title": "Молоко"}, {}, {})
    assert line.startswith("- ❌")
    assert "появилась" in line


def test_restore_success_task_is_back_among_open_is_ok():
    live_map = {"t1": {"taskId": "t1"}}
    line = s._verify_item("restore", {"taskId": "t1", "title": "Молоко"}, live_map, {})
    assert line.startswith("- ✅")


def test_complete_mismatch_task_still_open_is_bad():
    live_map = {"t1": {"taskId": "t1"}}  # still among open == NOT completed
    line = s._verify_item("complete", {"taskId": "t1", "title": "X"}, live_map, {})
    assert line.startswith("- ❌")
    assert "всё ещё среди открытых" in line


def test_abandon_mismatch_task_still_open_is_bad():
    live_map = {"t1": {"taskId": "t1"}}
    line = s._verify_item("abandon", {"taskId": "t1", "title": "X"}, live_map, {})
    assert line.startswith("- ❌")


def test_delete_project_mismatch_project_still_exists_is_bad(monkeypatch):
    monkeypatch.setattr(s, "_v2_project_names_or_none", lambda: {"p1": "Работа"})
    line = s._verify_item("delete_project", {"taskId": "p1", "title": "Работа"}, {}, {})
    assert line.startswith("- ❌")
    assert "ВСЁ ЕЩЁ существует" in line


def test_delete_project_read_failure_is_unverified_not_ok(monkeypatch):
    """A failed re-read must NEVER be silently read as "deleted successfully"
    — fail-closed to ⚠️, not ✅."""
    monkeypatch.setattr(s, "_v2_project_names_or_none", lambda: None)
    line = s._verify_item("delete_project", {"taskId": "p1", "title": "Работа"}, {}, {})
    assert line.startswith("- ⚠️")
    assert "не удалась" in line


def test_create_mismatch_wrong_project_is_warn_not_ok():
    live_map = {"t1": {"taskId": "t1", "projectId": "p2"}}
    item = {"taskId": "t1", "title": "X", "expect": {"projectId": "p1"}}
    line = s._verify_item("create", item, live_map, {"p1": "Работа", "p2": "Личное"})
    assert line.startswith("- ⚠️")
    assert "✅" not in line.split("**")[0]  # not silently reported as full success


def test_create_task_missing_entirely_is_bad():
    item = {"taskId": "t1", "title": "X", "expect": {"projectId": "p1"}}
    line = s._verify_item("create", item, {}, {"p1": "Работа"})
    assert line.startswith("- ❌")


def test_tags_mismatch_is_bad():
    live_map = {"t1": {"taskId": "t1", "tags": ["другой"]}}
    item = {"taskId": "t1", "title": "X", "expect": {"tags": ["важное"]}}
    line = s._verify_item("tags", item, live_map, {})
    assert line.startswith("- ❌")
    assert "ожидались" in line


def test_tags_match_is_ok():
    live_map = {"t1": {"taskId": "t1", "tags": ["важное"]}}
    item = {"taskId": "t1", "title": "X", "expect": {"tags": ["важное"]}}
    line = s._verify_item("tags", item, live_map, {})
    assert line.startswith("- ✅")


def test_parent_mismatch_is_bad():
    live_map = {"t1": {"taskId": "t1", "parentId": "wrong-parent"},
                "correct-parent": {"taskId": "correct-parent"}}
    item = {"taskId": "t1", "title": "X", "expect": {"parentId": "correct-parent"}}
    line = s._verify_item("parent", item, live_map, {})
    assert line.startswith("- ❌")


def test_parent_orphaned_under_dead_parent_is_bad():
    """parentId applied but points at a parent that isn't itself alive —
    orphaning, must be flagged, never silently ✅."""
    live_map = {"t1": {"taskId": "t1", "parentId": "gone-parent"}}
    item = {"taskId": "t1", "title": "X", "expect": {"parentId": "gone-parent"}}
    line = s._verify_item("parent", item, live_map, {})
    assert line.startswith("- ❌")
    assert "родитель" in line


def test_update_mismatch_field_not_applied_is_bad():
    live_map = {"t1": {"taskId": "t1", "title": "Старое название"}}
    item = {"taskId": "t1", "title": "X",
            "expect": {"changes": {"title": "Новое название"}}}
    line = s._verify_item("update", item, live_map, {})
    assert line.startswith("- ❌")
    assert "не применилось" in line


def test_update_match_is_ok():
    live_map = {"t1": {"taskId": "t1", "title": "Новое название"}}
    item = {"taskId": "t1", "title": "X",
            "expect": {"changes": {"title": "Новое название"}}}
    line = s._verify_item("update", item, live_map, {})
    assert line.startswith("- ✅")
