"""Регресс-тест на баг «спрятанный параметр гейта» из ночной QA-кампании:
тул, который внутри вызывает _gate_single()/_gate_batch() (а значит ТРЕБУЕТ
от вызывающего передать manifest_id + user_reply на 2-м вызове), но в
собственной сигнатуре — а значит и в JSON-схеме, которую FastMCP отдаёт
модели-клиенту, — эти параметры не объявлены. Модель тогда не видит, чем
завершить 2-й вызов, и вынуждена угадывать имя параметра — ровно тот класс
бага, что QA-кампания нашла в create_tag/add_task_comment/
update_task_comment/unset_task_parent/create_project/create_project_column/
delete_project (к моменту написания теста уже исправлено выше по потоку —
этот тест нужен, чтобы расхождение не могло вернуться незаметно).

Список «гейтованных» тулов ищется динамически через AST (не хардкодом
списком): в область охвата попадает любая функция-кандидат в @mcp.tool(),
в теле которой есть вызов _gate_single(...) или _gate_batch(...). Это
держит тест честным по мере добавления новых гейтованных тулов — никому не
нужно помнить про правку хардкод-списка здесь.
"""
import ast
import asyncio
import inspect
import os

from ticktick_mcp.src import server

_SERVER_PATH = os.path.join(os.path.dirname(server.__file__), "server.py")


def _gated_tool_names() -> list[str]:
    """Все функции верхнего уровня в server.py, в теле которых есть вызов
    _gate_single(...) или _gate_batch(...) — двух хелперов «план→исполнение»,
    требующих manifest_id + user_reply на 2-м вызове (см. references/gate.md
    §3.1 в скилле mcp-development-standard)."""
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
                continue  # это сами хелперы, а не тул
            if uses_gate_helper(node):
                names.append(node.name)
    return names


def test_discovery_finds_the_known_gated_tools():
    """Проверка самого AST-скана: он должен находить как минимум тулы,
    сегодня заведомо идущие через _gate_single/_gate_batch. Защита от
    случая, когда скан-хелпер после рефакторинга точки вызова молча
    перестаёт находить хоть что-то, и весь остальной файл начинает
    проходить впустую (по пустому множеству)."""
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
    assert not missing, f"AST-скан больше не находит эти известные гейтованные тулы: {missing}"


def test_gated_tools_declare_manifest_id_and_user_reply_in_signature():
    """Проверка на уровне Python: имена параметров должны быть в самой
    сигнатуре функции (а не приниматься только через **kwargs), потому что
    FastMCP строит клиентскую JSON-схему именно из сигнатуры."""
    problems = []
    for name in _gated_tool_names():
        fn = getattr(server, name, None)
        assert fn is not None, f"{name}: найден AST-сканом, но такого атрибута в модуле server нет"
        params = inspect.signature(fn).parameters
        for required in ("manifest_id", "user_reply"):
            if required not in params:
                problems.append(f"{name}: в сигнатуре нет '{required}'")
    assert not problems, "\n" + "\n".join(problems)


def test_gated_tools_declare_manifest_id_and_user_reply_in_schema():
    """Проверка на клиентском уровне: реальная JSON-схема, которую FastMCP
    публикует для тула (то, что видит модель, решая какие аргументы
    передать), должна содержать manifest_id и user_reply как свойства. Это
    ровно та проверка, что напрямую поймала бы баг из QA-отчёта — сигнатура
    может выглядеть правильно, а какая-то другая обёртка всё равно прячет
    параметр из схемы.

    2026-08-10 (§1.3.4): часть гейтованных тулов намеренно исключена из
    ответа на `tools/list` — их зовёт автоматика по имени, и опубликованная
    схема нужна ей ровно так же. Поэтому листинг берётся со СНЯТЫМ сокрытием
    (той же переменной окружения, что и штатный откат), а не подменяется
    собственным обходом реестра: схему строит FastMCP, и смотреть надо на неё."""
    os.environ[server._HIDDEN_TOOLS_ENV] = ""
    try:
        tools = asyncio.run(server.mcp.list_tools())
    finally:
        os.environ.pop(server._HIDDEN_TOOLS_ENV, None)
    by_name = {t.name: t for t in tools}
    problems = []
    for name in _gated_tool_names():
        tool = by_name.get(name)
        if tool is None:
            problems.append(f"{name}: найден AST-сканом, но не зарегистрирован как @mcp.tool()")
            continue
        props = (tool.inputSchema or {}).get("properties", {})
        for required in ("manifest_id", "user_reply"):
            if required not in props:
                problems.append(f"{name}: '{required}' отсутствует в опубликованной inputSchema")
    assert not problems, "\n" + "\n".join(problems)
