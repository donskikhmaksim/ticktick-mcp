"""П6 (2026-08-09): исполняющая часть обязана отказывать, если у манифеста
записан тул, который в MCP-сервере СЕЙЧАС не зарегистрирован.

Баг: `_resolve_auto_executor` отдавал исполнителя любому манифесту, у
которого нашлась подходящая метка — explicit-запись в `_AUTO_EXECUTORS`, либо
функция `_<tool>_impl` в globals() для generic `_gate_batch`/`_gate_single`
пути. Ни то, ни другое не проверяет, жив ли сам `@mcp.tool()` декоратор.
Отключение тула в этом проекте делают комментированием РОВНО ОДНОГО
декоратора (см. plan_declutter/execute_declutter/resume_declutter/
set_declutter_decision в server.py) — сама функция и её `_impl` остаются в
модуле нетронутыми, поэтому старая проверка их по-прежнему находила.
Практический эффект (описан в самом server.py у `_resolve_auto_executor` и
`_register_auto_executor`, 2026-08-04/05): непогашенный в базе план +
нажатие кнопки ✅ в Telegram исполнили бы операцию, которую владелец считает
выключенной.

Тесты ниже симулируют ровно это состояние («декоратор снят, всё остальное
живо») через `monkeypatch.delitem` над реальным реестром FastMCP
(`mcp._tool_manager._tools`) — тем же реестром, которым пользуется
tests/test_tool_registry.py, — а не выдуманным двойником.
"""
import asyncio

import ticktick_mcp.src.server as s


def _undeclare_tool(monkeypatch, name: str) -> None:
    """Снимает регистрацию `@mcp.tool()` для `name`, оставляя саму функцию и
    её `_impl`/запись в `_AUTO_EXECUTORS` нетронутыми — точная симуляция
    «закомментировали декоратор, забыли про остальное». `monkeypatch.delitem`
    сам восстановит запись по завершении теста."""
    tools = s.mcp._tool_manager._tools
    assert name in tools, f"{name} должен быть живым @mcp.tool() на старте теста"
    monkeypatch.delitem(tools, name)


def _registered_tool_names() -> set:
    return {t.name for t in asyncio.run(s.mcp.list_tools())}


# ───────────────────── explicit-регистрация (_AUTO_EXECUTORS) ─────────────

def test_explicit_registry_tool_refuses_when_decorator_disabled(monkeypatch):
    """create_tasks сегодня зарегистрирован явно через
    `_register_auto_executor`. Убираем ТОЛЬКО регистрацию `@mcp.tool()` (как
    будто декоратор закомментировали, а про `_register_auto_executor` в
    16053-й строке забыли) — исполнение обязано отказать, а не пройти."""
    real_entry = s._AUTO_EXECUTORS["create_tasks"]
    _undeclare_tool(monkeypatch, "create_tasks")

    entry = s._resolve_auto_executor("create_tasks", {})

    assert entry is not None, (
        "манифест не должен молча выпасть из очереди без объяснения — "
        "раньше это был бы None и тихий continue")
    assert entry is not real_entry, "не должен исполнять настоящую мутацию"
    assert entry.rehash is real_entry.rehash, (
        "привязка к показанному плану (хэш) не должна ломаться отказом — "
        "иначе try_auto_execute решит, что план 'уплыл', и снова смолчит")

    out = asyncio.run(entry.execute("mid-explicit", {}))
    assert out.startswith("🛑")
    assert "create_tasks" in out
    assert "отсутств" in out.lower()


# ───────────────────── generic _gate_batch/_gate_single путь ──────────────

def test_generic_gate_tool_refuses_when_decorator_disabled(monkeypatch):
    """manual_triage не в `_AUTO_EXECUTORS` — резолвится generic-путём по
    наличию `_manual_triage_impl` в globals(). Тот же сценарий: тул снят из
    реестра FastMCP, `_impl` остался — отказ, а не тихий пропуск."""
    _undeclare_tool(monkeypatch, "manual_triage")
    m = {"_gate": "batch", "tool": "manual_triage"}

    entry = s._resolve_auto_executor("manual_triage", m)

    assert entry is not None
    assert entry is not s._GENERIC_GATE_ENTRY
    assert entry.rehash is s._GENERIC_GATE_ENTRY.rehash

    out = asyncio.run(entry.execute("mid-generic", m))
    assert out.startswith("🛑")
    assert "manual_triage" in out
    assert "отсутств" in out.lower()


# ───────────────────── прямая регрессия по исходному инциденту ────────────

def test_declutter_stays_refused_even_if_registration_forgotten(monkeypatch):
    """Исходный инцидент, слово в слово (см. комментарий у `server.py`
    ~16064-16070, рядом с `_register_auto_executor`): «закомментировали
    только декоратор (первый проход), регистрацию в `_AUTO_EXECUTORS`
    забыли снять» — нажатие кнопки в Telegram исполнило бы declutter
    взаправду. Симулируем ровно эту забывчивость: временно (через
    monkeypatch.setitem — не raising=False, запись снимается автоматически)
    возвращаем execute_declutter в `_AUTO_EXECUTORS` и проверяем, что
    решает регистрация в FastMCP, а не наличие записи в реестре."""
    assert "execute_declutter" not in _registered_tool_names()
    assert "execute_declutter" not in s._AUTO_EXECUTORS

    async def _would_really_mutate(manifest_id, m):
        return "✅ настоящая мутация declutter выполнена"  # не должно случиться

    fake_entry = s._AutoExecutorEntry(lambda m: "fake-hash", _would_really_mutate)
    monkeypatch.setitem(s._AUTO_EXECUTORS, "execute_declutter", fake_entry)

    entry = s._resolve_auto_executor("execute_declutter", {})

    assert entry is not None
    assert entry is not fake_entry, "не должен исполнять забытую регистрацию"
    out = asyncio.run(entry.execute("mid-declutter", {}))
    assert out.startswith("🛑")
    assert "execute_declutter" in out
    assert "отсутств" in out.lower()


# ───────────────────── регресс: живые тулы работают как раньше ────────────

def test_registered_tools_resolve_exactly_as_before():
    """Обязательная страховка от собственной поломки: НИЧЕГО не должно
    измениться для тулов, которые реально зарегистрированы — тот же объект
    исполнителя (identity), не обёртка-отказ."""
    for tool in ("create_tasks", "delete_tasks", "delete_project", "rename_tag"):
        assert tool in s._AUTO_EXECUTORS, f"{tool} должен быть в _AUTO_EXECUTORS"
        assert s._resolve_auto_executor(tool, {}) is s._AUTO_EXECUTORS[tool]

    m = {"_gate": "batch", "tool": "manual_triage"}
    assert s._resolve_auto_executor("manual_triage", m) is s._GENERIC_GATE_ENTRY


def test_unregistered_and_unrecognized_tool_still_returns_none():
    """Имя, которого нет НИ в `_AUTO_EXECUTORS`, ни как `_<tool>_impl` —
    поведение не меняется: как и раньше, просто None (это не «отключённый
    тул», а «этот вид манифеста никогда не исполнялся кнопкой»)."""
    assert s._resolve_auto_executor("this_tool_never_existed", {}) is None
    assert s._resolve_auto_executor("", {}) is None
