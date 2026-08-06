"""СТРУКТУРНАЯ ЛОВУШКА: инструмент, который что-то меняет, обязан быть закрыт
подтверждением — и «что-то меняет» определяется по СЛЕДАМ В КОДЕ, а не по
самопометке.

Зачем отдельный файл. Точечный тест ловит ровно тот случай, который в него
записали; следующая дыра того же вида проезжает молча. Так уже было: три
мутирующих тула (`abandon_task`, `update_project`, `archive_project`) меняли
живое состояние TickTick с ОДНОГО вызова, без манифеста и без единого
вопроса человеку, — и ни один зелёный тест этого не замечал, потому что все
проверки гейта перечисляли тулы поимённо.

Почему по следам, а не по списку/докстрингу. Список и докстринг — это
САМОПОМЕТКА: их пишет тот же человек, который забыл поставить гейт. След в
коде подделать труднее — тул мутирует ровно тогда, когда он (пусть и через
цепочку внутренних функций) вызывает метод клиента, который шлёт в TickTick
POST/PUT/DELETE. Мутирующие методы клиентов здесь тоже не перечислены руками:
они вычисляются разбором `ticktick_v2_client.py` / `ticktick_client.py`.

Вторая ловушка в этом же файле — про ОБЩУЮ таблицу `tg_approvals`: она делится
с пятью TS-серверами (gmail/drive/calendar/docs/sheets), и любой запрос без
фильтра по колонке `server` трогает чужие строки. Такие запросы фейковые
курсоры в тестах по определению не видят: у них в «таблице» одни свои строки.
"""
import ast
import pathlib

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg
import ticktick_mcp.src.ticktick_client as v1_mod
import ticktick_mcp.src.ticktick_v2_client as v2_mod


# ─────────────────── шаг 1: мутирующие методы клиентов ───────────────────

_WRITE_VERBS = {"POST", "PUT", "DELETE", "PATCH"}


def _mutating_client_methods(module, transport_names):
    """Метод клиента считается мутирующим, если в его теле есть исходящий
    запрос с пишущим глаголом: либо `session.post/put/delete(...)`, либо
    вызов внутреннего транспорта (`_request`/`_make_request`) с "POST"/"PUT"/
    "DELETE"/"PATCH" в позиции метода."""
    src = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    found = set()
    for cls in ast.walk(ast.parse(src)):
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else node.func.id if isinstance(node.func, ast.Name)
                        else None)
                if name in ("post", "put", "delete"):
                    found.add(fn.name)
                    break
                if name in transport_names:
                    verb = None
                    if node.args and isinstance(node.args[0], ast.Constant):
                        verb = node.args[0].value
                    for kw in node.keywords:
                        if kw.arg == "method" and isinstance(kw.value, ast.Constant):
                            verb = kw.value.value
                    # Глагол не константа — считаем мутацией: пусть лучше
                    # ловушка потребует явности, чем пропустит запись.
                    if not isinstance(verb, str) or verb.upper() in _WRITE_VERBS:
                        found.add(fn.name)
                        break
    return found


def _all_mutating_client_methods():
    out = _mutating_client_methods(v2_mod, {"_request"})
    out |= _mutating_client_methods(v1_mod, {"_make_request"})
    return {m for m in out if not m.startswith("_")}


# Читающие эндпоинты, которые TickTick заставляет дёргать методом POST (тело
# запроса — фильтр, а не изменение). Единственное законное основание попасть
# в список: запрос ничего не меняет на той стороне.
_READ_ONLY_POST = {
    "get_habit_checkins": "POST /habitCheckins/query — это ЧТЕНИЕ истории "
                          "отметок; тело запроса несёт фильтр по привычкам и "
                          "дате, а не изменение",
}


def test_the_read_only_post_allowlist_has_not_gone_stale():
    """Список исключений обязан описывать существующие методы: переименовали
    метод — исключение молча перестанет действовать (или, наоборот, прикроет
    что-то другое)."""
    mutating = _all_mutating_client_methods()
    unknown = sorted(set(_READ_ONLY_POST) - mutating)
    assert not unknown, (
        f"в списке читающих POST-методов есть несуществующие/уже не пишущие: "
        f"{unknown} — исключение стало прикрытием неизвестно чего")


# ─────────────────── шаг 2: досягаемость из тула ───────────────────

def _server_tree():
    return ast.parse(pathlib.Path(s.__file__).read_text(encoding="utf-8"))


def _module_functions(tree):
    return {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _tool_names(tree):
    """Тул — функция, помеченная декоратором `@mcp.tool()`."""
    out = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) \
                    and d.func.attr == "tool":
                out.add(node.name)
    return out


def _direct_calls(fn):
    """(имена функций модуля, имена вызванных методов). Вызов `ticktick_v2.X()`
    попадает во вторую группу, вызов `_helper()` — в первую; так метод клиента
    не спутать с одноимённым тулом сервера."""
    names, attrs = set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                names.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                attrs.add(n.func.attr)
    return names, attrs


def _reachable(name, funcs, direct, seen=None):
    """Транзитивное замыкание: тул → его хелперы → … → методы клиента.
    Без замыкания ловушка ослепла бы на первом же `_<tool>_impl`."""
    seen = seen if seen is not None else set()
    if name in seen:
        return set(), set()
    seen.add(name)
    names, attrs = direct.get(name, (set(), set()))
    fn_names, methods = set(names), set(attrs)
    for nm in names:
        if nm in funcs:
            sub_fns, sub_methods = _reachable(nm, funcs, direct, seen)
            fn_names |= sub_fns
            methods |= sub_methods
    return fn_names, methods


def _gated_tool_names(tree):
    """Имена тулов, проведённых через общие гейт-функции (второй строковый
    аргумент `_gate_single`/`_gate_batch`)."""
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("_gate_single", "_gate_batch")):
            consts = [a.value for a in node.args
                      if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if len(consts) >= 2:
                out.add(consts[1])
    return out


# Формы защиты, любая из которых считается закрытым входом:
#   • _gate_single/_gate_batch — двухфазный план→исполнение с манифестом;
#   • _require_consent — та же проверка вручную, внутри тела тула;
#   • _automation_key_matches — вход ТОЛЬКО для headless-автоматики с секретом
#     подключения (человека здесь нет, его согласие спрашивать не у кого).
_GUARD_CALLS = {"_require_consent", "_automation_key_matches"}


def _classify_tools():
    tree = _server_tree()
    funcs = _module_functions(tree)
    direct = {name: _direct_calls(fn) for name, fn in funcs.items()}
    gated = _gated_tool_names(tree)
    mutating_methods = _all_mutating_client_methods() - set(_READ_ONLY_POST)

    mutating, unguarded = {}, {}
    for tool in sorted(_tool_names(tree)):
        fn_names, methods = _reachable(tool, funcs, direct)
        traces = sorted(methods & mutating_methods)
        if not traces:
            continue
        mutating[tool] = traces
        if tool not in gated and not (fn_names & _GUARD_CALLS):
            unguarded[tool] = traces
    return mutating, unguarded


def test_every_tool_that_mutates_ticktick_is_behind_a_confirmation():
    """ГЛАВНАЯ ловушка файла. Заведут новый тул, который пишет в TickTick, и
    забудут гейт — здесь станет красно, даже если про него не написали ни
    одного собственного теста.

    Ровно этот класс дыр (мутация с одного вызова, без спроса) уже проезжал
    мимо всего пакета: покрытие было поимённым, а имени нового тула в списках
    не было."""
    mutating, unguarded = _classify_tools()

    assert mutating, "разбор не нашёл ни одного мутирующего тула — ловушка ослепла"
    assert not unguarded, (
        "инструменты пишут в TickTick без подтверждения (тул: следы мутации): "
        f"{unguarded}")


def test_the_trap_sees_the_known_mutating_tools():
    """Страховка от «ловушка молчит, потому что разбор сломался»: несколько
    заведомо мутирующих тулов обязаны быть видны. Без этого рефакторинг
    server.py мог бы обнулить классификацию, и первый же тест остался бы
    зелёным на пустом множестве."""
    mutating, _ = _classify_tools()
    for tool in ("delete_tasks", "update_tasks", "delete_project", "rename_tag",
                 "create_tag", "move_tasks"):
        assert tool in mutating, (
            f"{tool} перестал определяться как мутирующий — сломан разбор "
            "следов, а не код тула")


# ───────────── ловушка 2: чужие строки в ОБЩЕЙ таблице ─────────────

def _functions_touching_shared_table():
    """Функции tg_approval.py, которые делают DML по общей таблице
    `tg_approvals`, вместе со всем SQL-текстом их тела.

    Разбор идёт по ФУНКЦИИ, а не по отдельному литералу, намеренно: запросы
    здесь собираются из кусков и f-строк (`f"FROM tg_approvals WHERE
    {where}"`), и проверка «фильтр в том же литерале» слепла бы ровно на
    самых сложных из них — то есть там, где ошибиться проще всего.

    DDL (CREATE/ALTER/INDEX) не в счёт: он про схему, а не про чужие строки.
    """
    src = pathlib.Path(tg.__file__).read_text(encoding="utf-8")
    out = {}
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parts = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                parts.append(" ".join(node.value.split()))
        text = " ".join(parts)
        upper = text.upper()
        if "TG_APPROVALS" not in upper:
            continue
        dml = any(v in upper for v in ("SELECT ", "INSERT INTO TG_APPROVALS",
                                       "UPDATE TG_APPROVALS", "DELETE FROM TG_APPROVALS"))
        ddl_only = ("CREATE TABLE" in upper or "ALTER TABLE" in upper) and not dml
        if not dml or ddl_only:
            continue
        out[fn.name] = text
    return out


def test_every_query_on_the_shared_table_filters_by_server():
    """`tg_approvals` — ОДНА таблица на шесть MCP-серверов, и ходят они одним
    ботом. Запрос без `server = 'ticktick'` читает и портит чужие строки:
    уборка стёрла бы из лички владельца сообщения gmail-mcp, а поиск
    потерянных планов проставил бы чужим строкам служебные отметки.

    Фейковые курсоры в тестах такую ошибку не видят в принципе — в их
    «таблице» только свои строки. Поэтому проверка структурная, по тексту
    запросов."""
    touching = _functions_touching_shared_table()
    assert len(touching) >= 5, (
        f"разбор нашёл подозрительно мало функций ({sorted(touching)}) — "
        "ловушка, вероятно, ослепла")

    bad = sorted(name for name, sql in touching.items()
                 if "server" not in sql.lower())
    assert not bad, (
        "работают с ОБЩЕЙ таблицей tg_approvals, не отделяя свои строки от "
        f"чужих: {bad}")
