"""PLAN_retrofit.md ПАКЕТ 8 — add_task_comment/update_task_comment/
delete_task_comment routed through the shared _gate_single gate (package 1),
with a pre-delete snapshot and a real post-verify. Before this package all
three mutated on the FIRST call, with no consent gate at all (group15,
STANDARD.md §3 / invariant 2). No real network — the v2 client and open-task
index are faked.

Covers, per the plan's checklist:
  8.1 — all three now require plan (call 1, no manifest_id/user_reply) then
        execute (call 2, manifest_id + affirmative user_reply); execute
        without a live/consumed manifest, or with a negative/missing
        user_reply, mutates nothing.
  8.2 — delete_task_comment journals a pre-delete snapshot of the FULL live
        comment (text/author/time) before calling the API.
  8.3 — add_task_comment re-reads comments after the mutation and only
        reports success when the new text is actually found.
  8.4 — every successful execute writes an _op_journal record
        (comment_add/comment_update/comment_delete) with an actor.
  8.5 — WRITE/DESTRUCTIVE annotations are on the registered tools.
  8.6 — output goes through _tool_response (### header, frozen emoji legend,
        Russian)."""
import json

import ticktick_mcp.src.server as s


class FakeV2Comments:
    """Fakes the v2 client surface the three comment tools touch."""

    def __init__(self, comments, *, add_error=None, update_error=None,
                 delete_error=None):
        # {task_id: [comment_dict, ...]}
        self._comments = comments
        self.add_error = add_error
        self.update_error = update_error
        self.delete_error = delete_error
        self.add_calls = []
        self.update_calls = []
        self.delete_calls = []

    def get_task_comments(self, project_id, task_id):
        return list(self._comments.get(task_id, []))

    def add_task_comment(self, project_id, task_id, text):
        if self.add_error:
            raise self.add_error
        self.add_calls.append((project_id, task_id, text))
        cm = {"id": f"c{len(self.add_calls)}", "title": text,
              "userProfile": {"displayName": "Максим"}}
        self._comments.setdefault(task_id, []).append(cm)
        return cm

    def update_task_comment(self, project_id, task_id, comment_id, text):
        if self.update_error:
            raise self.update_error
        self.update_calls.append((project_id, task_id, comment_id, text))
        for cm in self._comments.get(task_id, []):
            if cm.get("id") == comment_id:
                cm["title"] = text
        return {}

    def delete_task_comment(self, project_id, task_id, comment_id):
        if self.delete_error:
            raise self.delete_error
        self.delete_calls.append((project_id, task_id, comment_id))
        self._comments[task_id] = [
            c for c in self._comments.get(task_id, []) if c.get("id") != comment_id
        ]
        return {}


def _wire(monkeypatch, live, comments, tmp_path, names=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: names or {"p1": "Работа"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = FakeV2Comments(comments)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


def _journal_records(tmp_path):
    path = tmp_path / "deletion_journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# annotations (8.5)
# ---------------------------------------------------------------------------

def test_annotations_write_and_destructive():
    tools = {t.name: t for t in s.mcp._tool_manager.list_tools()}
    assert tools["add_task_comment"].annotations.destructiveHint is False
    assert tools["add_task_comment"].annotations.readOnlyHint is False
    assert tools["update_task_comment"].annotations.destructiveHint is False
    assert tools["delete_task_comment"].annotations.destructiveHint is True
    assert tools["delete_task_comment"].annotations.readOnlyHint is False


# ---------------------------------------------------------------------------
# add_task_comment (8.1 gate, 8.3 post-verify, 8.4 journal, 8.6 format)
# ---------------------------------------------------------------------------

async def test_add_comment_plan_then_execute_happy_path(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": []}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    plan = await s.add_task_comment("Позвонить", "напомнить о встрече", "p1", "t1")
    assert fake.add_calls == []
    assert "план" in plan.lower()
    mid = plan.split("`")[1]

    out = await s.add_task_comment("Позвонить", "напомнить о встрече", "p1", "t1",
                                    manifest_id=mid, user_reply="да")
    assert fake.add_calls == [("p1", "t1", "напомнить о встрече")]
    assert out.startswith("### ✅")
    assert "напомнить о встрече" in out
    assert "🧾" in out

    records = _journal_records(tmp_path)
    op_records = [r for r in records if r.get("op") == "comment_add"]
    assert len(op_records) == 1
    assert op_records[0]["actor"] == "human"
    assert op_records[0]["items"][0]["text"] == "напомнить о встрече"


async def test_add_comment_execute_without_user_reply_is_refused(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": []}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    plan = await s.add_task_comment("Позвонить", "текст", "p1", "t1")
    mid = plan.split("`")[1]

    out = await s.add_task_comment("Позвонить", "текст", "p1", "t1", manifest_id=mid)
    assert fake.add_calls == []
    assert "🛑" in out


async def test_add_comment_negative_reply_invalidates_manifest(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": []}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    plan = await s.add_task_comment("Позвонить", "текст", "p1", "t1")
    mid = plan.split("`")[1]

    out = await s.add_task_comment("Позвонить", "текст", "p1", "t1",
                                    manifest_id=mid, user_reply="нет, не надо")
    assert fake.add_calls == []
    assert "🛑" in out
    assert s._MANIFESTS[mid]["consumed"] is True

    # Manifest is one-shot — a later "да" against the same (now-consumed)
    # manifest_id must also be refused, not silently accepted.
    out2 = await s.add_task_comment("Позвонить", "текст", "p1", "t1",
                                     manifest_id=mid, user_reply="да")
    assert fake.add_calls == []
    assert "🛑" in out2


async def test_add_comment_identity_mismatch_refused_before_gate(monkeypatch, tmp_path):
    """id resolves to a DIFFERENT live task than the title claims — refuse
    outright, never even build a manifest."""
    live = {"t1": {"id": "t1", "title": "Другая задача", "projectId": "p1"}}
    comments = {"t1": []}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    out = await s.add_task_comment("Позвонить", "текст", "p1", "t1")
    assert fake.add_calls == []
    assert "🛑" in out
    assert "Другая задача" in out


async def test_add_comment_postverify_warns_when_comment_not_found(monkeypatch, tmp_path):
    """8.3: the API call succeeded (no exception) but a fresh re-read does
    NOT show the new text — must NOT report a bare ✅ (no more '200 OK as
    proof')."""
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": []}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    # Sabotage: add_task_comment "succeeds" but writes under a different key
    # than what get_task_comments will read back — simulates the API
    # accepting the write while the re-read reflects something else.
    def _add_but_dont_persist(project_id, task_id, text):
        fake.add_calls.append((project_id, task_id, text))
        return {"id": "ghost", "title": text}

    monkeypatch.setattr(fake, "add_task_comment", _add_but_dont_persist)

    plan = await s.add_task_comment("Позвонить", "текст", "p1", "t1")
    mid = plan.split("`")[1]
    out = await s.add_task_comment("Позвонить", "текст", "p1", "t1",
                                    manifest_id=mid, user_reply="да")
    assert fake.add_calls
    assert out.startswith("### ⚠️")
    assert "НЕ найден" in out


# ---------------------------------------------------------------------------
# update_task_comment
# ---------------------------------------------------------------------------

async def test_update_comment_plan_then_execute_happy_path(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": [{"id": "c1", "title": "старый текст"}]}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    plan = await s.update_task_comment("Позвонить", "новый текст", "p1", "t1", "c1")
    assert fake.update_calls == []
    mid = plan.split("`")[1]

    out = await s.update_task_comment("Позвонить", "новый текст", "p1", "t1", "c1",
                                       manifest_id=mid, user_reply="да, подтверждаю")
    assert fake.update_calls == [("p1", "t1", "c1", "новый текст")]
    assert out.startswith("### ✅")
    assert "старый текст" in out and "новый текст" in out

    records = _journal_records(tmp_path)
    op_records = [r for r in records if r.get("op") == "comment_update"]
    assert len(op_records) == 1
    assert op_records[0]["items"][0]["old_text"] == "старый текст"
    assert op_records[0]["items"][0]["text"] == "новый текст"


async def test_update_comment_stale_id_refused(monkeypatch, tmp_path):
    """comment_id no longer exists on the task (already deleted / wrong id)
    — refuse on the very first call, no manifest, no API call."""
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": []}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    out = await s.update_task_comment("Позвонить", "новый текст", "p1", "t1", "c-stale")
    assert fake.update_calls == []
    assert "🛑" in out


async def test_update_comment_execute_without_user_reply_is_refused(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": [{"id": "c1", "title": "старый текст"}]}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    plan = await s.update_task_comment("Позвонить", "новый текст", "p1", "t1", "c1")
    mid = plan.split("`")[1]
    out = await s.update_task_comment("Позвонить", "новый текст", "p1", "t1", "c1",
                                       manifest_id=mid)
    assert fake.update_calls == []
    assert "🛑" in out


# ---------------------------------------------------------------------------
# delete_task_comment (8.2 pre-snapshot, tier 2)
# ---------------------------------------------------------------------------

async def test_delete_comment_plan_then_execute_happy_path(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": [{"id": "c1", "title": "удалить меня",
                        "userProfile": {"displayName": "Максим"},
                        "commentTime": "2026-07-01T10:00:00Z"}]}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    plan = await s.delete_task_comment("Позвонить", "p1", "t1", "c1")
    assert fake.delete_calls == []
    mid = plan.split("`")[1]

    out = await s.delete_task_comment("Позвонить", "p1", "t1", "c1",
                                       manifest_id=mid, user_reply="да, удаляй")
    assert fake.delete_calls == [("p1", "t1", "c1")]
    assert out.startswith("### ✅")
    assert "удалить меня" in out

    # 8.2 — pre-delete snapshot of the FULL live comment (text/author/time),
    # written to the journal BEFORE the delete call.
    records = _journal_records(tmp_path)
    op_records = [r for r in records if r.get("op") == "comment_delete"]
    assert len(op_records) == 1
    snap = op_records[0]["items"][0]["snapshot"]
    assert snap["title"] == "удалить меня"
    assert snap["userProfile"]["displayName"] == "Максим"
    assert snap["commentTime"] == "2026-07-01T10:00:00Z"


async def test_delete_comment_negative_reply_refused_and_nothing_deleted(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": [{"id": "c1", "title": "удалить меня"}]}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    plan = await s.delete_task_comment("Позвонить", "p1", "t1", "c1")
    mid = plan.split("`")[1]
    out = await s.delete_task_comment("Позвонить", "p1", "t1", "c1",
                                       manifest_id=mid, user_reply="нет")
    assert fake.delete_calls == []
    assert "🛑" in out
    assert comments["t1"]  # still there


async def test_delete_comment_stale_id_refused(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": []}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    out = await s.delete_task_comment("Позвонить", "p1", "t1", "c-stale")
    assert fake.delete_calls == []
    assert "🛑" in out


async def test_delete_comment_already_gone_between_plan_and_execute_is_refused(
        monkeypatch, tmp_path):
    """The comment existed at plan time but disappeared (e.g. deleted via the
    TickTick app) before the user's "да" — the existence pre-check re-reads
    FRESH on every call (identity-guard freshness), so this is caught by the
    same fail-closed refusal a stale comment_id gets on the very first call —
    nothing is mutated, and the delete API is never called."""
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": [{"id": "c1", "title": "удалить меня"}]}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    plan = await s.delete_task_comment("Позвонить", "p1", "t1", "c1")
    mid = plan.split("`")[1]

    # Vanishes before execute.
    comments["t1"] = []

    out = await s.delete_task_comment("Позвонить", "p1", "t1", "c1",
                                       manifest_id=mid, user_reply="да")
    assert fake.delete_calls == []
    assert "🛑" in out


async def test_delete_comment_vanishing_mid_call_after_gate_is_skipped(
        monkeypatch, tmp_path):
    """Narrower race than the above: the comment is still there for the
    existence pre-check AND the gate, but is gone by the fresh re-read taken
    right before the actual delete (PLAN_retrofit.md п.8.2 pre-snapshot read)
    — must skip cleanly (↷) rather than delete/error, and must never call the
    delete API."""
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": [{"id": "c1", "title": "удалить меня"}]}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    plan = await s.delete_task_comment("Позвонить", "p1", "t1", "c1")
    mid = plan.split("`")[1]

    real_get = fake.get_task_comments
    calls = {"n": 0}

    def _get_then_vanish(project_id, task_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_get(project_id, task_id)
        return []

    monkeypatch.setattr(fake, "get_task_comments", _get_then_vanish)

    out = await s.delete_task_comment("Позвонить", "p1", "t1", "c1",
                                       manifest_id=mid, user_reply="да")
    assert fake.delete_calls == []
    assert out.startswith("### ↷")


async def test_delete_comment_identity_mismatch_refused(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Другая задача", "projectId": "p1"}}
    comments = {"t1": [{"id": "c1", "title": "удалить меня"}]}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    out = await s.delete_task_comment("Позвонить", "p1", "t1", "c1")
    assert fake.delete_calls == []
    assert "🛑" in out


async def test_delete_comment_postverify_reports_discrepancy(monkeypatch, tmp_path):
    """The delete call raises no exception but the comment is STILL visible
    on re-read — must report ❌, not a bare success."""
    live = {"t1": {"id": "t1", "title": "Позвонить", "projectId": "p1"}}
    comments = {"t1": [{"id": "c1", "title": "удалить меня"}]}
    fake = _wire(monkeypatch, live, comments, tmp_path)

    def _fake_delete_but_keep(project_id, task_id, comment_id):
        fake.delete_calls.append((project_id, task_id, comment_id))
        # doesn't actually remove it from `comments`

    monkeypatch.setattr(fake, "delete_task_comment", _fake_delete_but_keep)

    plan = await s.delete_task_comment("Позвонить", "p1", "t1", "c1")
    mid = plan.split("`")[1]
    out = await s.delete_task_comment("Позвонить", "p1", "t1", "c1",
                                       manifest_id=mid, user_reply="да")
    assert fake.delete_calls
    assert out.startswith("### ❌")
    assert "ВСЁ ЕЩЁ" in out
