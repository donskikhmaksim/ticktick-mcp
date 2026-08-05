"""assign_owners (БЭКЛОГ-фича): plan_assign / execute_assign.

Two-phase gate (same _MANIFESTS/_require_consent engine every other gated
write in server.py uses) over a rule-based (no-shim, by default in these
tests) owner heuristic: propose an owner by matching a shared-project
member's display name against a task's kanban column name, else its
title/content. execute_assign mirrors update_tasks's own multi-task branch
(identity-guard -> v2 batch update -> post-verify -> journal) rather than
delegating to _update_tasks_impl (see the module-level comment above
plan_assign in server.py for why: that function's single-task branch does
not post-verify the assignee field).
"""
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview) or \
        re.search(r'Манифест `([0-9a-f]+)`', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


class _FakeV2Assign:
    """Minimal fake covering exactly what plan_assign/execute_assign call:
    get_project_members, get_project_columns, batch_update_tasks (mutating a
    shared `live` dict so post-verify's fresh re-read reflects the change),
    and get_tasks_by_tag for the '#tag' scope form."""

    def __init__(self, live, members_by_project=None, columns_by_project=None,
                tag_tasks=None, batch_error_for=None):
        self.live = live
        self.members_by_project = members_by_project or {}
        self.columns_by_project = columns_by_project or {}
        self.tag_tasks = tag_tasks or {}
        # taskId -> error string: batch_update_tasks reports this taskId as
        # failed via id2error, WITHOUT touching `live` for it.
        self.batch_error_for = batch_error_for or {}
        self.calls = []

    def invalidate_cache(self):
        pass

    def get_project_members(self, project_id):
        return self.members_by_project.get(project_id, [])

    def get_project_columns(self, project_id):
        return self.columns_by_project.get(project_id, [])

    def get_tasks_by_tag(self, tag):
        return self.tag_tasks.get(tag, [])

    def batch_update_tasks(self, changes):
        self.calls.append(("update", changes))
        id2error = {}
        for c in changes:
            tid = c["taskId"]
            if tid in self.batch_error_for:
                id2error[tid] = self.batch_error_for[tid]
                continue
            t = self.live.setdefault(tid, {"id": tid})
            if "assignee" in c:
                t["assignee"] = c["assignee"]
        return {"id2error": id2error} if id2error else {}


def _wire(monkeypatch, live, members_by_project=None, columns_by_project=None,
         names=None, tag_tasks=None, batch_error_for=None, shim=False,
         tmp_path=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: names or {"p1": "Работа"})
    fake = _FakeV2Assign(live, members_by_project, columns_by_project,
                         tag_tasks, batch_error_for)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "_dc_shim_available", lambda: shim)
    if tmp_path is not None:
        monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    return fake


MEMBERS = [{"userId": "u1", "displayName": "Максим", "isOwner": True},
           {"userId": "u2", "displayName": "Аня"}]


def _task(tid, title, project="p1", column=None, content="", assignee=None):
    t = {"id": tid, "title": title, "projectId": project, "content": content}
    if column is not None:
        t["columnId"] = column
    if assignee is not None:
        t["assignee"] = assignee
    return t


# ===========================================================================
# _asn_propose_owner — pure rule-based heuristic (no network, no server state)
# ===========================================================================

def test_propose_owner_matches_column_name_first():
    pick = s._asn_propose_owner("Оплатить аренду", "", "Максим", MEMBERS)
    assert pick and pick["userId"] == "u1" and "колонк" in pick["reason"]


def test_propose_owner_falls_back_to_title_content():
    pick = s._asn_propose_owner("Написать Аня про счёт", "", "", MEMBERS)
    assert pick and pick["userId"] == "u2"


def test_propose_owner_none_when_no_member_name_found():
    assert s._asn_propose_owner("Купить молоко", "", "Разное", MEMBERS) is None


# ===========================================================================
# plan_assign — read-only, never mutates
# ===========================================================================

async def test_plan_assign_no_open_tasks_without_assignee(monkeypatch):
    live = {"t1": _task("t1", "A", assignee="u1")}  # already assigned
    fake = _wire(monkeypatch, live, {"p1": MEMBERS})
    result = await s.plan_assign()
    assert "нет" in result
    assert fake.calls == []


async def test_plan_assign_builds_manifest_without_mutating(monkeypatch):
    live = {"t1": _task("t1", "Оплатить аренду", column="c1")}
    fake = _wire(monkeypatch, live, {"p1": MEMBERS},
                {"p1": [{"id": "c1", "name": "Максим"}]})
    preview = await s.plan_assign()
    assert fake.calls == []  # read-only — nothing written
    assert live["t1"].get("assignee") is None
    assert "manifest_id" in preview
    assert "Максим" in preview
    mid = _extract_manifest_id(preview)
    assert s._MANIFESTS[mid]["kind"] == "assign"
    assert s._MANIFESTS[mid]["items"][0]["assignee"] == "u1"


async def test_plan_assign_lists_unconfident_tasks_separately(monkeypatch):
    live = {"t1": _task("t1", "Купить молоко")}  # no member name anywhere
    _wire(monkeypatch, live, {"p1": MEMBERS})
    preview = await s.plan_assign()
    assert "не предложено" in preview.lower() or "Не предложено" in preview
    mid = _extract_manifest_id(preview)
    assert s._MANIFESTS[mid]["items"] == []


async def test_plan_assign_skips_unshared_project(monkeypatch):
    live = {"t1": _task("t1", "Максим сделай это")}
    _wire(monkeypatch, live, {})  # get_project_members -> [] (not shared)
    preview = await s.plan_assign()
    assert "не расшарен" in preview
    assert "manifest_id" not in preview


async def test_plan_assign_cap_refusal(monkeypatch):
    live = {f"t{i}": _task(f"t{i}", "Максим задача") for i in range(5)}
    _wire(monkeypatch, live, {"p1": MEMBERS})
    result = await s.plan_assign(max_tasks=3)
    assert "🛑" in result and "кап" in result.lower()


async def test_plan_assign_tag_scope(monkeypatch):
    live = {}
    tagged = [_task("t1", "Максим: разобрать почту")]
    _wire(monkeypatch, live, {"p1": MEMBERS}, tag_tasks={"work": tagged})
    preview = await s.plan_assign(scope="#work")
    assert "Максим" in preview


# ===========================================================================
# execute_assign — gated, identity-guarded, post-verified
# ===========================================================================

async def test_execute_assign_full_gate_cycle(monkeypatch, tmp_path):
    live = {"t1": _task("t1", "Оплатить аренду", column="c1")}
    fake = _wire(monkeypatch, live, {"p1": MEMBERS},
                {"p1": [{"id": "c1", "name": "Максим"}]}, tmp_path=tmp_path)
    preview = await s.plan_assign()
    mid = _extract_manifest_id(preview)
    assert live["t1"].get("assignee") is None

    refused = await s.execute_assign(mid, user_reply="")
    assert "🛑" in refused
    assert fake.calls == []
    assert live["t1"].get("assignee") is None

    result = await s.execute_assign(mid, user_reply="да, назначай")
    assert fake.calls  # batch_update_tasks actually ran
    assert live["t1"]["assignee"] == "u1"
    assert "✅" in result
    assert "operation_report" in result  # proof block present


async def test_execute_assign_explicit_no_burns_manifest(monkeypatch):
    live = {"t1": _task("t1", "Оплатить аренду", column="c1")}
    _wire(monkeypatch, live, {"p1": MEMBERS}, {"p1": [{"id": "c1", "name": "Максим"}]})
    preview = await s.plan_assign()
    mid = _extract_manifest_id(preview)

    refused = await s.execute_assign(mid, user_reply="нет, не сейчас")
    assert "🛑" in refused
    assert live["t1"].get("assignee") is None

    dead = await s.execute_assign(mid, user_reply="да")
    assert "🛑" in dead
    assert live["t1"].get("assignee") is None


async def test_execute_assign_manifest_is_one_shot(monkeypatch):
    live = {"t1": _task("t1", "Оплатить аренду", column="c1")}
    _wire(monkeypatch, live, {"p1": MEMBERS}, {"p1": [{"id": "c1", "name": "Максим"}]})
    preview = await s.plan_assign()
    mid = _extract_manifest_id(preview)
    await s.execute_assign(mid, user_reply="да")

    second = await s.execute_assign(mid, user_reply="да")
    assert "🛑" in second


async def test_execute_assign_unknown_manifest(monkeypatch):
    _wire(monkeypatch, {}, {"p1": MEMBERS})
    result = await s.execute_assign("does-not-exist", user_reply="да")
    assert "🛑" in result


async def test_execute_assign_wrong_manifest_kind(monkeypatch):
    live = {"t1": _task("t1", "A", column="c1")}
    _wire(monkeypatch, live, {"p1": MEMBERS})
    s._MANIFESTS["fake123"] = {"kind": "delete", "items": [], "created": 0,
                               "plan_shown_at": 0, "consumed": False}
    result = await s.execute_assign("fake123", user_reply="да")
    assert "🛑" in result


async def test_execute_assign_empty_manifest_refuses(monkeypatch):
    live = {"t1": _task("t1", "Купить молоко")}  # no confident proposal
    _wire(monkeypatch, live, {"p1": MEMBERS})
    preview = await s.plan_assign()
    mid = _extract_manifest_id(preview)
    result = await s.execute_assign(mid, user_reply="да")
    assert "🛑" in result and "пуст" in result
    assert s._MANIFESTS[mid]["consumed"] is True


async def test_execute_assign_identity_mismatch_refuses(monkeypatch):
    live = {"t1": _task("t1", "Оплатить аренду", column="c1")}
    _wire(monkeypatch, live, {"p1": MEMBERS}, {"p1": [{"id": "c1", "name": "Максим"}]})
    preview = await s.plan_assign()
    mid = _extract_manifest_id(preview)
    # Title changed live between plan and execute -> id no longer matches.
    live["t1"]["title"] = "Совсем другая задача"

    result = await s.execute_assign(mid, user_reply="да")
    assert live["t1"].get("assignee") is None
    assert "НЕ" in result  # _mismatch_report wording


async def test_execute_assign_missing_task_reports_not_touched(monkeypatch):
    live = {"t1": _task("t1", "Оплатить аренду", column="c1")}
    _wire(monkeypatch, live, {"p1": MEMBERS}, {"p1": [{"id": "c1", "name": "Максим"}]})
    preview = await s.plan_assign()
    mid = _extract_manifest_id(preview)
    del live["t1"]  # task completed/deleted before execute

    result = await s.execute_assign(mid, user_reply="да")
    assert "не найдены" in result.lower() or "↷" in result


async def test_execute_assign_api_rejection_reported_as_failure(monkeypatch):
    live = {"t1": _task("t1", "Оплатить аренду", column="c1")}
    _wire(monkeypatch, live, {"p1": MEMBERS},
                {"p1": [{"id": "c1", "name": "Максим"}]},
                batch_error_for={"t1": "not shared"})
    preview = await s.plan_assign()
    mid = _extract_manifest_id(preview)

    result = await s.execute_assign(mid, user_reply="да")
    assert live["t1"].get("assignee") is None
    assert "❌" in result
    assert "not shared" in result
