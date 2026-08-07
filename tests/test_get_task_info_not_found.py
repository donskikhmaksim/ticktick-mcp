"""`get_task_info` отвечал на «не нашёл» одной фразой на ТРИ разные причины,
называя из них две:

    "not found among open tasks (it may be completed or in the trash)"

Задача может отсутствовать в открытом синке, потому что (1) завершена,
(2) лежит в корзине, (3) живёт в архивном/закрытом проекте. Третья не
называлась вовсе, а первые две подавались как равновероятные догадки — при
том что про корзину сервер МОЖЕТ спросить точно (тот же `_trash_state`, что
чинит get_task), и стоит это один запрос, который делается ТОЛЬКО на этой
ветке — то есть в редком случае «не нашёл», а не на каждом успешном чтении.
"""
import pytest

import ticktick_mcp.src.server as s

TASK_ID = "t-missing"


class FakeV2:
    def __init__(self, tasks=None, trash=None, trash_fails=False):
        self._tasks = tasks or []
        self._trash = trash or []
        self._trash_fails = trash_fails

    def get_state(self):
        return {"inboxId": "inbox1", "projectProfiles": [{"id": "p1", "name": "Работа"}],
                "syncTaskBean": {"update": self._tasks}}

    def get_trash(self, limit=50):
        if self._trash_fails:
            raise RuntimeError("v2 session expired")
        return list(self._trash)[:limit]


async def test_missing_task_found_in_trash_is_named_exactly(monkeypatch):
    """Про корзину можно ответить точно — значит гадать нельзя."""
    monkeypatch.setattr(s, "ticktick_v2",
                        FakeV2(trash=[{"id": TASK_ID, "title": "x", "projectId": "p1"}]))
    out = await s.get_task_info(TASK_ID)
    assert "TRASH" in out.upper(), out
    assert "may be completed" not in out.lower(), out


async def test_missing_task_not_in_trash_names_remaining_reasons(monkeypatch):
    """Корзина проверена и пуста — остаются «завершена» и «архивный/закрытый
    проект». Вторую причину фраза не называла вовсе."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(trash=[]))
    out = await s.get_task_info(TASK_ID)
    low = out.lower()
    assert "completed" in low, out
    assert "archived" in low or "closed" in low, out
    assert "not in the trash" in low, out


@pytest.mark.parametrize("v2", [FakeV2(trash_fails=True)])
async def test_missing_task_trash_uncheckable_says_so(v2, monkeypatch):
    """Сверка не состоялась — вывод обязан это признать, а не выдавать
    корзину за одну из равновероятных догадок."""
    monkeypatch.setattr(s, "ticktick_v2", v2)
    out = await s.get_task_info(TASK_ID)
    low = out.lower()
    assert "not checked" in low or "could not" in low, out
    assert "completed" in low and ("archived" in low or "closed" in low), out
