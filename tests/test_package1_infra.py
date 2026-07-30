"""Tests for PLAN_retrofit.md ПАКЕТ 1 — shared write infra added ahead of
packages 2-17 (no existing @mcp.tool() body is touched by this package):
_tool_response, _gate_single, actor in the journal, and the automation_key
infra. No real network — pure unit tests against module-level helpers."""
import ticktick_mcp.src.server as s


# ===========================================================================
# _tool_response — §7.1/§7.2 output-format helper
# ===========================================================================

def test_tool_response_forbidden_emoji_raises():
    for bad_status in ["✓", "✗", "🗑", "✏️", "↪", "✔", "x", ""]:
        try:
            s._tool_response(bad_status, "Что-то сделано")
            assert False, f"expected ValueError for status={bad_status!r}"
        except ValueError:
            pass


def test_tool_response_legend_statuses_all_accepted():
    for ok_status in ["✅", "⚠️", "❌", "🛑", "↷", "🧾"]:
        out = s._tool_response(ok_status, "Готово")
        assert out.startswith(f"### {ok_status} Готово")


def test_tool_response_empty_headline_raises():
    try:
        s._tool_response("✅", "   ")
        assert False, "expected ValueError for blank headline"
    except ValueError:
        pass


def test_tool_response_assembles_four_blocks_in_order():
    out = s._tool_response(
        "✅", "Создано **2**",
        bullets=["**«Задача 1»**", "**«Задача 2»**"],
        warnings=["одна задача без срока"],
        proof="🧾 Независимая проверка: ...")
    lines = out.splitlines()
    assert lines[0] == "### ✅ Создано **2**"
    assert "- **«Задача 1»**" in out
    assert "- **«Задача 2»**" in out
    assert "⚠️ одна задача без срока" in out
    assert out.rstrip().endswith("🧾 Независимая проверка: ...")
    # order: headline block, then bullets, then warnings, then proof
    assert out.index("Задача 1") < out.index("одна задача")
    assert out.index("одна задача") < out.index("Независимая проверка")


def test_tool_response_bullets_and_warnings_get_prefixed_once():
    out = s._tool_response("⚠️", "Готово частично",
                           bullets=["- уже с дефисом"],
                           warnings=["⚠️ уже с варнингом"])
    assert out.count("- уже с дефисом") == 1
    assert out.count("⚠️ уже с варнингом") == 1


# ===========================================================================
# _refuse — §6/§7 unified 🛑 text
# ===========================================================================

def test_refuse_always_says_nothing_changed():
    out = s._refuse("Манифест истёк.", "Вызови plan_* заново.")
    assert out.startswith("🛑")
    assert "ничего не изменено" in out.lower()
    assert "Вызови plan_* заново." in out


def test_refuse_requires_a_reason():
    try:
        s._refuse("", "что-то сделать")
        assert False, "expected ValueError for empty reason"
    except ValueError:
        pass


# ===========================================================================
# _snapshot_of — extended for non-task kinds (п.1.8), task kind unchanged
# ===========================================================================

def test_snapshot_of_task_kind_unchanged_default():
    live = {"title": "T", "priority": 3, "extra_junk": "ignored"}
    snap = s._snapshot_of(live)
    assert snap == {"title": "T", "priority": 3}


def test_snapshot_of_project_group_kind():
    live = {"id": "g1", "name": "Financial", "unrelated": "x"}
    snap = s._snapshot_of(live, kind="project_group")
    assert snap == {"id": "g1", "name": "Financial"}


def test_snapshot_of_tag_kind():
    live = {"name": "urgent", "color": "#ff0000", "sortType": "title",
            "unrelated": "x"}
    snap = s._snapshot_of(live, kind="tag")
    assert snap == {"name": "urgent", "color": "#ff0000", "sortType": "title"}


def test_snapshot_of_comment_kind():
    live = {"content": "hello", "authorName": "Max", "commentTime": "2026-01-01"}
    snap = s._snapshot_of(live, kind="comment")
    assert snap == {"content": "hello", "authorName": "Max",
                    "commentTime": "2026-01-01"}


def test_snapshot_of_none_live_returns_empty_dict():
    assert s._snapshot_of(None) == {}
    assert s._snapshot_of(None, kind="tag") == {}


# ===========================================================================
# actor in the journal — п.1.2
# ===========================================================================

def test_journal_write_defaults_actor_to_human(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    s._journal_write({"foo": "bar"})
    path = tmp_path / "deletion_journal.jsonl"
    import json
    rec = json.loads(path.read_text().strip().splitlines()[-1])
    assert rec["actor"] == "human"


def test_journal_write_respects_explicit_actor_kwarg(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    s._journal_write({"foo": "bar"}, actor="automation:tgbot")
    path = tmp_path / "deletion_journal.jsonl"
    import json
    rec = json.loads(path.read_text().strip().splitlines()[-1])
    assert rec["actor"] == "automation:tgbot"


def test_journal_write_does_not_overwrite_actor_already_in_record(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    s._journal_write({"foo": "bar", "actor": "automation:explicit"}, actor="human")
    path = tmp_path / "deletion_journal.jsonl"
    import json
    rec = json.loads(path.read_text().strip().splitlines()[-1])
    assert rec["actor"] == "automation:explicit"


def test_op_journal_carries_actor_and_new_optional_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    rid = s._op_journal("update", [{"taskId": "t1", "title": "X"}], "test",
                        actor="automation:tgbot", object_hash="deadbeef",
                        user_reply="да, применяй", gate_result="user_reply")
    assert rid
    path = tmp_path / "deletion_journal.jsonl"
    import json
    rec = json.loads(path.read_text().strip().splitlines()[-1])
    assert rec["actor"] == "automation:tgbot"
    assert rec["object_hash"] == "deadbeef"
    assert rec["user_reply"] == "да, применяй"
    assert rec["gate_result"] == "user_reply"


def test_op_journal_without_new_kwargs_still_works_and_defaults_human(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    rid = s._op_journal("update", [{"taskId": "t1", "title": "X"}], "test")
    assert rid
    path = tmp_path / "deletion_journal.jsonl"
    import json
    rec = json.loads(path.read_text().strip().splitlines()[-1])
    assert rec["actor"] == "human"
    assert "object_hash" not in rec
    assert "user_reply" not in rec


# ===========================================================================
# automation_key — п.1.1 (separate from MCP_SECRET)
# ===========================================================================

def test_resolve_automation_actor_empty_key_returns_none():
    assert s._resolve_automation_actor("") is None
    assert s._resolve_automation_actor(None) is None


def test_resolve_automation_actor_no_keys_configured_returns_none(monkeypatch):
    monkeypatch.delenv("TICKTICK_AUTOMATION_KEY", raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("TICKTICK_AUTOMATION_KEY_"):
            monkeypatch.delenv(name, raising=False)
    assert s._resolve_automation_actor("anything") is None


def test_resolve_automation_actor_valid_default_key(monkeypatch):
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY", "secret-default")
    assert s._resolve_automation_actor("secret-default") == "automation:default"


def test_resolve_automation_actor_valid_named_key(monkeypatch):
    monkeypatch.delenv("TICKTICK_AUTOMATION_KEY", raising=False)
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY_TGBOT", "secret-tgbot")
    assert s._resolve_automation_actor("secret-tgbot") == "automation:tgbot"


def test_resolve_automation_actor_invalid_key_returns_none(monkeypatch):
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY_TGBOT", "secret-tgbot")
    assert s._resolve_automation_actor("wrong-key") is None


def test_resolve_automation_actor_blank_env_value_not_treated_as_configured(monkeypatch):
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY_TGBOT", "")
    assert s._resolve_automation_actor("") is None
    assert s._resolve_automation_actor("anything") is None


def test_automation_key_never_leaks_into_gate_single_output(monkeypatch):
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY_TGBOT", "super-secret-value")
    out = s._gate_single(action="tag_delete", tool_name="delete_tag",
                         objects=[{"id": "tag1", "title": "urgent"}],
                         manifest_id="", user_reply="", tier=2,
                         automation_key="wrong-guess")
    assert "super-secret-value" not in out.message
    assert "wrong-guess" not in out.message


# ===========================================================================
# _gate_single — п.1.6
# ===========================================================================

def test_gate_single_plan_call_never_mutates_and_returns_preview():
    out = s._gate_single(action="tag_delete", tool_name="delete_tag",
                         objects=[{"id": "tag1", "title": "urgent"}],
                         manifest_id="", user_reply="", tier=2)
    assert out.proceed is False
    assert "urgent" in out.message
    assert "manifest_id" in out.message or "Манифест" in out.message


def test_gate_single_execute_without_user_reply_is_refused():
    plan = s._gate_single(action="tag_delete", tool_name="delete_tag",
                          objects=[{"id": "tag1", "title": "urgent"}],
                          manifest_id="", user_reply="", tier=2)
    mid = plan.extra.get("manifest_id") if plan.extra else None
    # extract manifest id from message since it's not returned structurally
    import re
    m = re.search(r'`([0-9a-f]{12})`', plan.message)
    assert m, plan.message
    mid = m.group(1)

    result = s._gate_single(action="tag_delete", tool_name="delete_tag",
                            objects=[{"id": "tag1", "title": "urgent"}],
                            manifest_id=mid, user_reply="", tier=2)
    assert result.proceed is False
    assert "🛑" in result.message


def test_gate_single_repeated_manifest_id_is_refused_one_shot():
    plan = s._gate_single(action="tag_delete", tool_name="delete_tag",
                          objects=[{"id": "tag1", "title": "urgent"}],
                          manifest_id="", user_reply="", tier=2)
    import re
    mid = re.search(r'`([0-9a-f]{12})`', plan.message).group(1)

    first = s._gate_single(action="tag_delete", tool_name="delete_tag",
                           objects=[{"id": "tag1", "title": "urgent"}],
                           manifest_id=mid, user_reply="да, удаляй", tier=2)
    assert first.proceed is True

    second = s._gate_single(action="tag_delete", tool_name="delete_tag",
                            objects=[{"id": "tag1", "title": "urgent"}],
                            manifest_id=mid, user_reply="да, удаляй", tier=2)
    assert second.proceed is False
    assert "🛑" in second.message


def test_gate_single_object_hash_mismatch_is_refused():
    plan = s._gate_single(action="tag_delete", tool_name="delete_tag",
                          objects=[{"id": "tag1", "title": "urgent"}],
                          manifest_id="", user_reply="", tier=2)
    import re
    mid = re.search(r'`([0-9a-f]{12})`', plan.message).group(1)

    # Tamper with the stored manifest's object set directly to simulate
    # "state changed between plan and execute" without waiting on real
    # TickTick state — the binding check must catch it either way.
    s._MANIFESTS[mid]["objects"] = [{"id": "tag2", "title": "different"}]

    result = s._gate_single(action="tag_delete", tool_name="delete_tag",
                            objects=[{"id": "tag1", "title": "urgent"}],
                            manifest_id=mid, user_reply="да, удаляй", tier=2)
    assert result.proceed is False
    assert "🛑" in result.message


def test_gate_single_empty_objects_on_plan_call_refused():
    out = s._gate_single(action="tag_delete", tool_name="delete_tag",
                         objects=[], manifest_id="", user_reply="", tier=2)
    assert out.proceed is False


def test_gate_single_automation_key_bypasses_without_plan_or_reply(monkeypatch):
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY_TGBOT", "secret-tgbot")
    # A valid automation_key proceeds in a SINGLE call — no plan_* round trip
    # and no user_reply needed (references/automation-secrets.md §8: a bot
    # has no human to say "да" for).
    result = s._gate_single(action="tag_delete", tool_name="delete_tag",
                            objects=[{"id": "tag1", "title": "urgent"}],
                            manifest_id="", user_reply="",
                            automation_key="secret-tgbot", tier=2)
    assert result.proceed is True
    assert result.tasks == [{"id": "tag1", "title": "urgent"}]


def test_gate_single_journals_actor_for_automation_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY_TGBOT", "secret-tgbot")
    s._gate_single(action="tag_delete", tool_name="delete_tag",
                   objects=[{"id": "tag1", "title": "urgent"}],
                   manifest_id="", user_reply="", automation_key="secret-tgbot",
                   tier=2)
    import json
    path = tmp_path / "deletion_journal.jsonl"
    recs = [json.loads(line) for line in path.read_text().strip().splitlines()]
    exec_recs = [r for r in recs if r.get("event") == "gate_execute"]
    assert exec_recs
    assert exec_recs[-1]["actor"] == "automation:tgbot"
    assert exec_recs[-1]["gate_ok"] is True


# ===========================================================================
# PLAN_retrofit.md §15.4 — create_tasks must journal `actor="automation:<name>"`
# for a bot call, not the "human" default, so the journal can tell a bot's
# create apart from an interactively-approved one after the fact.
# ===========================================================================

class _FakeTicktickCreate:
    """Minimal `ticktick` (official-API) fake: create_task always succeeds."""

    def create_task(self, title, project_id, **kwargs):
        return {"id": "new-task-1", "title": title, "projectId": project_id}


def test_create_tasks_journals_automation_actor_not_human(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "ticktick", _FakeTicktickCreate())
    # ticktick_v2 left falsy (None) on purpose: the post-verify branch is
    # gated on `and ticktick_v2`, so this keeps the test to exactly the
    # journal-actor plumbing under test, without needing a v2 fake too.
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY", "secret-default")
    monkeypatch.delenv("MCP_SECRET", raising=False)

    import asyncio
    result = asyncio.run(s.create_tasks(
        "Создаю задачу", [{"title": "X", "project_id": "p1"}],
        automation_key="secret-default"))
    assert "🛑" not in result

    import json
    path = tmp_path / "deletion_journal.jsonl"
    recs = [json.loads(line) for line in path.read_text().strip().splitlines()]
    create_recs = [r for r in recs if r.get("op") == "create"]
    assert create_recs, "expected a 'create' journal record"
    assert create_recs[-1]["actor"] == "automation:default"


def test_create_tasks_interactive_execute_mode_journals_human(tmp_path, monkeypatch):
    """The interactive (plan → approve → execute) path must keep journaling
    'human' — only the direct automation_key path gets 'automation:<name>'."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "ticktick", _FakeTicktickCreate())
    monkeypatch.setattr(s, "ticktick_v2", None)

    import time
    mid = "actor-check-mid"
    s._MANIFESTS[mid] = {
        "kind": "create", "raw": [{"title": "X", "project_id": "p1"}],
        "created": time.monotonic(), "plan_shown_at": time.monotonic() - 10,
        "summary": "test", "consumed": False,
    }

    import asyncio
    result = asyncio.run(s.create_tasks_interactive(
        "test", manifest_id=mid, user_reply="да"))
    assert "🛑" not in result

    import json
    path = tmp_path / "deletion_journal.jsonl"
    recs = [json.loads(line) for line in path.read_text().strip().splitlines()]
    create_recs = [r for r in recs if r.get("op") == "create"]
    assert create_recs, "expected a 'create' journal record"
    assert create_recs[-1]["actor"] == "human"
