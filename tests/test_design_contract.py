"""Контракт проектного документа `docs/DESIGN_apply_task_changes.md` (пункт 1.3.2).

Эти тесты судят ДОКУМЕНТ, а не код: пункт 1.3.2 сдаётся до первой строки реализации, и
единственный способ проверить его машинно — читать сам файл. Проверяется ровно то, чем этот
пункт можно подделать:

  * таблица двенадцати типов существует, содержит все двенадцать имён и **ни одной пустой,
    прочерковой или отсылочной клетки** («по аналогии с соседом» — это способ сдать семь новых
    типов, не спроектировав ни одного);
  * у каждого блока `### изм-` есть все четыре обязательные строки (`Файлы:`, `Функции:`,
    `Зависит от:`, `Тесты:`) — блок без строки `Тесты:` описывает изменение, приёмку которого
    никто не сформулировал;
  * каждое имя вида `_*_impl`, на которое ссылается документ, действительно есть в `server.py`
    (архитектор ссылался на существующие функции, а не на воображаемые);
  * заявленная параллельность изменений подтверждена НАПЕЧАТАННЫМИ списками функций, и эти
    списки совпадают со строками `Функции:` соответствующих блоков.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "docs" / "DESIGN_apply_task_changes.md"
SERVER = ROOT / "ticktick_mcp" / "src" / "server.py"

# Имена типов зафиксированы ТЗ (заход 1, пункты 1.3.2 и 1.3.3) — не выводятся из документа,
# иначе документ проверял бы сам себя.
EXPECTED_TYPES = {
    "create", "parent", "unparent", "restore", "duplicate", "tags",
    "update", "move", "complete", "abandon", "merge", "delete",
}

REQUIRED_CHANGE_FIELDS = ("Файлы:", "Функции:", "Зависит от:", "Тесты:")

# Клетка, состоящая из этого, — «решение не принято», как бы она ни выглядела в отрисованной
# таблице.
_DASHES = {"", "-", "--", "—", "–", "?", "n/a", "n/д", "н/д", "нет", "то же", "тот же"}
# Отсылка вместо содержания: по такой клетке нельзя написать ни обработчика, ни теста.
_REFERRALS = ("по аналогии", "аналогично", "см. выше", "см. соседн", "как у сосед",
              "то же, что", "как выше", "как в строке")
# Клетка, которая объявляет отсутствие проверки, обязана назвать причину.
_NO_CHECK = ("не проверяется", "не сверяется", "не проверяем", "ветки дрейфа")
_REASONS = ("причина", "потому", "так как", "иначе", "поэтому")


def _text() -> str:
    assert DESIGN.exists(), f"нет файла дизайна: {DESIGN}"
    return DESIGN.read_text(encoding="utf-8")


def _section(title_prefix: str) -> str:
    """Тело раздела `## <title_prefix>…` до следующего `## `."""
    text = _text()
    chunks = re.split(r"^## ", text, flags=re.M)
    for chunk in chunks[1:]:
        if chunk.startswith(title_prefix):
            return chunk
    pytest.fail(f"в документе нет раздела «## {title_prefix}…»")


def _table_rows(section: str):
    """Строки данных markdown-таблицы: без шапки и без строки-разделителя."""
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):   # разделитель шапки
            continue
        rows.append(cells)
    return rows[1:] if rows else rows                   # первая строка — шапка


def test_design_file_exists_and_has_ten_sections():
    """Ровно десять разделов `## ` — состав документа задан ТЗ поимённо."""
    heads = re.findall(r"^## .*$", _text(), flags=re.M)
    assert len(heads) == 10, f"разделов `## ` должно быть 10, а их {len(heads)}: {heads}"


def test_design_lists_twelve_types():
    """Раздел 2: ровно двенадцать строк, те самые двенадцать типов, ни одной пустой клетки."""
    rows = _table_rows(_section("2."))
    assert rows, "в разделе 2 нет таблицы типов"
    assert len(rows) == 12, f"в таблице типов должно быть 12 строк, а их {len(rows)}"

    names = set()
    for cells in rows:
        assert len(cells) == 5, (
            "строка таблицы типов должна иметь 5 колонок (тип + вход + план + исполнение + "
            f"подтверждение), а имеет {len(cells)}: {cells}")
        name = cells[0].strip().strip("`").strip()
        names.add(name)
        for col, cell in zip(("вход", "план", "исполнение", "подтверждение"), cells[1:]):
            low = cell.lower().strip()
            assert low not in _DASHES, f"тип {name}: клетка «{col}» — прочерк, решение не принято"
            assert len(low) >= 25, (
                f"тип {name}: клетка «{col}» слишком коротка ({len(low)} симв.) — "
                "по ней нельзя написать ни обработчика, ни теста")
            for bad in _REFERRALS:
                assert bad not in low, (
                    f"тип {name}: клетка «{col}» отсылает к соседу («{bad}») вместо решения")
            if any(marker in low for marker in _NO_CHECK):
                assert any(r in low for r in _REASONS), (
                    f"тип {name}: клетка «{col}» объявляет отсутствие проверки, "
                    "но не называет причину")

    assert names == EXPECTED_TYPES, (
        f"набор типов разошёлся с ТЗ: лишние {sorted(names - EXPECTED_TYPES)}, "
        f"недостающие {sorted(EXPECTED_TYPES - names)}")


def test_design_changes_have_four_fields():
    """У каждого блока `### изм-` все четыре обязательные строки: четыре счётчика равны."""
    text = _text()
    blocks = re.findall(r"^### изм-", text, flags=re.M)
    assert len(blocks) >= 12, (
        f"блоков `### изм-` должно быть не меньше 12 (минимальный состав задан ТЗ), "
        f"а их {len(blocks)}")
    counts = {f: len(re.findall(rf"^{re.escape(f)}", text, flags=re.M))
              for f in REQUIRED_CHANGE_FIELDS}
    assert set(counts.values()) == {len(blocks)}, (
        f"число блоков `### изм-` = {len(blocks)}, а счётчики обязательных строк: {counts}")


def test_design_impl_names_exist():
    """Каждое имя `_*_impl` из документа существует в server.py как определение функции."""
    mentioned = set(re.findall(r"_[A-Za-z0-9_]+_impl", _text()))
    assert mentioned, "документ не ссылается ни на одну функцию `_*_impl` — это подозрительно"
    server = SERVER.read_text(encoding="utf-8")
    defined = set(re.findall(r"^(?:async )?def (_[A-Za-z0-9_]+_impl)\(", server, flags=re.M))
    missing = sorted(mentioned - defined)
    assert not missing, (
        f"документ ссылается на несуществующие функции: {missing} "
        f"(в {SERVER.name} их нет)")


def test_design_parallel_pairs_have_disjoint_functions():
    """Каждая объявленная параллельная пара печатает списки функций, и они не пересекаются.

    Списки сверяются со строками `Функции:` самих блоков: напечатать рядом с «Параллельно»
    два удобных непересекающихся списка, не совпадающих с блоками, — это способ объявить
    ложную параллельность."""
    text = _text()
    by_block = {}
    for num, body in re.findall(r"^### изм-(\d+)\.(.*?)(?=^### |\Z)", text,
                                flags=re.M | re.S):
        line = re.search(r"^Функции:(.*)$", body, flags=re.M)
        assert line, f"в блоке изм-{num} нет строки `Функции:`"
        by_block[num] = set(re.findall(r"`([^`]+)`", line.group(1)))

    pairs = re.findall(r"\*\*Параллельно: изм-(\d+) ∥ изм-(\d+)\.?\*\*", text)
    assert pairs, "в разделе 10 не объявлено ни одной параллельной пары"
    for a, b in pairs:
        printed = {}
        for num in (a, b):
            line = re.search(rf"^Функции изм-{num}:(.*)$", text, flags=re.M)
            assert line, (
                f"пара изм-{a} ∥ изм-{b} объявлена, но список функций изм-{num} не напечатан")
            printed[num] = set(re.findall(r"`([^`]+)`", line.group(1)))
            assert printed[num], f"список функций изм-{num} пуст"
            assert printed[num] == by_block.get(num), (
                f"напечатанный список функций изм-{num} не совпадает со строкой `Функции:` "
                f"его блока: {sorted(printed[num] ^ by_block.get(num, set()))}")
        overlap = printed[a] & printed[b]
        assert not overlap, (
            f"изм-{a} и изм-{b} объявлены параллельными, но делят функции: {sorted(overlap)}")
