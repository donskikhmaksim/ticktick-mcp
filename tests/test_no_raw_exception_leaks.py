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

Эта проверка идёт по ИСХОДНОМУ ДЕРЕВУ `server.py`, а не по выводу одной
команды: для КАЖДОГО `except X as e:` ищет f-string, в который `e` (или
`str(e)`, `{e!r}`) подставлен ГОЛЫМ — то есть НЕ через `_redact_for_user()`
или `_tool_error()` — и это НЕ аргумент `logger.*(...)` (тот канал уже
чистит `SecretPathFilter` из `log_redaction.py`, см. `log_redaction.install`
и `tests/test_log_redaction.py`). Найденное — всегда дефект: не привязана к
конкретной строке/команде, значит ловит СЛЕДУЮЩИЙ такой случай, а не только
уже известные шесть.

Живой пример того, что тест ниже обязан ловить:
    except Exception as e:
        return f"Error fetching X: {e}"          # ГОЛО — тест краснеет
    except Exception as e:
        return f"Error: {_redact_for_user(e)}"    # ОК
    except Exception as e:
        logger.warning(f"diag: {e}")              # ОК — это канал логов
"""
import ast
import pathlib

_SERVER_PATH = (pathlib.Path(__file__).resolve().parent.parent
                / "ticktick_mcp" / "src" / "server.py")

_LOG_METHODS = frozenset({"error", "warning", "info", "debug", "exception",
                          "critical", "log"})
_SAFE_WRAPPER_FUNCS = frozenset({"_redact_for_user", "_tool_error"})


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
    return isinstance(func, ast.Name) and func.id in _SAFE_WRAPPER_FUNCS


def _is_bare_exception_ref(value: ast.AST, var_name: str) -> bool:
    """`{e}` / `{e!r}` (`!r` — атрибут `conversion` на FormattedValue, само
    значение всё равно голое имя) — или явное `{str(e)}`."""
    if isinstance(value, ast.Name) and value.id == var_name:
        return True
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "str" and len(value.args) == 1
            and isinstance(value.args[0], ast.Name)
            and value.args[0].id == var_name)


def _find_raw_exception_leaks(tree: ast.AST) -> list:
    parents = _build_parent_map(tree)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.name:
            continue
        var_name = node.name
        for sub in ast.walk(node):
            if not isinstance(sub, ast.JoinedStr):
                continue
            for value_node in sub.values:
                if not isinstance(value_node, ast.FormattedValue):
                    continue
                if not _is_bare_exception_ref(value_node.value, var_name):
                    continue
                call = _nearest_enclosing_call(value_node, parents)
                if call is not None and _is_safe_call(call):
                    continue
                violations.append((sub.lineno, ast.unparse(sub)[:150]))
    return violations


def test_no_raw_exception_text_in_except_fstrings():
    """Настоящая проверка: server.py целиком, все except-блоки файла."""
    tree = ast.parse(_SERVER_PATH.read_text(encoding="utf-8"))
    violations = _find_raw_exception_leaks(tree)
    assert not violations, (
        "текст исключения из `except ... as e` подставлен ГОЛЫМ в f-string "
        "(в обход _redact_for_user()/_tool_error()) — секрет из текста "
        f"исключения может уйти модели в чат: {violations}")


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
