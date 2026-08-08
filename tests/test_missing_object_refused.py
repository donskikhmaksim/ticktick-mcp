"""Дефект (живая приёмка 2026-08-07): `get_task` и `get_project` на заведомо
НЕСУЩЕСТВУЮЩЕМ идентификаторе возвращали не отказ, а правдоподобную карточку
несуществующего объекта:

    Title: No title          Name: No name
    Priority: None           Folder: (none)
    Status: Active           (id: ?)
    (id: ? | project: ?)

То есть по ответу НЕЛЬЗЯ отличить «объект есть» от «объекта не существует» —
инструмент, которым доказывают факты о конкретной задаче, придумывал факт.

Корень двойной:
  1. официальный v1 API на несуществующий id отвечает пустым телом, а
     `_make_request` превращает пустое тело в `{}` (это его нормальное
     соглашение для 204/пустого ответа);
  2. вызывающий код проверял ТОЛЬКО ключ `error` — пустой словарь ошибкой не
     считался и уезжал в `format_task()`/`format_project()`, где каждое поле
     подменялось заглушкой («No title», «(id: ?)»), а `status` по умолчанию 0
     давал бодрое «Status: Active».

Тот же инвариант в репозитории уже был принят — `_official_task_snapshot()`
отбраковывает ответ, у которого `id` не совпал с запрошенным. Здесь он просто
доведён до двух точечных читателей.
"""
import ticktick_mcp.src.server as s

GHOST_TASK = "deadbeefdeadbeefdeadbeef"
GHOST_PROJECT = "cafebabecafebabecafebabe"


class FakeV1:
    """Официальный клиент: отдаёт РОВНО то, что вернул бы v1 на неизвестный
    id — пустой словарь (пустое тело ответа), без всякого признака ошибки."""

    def __init__(self, task=None, project=None):
        self._task = {} if task is None else task
        self._project = {} if project is None else project

    def get_task(self, project_id, task_id):
        return dict(self._task)

    def get_project(self, project_id):
        return dict(self._project)


class FakeV2:
    """Корзина пуста и читается — чтобы отказ нельзя было объяснить упавшей
    проверкой корзины."""

    def get_trash(self, limit=50):
        return []

    def get_state(self):
        return {"inboxId": "inbox1", "projectProfiles": [], "projectGroups": []}


async def test_get_task_on_missing_id_refuses_instead_of_inventing(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeV1())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_task(project_id="p1", task_id=GHOST_TASK)

    # 1. Это ОТКАЗ, а не карточка.
    assert "No title" not in out, out
    assert "Status: Active" not in out, out
    assert "(id: ? | project: ?)" not in out, out
    # 2. Отказ называет id, который искали, — иначе непонятно, о чём он.
    assert GHOST_TASK in out, out
    assert "не найдена" in out.lower() or "not found" in out.lower(), out


async def test_get_task_on_foreign_id_refuses(monkeypatch):
    """Ответ пришёл непустой, но это ДРУГАЯ задача — подтверждать по нему
    запрошенный id нельзя (тот же инвариант, что в _official_task_snapshot)."""
    monkeypatch.setattr(s, "ticktick", FakeV1(
        task={"id": "someone-else", "title": "Чужая задача", "projectId": "p1"}))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_task(project_id="p1", task_id=GHOST_TASK)

    assert "Чужая задача" not in out, out
    assert GHOST_TASK in out, out


async def test_get_task_still_formats_a_real_task(monkeypatch):
    """Контроль: настоящая задача по-прежнему печатается как раньше."""
    monkeypatch.setattr(s, "ticktick", FakeV1(
        task={"id": "t1", "title": "Живая задача", "projectId": "p1", "status": 0}))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_task(project_id="p1", task_id="t1")

    assert "Title: Живая задача" in out, out
    assert "Status: Active" in out, out


async def test_get_project_on_missing_id_refuses_instead_of_inventing(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeV1())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_project(project_id=GHOST_PROJECT)

    assert "No name" not in out, out
    assert "(id: ?)" not in out, out
    assert GHOST_PROJECT in out, out
    assert "не найден" in out.lower() or "not found" in out.lower(), out


async def test_get_project_still_formats_a_real_project(monkeypatch):
    """Контроль: настоящий проект по-прежнему печатается как раньше."""
    monkeypatch.setattr(s, "ticktick", FakeV1(
        project={"id": "p1", "name": "Работа"}))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_project(project_id="p1")

    assert "Name: Работа" in out, out
    assert "(id: p1)" in out, out
