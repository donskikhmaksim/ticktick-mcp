"""`get_completed_tasks(limit=N)` — та же мина, что была у get_trash.

Тул принимает limit, но в рендер его не передавал: `format_task_list(tasks)`
с её собственным захардкоженным `limit=100`. Сейчас это не проявляется лишь
потому, что клиент клампит запрос до COMPLETED_MAX_LIMIT = 100 — то есть
корректность держится на СЛУЧАЙНОМ равенстве двух независимых констант в
разных файлах. Поднимут потолок ленты (или TickTick снимет свой) — и записи
со 101-й станут молча недостижимы, ровно как в корзине.

Второе: клиент молча урезает запрошенный limit до потолка ленты. Попросил
300 — получил 100 и не узнал, что это потолок, а не «столько и есть».
"""
import pytest

import ticktick_mcp.src.server as s
from ticktick_mcp.src.ticktick_v2_client import COMPLETED_MAX_LIMIT


class FakeV2:
    """Лента завершённых БЕЗ потолка в 100 — так выглядит мир, если потолок
    поднимут. Тест на устойчивость к этому, а не на сегодняшнее совпадение."""

    def __init__(self, tasks, cap=None):
        self._tasks = tasks
        self._cap = cap
        self.asked_limit = None

    # параметры объявлены ЯВНО, как у настоящего клиента (без **kwargs —
    # иначе двойник проглотит опечатку в имени параметра; см.
    # tests/test_doubles_do_not_cheat.py)
    def get_completed_tasks(self, limit=50, from_str="", to_str=None):
        self.asked_limit = limit
        end = min(limit, self._cap) if self._cap else limit
        return self._tasks[:end]

    def get_state(self):
        return {"projectProfiles": [{"id": "p1", "name": "Работа"}], "inboxId": "inbox1"}


def _completed(n):
    return [{"id": f"c{i}", "title": f"Завершённая задача {i}", "projectId": "p1"}
            for i in range(1, n + 1)]


@pytest.fixture(autouse=True)
def _official(monkeypatch):
    monkeypatch.setattr(s, "ticktick", object())


async def test_render_is_not_capped_below_what_the_feed_returned(monkeypatch):
    """Если лента вернула 250 задач, все 250 обязаны быть напечатаны —
    захардкоженной сотни в рендере быть не должно."""
    fake = FakeV2(_completed(250))
    monkeypatch.setattr(s, "ticktick_v2", fake)

    out = await s.get_completed_tasks(limit=250)

    assert fake.asked_limit == 250
    assert out.count("Завершённая задача ") == 250
    assert "more." not in out


async def test_feed_ceiling_is_disclosed_when_more_was_asked_for(monkeypatch):
    """Просили больше, чем отдаёт лента, — ответ обязан сказать, что это
    потолок ленты, а не полный список."""
    fake = FakeV2(_completed(500), cap=COMPLETED_MAX_LIMIT)
    monkeypatch.setattr(s, "ticktick_v2", fake)

    out = await s.get_completed_tasks(limit=300)

    assert out.count("Завершённая задача ") == COMPLETED_MAX_LIMIT
    assert str(COMPLETED_MAX_LIMIT) in out
    low = out.lower()
    assert "cap" in low or "most" in low or "ceiling" in low, out.splitlines()[0]


async def test_no_ceiling_notice_when_the_limit_fits(monkeypatch):
    """Приписка про потолок не должна появляться там, где её нет о чём
    говорить, иначе она превращается в шум и ей перестают верить."""
    fake = FakeV2(_completed(10), cap=COMPLETED_MAX_LIMIT)
    monkeypatch.setattr(s, "ticktick_v2", fake)

    out = await s.get_completed_tasks(limit=50)

    assert out.count("Завершённая задача ") == 10
    assert "more." not in out
    assert "cap" not in out.lower()
