"""Вход в отказ не должен быть закрыт аргументом, который сам объявлен мёртвым.

Дефект (живая приёмка 2026-08-07). `create_tasks` и
`delete_task_with_subtasks` интерактивному вызывающему НИЧЕГО не делают: они
всегда возвращают отказ и называют манифестную замену (plan_task_creation /
plan_task_deletion). Текст отказа хороший — но добраться до него можно было
только через параметр `summary`, объявленный ОБЯЗАТЕЛЬНЫМ. У
`delete_task_with_subtasks` докстринг про этот же параметр пишет дословно
«unused — has no effect»: тул требовал поле, которое сам называет ненужным.

Кто звал самым естественным способом — без бессмысленного аргумента —
получал не подсказку, а `1 validation error … errors.pydantic.dev`, то есть
техническую ошибку MCP-слоя вместо инструкции, куда идти.

Проверяется РОВНО то, что видит MCP-клиент по сети: вызов по имени через
реестр сервера (`tests/read_stand.call` → `mcp.call_tool`), а не питоновский
вызов функции напрямую — иначе pydantic-валидация FastMCP, где и жил дефект,
из теста выпадает целиком.
"""
import ast
import asyncio
import inspect
import os
import re

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import ticktick_mcp.src.server as s
from tests.read_stand import P_WORK, TASK_ROOT, call, wire

_SERVER_PATH = os.path.join(os.path.dirname(s.__file__), "server.py")


@pytest.fixture(autouse=True)
def stand(monkeypatch):
    return wire(monkeypatch)


async def _call_or_report(name: str, **args) -> str:
    """Как `call`, но техническую ошибку MCP-слоя превращает в понятный
    провал теста, а не в голый ToolError с pydantic-ссылкой."""
    try:
        return await call(name, **args)
    except ToolError as e:
        pytest.fail(f"{name} без бессмысленного аргумента ответил технической "
                    f"ошибкой вместо человеческого отказа:\n{e}")


# ─────────── сам вход ───────────

async def test_create_tasks_without_summary_answers_with_the_refusal():
    """Естественный вызов «создай вот эти задачи» — без `summary`. Тул всё
    равно ничего не создаёт, значит обязан объяснить это словами."""
    out = await _call_or_report(
        "create_tasks", tasks=[{"title": "Купить молоко", "project_id": P_WORK}])

    assert "🛑" in out, out
    assert "plan_task_creation" in out, "отказ обязан назвать замену: " + out
    assert "pydantic" not in out.lower()


async def test_delete_task_with_subtasks_without_summary_answers_with_the_refusal():
    out = await _call_or_report("delete_task_with_subtasks",
                                task_id=TASK_ROOT, project_id=P_WORK)

    assert "🛑" in out, out
    assert "plan_task_deletion" in out, "отказ обязан назвать замену: " + out


async def test_delete_task_with_subtasks_refuses_even_with_no_arguments_at_all():
    """Докстринг говорит, что НИ ОДИН аргумент ни на что не влияет. Тогда и
    вызов вообще без аргументов обязан дойти до того же отказа."""
    out = await _call_or_report("delete_task_with_subtasks")

    assert "🛑" in out and "plan_task_deletion" in out, out


# ─────────── страж: «unused» и «required» несовместимы ───────────

def _tool_docstrings() -> dict:
    """{имя тула: докстринг} по всем зарегистрированным @mcp.tool().

    Источник — реестр, а не листинг: с §1.3.4 часть инструментов из
    `list_tools()` исключена, но их описание читает автоматика, и правило
    «не требуй того, что сам зовёшь мёртвым» на них распространяется."""
    out = {}
    for name in s.mcp._tool_manager._tools:
        fn = getattr(s, name, None)
        if fn is not None:
            out[name] = inspect.getdoc(fn) or ""
    return out


def _args_declared_unused(doc: str) -> set:
    """Имена параметров, про которые докстринг сам пишет «unused — has no
    effect» (формат блока Args: «имя: описание»)."""
    return {m.group(1) for m in
            re.finditer(r"^\s*(\w+):\s*unused\b", doc, re.MULTILINE)}


def test_the_guard_actually_sees_a_tool_that_declares_dead_args():
    """Страж самого стража: если формат блока Args когда-нибудь изменится,
    разбор молча вернёт пустое множество, и тест ниже станет проверять
    пустоту. Здесь фиксируется, что хотя бы один такой тул он находит."""
    docs = _tool_docstrings()
    assert _args_declared_unused(docs["delete_task_with_subtasks"]) >= {
        "summary", "task_id", "project_id"}, (
        "разбор блока Args больше не находит параметры, помеченные «unused» — "
        "тест ниже проходит впустую")


def test_no_tool_requires_an_argument_it_calls_unused():
    """Параметр, который тул сам объявил не имеющим никакого эффекта, не
    имеет права быть обязательным в опубликованной схеме — иначе вызывающий
    обязан выдумать значение, чтобы получить ответ, который от этого
    значения не зависит."""
    tools = asyncio.run(s.mcp.list_tools())
    docs = _tool_docstrings()
    problems = []
    for t in tools:
        dead = _args_declared_unused(docs.get(t.name, ""))
        required = set((t.inputSchema or {}).get("required") or [])
        for arg in sorted(dead & required):
            problems.append(f"{t.name}: '{arg}' объявлен «unused», но в схеме обязателен")
    assert not problems, "\n" + "\n".join(problems)


def test_always_refusing_tools_require_nothing(monkeypatch):
    """Тул, чьё тело для обычного (неавтоматического) вызывающего сводится к
    одному `return "🛑 …"`, не имеет права требовать хоть один аргумент: весь
    его ответ от аргументов не зависит.

    Список ищется по AST, а не хардкодом, чтобы новый тул-заглушка попал под
    правило сам."""
    tree = ast.parse(open(_SERVER_PATH, encoding="utf-8").read())
    always_refusing = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        consts = [n.value.value for n in returns
                  if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)]
        # тело-заглушка: единственный строковый return, и он — отказ
        if len(consts) == 1 and consts[0].startswith("🛑") and len(returns) <= 2:
            always_refusing.append(node.name)
            continue
        # …либо единственный return — вызов общей фабрики отказа
        # (`_direct_path_refusal`, 2026-08-10 §1.3.4). Раньше текст отказа
        # стоял константой прямо в теле; после свёртки в один помощник скан по
        # строковым литералам перестал бы видеть такие тулы вовсе — и тест
        # проходил бы впустую (это ловит проверка `checked` в конце).
        if len(returns) == 1 and isinstance(returns[0].value, ast.Call):
            if getattr(returns[0].value.func, "id", None) == "_direct_path_refusal":
                always_refusing.append(node.name)

    # Листинг со СНЯТЫМ сокрытием: с §1.3.4 закрытые инструменты из
    # `list_tools()` намеренно исключены, а правило «всегда отказывающий не
    # требует аргументов» касается их в первую очередь — автоматика зовёт их
    # по имени и получает от сервера ту же опубликованную схему. Снятие через
    # ту же переменную окружения, что и штатный откат, — второго механизма
    # «показать всё» здесь заводить нельзя.
    monkeypatch.setenv(s._HIDDEN_TOOLS_ENV, "")
    tools = {t.name: t for t in asyncio.run(s.mcp.list_tools())}
    checked, problems = [], []
    for name in always_refusing:
        tool = tools.get(name)
        if tool is None:
            continue
        checked.append(name)
        required = (tool.inputSchema or {}).get("required") or []
        if required:
            problems.append(f"{name}: всегда отказывает, но требует {required}")
    assert "delete_task_with_subtasks" in checked, (
        "AST-скан больше не находит известный тул-заглушку — тест проходит впустую")
    assert not problems, "\n" + "\n".join(problems)
