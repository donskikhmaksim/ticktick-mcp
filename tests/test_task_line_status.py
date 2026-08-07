"""format_task_line: завершённая задача видна как завершённая (def-D5).

Корень: компактная строка не печатала статус НИКОГДА. В выводе
search_all_tasks(include_completed=True) — а он по умолчанию именно такой —
активные и уже сделанные задачи выглядели абсолютно одинаково, и «есть ли
эта задача в работе» по такому списку определить было нельзя.

Дифф намеренно минимальный: строка ничего больше не меняет (обработку даты
в этой же функции правит соседняя ветка).
"""
import pytest

import ticktick_mcp.src.server as s


class FakeV2:
    def __init__(self, open_tasks, completed):
        self._open = open_tasks
        self._completed = completed

    def get_open_tasks(self):
        return self._open

    # Параметры объявлены явно, как у настоящего клиента: **kwargs проглотил
    # бы опечатку в имени поля (см. tests/test_doubles_do_not_cheat.py).
    def get_completed_tasks(self, limit=50, from_str="", to_str=None):
        return self._completed[:limit]

    def get_state(self, force=False):
        return {"projectProfiles": [{"id": "p1", "name": "Проект"}], "inboxId": "inbox1"}


def test_completed_task_line_says_so():
    line = s.format_task_line({"id": "t1", "title": "Оплатить счёт",
                               "projectId": "p1", "status": 2})
    assert "Оплатить счёт" in line
    assert "completed" in line.lower(), f"статус не виден: {line!r}"


def test_active_task_line_is_not_labelled():
    line = s.format_task_line({"id": "t2", "title": "Позвонить в банк",
                               "projectId": "p1", "status": 0})
    assert "completed" not in line.lower()


def test_wont_do_task_is_distinguishable_too():
    line = s.format_task_line({"id": "t3", "title": "Забытая идея",
                               "projectId": "p1", "status": -1})
    assert "won't do" in line.lower(), f"статус «не буду делать» не виден: {line!r}"


@pytest.fixture
def _clients(monkeypatch):
    monkeypatch.setattr(s, "ticktick", object())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(
        [{"id": "t1", "title": "отчёт живой", "projectId": "p1", "status": 0}],
        [{"id": "t2", "title": "отчёт сданный", "projectId": "p1", "status": 2}],
    ))


async def test_search_all_tasks_marks_completed_hits(_clients):
    out = await s.search_all_tasks("отчёт", include_completed=True, scope="open")

    assert "отчёт живой" in out and "отчёт сданный" in out
    done_line = next(ln for ln in out.splitlines() if "отчёт сданный" in ln)
    live_line = next(ln for ln in out.splitlines() if "отчёт живой" in ln)
    assert "completed" in done_line.lower(), f"завершённая неотличима: {done_line!r}"
    assert "completed" not in live_line.lower()
