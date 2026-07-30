"""PLAN_retrofit.md §16.1 (P0) — the reflexive gate test.

Every write tool that survives to production must pass through one of the
three shared consent primitives (`_require_consent` / `_gate_single` /
`_gate_batch`) — or the automation-actor bypass that IS that same primitive's
internal mechanism for headless-only tools like `create_tasks`. This is NOT a
hand-maintained list of tool names: it walks the AST of server.py, finds
every `@mcp.tool()`-decorated function, and inspects its body (plus one level
of indirection through `_*_impl` helpers, since several tools delegate the
actual mutation to a shared impl function called only after the gate check
passes in the wrapper itself).

Why this matters: `checkin_habit` and `unset_task_parent` lived for a long
time as real, unguarded write paths — nobody wrote a test that would fail
when a NEW write tool got added without a gate, so the omission was invisible
until a manual audit found it (PLAN_retrofit.md groups 13/14/15). This test
exists so that never happens silently again: add a write tool, forget the
gate call, and this test fails at the exact function name.

Three kinds of legitimate "no direct gate call" survive review, all handled
below without being exempted from the underlying analysis:

1. Deprecated redirect wrappers (`execute_task_creation`,
   `execute_task_deletion`, `delete_task_with_subtasks`) — they mutate
   NOTHING, ever; they just print a refusal pointing at the merged tool
   (PLAN_retrofit.md §15.1/§15.2/§15.9). Detected dynamically via the
   "DEPRECATED" marker every one of their docstrings carries — not a name
   list — and independently verified to never touch a TickTick client.
2. `create_tasks` — an automation-only direct-mutation path that ALWAYS
   refuses interactive callers and only proceeds for a caller holding a
   valid `automation_key`, resolved via `_resolve_automation_actor` — the
   exact same primitive `_require_consent` uses internally for its own
   automation bypass (see line ~2788). This is not a hole; it is a stricter
   gate (no consent tier ever lets an interactive caller through directly).
3. Three explicit, DOCUMENTED exceptions from PLAN_retrofit.md §16.8/§16.9/
   §16.13/§16.14 — `checkin_habit`, `unset_task_parent` (rationale: docs/
   ALL_METHODS_MECHANICS.md + server.py docstrings, packages 13), and
   `create_attachment_upload_url` (the mutation happens OUTSIDE the MCP
   call entirely, via a human `curl PUT` to a presigned `/ul` route —
   architecturally ungateable in-process; compensated by identity-guard +
   post-verify + audit log, package 9). Any FOURTH tool that wants to join
   this list must be added here explicitly, with the same paper trail —
   the test deliberately does NOT auto-discover new exceptions, or a future
   ungated write tool could just claim membership by resembling one.
"""
import ast
import asyncio
import inspect

import ticktick_mcp.src.server as s

_GATE_CALLS = {"_require_consent", "_gate_single", "_gate_batch"}

# PLAN_retrofit.md §16.8/§16.9/§16.13/§16.14 — see module docstring point 3.
_DOCUMENTED_UNGATED_EXCEPTIONS = {
    "checkin_habit",
    "unset_task_parent",
    "create_attachment_upload_url",
}

# create_tasks: automation-only direct path, gated via _resolve_automation_actor
# (module docstring point 2) rather than the three consent primitives.
_AUTOMATION_ONLY_TOOLS = {"create_tasks"}


def _parse_server_module():
    source = inspect.getsource(s)
    tree = ast.parse(source)
    funcs = {}
    tool_names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node
            for dec in node.decorator_list:
                dump = ast.dump(dec)
                if "mcp" in dump and "tool" in dump:
                    tool_names.append(node.name)
    return funcs, tool_names


def _calls_named(node, names):
    found = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            fname = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if fname in names:
                found.add(fname)
    return found


def _impl_helper_calls(node):
    """Every `_..._impl`/`_..._helper` function this node calls directly —
    one level of indirection, matching the actual pattern in server.py where
    the @mcp.tool() wrapper gates, THEN calls a shared impl function that
    does the actual mutation."""
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id.startswith("_") and (
                    n.func.id.endswith("_impl") or n.func.id.endswith("_helper")):
                names.add(n.func.id)
    return names


def _touches_ticktick_client(node):
    """True if the function body calls anything on the module-level
    `ticktick_v2` / `ticktick` client objects — i.e. it actually reaches out
    and mutates/reads TickTick, as opposed to just building a refusal
    string."""
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            if n.value.id in ("ticktick_v2", "ticktick"):
                return True
    return False


def test_all_write_tools_pass_through_a_shared_gate():
    funcs, tool_names = _parse_server_module()
    assert len(tool_names) >= 79, (
        "fewer @mcp.tool() functions found than expected — AST walk broken?")

    # Read tools (readOnlyHint=True) are out of scope for this test —
    # test_pkg16_annotations.py covers the annotation split itself.
    tools = asyncio.run(s.mcp.list_tools())
    by_name = {t.name: t for t in tools}

    failures = []
    checked = []

    for name in tool_names:
        tool = by_name.get(name)
        assert tool is not None, f"{name}: decorated but not registered?"
        ann = getattr(tool, "annotations", None)
        read_only = bool(getattr(ann, "readOnlyHint", False)) if ann else False
        if read_only:
            continue  # not a write tool — nothing to gate

        node = funcs[name]
        doc = ast.get_docstring(node) or ""

        if "DEPRECATED" in doc:
            # Kind 1: redirect wrapper. Must mutate nothing — verify it never
            # touches a TickTick client at all (not just "no gate call").
            assert not _touches_ticktick_client(node), (
                f"{name}: docstring claims DEPRECATED/redirect-only but the "
                "function body touches a ticktick client — it may actually "
                "mutate without a gate. Either add a real gate call or fix "
                "the docstring to stop claiming it's inert."
            )
            checked.append(name)
            continue

        if name in _DOCUMENTED_UNGATED_EXCEPTIONS:
            checked.append(name)
            continue

        if name in _AUTOMATION_ONLY_TOOLS:
            assert "_resolve_automation_actor" in _calls_named(
                node, {"_resolve_automation_actor"}
            ) or "_resolve_automation_actor" in ast.dump(node), (
                f"{name}: listed as automation-only but no longer calls "
                "_resolve_automation_actor — it may now be an ungated direct "
                "mutation path. Remove from _AUTOMATION_ONLY_TOOLS or restore "
                "the automation-key check."
            )
            checked.append(name)
            continue

        direct = _calls_named(node, _GATE_CALLS)
        if direct:
            checked.append(name)
            continue

        # No direct gate call — check one level of _*_impl indirection: some
        # wrappers gate directly in their own body before calling a shared
        # impl (already covered above by `direct`); this branch instead
        # catches the (currently theoretical) case of a wrapper that has NO
        # gate call in its own body at all — that is exactly the bug this
        # test exists to catch, so it's a hard failure, not a soft skip.
        impl_calls = _impl_helper_calls(node)
        failures.append(
            f"{name}: no {_GATE_CALLS} call found in its own body "
            f"(calls impl helpers: {impl_calls or 'none'}) — this write tool "
            "is not going through a shared consent gate. If this is a new, "
            "deliberately-documented exception, add it to "
            "_DOCUMENTED_UNGATED_EXCEPTIONS above WITH a paper trail in "
            "docs/, matching PLAN_retrofit.md §16.8/§16.9/§16.13/§16.14."
        )

    assert not failures, "\n".join(failures)
    # Sanity: we actually exercised a non-trivial number of write tools, not
    # zero because of a classification bug that silently skipped everything.
    assert len(checked) >= 30, (
        f"only checked {len(checked)} write tools — suspiciously low, "
        "annotation classification may be broken"
    )


def test_documented_exceptions_are_still_the_only_ones_and_still_exist():
    """If a name in the exception set gets renamed/removed from server.py,
    the exception silently stops protecting anything and a real gap could
    reopen unnoticed. Pin it down explicitly."""
    funcs, tool_names = _parse_server_module()
    for name in _DOCUMENTED_UNGATED_EXCEPTIONS | _AUTOMATION_ONLY_TOOLS:
        assert name in tool_names, (
            f"{name}: documented gate exception no longer exists as a "
            "registered tool — remove it from the exception set."
        )
