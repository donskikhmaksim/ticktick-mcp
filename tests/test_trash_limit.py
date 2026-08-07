"""`get_trash(limit=N)` обязан ПОКАЗАТЬ все N записей, а не первые 100.

Живой дефект (2026-08-07): при `get_trash(limit=500)` TickTick вернул 497
записей, а в тексте оказалось 100 и приписка «...and 397 more» — потому что
`format_task_list(tasks)` звали без пользовательского limit, а у неё свой
захардкоженный `limit=100`. Параметр влиял на запрос к TickTick, но не на
вывод, поэтому записи со 101-й были недостижимы НИКАКИМ limit — а корзина
служит доказательством обратимости удаления.
"""
import pytest

import ticktick_mcp.src.server as s

TRASH_SIZE = 150  # больше захардкоженных 100 и меньше потолка клиента (500)


class FakeV2:
    """Только то, что трогает get_trash + рендер строки задачи."""

    def __init__(self, tasks):
        self._tasks = tasks
        self.asked_limit = None

    def get_trash(self, limit=50):
        self.asked_limit = limit
        return self._tasks[:limit]

    def get_state(self):
        return {"projectProfiles": [{"id": "p1", "name": "Работа"}], "inboxId": "inbox1"}


def _trashed(n):
    return [{"id": f"t{i}", "title": f"Удалённая задача {i}", "projectId": "p1"}
            for i in range(1, n + 1)]


@pytest.fixture
def fake_v2(monkeypatch):
    fake = FakeV2(_trashed(TRASH_SIZE))
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "ticktick", object())
    return fake


async def test_get_trash_shows_every_task_it_asked_for(fake_v2):
    out = await s.get_trash(limit=TRASH_SIZE)

    assert fake_v2.asked_limit == TRASH_SIZE
    assert f"Trashed tasks ({TRASH_SIZE})" in out
    # ни одна запись не должна быть срезана рендером
    assert "more." not in out, "часть записей недостижима: вывод обрезан рендером"
    assert f"Удалённая задача {TRASH_SIZE}" in out
    assert f"Удалённая задача {TRASH_SIZE - 1}" in out
    assert out.count("Удалённая задача ") == TRASH_SIZE


async def test_get_trash_small_limit_still_honoured(fake_v2):
    """Обратная сторона: маленький limit не должен вдруг печатать больше."""
    out = await s.get_trash(limit=5)
    assert out.count("Удалённая задача ") == 5
    assert "more." not in out
