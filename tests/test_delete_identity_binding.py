"""Tests for the FULL identity binding of an approved deletion: what actually
gets deleted must be exactly the task the human saw in the plan — same id,
same title AND same project. Anything that no longer matches the approved
plan is fail-closed (skipped + reported), never deleted "just in case".

Also covers the declutter manifest's object_hash binding (until now only
delete manifests carried one)."""
import time

import ticktick_mcp.src.server as s
# Автоуборка вынесена за пределы пакета (attic/declutter_disabled.py,
# пункт 1.2.4 захода 1, 2026-08-09) — загрузчик возвращает её определения
# в пространство имён `s`, где их ищут тесты ниже. См. tests/attic_loader.py.
from tests import attic_loader as _attic  # noqa: F401


class _FakeV2Delete:
    def __init__(self, live):
        self._live = live
        self.deleted_ids = []

    def batch_delete_tasks(self, items):
        for it in items:
            self._live.pop(it["taskId"], None)
            self.deleted_ids.append(it["taskId"])
        return {}


def _wire(monkeypatch, live, tmp_path, names=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: names or {"p1": "Покупки", "p2": "Работа"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = _FakeV2Delete(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


def _manifest(mid, items, summary="test"):
    now = time.monotonic()
    s._MANIFESTS[mid] = {
        "kind": "delete", "items": items, "created": now,
        "plan_shown_at": now, "summary": summary, "consumed": False,
        "object_hash": s._manifest_object_hash(
            "delete", [it["taskId"] for it in items]),
    }


async def test_task_moved_to_another_project_since_the_plan_is_not_deleted(
        monkeypatch, tmp_path):
    """The human approved «Купить молоко» IN «Покупки». By execute time the
    task sits in «Работа» — that is no longer the object that was approved."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p2"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _manifest("mid-moved", [{"taskId": "t1", "projectId": "p1",
                             "title": "Купить молоко", "project": "Покупки",
                             "snapshot": {"title": "Купить молоко"}}])

    out = await s.execute_task_deletion("mid-moved", user_reply="да")
    assert fake.deleted_ids == []
    assert "t1" in live
    assert "Пропущены" in out and "проект" in out


async def test_sheet_style_item_without_project_id_is_bound_by_project_name(
        monkeypatch, tmp_path):
    """Sheet-backed declutter rows carry only the project NAME (projectId is
    an empty placeholder) — the binding must still be armed by that name."""
    live = {"t1": {"id": "t1", "title": "Дубль", "projectId": "p2"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _manifest("mid-sheet", [{"taskId": "t1", "projectId": "",
                             "title": "Дубль", "project": "Покупки",
                             "snapshot": {"title": "Дубль"}}])

    out = await s.execute_task_deletion("mid-sheet", user_reply="да")
    assert fake.deleted_ids == []
    assert "Пропущены" in out


async def test_matching_title_and_project_is_deleted(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _manifest("mid-ok", [{"taskId": "t1", "projectId": "p1",
                          "title": "Купить молоко", "project": "Покупки",
                          "snapshot": {"title": "Купить молоко"}}])

    out = await s.execute_task_deletion("mid-ok", user_reply="да")
    assert fake.deleted_ids == ["t1"]
    assert "Удалено 1" in out


async def test_item_without_a_title_is_never_deleted_unverified(
        monkeypatch, tmp_path):
    """No title stored in the plan → the id↔task check has nothing to verify
    against → refuse rather than delete on a bare id."""
    live = {"t1": {"id": "t1", "title": "Что-то живое", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _manifest("mid-untitled", [{"taskId": "t1", "projectId": "p1",
                                "title": "", "project": "Покупки",
                                "snapshot": {}}])

    out = await s.execute_task_deletion("mid-untitled", user_reply="да")
    assert fake.deleted_ids == []
    assert "t1" in live
    assert "Пропущены" in out


async def test_neighbouring_task_is_untouched_when_one_item_drifts(
        monkeypatch, tmp_path):
    """A batch where ONE approved item drifted must still delete the others —
    and must never touch a task that was not in the manifest at all."""
    live = {
        "t1": {"id": "t1", "title": "A", "projectId": "p1"},
        "t2": {"id": "t2", "title": "B-переименована", "projectId": "p1"},
        "t3": {"id": "t3", "title": "Соседняя", "projectId": "p1"},
    }
    fake = _wire(monkeypatch, live, tmp_path)
    _manifest("mid-mixed", [
        {"taskId": "t1", "projectId": "p1", "title": "A", "project": "Покупки",
         "snapshot": {"title": "A"}},
        {"taskId": "t2", "projectId": "p1", "title": "B", "project": "Покупки",
         "snapshot": {"title": "B"}},
    ])

    out = await s.execute_task_deletion("mid-mixed", user_reply="да")
    assert fake.deleted_ids == ["t1"]
    assert "t3" in live and "t2" in live
    assert "Пропущены 1" in out


# ---------------------------------------------------------------------------
# declutter manifest binding (object_hash) — parity with delete manifests
# ---------------------------------------------------------------------------

def test_dc_object_ids_covers_every_mutating_action():
    actions = {
        "delete": [{"taskId": "d1"}],
        "rename": [{"taskId": "r1"}],
        "group": [{"children": [{"taskId": "g1"}, {"taskId": "g2"}]}],
        "flag_obsolete": [{"taskId": "ignored"}],
    }
    assert sorted(s._dc_object_ids(actions)) == ["d1", "g1", "g2", "r1"]


async def test_declutter_manifest_mutated_after_the_plan_is_refused(
        monkeypatch, tmp_path):
    """If the stored actions changed between plan and the user's "yes", the
    object_hash no longer matches what was shown — refuse, don't apply."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    actions = {"delete": [{"taskId": "d1", "projectId": "p1", "title": "X",
                           "project": "Покупки", "snapshot": {}}],
               "rename": [], "group": [],
               "flag_obsolete": [], "flag_dupe": [], "flag_nonsmart": []}
    now = time.monotonic()
    s._MANIFESTS["mid-dc"] = {
        "kind": "declutter", "actions": actions, "mutating_count": 1,
        "created": now, "plan_shown_at": now, "summary": "разбор",
        "consumed": False,
        "object_hash": s._manifest_object_hash("declutter",
                                               s._dc_object_ids(actions)),
    }
    # Someone/something swapped the target after the plan was printed.
    actions["delete"][0]["taskId"] = "OTHER"

    out = await s.execute_declutter("mid-dc", user_reply="да")
    assert "🛑" in out
    assert s._MANIFESTS["mid-dc"]["consumed"] is False
