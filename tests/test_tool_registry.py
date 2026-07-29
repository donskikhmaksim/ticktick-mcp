"""Regression guard: a stray text merge (e.g. a return statement glued to the
next @mcp.tool() decorator without a newline — happened once during a
multi-agent parallel edit session) silently de-registers a tool without any
syntax error. pytest/ruff don't catch it since `@` still parses as matmul.
This test would have caught it."""
import asyncio

from ticktick_mcp.src import server


def test_all_78_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 78, (
        f"expected 78 registered @mcp.tool()s, got {len(tools)} — "
        "a decorator likely got glued to the previous line (grep for "
        "'[^ ]@mcp\\.tool' in server.py)"
    )


def test_attach_file_to_task_is_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert "attach_file_to_task" in names
