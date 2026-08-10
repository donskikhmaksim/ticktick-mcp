"""НИЖНИЙ СЛОЙ `duplicate_task` ЧИТАЛ ТОЛЬКО ОТКРЫТЫЕ (живая приёмка
2026-08-07).

    def duplicate_task(self, task_id):
        self.get_state(force=True)
        src = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not src: raise ValueError(f"Open task {task_id} not found.")

Guard класса пропускает завершённую задачу (политика «дублировать
завершённое как шаблон — законно» принята и закреплена в
tests/test_completed_task_class_consistency.py), а клиент на ней падает.
Получается худший из возможных порядков: карточка обещает человеку, что
операция допустима, человек подтверждает кнопкой — и операция не происходит.
РАНЬШЕ отказ хотя бы стоял на плане, ДО согласия; теперь он переехал за
кнопку.

Образец решения лежит рядом в том же клиенте — `find_task_any_state`: она
находит задачу во всех трёх лентах и говорит, В КАКОЙ. Тест написан на
НАСТОЯЩЕМ клиенте (подменён только транспорт, tests/read_stand.py), поэтому
подмена самой чинимой функции двойником его не проходит.
"""
import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

DONE_TITLE = "Купить бумагу"          # rs.COMPLETED[0]
TRASH_TITLE = "Старая затея"          # rs.TRASHED[0]
OPEN_TITLE = "Собрать отчёт"          # rs.TASK_ROOT


def _wire(monkeypatch):
    return rs.wire(monkeypatch,
                   v1_tasks=list(rs.TASKS) + list(rs.COMPLETED) + list(rs.TRASHED))


# ===========================================================================
# 1. Клиент: завершённая задача дублируется
# ===========================================================================

def test_client_duplicates_a_completed_task(monkeypatch):
    v2, _v1, _tr = _wire(monkeypatch)

    copy = v2.duplicate_task(rs.TASK_COMPLETED)

    assert copy.get("title") == f"{DONE_TITLE} (copy)", copy
    assert copy.get("projectId") == rs.P_WORK, copy
    assert copy.get("status") == 0, (
        "копия завершённой задачи обязана быть ОТКРЫТОЙ — иначе «дублирую как "
        f"шаблон на следующий месяц» даёт заранее выполненный шаблон: {copy}")


def test_client_still_refuses_an_unknown_id(monkeypatch):
    """Расширение источника не означает «дублируем что попало»: id, которого
    нет ни в одной ленте, остаётся отказом — и текст обязан называть места,
    где искали, а не утверждать «задачи не существует»."""
    v2, _v1, _tr = _wire(monkeypatch)

    with pytest.raises(ValueError) as e:
        v2.duplicate_task("deadbeefdeadbeefdeadbeef")

    assert "not found" in str(e.value).lower(), str(e.value)


def test_client_refuses_a_trashed_task(monkeypatch):
    """Политика корзины (см. tests/test_trashed_task_is_not_alive.py) держится
    и на нижнем слое: guard отказывает раньше, но клиент, позванный напрямую,
    не имеет права молча воскрешать удалённое в виде копии."""
    v2, _v1, _tr = _wire(monkeypatch)

    with pytest.raises(ValueError) as e:
        v2.duplicate_task(rs.TASK_TRASHED)

    # id стенда сам содержит подстроку «trash» — вырезаем его, иначе проверка
    # прошла бы на любом тексте, где просто повторён id.
    said = str(e.value).lower().replace(rs.TASK_TRASHED, "<id>")
    assert "trash" in said, said
    assert "restore" in said, (
        f"отказ не подсказывает, как вернуть задачу: {said}")


# ===========================================================================
# 2. Сквозь инструмент: план обещал — исполнение сделало
# ===========================================================================

async def test_promise_of_the_card_is_kept_on_a_completed_task(monkeypatch):
    """Главное следствие дефекта: карточка говорит «операция допустима», а за
    кнопкой — ошибка. Проверяется именно эта пара: план не отказывает И
    исполнитель доводит операцию до конца."""
    _v2, _v1, _tr = _wire(monkeypatch)

    plan = await rs.call_direct("duplicate_task", task_id=rs.TASK_COMPLETED,
                         task_title=DONE_TITLE, summary="как шаблон")
    assert "🛑" not in plan, plan

    result = await s._duplicate_task_impl("как шаблон", rs.TASK_COMPLETED,
                                          DONE_TITLE)

    assert "Error" not in result and "🛑" not in result, (
        f"план обещал операцию, а исполнение упало:\n{result}")
    assert result.lstrip().startswith("✅"), result
    assert DONE_TITLE in result, result


async def test_open_task_duplication_unchanged(monkeypatch):
    """Контроль: обычный путь не тронут."""
    _wire(monkeypatch)

    result = await s._duplicate_task_impl("копия", rs.TASK_ROOT, OPEN_TITLE)

    assert result.lstrip().startswith("✅"), result
    assert OPEN_TITLE in result, result
