"""КОНТРАКТ: мутирующий инструмент НЕ имеет права носить `readOnlyHint=True`
(2026-08-19, разбор QA-2, дефект №1).

Почему это отдельная ловушка, а не строка в чужом тесте. `readOnlyHint` — то,
по чему MCP-клиенты (включая Claude Code) АВТООДОБРЯЮТ вызов без вопроса
человеку. `plan_task_creation`/`plan_task_deletion` носили эту аннотацию,
а при включённом аварийном выключателе (`TICKTICK_MCP_GATE_DISABLED`) первая
же их строка kill-switch-ветки делала `return await execute_task_*(...)` —
«read-only план» немедленно СОЗДАВАЛ/УДАЛЯЛ задачи. Клиент считал вызов
безопасным чтением, звал «просто прикинуть план» — и задачи улетали в корзину
без единого вопроса: ни сервер (гейт выключен), ни клиент (hint врёт) не
спрашивали. Список инструментов кэшируется клиентом, поэтому «поменять
аннотацию на лету при переключении выключателя» — не выход; выход — не врать.

Мутирует тул или нет, решает НЕ список имён (самопометка), а след в коде —
транзитивная досягаемость пишущего метода клиента (POST/PUT/DELETE), та же
машинерия, что в tests/test_mutating_tools_are_gated.py.
"""
import asyncio

import ticktick_mcp.src.server as s
from tests.test_mutating_tools_are_gated import (
    _all_mutating_client_methods, _direct_calls, _module_functions,
    _reachable, _READ_ONLY_POST, _server_tree, _tool_names)


def _readonly_tools():
    """Имена инструментов, которые СЕРВЕР реально отдаёт клиенту с
    `readOnlyHint=True` — из рантайм-списка `mcp.list_tools()`, а не из
    текста исходника: врёт клиенту именно этот список."""
    tools = asyncio.run(s.mcp.list_tools())
    return {t.name for t in tools
            if t.annotations is not None
            and getattr(t.annotations, "readOnlyHint", None) is True}


def _mutating_tools():
    """Имена инструментов, из которых транзитивно достижим пишущий метод
    клиента (POST/PUT/DELETE в TickTick)."""
    tree = _server_tree()
    funcs = _module_functions(tree)
    direct = {name: _direct_calls(fn) for name, fn in funcs.items()}
    mutating_methods = _all_mutating_client_methods() - set(_READ_ONLY_POST)
    out = set()
    for tool in _tool_names(tree):
        _, methods = _reachable(tool, funcs, direct)
        if methods & mutating_methods:
            out.add(tool)
    return out


def test_no_mutating_tool_carries_a_read_only_hint():
    """ГЛАВНАЯ ловушка: пересечение «носит readOnlyHint» × «достигает пишущего
    метода клиента» обязано быть пустым. Заведут новый «план»-тул с
    kill-switch-веткой и пометят его readonly — здесь станет красно."""
    liars = sorted(_readonly_tools() & _mutating_tools())
    assert not liars, (
        f"инструменты {liars} помечены readOnlyHint=True, но из них достижим "
        "пишущий метод клиента TickTick — клиент автоодобрит вызов как "
        "чтение, а сервер выполнит мутацию (например, по аварийному "
        "выключателю гейта)")


def test_plan_phase_tools_are_not_marked_read_only():
    """Точечная приёмка дефекта №1: оба plan-инструмента, чьи kill-switch-ветки
    делегируют напрямую в execute_*, не имеют права на readOnlyHint — даже
    если общая ловушка выше когда-нибудь ослепнет на разборе исходника."""
    readonly = _readonly_tools()
    for tool in ("plan_task_creation", "plan_task_deletion"):
        assert tool not in readonly, (
            f"{tool} снова помечен readOnlyHint=True — при включённом "
            "TICKTICK_MCP_GATE_DISABLED он исполняет план немедленно, это "
            "не чтение")


def test_the_read_only_survivors_look_like_reads():
    """Обратная сторона: у всех, кто ОСТАЛСЯ с readOnlyHint, из тела не
    достижим ни один пишущий метод клиента. Дешёвая перекрёстная проверка,
    что сама машинерия досягаемости жива (пустое множество readonly-тулов
    означало бы, что мы проверяем воздух)."""
    readonly = _readonly_tools()
    assert readonly, "у сервера пропали все readonly-аннотации — тест ослеп"
    assert not (readonly & _mutating_tools())
