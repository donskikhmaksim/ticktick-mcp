"""Regression guard for the night-QA "hidden gate params" bug: a tool that
internally calls _gate_single()/_gate_batch() (and therefore NEEDS a caller
to pass manifest_id + user_reply on call #2) but whose own signature — and
therefore the JSON schema FastMCP hands to the client model — doesn't
declare those params. The model then has no visible way to complete call #2
and has to guess a name, which is exactly the class of bug the QA campaign
found in create_tag/add_task_comment/update_task_comment/unset_task_parent/
create_project/create_project_column/delete_project (already fixed upstream
by the time this test was written — this test exists so it can't silently
come back).

The set of "gated" tool names is discovered dynamically via AST (not
hand-maintained): any @mcp.tool()-eligible function whose body calls
_gate_single(...) or _gate_batch(...) is in scope. This keeps the test
honest as new gated tools are added — no one has to remember to update a
hardcoded list here.
"""
import ast
import asyncio
import inspect
import os

from ticktick_mcp.src import server

_SERVER_PATH = os.path.join(os.path.dirname(server.__file__), "server.py")


def _gated_tool_names() -> list[str]:
    """Every top-level function in server.py whose body contains a call to
    _gate_single(...) or _gate_batch(...) — the two-call plan/execute helpers
    that require manifest_id + user_reply on call #2 (see
    references/gate.md §3.1 in the mcp-development-standard skill)."""
    src = open(_SERVER_PATH, encoding="utf-8").read()
    tree = ast.parse(src)

    def uses_gate_helper(node) -> bool:
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id in ("_gate_single", "_gate_batch")):
                return True
        return False

    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ("_gate_single", "_gate_batch"):
                continue  # the helpers themselves, not a tool
            if uses_gate_helper(node):
                names.append(node.name)
    return names


def test_discovery_finds_the_known_gated_tools():
    """Sanity check on the AST scan itself: it should find at least the
    tools known to route through _gate_single/_gate_batch today. Guards
    against the discovery helper silently finding nothing (e.g. after a
    refactor of the call site) and the rest of this file passing vacuously."""
    names = set(_gated_tool_names())
    expected_at_least = {
        # _gate_batch
        "update_tasks", "complete_tasks", "move_tasks", "set_task_parent",
        "set_task_tags", "restore_tasks",
        # _gate_single
        "create_project", "create_subtask", "checkin_habit",
        "unset_task_parent", "create_project_group", "delete_project_group",
        "move_project_to_group", "add_task_comment", "attach_file_to_task",
        "create_tag", "duplicate_task", "update_task_comment",
        "create_project_column",
    }
    missing = expected_at_least - names
    assert not missing, f"AST scan no longer finds these known gated tools: {missing}"


def test_gated_tools_declare_manifest_id_and_user_reply_in_signature():
    """Python-level check: the param names must exist in the function's own
    signature (not just be accepted via **kwargs), because FastMCP derives
    the client-facing JSON schema from the signature."""
    problems = []
    for name in _gated_tool_names():
        fn = getattr(server, name, None)
        assert fn is not None, f"{name}: found by AST scan but no such attribute on server module"
        params = inspect.signature(fn).parameters
        for required in ("manifest_id", "user_reply"):
            if required not in params:
                problems.append(f"{name}: missing '{required}' in signature")
    assert not problems, "\n" + "\n".join(problems)


def test_gated_tools_declare_manifest_id_and_user_reply_in_schema():
    """Client-facing check: the actual JSON schema FastMCP publishes for the
    tool (what the model sees when deciding what args to pass) must list
    manifest_id and user_reply as properties. This is the check that would
    have caught the QA-reported bug directly — a signature can look right
    while some other wrapping still hides the param from the schema."""
    tools = asyncio.run(server.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    problems = []
    for name in _gated_tool_names():
        tool = by_name.get(name)
        if tool is None:
            problems.append(f"{name}: found by AST scan but not registered as an @mcp.tool()")
            continue
        props = (tool.inputSchema or {}).get("properties", {})
        for required in ("manifest_id", "user_reply"):
            if required not in props:
                problems.append(f"{name}: '{required}' missing from published inputSchema")
    assert not problems, "\n" + "\n".join(problems)
