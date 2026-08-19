"""QA 2026-08-19, бага №1 (живая приёмка): `get_task` вообще не показывал
теги задачи.

Тег ставился успешно (подтверждено НЕЗАВИСИМО через `get_tasks_by_tag`), но
`get_task` печатал только Title/Priority/Status/id — строки с тегами не было
ВООБЩЕ. `get_task` — основной инструмент точечной проверки состояния задачи;
слепота к тегам означала, что единственный рабочий способ узнать теги задачи
— перебирать `get_tasks_by_tag` по каждому кандидату, что абсурдно для
пост-верификации.

Корень был чисто в `format_task()`: официальный v1 API отдаёт поле `tags` в
объекте задачи (тот же ключ, что уже читают format_task_line() и
get_task_info() — оба и раньше печатали теги нормально), format_task() просто
никогда не заглядывала в него.
"""
import pytest

import ticktick_mcp.src.server as s


class FakeV1:
    """Официальный клиент: отдаёт ровно то, что вернул бы v1 GET
    /project/{pid}/task/{tid} — включая поле `tags`."""

    def __init__(self, task):
        self._task = task

    def get_task(self, project_id, task_id):
        return dict(self._task)


class FakeV2:
    def __init__(self, trash=None):
        self._trash = trash or []

    def get_trash(self, limit=50):
        return list(self._trash)[:limit]


TAGGED = {"id": "t1", "projectId": "p1", "title": "Полить цветы",
          "status": 0, "tags": ["дом", "срочно"]}
UNTAGGED = {"id": "t2", "projectId": "p1", "title": "Без тегов", "status": 0}


# ─────────── чистый форматтер ───────────

def test_format_task_prints_tags_when_present():
    out = s.format_task(TAGGED)
    assert "Tags:" in out, out
    assert "#дом" in out and "#срочно" in out, out


def test_format_task_prints_no_tags_line_when_absent():
    """Пустого списка/отсутствия поля — строка не печатается вовсе (тот же
    принцип, что у Content/Subtasks выше по функции)."""
    out = s.format_task(UNTAGGED)
    assert "Tags:" not in out, out


def test_format_task_prints_no_tags_line_for_empty_list():
    out = s.format_task({**UNTAGGED, "tags": []})
    assert "Tags:" not in out, out


# ─────────── get_task end-to-end ───────────

async def test_get_task_shows_tags(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeV1(TAGGED))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(trash=[]))
    out = await s.get_task("p1", "t1")
    assert "Tags:" in out, out
    assert "#дом" in out and "#срочно" in out, out


async def test_get_task_untagged_has_no_tags_line(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeV1(UNTAGGED))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(trash=[]))
    out = await s.get_task("p1", "t2")
    assert "Tags:" not in out, out
