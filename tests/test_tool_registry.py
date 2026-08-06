"""Regression guard: a stray text merge (e.g. a return statement glued to the
next @mcp.tool() decorator without a newline — happened once during a
multi-agent parallel edit session) silently de-registers a tool without any
syntax error. pytest/ruff don't catch it since `@` still parses as matmul.
This test would have caught it."""
import asyncio

from ticktick_mcp.src import server


# 75 = 79 authored tools MINUS the 4 declutter tools (plan_declutter,
# execute_declutter, resume_declutter, set_declutter_decision), whose
# @mcp.tool() decorators are commented out on purpose — Maksim disabled the
# declutter feature 2026-08-04/05 and it must stay disabled. The count was
# left at 78 when that happened, so this test has been red on main ever
# since; fixed to the real number rather than silently ignored. Re-enabling
# declutter means bumping this back up by 4 in the same commit.
# 74 → 75 (2026-08-06): +1 = `manual_triage` — один гейтованный тул для
# смешанного ручного разбора (delete/complete/update/move/merge в одном плане
# с ОДНИМ подтверждением).
_EXPECTED_TOOLS = 75


def test_all_expected_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == _EXPECTED_TOOLS, (
        f"expected {_EXPECTED_TOOLS} registered @mcp.tool()s, got {len(tools)} — "
        "a decorator likely got glued to the previous line (grep for "
        "'[^ ]@mcp\\.tool' in server.py), or a tool was added/removed without "
        "updating _EXPECTED_TOOLS"
    )


def test_attach_file_to_task_is_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert "attach_file_to_task" in names


def test_manual_triage_is_registered_and_declutter_is_not():
    """`manual_triage` — ручная замена отключённого автоматического
    declutter'а: он обязан быть опубликован, а все четыре declutter-тула —
    оставаться снятыми с регистрации (владелец отключил их навсегда)."""
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "manual_triage" in names
    assert names.isdisjoint({"plan_declutter", "execute_declutter",
                             "resume_declutter", "set_declutter_decision"})
