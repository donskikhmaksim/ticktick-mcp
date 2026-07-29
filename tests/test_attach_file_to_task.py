"""attach_file_to_task — PLAN_retrofit.md ПАКЕТ 9 (п.9.5/9.6/9.8/9.9): the
tool now runs through the shared `_gate_single` two-call gate (tier 1, 🟡),
verifies the uploaded attachment by NAME + SIZE (not just a count), writes an
`_op_journal` audit record, and answers in Russian via `_tool_response`. No
real network — the v2 client and the identity-guard's live state are faked."""
import base64
import json
import re

import pytest

import ticktick_mcp.src.server as s

TASK_ID = "task1"
PROJECT_ID = "proj1"
TASK_TITLE = "Отчёт за июль"


def _live_task(title=TASK_TITLE, project_id=PROJECT_ID, task_id=TASK_ID):
    return {task_id: {"id": task_id, "title": title, "projectId": project_id,
                      "attachments": []}}


class FakeV2:
    def __init__(self, raises=None, attachments=None):
        self.raises = raises
        self.uploaded = None
        # What a fresh re-read (_merged_task_attachments -> get_task_attachments)
        # sees AFTER the upload — set per-test to simulate the real outcome.
        self.attachments = attachments if attachments is not None else []

    def upload_attachment(self, project_id, task_id, *, url=None,
                          content_base64=None, filename=None):
        if self.raises:
            raise self.raises
        data = base64.b64decode(content_base64) if content_base64 else b"from-url"
        self.uploaded = (project_id, task_id, url, content_base64, filename)
        return {"fileName": filename, "size": len(data)}

    def get_task_attachments(self, task_id):
        return list(self.attachments)

    def get_content_attachment_refs(self, task_id):
        return []


@pytest.fixture(autouse=True)
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: _live_task())
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    # Don't let a developer's real automation keys leak into these tests.
    monkeypatch.delenv("TICKTICK_AUTOMATION_KEY", raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("TICKTICK_AUTOMATION_KEY_"):
            monkeypatch.delenv(name, raising=False)
    fake = FakeV2()
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


def _journal_records(tmp_path):
    path = tmp_path / "deletion_journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _manifest_id(text):
    m = re.search(r"`([0-9a-f]{12})`", text)
    assert m, f"no manifest id found in: {text!r}"
    return m.group(1)


# ===========================================================================
# Gate (п.9.5): call #1 plans, call #2 executes — nothing mutates on #1.
# ===========================================================================

async def test_call1_shows_a_plan_and_uploads_nothing(wired):
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    assert "manifest_id" in out or "Манифест" in out
    assert wired.uploaded is None


async def test_call1_requires_a_file_source(wired):
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID)
    assert "🛑" in out
    assert wired.uploaded is None


async def test_call2_without_reply_does_not_upload(wired):
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    mid = _manifest_id(plan)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id=mid, user_reply="")
    assert wired.uploaded is None
    assert "🛑" in out


async def test_call2_with_affirmative_reply_uploads_and_confirms(wired, tmp_path):
    wired.attachments = [{"id": "a1", "fileName": "note.txt", "fileSize": 2}]
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    mid = _manifest_id(plan)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id=mid, user_reply="да, давай")
    assert wired.uploaded == (PROJECT_ID, TASK_ID, None,
                              base64.b64encode(b"hi").decode(), "note.txt")
    assert "✅" in out
    assert "note.txt" in out
    assert TASK_TITLE in out


async def test_call2_with_negative_reply_refuses(wired):
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    mid = _manifest_id(plan)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id=mid, user_reply="нет, не надо")
    assert wired.uploaded is None
    assert "🛑" in out


async def test_call2_ignores_a_swapped_file_and_uses_the_planned_one(wired):
    """The url/content_base64/filename from call #2's own arguments must be
    IGNORED — only what was stored in the manifest at plan time may upload,
    otherwise a plan for a harmless file could be swapped for another one
    right before the human's "yes" is spent."""
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"planned").decode(), filename="planned.txt")
    mid = _manifest_id(plan)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"SWAPPED").decode(), filename="evil.txt",
        manifest_id=mid, user_reply="да")
    assert wired.uploaded[3] == base64.b64encode(b"planned").decode()
    assert wired.uploaded[4] == "planned.txt"
    assert "planned.txt" in out
    assert "evil.txt" not in out


async def test_unknown_manifest_id_is_refused(wired):
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id="doesnotexist", user_reply="да")
    assert "🛑" in out
    assert wired.uploaded is None


# ===========================================================================
# Identity-guard (pre-existing ✅, now wrapped around the gate too).
# ===========================================================================

async def test_wrong_title_for_the_id_refuses_before_planning(wired):
    out = await s.attach_file_to_task(
        task_title="Совсем другая задача", task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode())
    assert "🛑" in out
    assert wired.uploaded is None


async def test_state_unavailable_refuses(wired, monkeypatch):
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: None)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode())
    assert "🛑" in out
    assert wired.uploaded is None


async def test_task_renamed_between_plan_and_execute_refuses(wired, monkeypatch):
    """Right-before-mutation identity re-check: the manifest was planned
    against one live title; by call #2 the task has a different one."""
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    mid = _manifest_id(plan)
    monkeypatch.setattr(s, "_open_by_id",
                        lambda fresh=False: _live_task(title="Новое имя"))
    out = await s.attach_file_to_task(
        task_title="", task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id=mid, user_reply="да")
    assert "🛑" in out
    assert wired.uploaded is None


# ===========================================================================
# Post-verify (п.9.6): name + size, not just an attachment-count bump.
# ===========================================================================

async def test_post_verify_warns_when_attachment_not_found(wired):
    wired.attachments = []  # upload "succeeded" but nothing shows up afterwards
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    mid = _manifest_id(plan)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id=mid, user_reply="да")
    assert "⚠️" in out
    assert "не найдено" in out


async def test_post_verify_warns_on_size_mismatch(wired):
    wired.attachments = [{"id": "a1", "fileName": "note.txt", "fileSize": 999}]
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    mid = _manifest_id(plan)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id=mid, user_reply="да")
    assert "⚠️" in out
    assert "расходится" in out


async def test_post_verify_prefers_a_newly_appeared_attachment(wired, monkeypatch):
    """A same-named attachment that already existed BEFORE this upload must
    not be mistaken for the one just added (the count-only bug this package
    fixes) — the id-not-in-pre check picks the new one. `calls` counts
    `_merged_task_attachments` invocations inside the tool: call #1 is the
    pre-upload snapshot (only "old"), call #2 is post-verify (both) — the
    tool must bind to "new", the one absent from the pre snapshot."""
    calls = {"n": 0}
    before = [{"id": "old", "fileName": "note.txt", "fileSize": 2}]
    after = before + [{"id": "new", "fileName": "note.txt", "fileSize": 2}]

    def fake_merged(task_id):
        calls["n"] += 1
        return before if calls["n"] == 1 else after

    monkeypatch.setattr(s, "_merged_task_attachments", fake_merged)
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    mid = _manifest_id(plan)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id=mid, user_reply="да")
    assert "✅" in out


# ===========================================================================
# Errors, audit (п.9.8), automation_key bypass.
# ===========================================================================

async def test_upload_exception_is_refused_cleanly(wired):
    wired.raises = RuntimeError("upstream 500")
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    mid = _manifest_id(plan)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id=mid, user_reply="да")
    assert "🛑" in out
    assert "upstream 500" in out


async def test_successful_attach_writes_an_audit_journal_entry(wired, tmp_path):
    wired.attachments = [{"id": "a1", "fileName": "note.txt", "fileSize": 2}]
    plan = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt")
    mid = _manifest_id(plan)
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        manifest_id=mid, user_reply="да")

    records = _journal_records(tmp_path)
    attach_records = [r for r in records if r.get("op") == "attach"]
    assert len(attach_records) == 1
    rec = attach_records[0]
    assert rec["items"][0]["taskId"] == TASK_ID
    assert rec["items"][0]["title"] == TASK_TITLE
    assert rec["items"][0]["snapshot"]["fileName"] == "note.txt"
    assert rec["actor"] == "human"
    assert "🧾" in out


async def test_automation_key_bypasses_the_gate_in_one_call(wired, tmp_path, monkeypatch):
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY", "bot-secret-123")
    wired.attachments = [{"id": "a1", "fileName": "note.txt", "fileSize": 2}]
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt",
        automation_key="bot-secret-123")
    assert wired.uploaded is not None
    assert "✅" in out
    records = _journal_records(tmp_path)
    attach_records = [r for r in records if r.get("op") == "attach"]
    assert attach_records[0]["actor"] == "automation:default"


async def test_wrong_automation_key_falls_back_to_the_interactive_gate(wired):
    out = await s.attach_file_to_task(
        task_title=TASK_TITLE, task_id=TASK_ID, project_id=PROJECT_ID,
        content_base64=base64.b64encode(b"hi").decode(), filename="note.txt",
        automation_key="not-the-real-secret")
    assert wired.uploaded is None
    assert "Манифест" in out or "manifest_id" in out
