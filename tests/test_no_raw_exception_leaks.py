"""СТРУКТУРНАЯ ЛОВУШКА (П7 follow-up, независимый аудит 2026-08-09).

Точечные тесты в tests/test_tool_error_redaction.py гоняют четыре конкретные
команды (get_projects, get_project, get_task, _create_project_impl) через
настоящий вызов тула. Этого достаточно, чтобы поймать поломку ОБЩЕЙ функции
`_redact_for_user`/`_tool_error` — но НЕ поймает точечное снятие обёртки в
одном из ~110 остальных мест, где текст исключения подставляется в ответ
тула. Ровно так и было на первом проходе: аудит мутировал редакцию в шести
местах (`create_habit`, `create_project_group`, `archive_project`,
`create_project_column`, `_execute_declutter_from_sheet`,
`_auto_execute_delete_project`) по одному — и весь набор из 2439 тестов
остался зелёным, потому что ни один тест не смотрел именно туда.

Эта проверка идёт по ИСХОДНОМУ ДЕРЕВУ, а не по выводу одной команды: для
КАЖДОГО `except X as e:` ищет место, где `e` (или производное от него)
подставлено ГОЛЫМ в текст, который уходит наружу — то есть НЕ через
`_redact_for_user()`/`_tool_error()` и НЕ в аргумент `logger.*(...)` (тот
канал уже чистит `SecretPathFilter` из `log_redaction.py`). Найденное —
всегда дефект: не привязана к конкретной строке/команде, значит ловит
СЛЕДУЮЩИЙ такой случай, а не только уже известные шесть.

ДОБАВЛЕНО 2026-08-09 (доработка по замечанию ревью, вторая дыра в том же
пункте) — ДВА расширения:

1. БОЛЬШЕ ФАЙЛОВ. Раньше проверялся только server.py. Но текст исключения,
   пойманного в других модулях, тоже долетает до модели напрямую: клиенты
   TickTick возвращают `{"error": str(e)}` из `_make_request`, `tg_approval`
   строит текст отказа из ошибки Postgres/HTTP, `manifest_store` читает
   Postgres при восстановлении планов после рестарта, `auth.py` — тот же
   `exchange_code_for_token`, чей текст в проде может дойти до пользователя
   через CLI-обвязку. Список файлов ниже — те, чьи строки сервер (`server.py`)
   пробрасывает наружу как есть; каждый проверяется по отдельности.

2. ПОДСТАНОВКА ЧЕРЕЗ ПРОМЕЖУТОЧНУЮ ПЕРЕМЕННУЮ. Живой пример обмана,
   которым прежняя версия проверки обманывается:

       except Exception as e:
           msg = str(e)                    # эта строка сама по себе не
                                            # f-string — предыдущая версия
                                            # проверки смотрела ТОЛЬКО на
                                            # f-string и её не видела
           return "Error: " + msg          # а тут msg подставлена мимо
                                            # _redact_for_user — старая
                                            # проверка искала f'{e}'/f'{str(e)}'
                                            # буквально, 'msg' для неё просто
                                            # обычное имя, не связанное с 'e'

   Проверка теперь помечает `msg` «заражённой» в момент присваивания (RHS
   ссылается на `e` и не обёрнут `_redact_for_user`/`_tool_error`) и ищет
   голую подстановку заражённых имён не только в f-string, но и в `.format()`,
   в `%`-форматировании и в конкатенации строк (`+`) — три способа собрать
   текст ответа, которыми f-string не исчерпывается.

   ЧЕСТНО НЕ ЗАКРЫТО (это уже граничит с полным анализом потоков данных, см.
   docstring `_propagate_taint`): распаковка кортежей (`a, b = x, str(e)`),
   `+=`, атрибуты/подписки как цель присваивания (`self.msg = str(e)`),
   передача заражённого значения через вызов постороннней функции, которая
   возвращает его как есть (`h = helper(e); return f"{h}"` не поймает, если
   `helper` не входит в список безопасных обёрток и всего лишь возвращает
   аргумент нетронутым — проверка не умеет заглядывать внутрь чужих функций).
   Всё это — примеры, для которых нужен настоящий inter-procedural
   data-flow анализ, а не AST одного файла; ниже — то, что ловит частые
   случаи внутри одного `except`-блока.

Живой пример того, что тест ниже обязан ловить:
    except Exception as e:
        return f"Error fetching X: {e}"          # ГОЛО — тест краснеет
    except Exception as e:
        msg = str(e)
        return "Error: " + msg                    # ГОЛО через переменную
    except Exception as e:
        return f"Error: {_redact_for_user(e)}"     # ОК
    except Exception as e:
        logger.warning(f"diag: {e}")               # ОК — это канал логов
"""
import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "ticktick_mcp" / "src"

# Файлы, чьи строки server.py пробрасывает наружу как есть (напрямую видны
# модели в ответе тула) — не всё дерево репозитория, а ровно те модули,
# исключения которых долетают до пользователя без посредника.
_SCANNED_FILES = [
    _SRC / "server.py",
    _SRC / "ticktick_client.py",
    _SRC / "ticktick_v2_client.py",
    _SRC / "tg_approval.py",
    _SRC / "manifest_store.py",
    _SRC / "auth.py",
]

_LOG_METHODS = frozenset({"error", "warning", "info", "debug", "exception",
                          "critical", "log"})
_SAFE_WRAPPER_FUNCS = frozenset({"_redact_for_user", "_tool_error"})
#: `log_redaction.redact(...)` — та же маскировка секретов, но по имени
#: модуля: единственная обёртка, доступная файлам ВНЕ server.py (у них нет
#: доступа к `_redact_for_user`/`_tool_error`, те завязаны на SECRET сервера).


def _build_parent_map(tree: ast.AST) -> dict:
    """ast не даёт родителей узлам из коробки — строим карту сами, чтобы от
    `{e}` внутри f-string подняться и посмотреть, во что она обёрнута."""
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _nearest_enclosing_call(node: ast.AST, parents: dict):
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.Call):
            return cur
        cur = parents.get(cur)
    return None


def _is_safe_call(call: ast.Call) -> bool:
    func = call.func
    if (isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS
            and isinstance(func.value, ast.Name) and func.value.id == "logger"):
        return True
    if (isinstance(func, ast.Attribute) and func.attr == "redact"
            and isinstance(func.value, ast.Name) and func.value.id == "log_redaction"):
        return True
    return isinstance(func, ast.Name) and func.id in _SAFE_WRAPPER_FUNCS


def _is_exception_class_name_access(node: ast.AST) -> bool:
    """`type(e).__name__` / `e.__class__.__name__` — достаёт ТОЛЬКО имя
    класса исключения ("ValueError", "ConnectionError"), не его текст:
    секрета там взяться неоткуда, а этот паттерн — обычная (и безопасная)
    часть диагностических сообщений в этом проекте (см. server.py:18523:
    'Исключение: {type(e).__name__}: {_redact_for_user(e)}').
    Без этого исключения детектор красил бы ЛЮБОЕ упоминание `type(e)`,
    даже когда сообщение целиком безопасно."""
    if not (isinstance(node, ast.Attribute) and node.attr == "__name__"):
        return False
    inner = node.value
    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "type":
        return True
    return isinstance(inner, ast.Attribute) and inner.attr == "__class__"


def _contains_bare_taint(node: ast.AST, tainted: set) -> bool:
    """Есть ли где-то внутри `node` голая ссылка на «заражённое» имя (текст
    исключения или то, что из него собрано), которую по пути НЕ поглотил
    безопасный вызов.

    Рекурсия останавливается на вызовах `_redact_for_user`/`_tool_error`/
    `log_redaction.redact`/`logger.*` — их РЕЗУЛЬТАТ уже безопасен (или это
    отдельный канал), внутрь их аргументов заглядывать незачем:
    `f"{_redact_for_user(e)}"` не течёт, даже если `_redact_for_user` — не
    единственное, что происходит с `e` в этом выражении. Аналогично
    останавливается на `type(e).__name__`/`e.__class__.__name__` — доступ
    только к имени класса, не к тексту исключения."""
    if isinstance(node, ast.Name):
        return node.id in tainted
    if isinstance(node, ast.Call) and _is_safe_call(node):
        return False
    if _is_exception_class_name_access(node):
        return False
    for child in ast.iter_child_nodes(node):
        if _contains_bare_taint(child, tainted):
            return True
    return False


def _propagate_taint(handler: ast.ExceptHandler, tainted: set) -> None:
    """Простая передача «заражения» через присваивания внутри ОДНОГО
    except-блока, в порядке появления строк: `var = <выражение с заражённым
    именем>` делает `var` тоже заражённым, если выражение не обёрнуто целиком
    безопасным вызовом (тогда `var` — уже чистый, ОЧИЩЕННЫЙ текст, и заражение
    на нём обрывается). Однопроходная, в порядке строк — этого достаточно для
    обычного прямого кода (`msg = str(e); return "..." + msg`), но НЕ ловит
    распаковку кортежей, `+=`, и цели-атрибуты/подписки (`self.msg = ...`) —
    см. docstring модуля."""
    assigns = [n for n in ast.walk(handler) if isinstance(n, ast.Assign)]
    assigns.sort(key=lambda n: (n.lineno, n.col_offset))
    for a in assigns:
        names = [t.id for t in a.targets if isinstance(t, ast.Name)]
        if not names:
            continue
        if _contains_bare_taint(a.value, tainted):
            tainted.update(names)


def _leak_sites(node: ast.AST):
    """Узлы, которые СОБИРАЮТ текст для внешнего вывода: f-string, `.format()`,
    `%`-форматирование, конкатенация строк `+`. Возвращает (узел_для_отчёта,
    узел_для_проверки_на_заражение, узел_для_поиска_обёртывающего_вызова)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.JoinedStr):
            for value_node in sub.values:
                if isinstance(value_node, ast.FormattedValue):
                    yield sub, value_node.value, value_node
        elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
              and sub.func.attr == "format"):
            for arg in list(sub.args) + [kw.value for kw in sub.keywords]:
                yield sub, arg, sub
        elif isinstance(sub, ast.BinOp) and isinstance(sub.op, (ast.Mod, ast.Add)):
            yield sub, sub.left, sub
            yield sub, sub.right, sub


def _find_raw_exception_leaks(tree: ast.AST) -> list:
    parents = _build_parent_map(tree)
    violations = []
    seen = set()  # (lineno, id(report_node)) — не дублировать одно место
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.name:
            continue
        tainted = {node.name}
        _propagate_taint(node, tainted)
        for report_node, check_node, call_anchor in _leak_sites(node):
            if not _contains_bare_taint(check_node, tainted):
                continue
            call = _nearest_enclosing_call(call_anchor, parents)
            if call is not None and _is_safe_call(call):
                continue
            key = (report_node.lineno, id(report_node))
            if key in seen:
                continue
            seen.add(key)
            violations.append((report_node.lineno, ast.unparse(report_node)[:150]))
    return violations


@pytest.mark.parametrize("path", _SCANNED_FILES, ids=lambda p: p.name)
def test_no_raw_exception_text_in_except_fstrings(path):
    """Настоящая проверка: КАЖДЫЙ из шести файлов, чьи строки сервер
    пробрасывает наружу, — все except-блоки файла, не только server.py."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = _find_raw_exception_leaks(tree)
    assert not violations, (
        "текст исключения из `except ... as e` подставлен ГОЛЫМ в вывод "
        "(в обход _redact_for_user()/_tool_error()) — секрет из текста "
        f"исключения может уйти модели в чат: {path.name}: {violations}")


# ─────────── Детектор не должен быть пустышкой — проверяем сам детектор ───

def test_detector_catches_a_bare_leak():
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        return f'Error doing X: {e}'\n"
    )
    assert _find_raw_exception_leaks(ast.parse(src))


def test_detector_catches_bare_str_and_repr_forms():
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        return f'Error: {str(e)}'\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        return f'Error: {e!r}'\n"
    )
    assert len(_find_raw_exception_leaks(ast.parse(src))) == 2


def test_detector_allows_the_wrapped_and_logged_forms():
    """И не должен красить безопасные формы — иначе первая же реальная
    проверка потребовала бы аллоулист на весь файл вместо ловли дефектов."""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        logger.exception('Error doing X')\n"
        "        logger.warning(f'diag: {e}')\n"
        "        return _tool_error('doing X', e)\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        return f'Error: {_redact_for_user(e)}'\n"
    )
    assert not _find_raw_exception_leaks(ast.parse(src))


# ────── Аудит 2026-08-09: подстановка через промежуточную переменную ──────

def test_detector_catches_a_leak_through_an_intermediate_variable_in_fstring():
    """Живой обман: `msg = str(e)`, сама по себе не f-string — предыдущая
    версия проверки смотрела только на f-string, эту строку не видела
    вовсе; а `f'...{msg}'` для неё было обычным именем, не связанным с `e`."""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        msg = str(e)\n"
        "        return f'Error: {msg}'\n"
    )
    assert _find_raw_exception_leaks(ast.parse(src))


def test_detector_catches_a_leak_through_string_concatenation():
    """«Склеить» из таблицы аудита: `+` вместо f-string/`.format()`."""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        msg = str(e)\n"
        "        return 'Error: ' + msg\n"
    )
    assert _find_raw_exception_leaks(ast.parse(src))


def test_detector_catches_a_leak_through_dot_format():
    """«Подставить через формат» из таблицы аудита: `.format()`."""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        return 'Error: {}'.format(e)\n"
    )
    assert _find_raw_exception_leaks(ast.parse(src))


def test_detector_catches_a_leak_through_percent_formatting():
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        return 'Error: %s' % str(e)\n"
    )
    assert _find_raw_exception_leaks(ast.parse(src))


def test_detector_allows_a_variable_sanitised_by_the_safe_wrapper():
    """Заражение обрывается на присваивании: если переменной присвоен
    результат `_redact_for_user`/`_tool_error`, дальнейшее использование этой
    переменной — уже безопасный текст, а не сырое исключение."""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        msg = _redact_for_user(e)\n"
        "        return f'Error: {msg}'\n"
    )
    assert not _find_raw_exception_leaks(ast.parse(src))


def test_detector_allows_the_exception_class_name_alongside_a_redacted_message():
    """`type(e).__name__`/`e.__class__.__name__` не текут — секрета в имени
    класса нет. Живой пример из server.py:18523."""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        return f'Исключение: `{type(e).__name__}: {_redact_for_user(e)}`'\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e2:\n"
        "        return f'{e2.__class__.__name__}: {_redact_for_user(e2)}'\n"
    )
    assert not _find_raw_exception_leaks(ast.parse(src))


def test_detector_still_catches_the_class_name_trick_hiding_the_real_message():
    """Контроль на саму проверку: `type(e).__name__` безопасен ТОЛЬКО когда
    рядом остаётся редакция сообщения — не превращается в лазейку, если кто-то
    решит, что «раз про класс можно» — можно и `str(e)` рядом без обёртки."""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        return f'{type(e).__name__}: {str(e)}'\n"
    )
    assert _find_raw_exception_leaks(ast.parse(src))


def test_detector_allows_log_redaction_module_wrapper():
    """Файлы вне server.py не видят `_redact_for_user`/`_tool_error` (они
    завязаны на SECRET сервера) — их обёртка `log_redaction.redact(...)`."""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        return f'Error: {log_redaction.redact(str(e))}'\n"
    )
    assert not _find_raw_exception_leaks(ast.parse(src))


def test_detector_does_not_confuse_two_different_handlers_variables():
    """Заражение — по имени переменной ВНУТРИ одного except-блока, а не по
    всему модулю: `msg` из одного обработчика не должен мешать одноимённой,
    но чистой переменной в другом (иначе проверка стала бы неюзабельным
    аллоулистом уже на втором похожем блоке в файле)."""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        msg = str(e)\n"
        "        logger.warning(msg)\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e2:\n"
        "        msg = 'всё в порядке, это обычный текст'\n"
        "        return f'Status: {msg}'\n"
    )
    assert not _find_raw_exception_leaks(ast.parse(src))
