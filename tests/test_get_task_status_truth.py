"""Дефект: `get_task` дословно печатал «Status: Active» про задачу, которая
лежит в КОРЗИНЕ или помечена «не буду делать» (status -1).

Живьём на двух объектах (`__AUTOTEST__…-2-abandon`), физически находящихся в
корзине — подтверждено `get_trash` — инструмент отвечал «Status: Active».
Корень двойной:

  1. `format_task()` знала ровно два статуса: `2 → Completed`, ВСЁ ОСТАЛЬНОЕ
     → Active. Официальный v1 API отдаёт abandoned-задаче `status: -1`, и она
     молча попадала в ветку «Active». Соседний `get_task_info` (v2) тот же
     `-1` маппит как «Won't do» — то есть правильная карта в репозитории уже
     была, просто не здесь.
  2. Факт «в корзине» официальный v1 API не отдаёт вообще: он честно
     возвращает объект задачи по прямому id и никакого признака удаления в
     нём нет. Поэтому знать это можно ТОЛЬКО сверкой с v2-корзиной, а
     молчание — это утверждение «не в корзине», которого источник не делал.

Почему это тяжелее обычного дефекта отображения: `get_task` был главным
инструментом ДОКАЗАТЕЛЬСТВА в живой приёмке. Врущий инструмент доказательства
обесценивает все выводы по своему классу объектов.
"""
import pytest

import ticktick_mcp.src.server as s


class FakeV1:
    """Официальный клиент: отдаёт ровно то, что вернул бы v1 GET
    /project/{pid}/task/{tid} — включая задачу, лежащую в корзине (признака
    удаления в ответе НЕТ, в этом и суть)."""

    def __init__(self, task):
        self._task = task
        self.calls = 0

    def get_task(self, project_id, task_id):
        self.calls += 1
        return dict(self._task)


class FakeV2:
    def __init__(self, trash=None, fail=False):
        self._trash = trash or []
        self._fail = fail
        self.trash_calls = 0

    def get_trash(self, limit=50):
        self.trash_calls += 1
        if self._fail:
            raise RuntimeError("v2 session expired")
        return list(self._trash)[:limit]


ACTIVE = {"id": "t1", "projectId": "p1", "title": "Deliver the door", "status": 0}
ABANDONED = {**ACTIVE, "id": "t2", "title": "__AUTOTEST__-2-abandon", "status": -1}
DONE = {**ACTIVE, "id": "t3", "status": 2}


def test_format_task_abandoned_is_not_reported_active():
    """Чистый форматтер: status -1 («не буду делать») — не «Active»."""
    out = s.format_task(ABANDONED)
    assert "Status: Active" not in out, out
    assert "Won't do" in out, out


def test_format_task_unknown_status_is_not_silently_active():
    """Любой статус, которого карта не знает, обязан быть виден как
    неизвестный, а не превращаться в «Active» по else-ветке — иначе
    следующий незнакомый код TickTick повторит ровно этот дефект."""
    out = s.format_task({**ACTIVE, "status": 7})
    assert "Status: Active" not in out, out
    assert "7" in out, out


def test_format_task_known_statuses_unchanged():
    """Регресс: правда про 0 и 2 не пострадала."""
    assert "Status: Active" in s.format_task(ACTIVE)
    assert "Status: Completed" in s.format_task(DONE)


async def test_get_task_abandoned_says_wont_do(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeV1(ABANDONED))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(trash=[]))
    out = await s.get_task("p1", "t2")
    assert "Status: Active" not in out, out
    assert "Won't do" in out, out


async def test_get_task_in_trash_is_reported_as_trashed(monkeypatch):
    """Главный кейс. v1 отдаёт объект как ни в чём не бывало; правду знает
    только v2-корзина. Инструмент обязан её назвать, а не печатать «Active»."""
    monkeypatch.setattr(s, "ticktick", FakeV1(ACTIVE))
    v2 = FakeV2(trash=[{"id": "t1", "title": "Deliver the door", "projectId": "p1"}])
    monkeypatch.setattr(s, "ticktick_v2", v2)
    out = await s.get_task("p1", "t1")
    assert "TRASH" in out.upper(), out
    assert "Status: Active" not in out, out
    assert v2.trash_calls == 1, "корзина должна быть проверена ровно один раз"


async def test_get_task_not_in_trash_stays_plain_active(monkeypatch):
    """Контроль: обычная живая задача не получает ложного «в корзине» и не
    обрастает оговорками — сверка состоялась и дала отрицательный ответ."""
    monkeypatch.setattr(s, "ticktick", FakeV1(ACTIVE))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(trash=[{"id": "other", "title": "z"}]))
    out = await s.get_task("p1", "t1")
    assert "Status: Active" in out, out
    assert "TRASH" not in out.upper(), out


@pytest.mark.parametrize("v2", [None, FakeV2(fail=True)])
async def test_get_task_says_when_trash_could_not_be_checked(v2, monkeypatch):
    """Сверка не состоялась (v2 не настроен / упал) — вывод обязан это
    признать. Молчание здесь читается как «точно не в корзине», а этого
    официальный API не гарантирует."""
    monkeypatch.setattr(s, "ticktick", FakeV1(ACTIVE))
    monkeypatch.setattr(s, "ticktick_v2", v2)
    out = await s.get_task("p1", "t1")
    assert "not checked" in out.lower(), out


async def test_get_task_completed_still_completed(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeV1(DONE))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(trash=[]))
    out = await s.get_task("p1", "t3")
    assert "Status: Completed" in out, out
