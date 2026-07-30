"""PLAN_retrofit.md §16.2 (P1) — annotation split test.

Invariant 1 of the standard (§1 of STANDARD.md): every tool must be
unambiguously classified read vs write, and that classification must be
visible to the MCP client via `readOnlyHint` so Claude's own UI can group
permissions correctly. This test walks the actually-registered tool list
(not a hand-maintained name list) and checks:

  - every tool built with the shared `READONLY` constant reports
    `readOnlyHint=True`
  - every tool NOT built with `READONLY` (i.e. `WRITE`, `DESTRUCTIVE`, or no
    `annotations=` kwarg at all) never reports `readOnlyHint=True`

Note on the "no annotations=" tools: `create_tasks`, `create_tasks_interactive`,
`execute_task_creation`, `delete_tasks`, `execute_task_deletion`,
`execute_declutter`, `resume_declutter`, `set_declutter_decision`,
`delete_task_with_subtasks`, `delete_project`, `attach_file_to_task`, and
`create_attachment_upload_url` currently register with bare `@mcp.tool()` —
no `annotations=` kwarg means FastMCP leaves `readOnlyHint` unset (falsy, not
True), which satisfies the letter of PLAN_retrofit.md §16.2 ("all write have
readOnlyHint False *or absent*") but NOT its spirit ("assigned via the shared
constants"). That gap is server.py code, out of scope for package 16 (tests/
docs/GUIDE.md only) — flagged here in the docstring and in the package 16
completion report rather than silently patched.
"""
import asyncio

import ticktick_mcp.src.server as s


def test_readonly_annotated_tools_report_read_only_hint_true():
    tools = asyncio.run(s.mcp.list_tools())
    failures = []
    # Build the READONLY-name set from the actual decorator source, not a
    # hand list, so a future addition/removal is picked up automatically.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(s))
    readonly_names = set()
    write_ish_names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                dump = ast.dump(dec)
                if "mcp" not in dump or "tool" not in dump:
                    continue
                ann_kw = next((kw for kw in dec.keywords if kw.arg == "annotations"),
                              None)
                if ann_kw is None:
                    write_ish_names.add(node.name)
                elif isinstance(ann_kw.value, ast.Name) and ann_kw.value.id == "READONLY":
                    readonly_names.add(node.name)
                else:
                    write_ish_names.add(node.name)  # WRITE or DESTRUCTIVE

    by_name = {t.name: t for t in tools}
    for name in readonly_names:
        tool = by_name.get(name)
        assert tool is not None, f"{name}: READONLY-decorated but not registered"
        ann = getattr(tool, "annotations", None)
        read_only = bool(getattr(ann, "readOnlyHint", False)) if ann else False
        if not read_only:
            failures.append(f"{name}: annotated READONLY but readOnlyHint != True")

    for name in write_ish_names:
        tool = by_name.get(name)
        assert tool is not None, f"{name}: registered tool missing from list_tools()"
        ann = getattr(tool, "annotations", None)
        read_only = bool(getattr(ann, "readOnlyHint", False)) if ann else False
        if read_only:
            failures.append(
                f"{name}: NOT annotated READONLY but readOnlyHint == True — "
                "a write tool would be offered to the client as safe-to-auto-allow"
            )

    assert not failures, "\n".join(failures)
    assert len(readonly_names) >= 30 and len(write_ish_names) >= 30, (
        "suspiciously few tools classified either way — decorator parsing "
        f"may be broken (readonly={len(readonly_names)}, "
        f"write_ish={len(write_ish_names)})"
    )


def test_destructive_tools_are_a_subset_of_non_readonly():
    """DESTRUCTIVE-annotated tools must also never be readOnlyHint=True —
    they're the highest-risk subset of the write group."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(s))
    destructive_names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                dump = ast.dump(dec)
                if "mcp" not in dump or "tool" not in dump:
                    continue
                ann_kw = next((kw for kw in dec.keywords if kw.arg == "annotations"),
                              None)
                if ann_kw is not None and isinstance(ann_kw.value, ast.Name) \
                        and ann_kw.value.id == "DESTRUCTIVE":
                    destructive_names.add(node.name)

    assert destructive_names, "no DESTRUCTIVE-annotated tools found — unexpected"
    tools = asyncio.run(s.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    for name in destructive_names:
        tool = by_name[name]
        ann = getattr(tool, "annotations", None)
        read_only = bool(getattr(ann, "readOnlyHint", False)) if ann else False
        assert not read_only, f"{name}: DESTRUCTIVE tool reports readOnlyHint=True"
        destructive_hint = bool(getattr(ann, "destructiveHint", False)) if ann else False
        assert destructive_hint, f"{name}: DESTRUCTIVE tool missing destructiveHint=True"
