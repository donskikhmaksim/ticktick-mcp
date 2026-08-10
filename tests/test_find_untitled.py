"""П15 п.4 — `find_untitled_tasks`: сколько открытых задач без названия в
аккаунте и где они лежат (2026-08-09).

Живой случай, ради которого инструмент заведён: из трёх «пустышек» две
оказались содержательными — фотография чека Home Depot на возврат $374.92 и
скриншот дефекта. Такие задачи обычным поиском не находятся (искать нечего),
поэтому нужен отдельный проход по ВСЕМ открытым задачам с условием «название
пустое».

Две ловушки задания, которые здесь проверяются отдельно:
1. Источник обязан читаться `fresh=True` (иначе задача, созданная минуты
   назад, не попадёт в выборку) — `test_state_unavailable_is_not_zero`
   проверяет ИМЕННО параметр вызова, а не только итоговое число.
2. Пятая категория «источник не показал» не должна тихо сворачиваться в
   «пусто» — `test_unknown_content_is_not_called_empty`.
"""
import ticktick_mcp.src.server as s


def _untitled(tid, pid="p1", **extra):
    """Безымянная (title="") задача с минимальным набором полей."""
    base = {"id": tid, "projectId": pid, "title": ""}
    base.update(extra)
    return base


def _named(tid, title, pid="p1"):
    return {"id": tid, "projectId": pid, "title": title}


def _wire_open_by_id(monkeypatch, by_id, record_fresh=None):
    """Подменяет `_open_by_id`, опционально записывая переданный `fresh` в
    список `record_fresh` — так тест видит, каким аргументом реально был
    вызван источник, а не верит на слово итоговому числу (ловушка №1)."""

    def fake(fresh=False):
        if record_fresh is not None:
            record_fresh.append(fresh)
        return by_id

    monkeypatch.setattr(s, "_open_by_id", fake)


def _wire_names(monkeypatch, names=None):
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: names or {"p1": "Inbox", "p2": "Работа"})


# ═════════════ 1. Находит все три вида безымянных ═════════════

async def test_finds_all_three_kinds(monkeypatch):
    """10 открытых задач, 3 безымянные разных видов (пустая, из пробелов, из
    невидимого символа) → инструмент нашёл ровно 3. `_is_untitled` поймал бы
    только 2 — невидимый символ не пробельный и им не вычищается (см. её
    докстринг); поэтому фильтром обязан быть `_looks_untitled`."""
    zero_width = "​"
    by_id = {}
    by_id["u_empty"] = _untitled("u_empty")
    by_id["u_spaces"] = dict(_untitled("u_spaces"), title="   ")
    by_id["u_invisible"] = dict(_untitled("u_invisible"), title=zero_width)
    for i in range(7):
        by_id[f"n{i}"] = _named(f"n{i}", f"Обычная задача {i}")

    record = []
    _wire_open_by_id(monkeypatch, by_id, record)
    _wire_names(monkeypatch)

    out = await s.find_untitled_tasks()

    assert "Безымянных задач найдено: 3" in out
    assert record == [True]


# ═════════════ 2. Вложение и пустота различимы ═════════════

async def test_attachment_and_empty_are_distinguishable(monkeypatch):
    by_id = {
        "t_file": dict(_untitled("t_file"),
                       attachments=[{"fileName": "receipt.jpg", "id": "a" * 24}]),
        "t_empty": dict(_untitled("t_empty"), attachments=[]),
    }
    _wire_open_by_id(monkeypatch, by_id)
    _wire_names(monkeypatch)

    out = await s.find_untitled_tasks()

    task_lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(task_lines) == 2
    line_with_file = next(ln for ln in task_lines if "t_file" in ln)
    line_empty = next(ln for ln in task_lines if "t_empty" in ln)

    assert line_with_file != line_empty
    assert "📎" in line_with_file
    assert "📎" not in line_empty
    assert "с вложениями — 1" in out
    assert "действительно пустых — 1" in out


# ═════════════ 3. «Не показано» ≠ «пусто» ═════════════

async def test_unknown_content_is_not_called_empty(monkeypatch):
    """Задача, чьи вложения источник не перечислил вовсе (поля `attachments`
    нет, текста нет), не должна попасть в категорию «действительно пустых» —
    это ровно та путаница, из-за которой чек Home Depot чуть не удалили."""
    by_id = {"t_unknown": _untitled("t_unknown")}  # без ключа "attachments"
    _wire_open_by_id(monkeypatch, by_id)
    _wire_names(monkeypatch)

    out = await s.find_untitled_tasks()

    assert "содержимое источник не показал — 1" in out
    assert "действительно пустых — 0" in out
    task_line = next(ln for ln in out.splitlines() if ln.startswith("- "))
    assert "пусто" not in task_line


# ═════════════ 4. Недоступное состояние — «не могу», не 0 ═════════════

async def test_state_unavailable_is_not_zero(monkeypatch):
    record = []
    _wire_open_by_id(monkeypatch, None, record)
    _wire_names(monkeypatch)

    out = await s.find_untitled_tasks()

    assert "проверить не удалось" in out
    assert "0" not in out
    # Ловушка №1: источник обязан быть спрошен именно с fresh=True.
    assert record == [True]


# ═════════════ 5. Оговорка об охвате ═════════════

async def test_coverage_disclaimer_present(monkeypatch):
    by_id = {"t1": _untitled("t1")}
    _wire_open_by_id(monkeypatch, by_id)
    _wire_names(monkeypatch)

    out = await s.find_untitled_tasks()

    assert "Закрытые и архивные проекты" in out
    assert "корзина" in out
    assert "завершённые" in out


# ═════════════ 6. Постраничность называет правду ═════════════

async def test_paging_states_real_total(monkeypatch):
    by_id = {f"t{i}": _untitled(f"t{i}") for i in range(137)}
    _wire_open_by_id(monkeypatch, by_id)
    _wire_names(monkeypatch)

    out = await s.find_untitled_tasks(limit=50)

    assert "показаны 50 из 137" in out
    task_lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(task_lines) == 50
    assert "offset=50" in out
