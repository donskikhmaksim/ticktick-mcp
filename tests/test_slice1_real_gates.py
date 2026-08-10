"""Slice 1 — real server-side consent gates on the 7 methods that used to be
"docstring-only" (the model could mutate without ANY real user_reply check):
update_tasks, complete_tasks, move_tasks, set_task_parent, set_task_tags,
restore_tasks, execute_task_creation.

Pattern under test (docs/DESIGN_approval_gate.md §4, mirrored from
delete_tasks): each of the 6 batch tools is now ONE function, called TWICE —
call #1 (manifest_id omitted) builds a manifest and returns a preview,
nothing mutates; call #2 (manifest_id + user_reply) is checked by
_require_consent(tier=1, ...) and only then runs the real mutation.
execute_task_creation keeps its existing plan/execute pair but the tier was
bumped 0 -> 1 so user_reply is now hard-enforced.

Also verifies the internal-call fix: execute_declutter/resume_declutter call
an already-approved declutter manifest's rename/group actions through
_update_tasks_impl/_set_task_parent_impl directly — NOT through the public
gated update_tasks/set_task_parent — so an already-obtained "yes" is never
asked for twice.

Note: an explicit NEGATIVE reply ("нет"/"стоп"/...) burns the manifest
(one-shot invalidation, see _require_consent) — tests that continue on to a
successful confirm after checking the "no reply yet" case use an EMPTY
user_reply for that intermediate check (retryable), and cover the
negative-burns-the-manifest behaviour in its own dedicated test instead.
"""
import re
import time

import ticktick_mcp.src.server as s
# Автоуборка вынесена за пределы пакета (attic/declutter_disabled.py,
# пункт 1.2.4 захода 1, 2026-08-09) — загрузчик возвращает её определения
# в пространство имён `s`, где их ищут тесты ниже. См. tests/attic_loader.py.
from tests import attic_loader as _attic  # noqa: F401


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


class _FakeV2:
    """Minimal fake covering every v2 batch call the 6 gated tools use,
    mutating a shared `live` dict (and an optional `trash` dict for
    restore_tasks) so post-verify's fresh re-read reflects the change.

    `tags` is the fake account tag list (list of {"name","label"} dicts) that
    backs get_tags()/get_state()/create_tag() — set_task_tags auto-registers
    not-yet-existing tags through create_tag before touching any task (see
    tests/test_set_task_tags_orphans.py for the dedicated coverage of that
    path); this fake just needs to not blow up with AttributeError when it's
    called incidentally by the other gate-cycle tests in this file."""

    def __init__(self, live, trash=None, tags=None):
        self.live = live
        self.trash = trash or {}
        self.calls = []
        self.tags = tags if tags is not None else []

    def invalidate_cache(self):
        pass

    def get_state(self, force=False):
        # Read-only — deliberately NOT appended to self.calls, which this
        # file's tests treat as a mutation log (`fake.calls == []` after a
        # preview-only call #1 asserts "nothing was mutated", not "nothing
        # was read").
        return {"tags": self.tags}

    def get_tags(self):
        return self.tags

    def create_tag(self, name, color=None):
        # Как настоящий клиент: только нижний регистр. Решётку обязан срезать
        # вызывающий (server.py) — двойник за него этого не делает, иначе не
        # заметил бы, что нормализация из сервера исчезла.
        self.calls.append(("create_tag", name))
        self.tags.append({"name": name.lower(), "label": name, "color": color})
        return {}

    def batch_update_tasks(self, changes):
        self.calls.append(("update", changes))
        for c in changes:
            t = self.live.setdefault(c["taskId"], {"id": c["taskId"]})
            if "title" in c:
                t["title"] = c["title"]
            if "tags" in c:
                t["tags"] = c["tags"]
            if "priority" in c:
                t["priority"] = c["priority"]
            if "dueDate" in c:
                t["dueDate"] = c["dueDate"]
            if "startDate" in c:
                t["startDate"] = c["startDate"]
        return {}

    def batch_complete_tasks(self, ids):
        self.calls.append(("complete", ids))
        for tid in ids:
            self.live.pop(tid, None)
        return {}

    def batch_move_tasks_raw(self, rows):
        ids = [r["taskId"] for r in rows]
        to_project_id = rows[0]["toProjectId"] if rows else None
        self.calls.append(("move", ids, to_project_id))
        for r in rows:
            tid = r["taskId"]
            if tid in self.live:
                self.live[tid]["projectId"] = r["toProjectId"]
        return {}

    def set_task_parents(self, rows):
        self.calls.append(("parent", rows))
        for r in rows:
            if r["taskId"] in self.live:
                self.live[r["taskId"]]["parentId"] = r["parentId"]
        return {}

    def batch_restore_tasks(self, ids, to_project_id=None):
        self.calls.append(("restore", ids, to_project_id))
        for tid in ids:
            entry = self.trash.get(tid)
            if entry:
                restored = dict(entry)
                if to_project_id:
                    restored["projectId"] = to_project_id
                self.live[tid] = restored
        return {}

    def get_trash(self, limit=500):
        return list(self.trash.values())[:limit]


class _FakeOfficial:
    """Fakes the official-API client (`ticktick`) — update_tasks/
    complete_tasks fall through to this for a SINGLE task even when v2 is
    also wired, matching the real code's branching."""

    def __init__(self, live):
        self.live = live
        self.calls = []

    def update_task(self, task_id, project_id, title=None, content=None,
                    start_date=None, due_date=None, priority=None,
                    repeat_flag=None, reminders=None):
        self.calls.append(("update", task_id))
        t = self.live.setdefault(task_id, {"id": task_id})
        if title is not None:
            t["title"] = title
        if priority is not None:
            t["priority"] = priority
        return {"id": task_id}

    def complete_task(self, project_id, task_id):
        self.calls.append(("complete", task_id))
        self.live.pop(task_id, None)
        return {"id": task_id}


def _wire(monkeypatch, live, names=None, trash=None, ensure="official", tags=None):
    if ensure == "official":
        monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: names or {"p1": "Работа"})
    fake = _FakeV2(live, trash, tags)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    official = _FakeOfficial(live)
    monkeypatch.setattr(s, "ticktick", official)
    return fake, official


# ===========================================================================
# update_tasks — single-task path goes through the OFFICIAL client
# ===========================================================================

async def test_update_tasks_call1_previews_nothing_mutated(monkeypatch):
    live = {"t1": {"id": "t1", "title": "A", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live)
    preview = await s.update_tasks("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "A", "new_title": "B"}])
    assert fake.calls == [] and official.calls == []
    assert live["t1"]["title"] == "A"
    assert "manifest_id" in preview
    assert "«A»" in preview


async def test_update_tasks_call2_without_reply_is_refused_and_retryable(monkeypatch):
    live = {"t1": {"id": "t1", "title": "A", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live)
    preview = await s.update_tasks("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "A", "new_title": "B"}])
    mid = _extract_manifest_id(preview)

    refused = await s.update_tasks("тест", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert fake.calls == [] and official.calls == []
    assert live["t1"]["title"] == "A"

    # manifest survives an empty (not explicitly negative) reply — retryable
    result = await s.update_tasks("тест", manifest_id=mid, user_reply="да, обновляй")
    assert official.calls  # single-task path went through the official client
    assert live["t1"]["title"] == "B"
    assert "🛑" not in result


async def test_update_tasks_explicit_no_burns_the_manifest(monkeypatch):
    live = {"t1": {"id": "t1", "title": "A", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live)
    preview = await s.update_tasks("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "A", "new_title": "B"}])
    mid = _extract_manifest_id(preview)

    refused = await s.update_tasks("тест", manifest_id=mid, user_reply="нет, погоди")
    assert "🛑" in refused
    assert live["t1"]["title"] == "A"

    # the manifest is now dead — even a genuine "yes" afterwards must fail
    dead = await s.update_tasks("тест", manifest_id=mid, user_reply="да")
    assert "🛑" in dead
    assert live["t1"]["title"] == "A"


async def test_update_tasks_manifest_is_one_shot(monkeypatch):
    live = {"t1": {"id": "t1", "title": "A", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live)
    preview = await s.update_tasks("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "A", "new_title": "B"}])
    mid = _extract_manifest_id(preview)
    await s.update_tasks("тест", manifest_id=mid, user_reply="да")
    calls_after_first = len(official.calls)

    second = await s.update_tasks("тест", manifest_id=mid, user_reply="да")
    assert "🛑" in second
    assert len(official.calls) == calls_after_first  # nothing new happened


async def test_update_tasks_batch_path_uses_v2(monkeypatch):
    live = {"t1": {"id": "t1", "title": "A", "projectId": "p1"},
            "t2": {"id": "t2", "title": "B", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live)
    preview = await s.update_tasks("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "A", "priority": 3},
        {"taskId": "t2", "projectId": "p1", "title": "B", "priority": 5}])
    mid = _extract_manifest_id(preview)
    result = await s.update_tasks("тест", manifest_id=mid, user_reply="да")
    assert fake.calls  # v2 batch_update_tasks ran, not the official client
    assert live["t1"]["priority"] == 3 and live["t2"]["priority"] == 5
    assert "🛑" not in result


async def test_update_tasks_one_shot_holds_without_identity_guard(monkeypatch):
    """Proves _gate_batch is a REAL one-shot (flips `consumed`) rather than
    "accidentally" one-shot via the identity-guard tripping on a changed
    title (that's what test_update_tasks_manifest_is_one_shot above actually
    exercises — its retry fails identity-guard because title went A -> B, so
    it would pass even on the old buggy _gate_batch that never sets
    `consumed`). Here only `priority` changes and `title`/`projectId` stay
    identical to live state on every call, so the identity-guard (which only
    compares title + project) would happily approve a REPLAY too — the only
    thing that can catch a replay here is the `consumed` flag itself.

    A single-task update goes through the OFFICIAL client (see
    _update_tasks_impl: `len(tasks) == 1` branch), not v2 batch — verified by
    reading the code, not assumed — so the mutation counter here is
    official.calls, unlike test_update_tasks_batch_path_uses_v2 (2 tasks ->
    v2 batch -> fake.calls)."""
    live = {"t1": {"id": "t1", "title": "A", "projectId": "p1", "priority": 0}}
    fake, official = _wire(monkeypatch, live)
    preview = await s.update_tasks("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "A", "priority": 3}])
    mid = _extract_manifest_id(preview)

    first = await s.update_tasks("тест", manifest_id=mid, user_reply="да")
    assert "🛑" not in first
    assert live["t1"]["priority"] == 3
    calls_after_first = len(official.calls)
    assert fake.calls == []  # confirms this went through the official path

    second = await s.update_tasks("тест", manifest_id=mid, user_reply="да")
    assert "🛑" in second
    # The real assertion: no NEW mutation happened on the replay. On the old
    # buggy _gate_batch (never sets consumed=True on success), the identity
    # guard alone would have let this through again, growing official.calls.
    assert len(official.calls) == calls_after_first


# ===========================================================================
# complete_tasks
# ===========================================================================

async def test_complete_tasks_full_gate_cycle(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live)

    preview = await s.complete_tasks(
        "Завершаю", [{"taskId": "t1", "title": "Купить молоко", "projectId": "p1"}])
    assert fake.calls == [] and official.calls == []
    assert "t1" in live

    mid = _extract_manifest_id(preview)
    refused = await s.complete_tasks("Завершаю", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert "t1" in live

    result = await s.complete_tasks("Завершаю", manifest_id=mid, user_reply="да")
    assert "t1" not in live
    assert "🛑" not in result


# ===========================================================================
# move_tasks
# ===========================================================================

async def test_move_tasks_full_gate_cycle(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live, names={"p1": "Inbox", "p2": "Покупки"})

    preview = await s.move_tasks(
        "Перемещаю", [{"taskId": "t1", "title": "Купить молоко"}],
        to_project_id="p2", to_project_name="Покупки")
    assert fake.calls == []
    assert live["t1"]["projectId"] == "p1"

    mid = _extract_manifest_id(preview)
    refused = await s.move_tasks("Перемещаю", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert live["t1"]["projectId"] == "p1"

    result = await s.move_tasks("Перемещаю", manifest_id=mid, user_reply="да, давай")
    assert live["t1"]["projectId"] == "p2"
    assert "🛑" not in result


# ===========================================================================
# set_task_parent
# ===========================================================================

async def test_set_task_parent_full_gate_cycle(monkeypatch):
    live = {
        "p": {"id": "p", "title": "Большой проект", "projectId": "p1"},
        "c": {"id": "c", "title": "Шаг 1", "projectId": "p1"},
    }
    fake, official = _wire(monkeypatch, live)

    preview = await s.set_task_parent(
        "Вкладываю", [{"taskId": "c", "title": "Шаг 1"}],
        parent_task_id="p", project_id="p1", parent_task_title="Большой проект")
    assert fake.calls == []
    assert "parentId" not in live["c"]

    mid = _extract_manifest_id(preview)
    refused = await s.set_task_parent("Вкладываю", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert "parentId" not in live["c"]

    result = await s.set_task_parent("Вкладываю", manifest_id=mid, user_reply="да")
    assert live["c"]["parentId"] == "p"
    assert "🛑" not in result


# ---------------------------------------------------------------------------
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group B) — the
# PARENT's parent_task_id↔parent_task_title is now cross-checked BEFORE the
# plan is built, not only inside _set_task_parent_impl on execution. Same
# shape as create_subtask/unset_task_parent/duplicate_task: mismatch AND
# missing both block the plan (the impl already treats both as a hard 🛑 for
# the parent — "вложение под мёртвого родителя осиротит задачи"), unavailable
# only warns. See set_task_parent's own docstring for why per-CHILD identity
# (the `tasks` list) is explicitly OUT of scope for this transfer.
# ---------------------------------------------------------------------------

async def test_set_task_parent_plan_identity_guard_blocks_wrong_parent_title(monkeypatch):
    """parent_task_id points at a REAL task ("Большой проект"), caller's
    parent_task_title claims a DIFFERENT one ("Другой проект") — before this
    fix, call #1 would have built and shown a plan card reading '... → под
    «Другой проект»' even though the id has nothing to do with that task.
    Now the plan is refused outright, before any card is built, and
    set_task_parents is never called."""
    live = {
        "p": {"id": "p", "title": "Большой проект", "projectId": "p1"},
        "c": {"id": "c", "title": "Шаг 1", "projectId": "p1"},
    }
    fake, official = _wire(monkeypatch, live)

    result = await s.set_task_parent(
        "Вкладываю", [{"taskId": "c", "title": "Шаг 1"}],
        parent_task_id="p", project_id="p1", parent_task_title="Другой проект")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Большой проект»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert fake.calls == []
    assert "parentId" not in live["c"]


async def test_set_task_parent_plan_identity_guard_blocks_missing_parent(monkeypatch):
    """_set_task_parent_impl treats a MISSING parent as a hard 🛑 too, not a
    soft warning ("вложение под мёртвого родителя осиротит задачи") — the
    plan-phase transfer must reproduce that same severity."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live)

    result = await s.set_task_parent(
        "Вкладываю", [{"taskId": "c", "title": "Шаг 1"}],
        parent_task_id="p-нет-такой", project_id="p1", parent_task_title="Большой проект")

    assert result.startswith("🛑 План НЕ построен")
    assert "manifest_id" not in result
    assert fake.calls == []


async def test_set_task_parent_plan_read_failure_does_not_block_but_warns(monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every nesting — fail-open here is cheaper than refusing everyone whose
    network is briefly flaky. The plan is still built, honestly warns that
    the parent's title was not verified, and the real (unchanged) identity-
    guard on execution (call #2) does its normal job right after. Mirrors
    _open_by_id's own real contract (returns None on failure, never raises)
    rather than raising an exception."""
    live = {
        "p": {"id": "p", "title": "Большой проект", "projectId": "p1"},
        "c": {"id": "c", "title": "Шаг 1", "projectId": "p1"},
    }
    fake, official = _wire(monkeypatch, live)
    real_open_by_id = s._open_by_id
    calls = {"n": 0}

    def _flaky(fresh=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_open_by_id(fresh=fresh)
    monkeypatch.setattr(s, "_open_by_id", _flaky)

    preview = await s.set_task_parent(
        "Вкладываю", [{"taskId": "c", "title": "Шаг 1"}],
        parent_task_id="p", project_id="p1", parent_task_title="Большой проект")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.set_task_parent("Вкладываю", manifest_id=mid, user_reply="да")
    assert live["c"]["parentId"] == "p"
    assert "🛑" not in result


async def test_set_task_parent_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside
    `_set_task_parent_impl` — untouched by this change — still catches the
    real mismatch: a network blip on planning must not weaken the protection
    at execution time."""
    live = {
        "p": {"id": "p", "title": "Большой проект", "projectId": "p1"},
        "c": {"id": "c", "title": "Шаг 1", "projectId": "p1"},
    }
    fake, official = _wire(monkeypatch, live)
    real_open_by_id = s._open_by_id
    calls = {"n": 0}

    def _flaky(fresh=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_open_by_id(fresh=fresh)
    monkeypatch.setattr(s, "_open_by_id", _flaky)

    preview = await s.set_task_parent(
        "Вкладываю", [{"taskId": "c", "title": "Шаг 1"}],
        parent_task_id="p", project_id="p1", parent_task_title="Другой проект")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.set_task_parent("Вкладываю", manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Большой проект»" in result
    assert fake.calls == []
    assert "parentId" not in live["c"]


async def test_set_task_parent_automation_key_mismatch_is_refused_before_plan(monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_batch/execution, a false parent name+id
    pair would sail through silently on a single valid key. The check sits
    BEFORE _gate_batch, so it applies here too."""
    live = {
        "p": {"id": "p", "title": "Большой проект", "projectId": "p1"},
        "c": {"id": "c", "title": "Шаг 1", "projectId": "p1"},
    }
    fake, official = _wire(monkeypatch, live)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.set_task_parent(
        "Вкладываю", [{"taskId": "c", "title": "Шаг 1"}],
        parent_task_id="p", project_id="p1", parent_task_title="Другой проект",
        automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Большой проект»" in result
    assert fake.calls == []
    assert "parentId" not in live["c"]


# ===========================================================================
# set_task_tags
# ===========================================================================

async def test_set_task_tags_full_gate_cycle(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1", "tags": []}}
    fake, official = _wire(monkeypatch, live)

    preview = await s.set_task_tags(
        "Ставлю тег", [{"taskId": "t1", "title": "Купить молоко", "tags": ["дом"]}])
    assert fake.calls == []
    assert live["t1"]["tags"] == []

    mid = _extract_manifest_id(preview)
    refused = await s.set_task_tags("Ставлю тег", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert live["t1"]["tags"] == []

    result = await s.set_task_tags("Ставлю тег", manifest_id=mid, user_reply="да")
    assert live["t1"]["tags"] == ["дом"]
    assert "🛑" not in result


# ===========================================================================
# restore_tasks
# ===========================================================================

async def test_restore_tasks_full_gate_cycle(monkeypatch):
    live = {}
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live, trash=trash)

    preview = await s.restore_tasks(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])
    assert fake.calls == []
    assert "t1" not in live

    mid = _extract_manifest_id(preview)
    refused = await s.restore_tasks("Восстанавливаю", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert "t1" not in live

    result = await s.restore_tasks("Восстанавливаю", manifest_id=mid, user_reply="да")
    assert "t1" in live
    assert "🛑" not in result


# ===========================================================================
# execute_task_creation — tier bumped 0 -> 1, now hard-enforced
# ===========================================================================

async def test_execute_task_creation_hard_enforced(monkeypatch):
    async def fake_impl(summary, tasks):
        return "created ok"
    monkeypatch.setattr(s, "_create_tasks_impl", fake_impl)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    mid = "slice1-create-mid"
    s._MANIFESTS[mid] = {"kind": "create", "raw": [{"title": "X"}],
                         "created": time.monotonic(),
                         "plan_shown_at": time.monotonic(),
                         "summary": "test", "consumed": False}

    refused = await s.execute_task_creation(mid, user_reply="")
    assert "🛑" in refused
    assert s._MANIFESTS[mid]["consumed"] is False

    result = await s.execute_task_creation(mid, user_reply="да, создавай")
    assert "created ok" in result
    assert s._MANIFESTS[mid]["consumed"] is True


# ===========================================================================
# Internal declutter call sites must NOT re-ask consent — the "yes" was
# already obtained at the execute_declutter/resume_declutter gate level.
# ===========================================================================

async def test_declutter_rename_bypasses_the_update_tasks_gate(monkeypatch, tmp_path):
    live = {"x": {"id": "x", "title": "Старое имя", "projectId": "p1"}}
    fake, official = _wire(monkeypatch, live, ensure=None)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))

    def boom_gate(*args, **kwargs):
        raise AssertionError(
            "declutter must not route through the public gated update_tasks "
            "(_gate_batch) — the user already said yes to the whole declutter")
    monkeypatch.setattr(s, "_gate_batch", boom_gate)

    mid = "slice1-declutter-rename"
    s._MANIFESTS[mid] = {
        "kind": "declutter",
        "actions": {"delete": [],
                    "rename": [{"taskId": "x", "projectId": "p1",
                                "title": "Старое имя", "new_title": "Новое имя"}],
                    "group": []},
        "mutating_count": 1, "created": time.monotonic(),
        "summary": "test declutter", "consumed": False,
    }
    result = await s.execute_declutter(mid, user_reply="да")
    assert "🛑" not in result.split("\n")[0]
    assert live["x"]["title"] == "Новое имя"


async def test_declutter_group_bypasses_the_set_task_parent_gate(monkeypatch, tmp_path):
    live = {
        "p": {"id": "p", "title": "Родитель", "projectId": "p1"},
        "c": {"id": "c", "title": "Ребёнок", "projectId": "p1"},
    }
    fake, official = _wire(monkeypatch, live, ensure=None)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))

    def boom_gate(*args, **kwargs):
        raise AssertionError(
            "declutter must not route through the public gated "
            "set_task_parent (_gate_batch)")
    monkeypatch.setattr(s, "_gate_batch", boom_gate)

    mid = "slice1-declutter-group"
    s._MANIFESTS[mid] = {
        "kind": "declutter",
        "actions": {"delete": [], "rename": [],
                    "group": [{"parentId": "p", "project_id": "p1",
                               "parent_title": "Родитель",
                               "children": [{"taskId": "c", "title": "Ребёнок"}]}]},
        "mutating_count": 1, "created": time.monotonic(),
        "summary": "test declutter", "consumed": False,
    }
    result = await s.execute_declutter(mid, user_reply="да")
    assert live["c"]["parentId"] == "p"
    assert "Группировка" in result
