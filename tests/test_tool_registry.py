"""Regression guard: a stray text merge (e.g. a return statement glued to the
next @mcp.tool() decorator without a newline — happened once during a
multi-agent parallel edit session) silently de-registers a tool without any
syntax error. pytest/ruff don't catch it since `@` still parses as matmul.
This test would have caught it."""
import asyncio

from ticktick_mcp.src import server


def test_all_81_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 81, (
        f"expected 81 registered @mcp.tool()s, got {len(tools)} — "
        "a decorator likely got glued to the previous line (grep for "
        "'[^ ]@mcp\\.tool' in server.py). PLAN_retrofit.md §15.1/§15.10: was "
        "78 before package 15 — plan_task_creation + execute_task_creation "
        "merged into ONE new tool, create_tasks_interactive, while BOTH old "
        "names are kept registered as deprecated redirect wrappers (net "
        "+1). plan_task_deletion/execute_task_deletion also merged (§15.2), "
        "but that fold went INTO the pre-existing delete_tasks (no new tool "
        "name), so their net contribution is 0 — both old names stay "
        "registered as wrappers, no third tool was added (79 total). "
        "assign_owners (backlog feature, plan_assign/execute_assign) added "
        "two brand-new tools on top of that — net +2 (81 total)."
    )


def test_assign_owners_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"plan_assign", "execute_assign"} <= names


def test_attach_file_to_task_is_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert "attach_file_to_task" in names


def test_merged_creation_and_old_wrappers_all_registered():
    """PLAN_retrofit.md §15.1/§12: the merge must never delete the old
    tool names — they stay registered as deprecated redirect wrappers."""
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"create_tasks_interactive", "plan_task_creation",
            "execute_task_creation"} <= names


def test_merged_deletion_and_old_wrappers_all_registered():
    """PLAN_retrofit.md §15.2/§12: delete_tasks now owns the bulk-planning
    logic too, but plan_task_deletion/execute_task_deletion stay registered
    as deprecated redirect wrappers."""
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"delete_tasks", "plan_task_deletion",
            "execute_task_deletion"} <= names
