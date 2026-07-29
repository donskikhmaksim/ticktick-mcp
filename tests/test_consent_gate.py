"""Tests for the in-chat consent gate (docs/DESIGN_approval_gate.md, v2 —
"no buttons, plain 'yes' in chat"). Covers the shared primitives
(_is_affirmative_reply / _is_negative_reply / _require_consent) plus the 🔴
tools that were migrated onto them: delete_tasks (single, direct-delete
hole), execute_task_deletion, execute_declutter, resume_declutter,
set_declutter_decision, delete_project, rename_tag's merge branch, and the
green-tier execute_task_creation pass-through. No real network — TickTick
clients are faked."""
import re
import time

import ticktick_mcp.src.server as s


# ===========================================================================
# _is_affirmative_reply / _is_negative_reply — pure vocabulary tests
# ===========================================================================

def test_empty_reply_is_neither_affirmative_nor_negative():
    assert s._is_affirmative_reply("") is False
    assert s._is_affirmative_reply(None) is False
    assert s._is_negative_reply("") is False


def test_plain_yes_variants_are_affirmative():
    for reply in ["да", "Да!", "ок", "окей.", "давай", "подтверждаю",
                  "yes", "confirm", "го", "  да  "]:
        assert s._is_affirmative_reply(reply) is True, reply


def test_plain_no_variants_are_negative_and_not_affirmative():
    for reply in ["нет", "нет, отмена", "стоп", "погоди", "cancel", "no"]:
        assert s._is_negative_reply(reply) is True, reply
        assert s._is_affirmative_reply(reply) is False, reply


def test_manifest_echo_artifact_is_never_affirmative():
    """A reply that just replays the server's OWN old-style jargon back is
    exactly what a self-confirming model would type — must never count as a
    genuine human "yes"."""
    for reply in ["DELETE 5", "delete 3", "CREATE 2", "DECLUTTER 1",
                  'execute_task_deletion(manifest_id="abc")',
                  '{"decision": "approved"}']:
        assert s._is_affirmative_reply(reply) is False, reply


def test_affirmative_word_buried_in_a_longer_sentence_still_counts():
    assert s._is_affirmative_reply("да, удаляй") is True
    assert s._is_affirmative_reply("ну давай") is True


def test_sentence_with_both_yes_and_no_words_is_not_affirmative():
    # Fail-closed: ambiguous input must never be treated as consent.
    assert s._is_affirmative_reply("да нет наверное") is False


# ===========================================================================
# _require_consent — the shared gate
# ===========================================================================

def _fresh_manifest(**overrides):
    now = time.monotonic()
    m = {"kind": "delete", "items": [{"taskId": "t1"}], "created": now,
         "plan_shown_at": now, "consumed": False}
    m.update(overrides)
    return m


def test_tier0_always_passes_regardless_of_reply():
    r = s._require_consent(action="create", tier=0, manifest=_fresh_manifest(),
                           user_reply="")
    assert r.ok is True


def test_tier2_no_reply_is_fail_closed():
    m = _fresh_manifest()
    r = s._require_consent(action="delete", tier=2, manifest=m, user_reply="")
    assert r.ok is False
    assert "user_reply" in r.reason
    assert m["consumed"] is False  # retryable — no reply is not a refusal


def test_tier2_affirmative_reply_passes_when_gap_satisfied():
    m = _fresh_manifest(plan_shown_at=time.monotonic() - 10)
    r = s._require_consent(action="delete", tier=2, manifest=m,
                           user_reply="да", min_gap=2.0)
    assert r.ok is True


def test_tier2_negative_reply_refuses_and_invalidates_manifest():
    m = _fresh_manifest()
    r = s._require_consent(action="delete", tier=2, manifest=m,
                           user_reply="нет, отмена")
    assert r.ok is False
    assert m["consumed"] is True  # a "no" burns the plan — no silent retry


def test_tier2_already_consumed_manifest_is_refused():
    m = _fresh_manifest(consumed=True)
    r = s._require_consent(action="delete", tier=2, manifest=m, user_reply="да")
    assert r.ok is False
    assert "one-shot" in r.reason or "исполнен" in r.reason


def test_tier2_expired_manifest_is_refused_and_consumed():
    m = _fresh_manifest(created=time.monotonic() - s._MANIFEST_TTL - 1)
    r = s._require_consent(action="delete", tier=2, manifest=m, user_reply="да")
    assert r.ok is False
    assert "истёк" in r.reason
    assert m["consumed"] is True


def test_tier2_duplet_within_min_gap_is_refused():
    """plan and 'yes' arriving suspiciously close together — the model firing
    both in one turn without a real human reply in between."""
    m = _fresh_manifest()  # plan_shown_at == now
    r = s._require_consent(action="delete", tier=2, manifest=m,
                           user_reply="да", min_gap=5.0)
    assert r.ok is False
    assert "быстро" in r.reason
    assert m["consumed"] is False  # not consumed — a slower retry can still work


def test_tier2_inline_no_manifest_only_needs_affirmative_reply():
    """Non-manifest 🔴 checks (rename_tag's merge branch, sheet-backed
    declutter rows) have no plan_shown_at to time against — the affirmative
    check alone still fully applies."""
    assert s._require_consent(action="rename_tag_merge", tier=2, manifest=None,
                              user_reply="").ok is False
    assert s._require_consent(action="rename_tag_merge", tier=2, manifest=None,
                              user_reply="да, сливай").ok is True


def test_automation_key_bypasses_everything(monkeypatch):
    # 17.5: the automation-key bypass is resolved against the separate
    # TICKTICK_AUTOMATION_KEY_<name> keys now, never against MCP_SECRET (the
    # /mcp path secret) — see _resolve_automation_actor.
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY", "test-automation-key")
    m = _fresh_manifest(consumed=True)  # would refuse on every other ground
    r = s._require_consent(action="delete", tier=2, manifest=m, user_reply="",
                           automation_key="test-automation-key")
    assert r.ok is True


def test_mcp_secret_no_longer_doubles_as_automation_key(monkeypatch):
    """17.5 regression guard: MCP_SECRET (the /mcp path secret) must NOT
    also work as automation_key — reusing it would mean handing the path
    secret to every automation consumer as a tool ARGUMENT (visible in the
    model's own transcript), and a compromised consumer could only be
    revoked by rotating the path secret for everyone."""
    monkeypatch.delenv("TICKTICK_AUTOMATION_KEY", raising=False)
    m = _fresh_manifest(consumed=True)
    r = s._require_consent(action="delete", tier=2, manifest=m, user_reply="",
                           automation_key=s.SECRET)
    assert r.ok is False


def test_wrong_automation_key_does_not_bypass():
    m = _fresh_manifest()
    r = s._require_consent(action="delete", tier=2, manifest=m, user_reply="",
                           automation_key="not-the-real-secret")
    assert r.ok is False


# ===========================================================================
# create_tasks — automation_key path must still work UNCHANGED
# ===========================================================================

async def test_create_tasks_direct_path_refuses_interactive_callers(monkeypatch):
    calls = []

    async def fake_impl(summary, tasks):
        calls.append((summary, tasks))
        return "created"

    monkeypatch.setattr(s, "_create_tasks_impl", fake_impl)
    result = await s.create_tasks("Test", [{"title": "X", "project_id": "p1"}])
    assert "🛑" in result
    assert "plan_task_creation" in result
    assert calls == []


async def test_create_tasks_automation_key_still_bypasses(monkeypatch):
    calls = []

    async def fake_impl(summary, tasks):
        calls.append((summary, tasks))
        return "created"

    monkeypatch.setattr(s, "_create_tasks_impl", fake_impl)
    # 17.5: same migration as _require_consent — the per-consumer key, not
    # MCP_SECRET.
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY", "test-automation-key")
    result = await s.create_tasks("Test", [{"title": "X", "project_id": "p1"}],
                                  automation_key="test-automation-key")
    assert result == "created"
    assert len(calls) == 1


async def test_create_tasks_mcp_secret_no_longer_bypasses(monkeypatch):
    monkeypatch.delenv("TICKTICK_AUTOMATION_KEY", raising=False)
    calls = []

    async def fake_impl(summary, tasks):
        calls.append((summary, tasks))
        return "created"

    monkeypatch.setattr(s, "_create_tasks_impl", fake_impl)
    result = await s.create_tasks("Test", [{"title": "X", "project_id": "p1"}],
                                  automation_key=s.SECRET)
    assert "🛑" in result
    assert calls == []


# ===========================================================================
# execute_task_creation — Slice 1 real gate: tier bumped 0 -> 1, now HARD
# enforced (creation is still reversible, but the model can no longer
# self-confirm by passing an empty/blank user_reply).
# ===========================================================================

async def test_execute_task_creation_empty_reply_is_refused_and_does_not_create(monkeypatch):
    calls = []

    async def fake_impl(summary, tasks):
        calls.append((summary, tasks))
        return "created ok"
    monkeypatch.setattr(s, "_create_tasks_impl", fake_impl)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    mid = "test-create-mid-empty"
    s._MANIFESTS[mid] = {"kind": "create", "raw": [{"title": "X"}],
                         "created": time.monotonic(),
                         "plan_shown_at": time.monotonic() - 10,
                         "summary": "test", "consumed": False}
    result = await s.execute_task_creation(mid, user_reply="")
    assert "🛑" in result
    assert calls == []
    assert s._MANIFESTS[mid]["consumed"] is False  # retryable


async def test_execute_task_creation_negative_reply_is_refused(monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    mid = "test-create-mid-no"
    s._MANIFESTS[mid] = {"kind": "create", "raw": [{"title": "X"}],
                         "created": time.monotonic(),
                         "plan_shown_at": time.monotonic() - 10,
                         "summary": "test", "consumed": False}
    result = await s.execute_task_creation(mid, user_reply="нет, отмена")
    assert "🛑" in result
    assert s._MANIFESTS[mid]["consumed"] is True  # a "no" burns the plan


async def test_execute_task_creation_with_genuine_yes_creates(monkeypatch):
    async def fake_impl(summary, tasks):
        return "created ok"
    monkeypatch.setattr(s, "_create_tasks_impl", fake_impl)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    mid = "test-create-mid-yes"
    s._MANIFESTS[mid] = {"kind": "create", "raw": [{"title": "X"}],
                         "created": time.monotonic(),
                         "plan_shown_at": time.monotonic() - 10,
                         "summary": "test", "consumed": False}
    result = await s.execute_task_creation(mid, user_reply="да, создавай")
    assert "created ok" in result
    assert s._MANIFESTS[mid]["consumed"] is True


async def test_execute_task_creation_unknown_manifest_is_refused(monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    result = await s.execute_task_creation("no-such-manifest", user_reply="да")
    assert "🛑" in result


# ===========================================================================
# delete_tasks — closing the "single direct delete, no confirm" hole
# ===========================================================================

class _FakeV2Delete:
    def __init__(self, live):
        self._live = live
        self.deleted_ids = []

    def batch_delete_tasks(self, items):
        for it in items:
            self._live.pop(it["taskId"], None)
            self.deleted_ids.append(it["taskId"])
        return {}


def _wire_delete_tasks(monkeypatch, live, tmp_path=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})
    fake = _FakeV2Delete(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    if tmp_path is not None:
        monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    return fake


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


async def test_delete_tasks_single_call_only_previews_never_deletes(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire_delete_tasks(monkeypatch, live, tmp_path)

    preview = await s.delete_tasks(
        "⚠️ Удаляю «Купить молоко»",
        [{"taskId": "t1", "title": "Купить молоко", "projectId": "p1"}])
    assert fake.deleted_ids == []
    assert "t1" in live
    assert "Купить молоко" in preview
    assert "user_reply" in preview


async def test_delete_tasks_second_call_without_reply_is_refused(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire_delete_tasks(monkeypatch, live, tmp_path)

    preview = await s.delete_tasks(
        "⚠️ Удаляю «Купить молоко»",
        [{"taskId": "t1", "title": "Купить молоко", "projectId": "p1"}])
    mid = _extract_manifest_id(preview)

    result = await s.delete_tasks("⚠️ Удаляю «Купить молоко»", manifest_id=mid)
    assert fake.deleted_ids == []
    assert "t1" in live
    assert "🛑" in result


async def test_delete_tasks_with_genuine_yes_deletes(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire_delete_tasks(monkeypatch, live, tmp_path)

    preview = await s.delete_tasks(
        "⚠️ Удаляю «Купить молоко»",
        [{"taskId": "t1", "title": "Купить молоко", "projectId": "p1"}])
    mid = _extract_manifest_id(preview)

    result = await s.delete_tasks("⚠️ Удаляю «Купить молоко»", manifest_id=mid,
                                  user_reply="да, удаляй")
    assert fake.deleted_ids == ["t1"]
    assert "t1" not in live
    assert "Удалено" in result


async def test_delete_tasks_manifest_is_one_shot(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire_delete_tasks(monkeypatch, live, tmp_path)

    preview = await s.delete_tasks(
        "⚠️ Удаляю «Купить молоко»",
        [{"taskId": "t1", "title": "Купить молоко", "projectId": "p1"}])
    mid = _extract_manifest_id(preview)

    await s.delete_tasks("⚠️ Удаляю «Купить молоко»", manifest_id=mid,
                         user_reply="да")
    assert fake.deleted_ids == ["t1"]

    # Same manifest again — must be refused, not re-deleted (nothing left to
    # delete anyway, but the manifest itself must be dead).
    second = await s.delete_tasks("⚠️ Удаляю «Купить молоко»", manifest_id=mid,
                                  user_reply="да")
    assert fake.deleted_ids == ["t1"]  # unchanged — no second delete attempt
    assert "🛑" in second


async def test_delete_tasks_bulk_over_cap_is_unaffected_by_the_gate(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    result = await s.delete_tasks("⚠️ Удаляю 3", [
        {"taskId": "t1", "title": "A", "projectId": "p1"},
        {"taskId": "t2", "title": "B", "projectId": "p1"},
        {"taskId": "t3", "title": "C", "projectId": "p1"},
    ])
    assert "🛑" in result
    assert "plan_task_deletion" in result


# ===========================================================================
# execute_task_deletion — the plan_task_deletion → execute pair
# ===========================================================================

async def test_execute_task_deletion_requires_affirmative_reply(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    live = {"t1": {"id": "t1", "title": "Old task", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    fake = _FakeV2Delete(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)

    mid = "test-del-mid"
    s._MANIFESTS[mid] = {
        "kind": "delete",
        "items": [{"taskId": "t1", "projectId": "p1", "title": "Old task",
                   "snapshot": {"title": "Old task"}}],
        "created": time.monotonic(), "plan_shown_at": time.monotonic(),
        "summary": "test", "consumed": False,
    }

    refused = await s.execute_task_deletion(mid, user_reply="")
    assert "t1" in live
    assert fake.deleted_ids == []
    assert "🛑" in refused

    await s.execute_task_deletion(mid, user_reply="да")
    assert fake.deleted_ids == ["t1"]
    assert "t1" not in live
    assert s._MANIFESTS[mid]["consumed"] is True


# ===========================================================================
# rename_tag — the merge branch (already had a real code-gate, allow_merge;
# now ALSO requires genuine consent, not just the model setting its own flag)
# ===========================================================================

class _FakeV2Tags:
    def __init__(self, names):
        self._names = names  # list[str], lowercase

    def get_state(self, force=True):
        return {}

    def get_tags(self):
        return [{"name": n} for n in self._names]

    def rename_tag(self, old_name, new_name):
        self._names = [n for n in self._names if n != old_name.lower()]
        if new_name.lower() not in self._names:
            self._names.append(new_name.lower())
        return {}


async def test_rename_tag_plain_rename_needs_no_consent(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(["старый"])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    result = await s.rename_tag("старый", "новый")
    assert "renamed" in result
    assert "новый" in fake._names


async def test_rename_tag_merge_without_consent_is_refused(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(["a", "b"])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    # allow_merge=True alone (model self-setting the flag) is NOT enough.
    result = await s.rename_tag("a", "b", allow_merge=True)
    assert "🛑" in result
    assert fake._names == ["a", "b"]  # untouched


async def test_rename_tag_merge_with_genuine_consent_succeeds(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(["a", "b"])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")
    assert "renamed" in result
    assert fake._names == ["b"]
