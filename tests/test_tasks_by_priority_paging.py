"""get_tasks_by_priority: хвост списка после 200-й задачи достижим (def-D4).

Корень: вывод обрезался на 200 задач (жёсткий лимит format_task_tree) и
получить остальные было НЕЧЕМ — у инструмента не было ни limit, ни offset.
Пометка «... and N more.» при этом печаталась, то есть человек видел, что
список неполон, но не мог его дочитать: аудит-хвост был недостижим.

Здесь же зафиксирован сам факт пометки, чтобы её нельзя было потерять при
будущих правках форматтера.
"""
import pytest

import ticktick_mcp.src.server as s


class FakeV2:
    """Пул открытых задач v2 — единственное, что трогает этот путь."""

    def __init__(self, tasks):
        self._tasks = tasks

    def get_state(self, force=False):
        return {"syncTaskBean": {"update": self._tasks},
                "projectProfiles": [{"id": "p1", "name": "Проект"}],
                "inboxId": "inbox1"}


@pytest.fixture(autouse=True)
def _clients(monkeypatch):
    tasks = [{"id": f"t{i}", "title": f"Задача {i}", "projectId": "p1", "priority": 5}
             for i in range(250)]
    monkeypatch.setattr(s, "ticktick", object())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))
    return tasks


async def test_tail_beyond_200_is_reachable_via_offset():
    out = await s.get_tasks_by_priority(5, offset=200)

    # Именно те задачи, которые первый экран отрезал.
    assert "Задача 200" in out
    assert "Задача 249" in out
    # И ничего с первого экрана — это вторая страница, а не повтор.
    assert "Задача 0 " not in out and "Задача 199" not in out


async def test_limit_narrows_the_page():
    out = await s.get_tasks_by_priority(5, limit=10)

    assert "Задача 9" in out
    assert "Задача 10 " not in out


async def test_default_page_still_says_how_much_was_cut_and_how_to_continue():
    out = await s.get_tasks_by_priority(5)

    assert "Задача 199" in out          # первые 200 на месте
    assert "Задача 200" not in out      # хвост обрезан
    assert "50 more" in out             # и об этом сказано
    assert "offset=200" in out          # с указанием, как дочитать


async def test_offset_past_the_end_is_reported_not_pretended_empty():
    out = await s.get_tasks_by_priority(5, offset=500)

    # 250 задач есть; пустой экран не должен выглядеть как «задач нет».
    assert "250" in out
    assert "offset" in out.lower()
