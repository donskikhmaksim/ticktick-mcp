"""delete_project: the count-then-confirm guard added after the mutating-tool
audit found this was the one destructive project tool without a blast-radius
disclosure, a confirm cap, or a pre-delete journal snapshot. Mirrors
delete_tasks/execute_task_deletion's own identity-guard + confirm + journal +
post-verify conventions. No real network — the official client and the
project-name resolver are faked."""
import json

import ticktick_mcp.src.server as s


class FakeOfficial:
    """Fakes the bits of TickTickClient delete_project touches."""

    def __init__(self, tasks, project_error=None, delete_error=None):
        self._tasks = tasks
        self._project_error = project_error
        self._delete_error = delete_error
        self.deleted_ids = []

    def get_project_with_data(self, project_id):
        if self._project_error:
            return {"error": self._project_error}
        return {"project": {"id": project_id}, "tasks": self._tasks}

    def delete_project(self, project_id):
        if self._delete_error:
            return {"error": self._delete_error}
        self.deleted_ids.append(project_id)
        return {}


def _mk_task(tid, title, project_id="p1"):
    return {"id": tid, "title": title, "projectId": project_id, "priority": 0}


def _wire(monkeypatch, fake, names):
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "ticktick_v2", None)
    # names is looked up live (twice: identity guard, post-verify) — hand back
    # the SAME dict object each time so a test can mutate it mid-flight to
    # simulate the project actually disappearing after delete_project() runs.
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)
    # Post-verify (delete_project's own + _verify_item's "delete_project"
    # branch) uses the None-distinguishing variant instead — default it to
    # the same live dict so existing tests are unaffected; tests exercising
    # the fetch-failure path override this directly.
    monkeypatch.setattr(s, "_v2_project_names_or_none", lambda: dict(names))


# ---- identity guard: wrong name / unknown id → refuse, nothing touched ----

async def test_identity_mismatch_is_refused(monkeypatch):
    fake = FakeOfficial([])
    _wire(monkeypatch, fake, {"p1": "Работа"})
    result = await s.delete_project("Совсем другое имя", "p1")
    assert "🛑" in result
    assert "Работа" in result
    assert fake.deleted_ids == []


async def test_unknown_project_id_is_refused(monkeypatch):
    fake = FakeOfficial([])
    _wire(monkeypatch, fake, {})
    result = await s.delete_project("Работа", "p1")
    assert "🛑" in result
    assert "не найден" in result
    assert fake.deleted_ids == []


# ---- 1st call (no/omitted confirm): disclose count + sample, delete nothing

async def test_first_call_shows_count_and_sample_without_deleting(monkeypatch):
    tasks = [_mk_task(f"t{i}", f"Задача {i}") for i in range(3)]
    fake = FakeOfficial(tasks)
    _wire(monkeypatch, fake, {"p1": "Работа"})
    result = await s.delete_project("Работа", "p1")
    assert fake.deleted_ids == []
    assert "3 задач" in result
    assert "Задача 0" in result
    assert 'confirm="DELETE 3"' in result


async def test_sample_is_capped_with_a_remainder_note(monkeypatch):
    tasks = [_mk_task(f"t{i}", f"Задача {i}") for i in range(25)]
    fake = FakeOfficial(tasks)
    _wire(monkeypatch, fake, {"p1": "Работа"})
    result = await s.delete_project("Работа", "p1")
    assert fake.deleted_ids == []
    shown = result.count("Задача ")
    assert shown == s._PROJECT_DELETE_SAMPLE_CAP
    assert "и ещё 5" in result


# ---- wrong/stale confirm count → refuse (re-shows the FRESH count) --------

async def test_wrong_confirm_count_is_refused(monkeypatch):
    tasks = [_mk_task(f"t{i}", f"Задача {i}") for i in range(3)]
    fake = FakeOfficial(tasks)
    _wire(monkeypatch, fake, {"p1": "Работа"})
    result = await s.delete_project("Работа", "p1", confirm="DELETE 2")
    assert fake.deleted_ids == []
    assert "3 задач" in result  # fresh recount shown, not the caller's guess


# ---- correct confirm → deletes, journals a pre-delete snapshot -----------

async def test_correct_confirm_deletes_and_journals(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    names = {"p1": "Работа"}
    tasks = [_mk_task(f"t{i}", f"Задача {i}") for i in range(2)]
    open_tasks = {t["id"]: t for t in tasks}
    fake = FakeOfficial(tasks)

    def fake_delete_project(project_id):
        names.pop(project_id, None)  # simulate TickTick's cascade effect
        for t in tasks:
            open_tasks.pop(t["id"], None)
        fake.deleted_ids.append(project_id)
        return {}

    fake.delete_project = fake_delete_project
    _wire(monkeypatch, fake, names)
    # operation_report's independent re-check reads OPEN task state via
    # _open_by_id — fake it too so the report can see the cascade's effect.
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(open_tasks))

    result = await s.delete_project("Работа", "p1", confirm="DELETE 2")
    assert fake.deleted_ids == ["p1"]
    assert "удалён вместе с 2" in result
    assert "operation_report" in result

    journal_path = tmp_path / "deletion_journal.jsonl"
    assert journal_path.exists()
    lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec1 = json.loads(lines[0])
    assert rec1["op"] == "delete_project"
    assert rec1["items"][0]["taskId"] == "p1"
    rec2 = json.loads(lines[1])
    assert len(rec2["deleted"]) == 2
    assert {d["title"] for d in rec2["deleted"]} == {"Задача 0", "Задача 1"}

    # operation_report re-checks the journal against (now post-delete) live
    # state and must confirm BOTH the project and its tasks are gone.
    rid = rec1["manifest"]
    report = s._build_operation_report(rid)
    assert "проект удалён" in report
    assert "Итог: ✅ 3 подтверждено, ❌ 0 расхождений" in report


async def test_zero_task_project_still_requires_explicit_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    names = {"p1": "Пусто"}
    fake = FakeOfficial([])

    def fake_delete_project(project_id):
        names.pop(project_id, None)
        fake.deleted_ids.append(project_id)
        return {}

    fake.delete_project = fake_delete_project
    _wire(monkeypatch, fake, names)

    preview = await s.delete_project("Пусто", "p1")
    assert fake.deleted_ids == []
    assert 'confirm="DELETE 0"' in preview

    result = await s.delete_project("Пусто", "p1", confirm="DELETE 0")
    assert fake.deleted_ids == ["p1"]
    assert "удалён вместе с 0" in result


# ---- post-verify catches a false-positive TickTick response --------------

async def test_operation_report_flags_project_that_did_not_actually_disappear(
        monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = FakeOfficial([])  # delete_project "succeeds" but leaves names as-is
    _wire(monkeypatch, fake, {"p1": "Упрямый"})

    result = await s.delete_project("Упрямый", "p1", confirm="DELETE 0")
    assert "ВСЁ ЕЩЁ существует" in result


# ---- post-verify fetch failure → "unverified", never a false "deleted" ---

async def test_postverify_fetch_failure_is_reported_as_unverified_not_deleted(
        monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = FakeOfficial([])
    _wire(monkeypatch, fake, {"p1": "Работа"})
    # _v2_project_names_or_none() catches its own internal errors and
    # returns None (never raises) — mock that graceful-failure contract.
    monkeypatch.setattr(s, "_v2_project_names_or_none", lambda: None)

    result = await s.delete_project("Работа", "p1", confirm="DELETE 0")
    assert fake.deleted_ids == ["p1"]  # the TickTick call itself did succeed
    assert "НЕ ПОДТВЕРЖДЁН" in result
    assert "удалён вместе с" not in result
    assert "ВСЁ ЕЩЁ существует" not in result


async def test_operation_report_flags_unverified_delete_project_on_fetch_failure(
        monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = FakeOfficial([])
    _wire(monkeypatch, fake, {"p1": "Работа"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})

    await s.delete_project("Работа", "p1", confirm="DELETE 0")
    journal_path = tmp_path / "deletion_journal.jsonl"
    rec1 = json.loads(journal_path.read_text(encoding="utf-8").splitlines()[0])
    rid = rec1["manifest"]

    # Now simulate operation_report being re-run later when the project-name
    # fetch itself is failing — _verify_item's "delete_project" branch must
    # say "unverified", not silently claim success.
    monkeypatch.setattr(s, "_v2_project_names_or_none", lambda: None)
    report = s._build_operation_report(rid)
    assert "НЕ ПОДТВЕРЖДЁН" in report
    assert "проект удалён" not in report


# ---- fetch-contents failure → refuse, never delete blind ------------------

async def test_contents_fetch_error_refuses_before_any_delete(monkeypatch):
    fake = FakeOfficial([], project_error="rate limited")
    _wire(monkeypatch, fake, {"p1": "Работа"})
    result = await s.delete_project("Работа", "p1", confirm="DELETE 0")
    assert fake.deleted_ids == []
    assert "🛑" in result
    assert "rate limited" in result
