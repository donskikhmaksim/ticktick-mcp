"""Синонимы имён параметра «родитель» (QA-2 2026-08-19, добор №6).

Одна и та же концепция звалась по-разному в двух точках:
`plan_task_creation` принимал `parent_id`/`parent_title`, а
`apply_task_changes(op="parent")` — `to_task_id`/`to_title`. Модель,
выучившая один путь, ошибалась на другом (наблюдалось в QA живьём: первая
попытка вложения с `parent_id` была отвергнута). Теперь ОБЕ точки принимают
ОБА набора имён как синонимы — существующие имена не тронуты (на них
завязаны тесты и живые вызовы), а конфликт двух имён с РАЗНЫМИ значениями —
явный отказ, не молчаливый выбор сервера.
"""
import re

import pytest

import ticktick_mcp.src.server as s


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _mid(preview: str) -> str:
    m = re.search(r"Манифест `([0-9a-f]+)`", preview)
    assert m, f"в превью нет id манифеста:\n{preview}"
    return m.group(1)


# ═══════ plan_task_creation принимает to_task_id/to_title ═══════

_LIVE = {"par": {"id": "par", "title": "Ипотека", "projectId": "p1"}}


def _wire_creation(monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(_LIVE))


async def test_plan_creation_accepts_to_task_id_to_title_as_parent(monkeypatch):
    _wire_creation(monkeypatch)
    out = await s.plan_task_creation(
        "Создаю подзадачу",
        [{"title": "Позвонить в банк", "project_id": "p1",
          "to_task_id": "par", "to_title": "Ипотека"}])
    # Строка спланирована ИМЕННО как подзадача — карточка называет вложение.
    assert "подзадача" in out, out
    assert "Ипотека" in out, out
    mid = _mid(out)
    raw = s._MANIFESTS[mid]["raw"][0]
    assert raw.get("parent_id") == "par", raw


async def test_plan_creation_conflicting_parent_names_refuse_the_row(monkeypatch):
    _wire_creation(monkeypatch)
    out = await s.plan_task_creation(
        "Создаю", [{"title": "Позвонить в банк", "project_id": "p1",
                    "parent_id": "par", "to_task_id": "другой-id",
                    "parent_title": "Ипотека"}])
    assert "синоним" in out, out
    assert "Исключены" in out or "манифест НЕ" in out, out


async def test_plan_creation_canonical_names_still_work(monkeypatch):
    """Регресс: родные имена parent_id/parent_title не тронуты."""
    _wire_creation(monkeypatch)
    out = await s.plan_task_creation(
        "Создаю подзадачу",
        [{"title": "Позвонить в банк", "project_id": "p1",
          "parent_id": "par", "parent_title": "Ипотека"}])
    assert "подзадача" in out, out


# ═══════ apply_task_changes(op="parent") принимает parent_id/parent_title ══

class _FakeV2:
    def __init__(self, live):
        self.live = live
        self.calls = []

    def invalidate_cache(self):
        pass

    def get_open_tasks(self):
        return list(self.live.values())

    def get_state(self, force=False):
        return {"tags": []}

    def set_task_parents(self, rows):
        self.calls.append(("parent", [r["taskId"] for r in rows],
                           rows[0]["parentId"] if rows else None))
        for r in rows:
            if r["taskId"] in self.live:
                self.live[r["taskId"]]["parentId"] = r["parentId"]
        return {}


def _wire_apply(monkeypatch, live, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2 = _FakeV2(live)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    return v2


async def test_apply_parent_accepts_parent_id_parent_title(monkeypatch, tmp_path):
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p1"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p1"},
    }
    v2 = _wire_apply(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "parent", "task_id": "kid", "title": "Позвонить в банк",
         "parent_id": "par", "parent_title": "Ипотека",
         "said": "это часть ипотеки"}])
    assert "🛑" not in preview.splitlines()[0], preview
    assert "Ипотека" in preview, preview

    out = await s.apply_task_changes("Разбираю", manifest_id=_mid(preview),
                                     user_reply="да, давай")
    # Судим по ЖИВОМУ состоянию, а не по тексту (стиль test_triage_new_types).
    assert live["kid"].get("parentId") == "par", (out, live)
    assert v2.calls and v2.calls[0][0] == "parent"


async def test_apply_parent_conflicting_names_refuse_outright(
        monkeypatch, tmp_path):
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p1"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p1"},
    }
    v2 = _wire_apply(monkeypatch, live, tmp_path)

    out = await s.apply_task_changes("Разбираю", [
        {"op": "parent", "task_id": "kid", "title": "Позвонить в банк",
         "to_task_id": "par", "parent_id": "другой-id",
         "to_title": "Ипотека", "said": "это часть ипотеки"}])
    assert "🛑" in out and "синоним" in out, out
    assert v2.calls == []
    assert live["kid"].get("parentId") is None


async def test_apply_parent_same_value_in_both_names_is_fine(
        monkeypatch, tmp_path):
    """Оба имени с ОДНИМ значением — не конфликт (модель могла прислать оба
    «на всякий случай»)."""
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p1"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p1"},
    }
    _wire_apply(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "parent", "task_id": "kid", "title": "Позвонить в банк",
         "to_task_id": "par", "parent_id": "par",
         "to_title": "Ипотека", "said": "это часть ипотеки"}])
    assert "🛑" not in preview.splitlines()[0], preview
