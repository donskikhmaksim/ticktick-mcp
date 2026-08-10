"""Тест-повторитель для пункта 1.1.7 (docs/TZ/ZAHOD1.md, строки 174-191 и
873-925) — «Инвентаризация описаний: все 77 команд по четырём осям».

ПОЧЕМУ ПОВТОРИТЕЛЬ, А НЕ ОБЫЧНЫЙ УПАВШИЙ ТЕСТ. Продукт пункта 1.1.7 —
перечень (docs/TZ/inventar_opisaniy.md), не правка кода: откатывать нечего,
поэтому «упавший тест» здесь означает другое — тест САМ заново делает ту же
выборку (тот же список зарегистрированных команд, той же командой, что и
в задании — `rg -c "^@mcp\\.tool\\(" ticktick_mcp/src/server.py`, здесь через
`server.mcp.list_tools()`, что даёт тот же результат из реального реестра, а
не текстовым разбором) и сверяет её с зафиксированным содержимым таблицы.
Порча зафиксированного результата (убрать строку, опустошить вердикт) или
изменение условия выборки (найти команду, но не добавить её в таблицу)
обязаны его уронить.

ЧТО ПРОВЕРЯЕТСЯ:
  1. Заново собранный список зарегистрированных `@mcp.tool()` команд —
     ровно 77 штук (контрольное число из задания).
  2. Каждая из них присутствует в таблице `docs/TZ/inventar_opisaniy.md`
     ровно один раз, и в таблице нет ЛИШНИХ строк (строки таблицы == строки
     реестра, без исключений в обе стороны).
  3. Ни одна из 4 клеток-вердиктов (оси а/б/в/г) не пуста ни у одной строки.
  4. Число клеток, помеченных маркером расхождения (⚠ РАСХОЖДЕНИЕ), совпадает
     с числом, зафиксированным в самом файле таблицы (маркер
     `<!-- TOTAL_MISMATCHES: N -->` в конце файла) — так порча ОДНОЙ клетки
     (стереть вердикт с расхождением или добавить фиктивное) расходится с
     зафиксированной цифрой и роняет тест, а не тихо проходит мимо.
"""
import asyncio
import re
from pathlib import Path

import pytest

from ticktick_mcp.src import server

REPO_ROOT = Path(__file__).resolve().parents[1]
TABLE_PATH = REPO_ROOT / "docs" / "TZ" / "inventar_opisaniy.md"

# Контрольное число из задания (docs/TZ/ZAHOD1.md:901): результат
# `rg -c "^@mcp\.tool\(" ticktick_mcp/src/server.py` был 77 на момент
# пункта 1.1.7. 77 → 78 (2026-08-09, П20/ZAHOD1.md §1.3.6): +1 = `delete_tags`
# — строка добавлена в docs/TZ/inventar_opisaniy.md отдельно, см. её
# addendum-примечание там же.
_EXPECTED_TOOL_COUNT = 78

_MISMATCH_MARKER = "⚠ РАСХОЖДЕНИЕ"
_CLEAN_MARKER = "Проверено, чисто"

# Строка данных таблицы: "| `command_name` ... | ось-а | ось-б | ось-в | ось-г |"
_ROW_RE = re.compile(r"^\|\s*`([a-zA-Z_][a-zA-Z0-9_]*)`")
_TOTAL_MARKER_RE = re.compile(r"<!--\s*TOTAL_MISMATCHES:\s*(\d+)\s*-->")


def _registered_tool_names():
    """Тот же реестр, которым сервер реально отвечает на `tools/list` —
    не текстовый grep по исходнику (декоратор может быть закомментирован,
    склеен с предыдущей строкой и т.п. — ровно то, от чего уже страхует
    tests/test_tool_registry.py; здесь используется тот же надёжный путь)."""
    tools = asyncio.run(server.mcp.list_tools())
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "дублирующиеся имена команд в реестре"
    return names


def _read_table_rows():
    """`"| a | b |".strip().strip("|")` yields `" a | b "` — `.strip("|")`
    only eats the two OUTERMOST pipe characters, not the whitespace next to
    them — so splitting by "|" gives exactly N cells for an N-column row,
    with NO empty padding element at either end. A row here has 5 columns
    (name + 4 axes), so `cells` always has length 5, never 7."""
    assert TABLE_PATH.exists(), f"таблица инвентаризации не найдена: {TABLE_PATH}"
    text = TABLE_PATH.read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append({"name": m.group(1), "line": line, "cells": cells})
    return text, rows


def _verdict_cells(row):
    """Клетки осей а/б/в/г, в этом порядке — всё, кроме первой (имя команды)."""
    return row["cells"][1:]


def test_registered_tool_count_is_77():
    names = _registered_tool_names()
    assert len(names) == _EXPECTED_TOOL_COUNT, (
        f"ожидалось {_EXPECTED_TOOL_COUNT} зарегистрированных команд, "
        f"получено {len(names)} — выборка разошлась с зафиксированной в "
        f"docs/TZ/inventar_opisaniy.md (число команд сервера изменилось, "
        f"таблицу нужно пересобрать, а не тихо подогнать число здесь)"
    )


def test_every_registered_tool_is_a_table_row_and_vice_versa():
    registry_names = set(_registered_tool_names())
    _, rows = _read_table_rows()
    table_names = [r["name"] for r in rows]

    assert len(table_names) == len(set(table_names)), (
        "в таблице инвентаризации есть повторяющаяся команда — "
        f"{[n for n in table_names if table_names.count(n) > 1]}"
    )
    assert len(rows) == _EXPECTED_TOOL_COUNT, (
        f"в таблице {len(rows)} строк с командами, ожидалось "
        f"{_EXPECTED_TOOL_COUNT} — пропала или добавилась строка"
    )

    missing_from_table = registry_names - set(table_names)
    extra_in_table = set(table_names) - registry_names
    assert not missing_from_table, (
        f"эти зарегистрированные команды отсутствуют в таблице: "
        f"{sorted(missing_from_table)}"
    )
    assert not extra_in_table, (
        f"в таблице есть команды, которых больше нет в реестре сервера "
        f"(переименованы/удалены — таблицу пора пересобрать): "
        f"{sorted(extra_in_table)}"
    )


def test_no_empty_verdict_cells():
    _, rows = _read_table_rows()
    assert rows, "таблица инвентаризации пуста или не распозналась"
    problems = []
    for r in rows:
        verdict_cells = _verdict_cells(r)
        if len(verdict_cells) != 4:
            problems.append(f"{r['name']}: ожидалось 4 клетки-вердикта, "
                             f"нашлось {len(verdict_cells)} (проверь, что в "
                             f"тексте клетки нет символа '|')")
            continue
        for axis, cell in zip("абвг", verdict_cells):
            if not cell or not cell.strip():
                problems.append(f"{r['name']}: ось «{axis}» пуста")
    assert not problems, "пустые/повреждённые клетки вердиктов:\n" + "\n".join(problems)


def test_mismatch_count_matches_recorded_total():
    text, rows = _read_table_rows()

    m = _TOTAL_MARKER_RE.search(text)
    assert m, (
        "в docs/TZ/inventar_opisaniy.md не найден маркер "
        "'<!-- TOTAL_MISMATCHES: N -->' с зафиксированным числом расхождений"
    )
    recorded_total = int(m.group(1))

    counted_total = sum(
        cell.count(_MISMATCH_MARKER)
        for r in rows
        for cell in r["cells"]
    )

    assert counted_total == recorded_total, (
        f"в самой таблице сейчас {counted_total} клеток с маркером "
        f"'{_MISMATCH_MARKER}', а зафиксировано (TOTAL_MISMATCHES) "
        f"{recorded_total} — таблица испорчена (строка/клетка стёрта или "
        f"добавлена) без обновления зафиксированного числа"
    )
    assert counted_total > 0, (
        "в таблице должно быть хотя бы одно найденное расхождение "
        "(manual_triage, известный случай из задания) — 0 значит, что "
        "либо обход не был реальным, либо таблица испорчена"
    )


def test_every_row_verdict_is_clean_or_marked_mismatch():
    """Каждая клетка обязана быть либо явным «Проверено, чисто», либо нести
    маркер расхождения — вольный текст без одного из двух опознавательных
    слов не считается вердиктом (защита от «write something to fill the
    cell» без реальной проверки)."""
    _, rows = _read_table_rows()
    problems = []
    for r in rows:
        verdict_cells = _verdict_cells(r)
        for axis, cell in zip("абвг", verdict_cells):
            if _MISMATCH_MARKER not in cell and _CLEAN_MARKER not in cell:
                problems.append(
                    f"{r['name']}: ось «{axis}» — вердикт не содержит ни "
                    f"'{_CLEAN_MARKER}', ни '{_MISMATCH_MARKER}': {cell[:80]!r}"
                )
    assert not problems, "клетки без опознаваемого вердикта:\n" + "\n".join(problems)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
