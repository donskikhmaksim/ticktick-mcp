"""format_task_tree must render arbitrary nesting depth. The audit found the
old version dropped grandchildren entirely while still counting them."""
import ticktick_mcp.src.server as s


def _mk(tid, parent=None, title=None):
    t = {"id": tid, "title": title or tid, "projectId": "p1"}
    if parent:
        t["parentId"] = parent
    return t


def test_grandchildren_are_rendered():
    tasks = [
        _mk("root"),
        _mk("child", parent="root"),
        _mk("grandchild", parent="child"),
        _mk("greatgrand", parent="grandchild"),
    ]
    out = s.format_task_tree(tasks)
    # every task title must appear somewhere in the output
    for tid in ("root", "child", "grandchild", "greatgrand"):
        assert tid in out, f"{tid} missing from tree output"


def test_indentation_increases_with_depth():
    tasks = [_mk("root"), _mk("child", parent="root"), _mk("grandchild", parent="child")]
    lines = s.format_task_tree(tasks).splitlines()
    # deeper tasks are indented more (leading whitespace grows)
    indents = [len(ln) - len(ln.lstrip()) for ln in lines]
    assert indents == sorted(indents)
    assert indents[-1] > indents[0]


def test_orphan_parent_promoted_to_top():
    # parent not present in the list -> child shows at top level, not dropped
    tasks = [_mk("child", parent="missing-parent")]
    out = s.format_task_tree(tasks)
    assert "child" in out


def test_cycle_does_not_hang():
    # a <-> b cycle must terminate (seen-guard)
    tasks = [_mk("a", parent="b"), _mk("b", parent="a")]
    out = s.format_task_tree(tasks)  # must return, not recurse forever
    assert isinstance(out, str)


def test_limit_truncation_note():
    tasks = [_mk(f"t{i}") for i in range(250)]
    out = s.format_task_tree(tasks, limit=200)
    assert "more" in out.lower()
