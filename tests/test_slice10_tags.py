"""PLAN_retrofit.md ПАКЕТ 10 — gate + journal + format for create_tag /
rename_tag / delete_tag.

Covers:
  10.1 delete_tag через _gate_single(tier=2) — no longer mutates ungated.
  10.2 delete_tag pre-snapshot: full carrier task list (id+title) in the
       journal record, not just a count.
  10.3 rename_tag plain (non-merge) branch through _gate_single(tier=1).
  10.5 create_tag through _gate_single(tier=1).
  10.6 op="tag_create"/"tag_rename"/"tag_delete" journal records with actor.
  10.7 create_tag: duplicate short-circuits to ↷ instead of creating a
       second tag; post-verify also checks color, giving a real ❌ branch.
  10.8/10.9 WRITE/DESTRUCTIVE annotations + unified _tool_response output
       (checked for create_tag/delete_tag/rename_tag's PLAIN branch only —
       the merge branch's format is untouched per explicit instruction and
       is covered separately in test_consent_gate.py).

rename_tag's MERGE branch is not touched by this package (deferred item
10.4) and is not re-tested here — see test_consent_gate.py.
"""
import json
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


class _FakeV2Tags:
    """Fake v2 client covering tag reads/writes for gate-flow tests. `live`
    is the list of tag dicts ({"name", "color"}); `tasks_by_tag` maps a
    lowercased tag name to the list of carrier task dicts."""

    def __init__(self, tags=None, tasks_by_tag=None):
        self.tags = tags or []
        self.tasks_by_tag = tasks_by_tag or {}
        self.calls = []

    def get_state(self, force=True):
        return {}

    def get_tags(self):
        return [dict(t) for t in self.tags]

    def get_tasks_by_tag(self, tag_label):
        return list(self.tasks_by_tag.get(tag_label.lower(), []))

    def create_tag(self, name, color=None):
        self.calls.append(("create", name, color))
        self.tags.append({"name": name.lower(), "color": color})
        return {}

    def rename_tag(self, old_name, new_name):
        self.calls.append(("rename", old_name, new_name))
        self.tags = [t for t in self.tags if t.get("name") != old_name.lower()]
        self.tags.append({"name": new_name.lower(), "color": None})
        return {}

    def delete_tag(self, name):
        self.calls.append(("delete", name))
        self.tags = [t for t in self.tags if t.get("name") != name.lower()]
        return {}


def _journal_records(tmp_path):
    path = tmp_path / "deletion_journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ===========================================================================
# create_tag — 10.5, 10.6, 10.7
# ===========================================================================

async def test_create_tag_duplicate_is_skipped_not_recreated(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(tags=[{"name": "работа", "color": None}])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    result = await s.create_tag("работа")
    assert "↷" in result
    assert fake.calls == []  # never even planned


async def test_create_tag_plan_call_mutates_nothing(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(tags=[])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    result = await s.create_tag("новый")
    assert "📋" in result
    assert fake.calls == []
    assert fake.tags == []


async def test_create_tag_execute_without_reply_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = _FakeV2Tags(tags=[])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    plan = await s.create_tag("новый")
    mid = _extract_manifest_id(plan)
    result = await s.create_tag("новый", manifest_id=mid)
    assert "🛑" in result
    assert fake.tags == []


async def test_create_tag_execute_with_consent_creates_and_journals(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_MIN_CONSENT_GAP", 0)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = _FakeV2Tags(tags=[])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    plan = await s.create_tag("новый", color="#FF6161")
    mid = _extract_manifest_id(plan)
    result = await s.create_tag("новый", color="#FF6161", manifest_id=mid,
                                user_reply="да, создавай")
    assert result.startswith("### ✅")
    assert "новый" in result
    assert fake.calls == [("create", "новый", "#FF6161")]

    recs = _journal_records(tmp_path)
    op_recs = [r for r in recs if r.get("op") == "tag_create"]
    assert len(op_recs) == 1
    assert op_recs[0]["actor"] == "human"
    assert op_recs[0]["user_reply"] == "да, создавай"
    assert op_recs[0]["items"][0]["title"] == "новый"


async def test_create_tag_color_mismatch_after_creation_is_discrepancy(monkeypatch, tmp_path):
    """10.7 — post-verify now also checks color, so a THIRD (❌) outcome is
    reachable, not just ✅/⚠️."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_MIN_CONSENT_GAP", 0)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))

    class _WrongColorFake(_FakeV2Tags):
        def create_tag(self, name, color=None):
            self.calls.append(("create", name, color))
            # TickTick "ignores" the requested color — simulate the drift.
            self.tags.append({"name": name.lower(), "color": "#000000"})
            return {}

    fake = _WrongColorFake(tags=[])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    plan = await s.create_tag("новый", color="#FF6161")
    mid = _extract_manifest_id(plan)
    result = await s.create_tag("новый", color="#FF6161", manifest_id=mid,
                                user_reply="да")
    assert result.startswith("### ❌")


# ===========================================================================
# rename_tag (plain branch) — 10.3
# ===========================================================================

async def test_rename_tag_plain_plan_call_mutates_nothing(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(tags=[{"name": "старый", "color": None}])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    result = await s.rename_tag("старый", "новый")
    assert "📋" in result
    assert fake.calls == []


async def test_rename_tag_plain_execute_with_consent_renames_and_journals(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_MIN_CONSENT_GAP", 0)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = _FakeV2Tags(tags=[{"name": "старый", "color": None}])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    plan = await s.rename_tag("старый", "новый")
    mid = _extract_manifest_id(plan)
    result = await s.rename_tag("старый", "новый", manifest_id=mid,
                                user_reply="да, переименуй")
    assert result.startswith("### ✅")
    assert fake.calls == [("rename", "старый", "новый")]

    recs = _journal_records(tmp_path)
    op_recs = [r for r in recs if r.get("op") == "tag_rename"]
    assert len(op_recs) == 1
    assert op_recs[0]["actor"] == "human"
    assert op_recs[0]["user_reply"] == "да, переименуй"


async def test_rename_tag_plain_execute_without_reply_is_refused(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(tags=[{"name": "старый", "color": None}])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    plan = await s.rename_tag("старый", "новый")
    mid = _extract_manifest_id(plan)
    result = await s.rename_tag("старый", "новый", manifest_id=mid)
    assert "🛑" in result
    assert fake.calls == []
    assert fake.tags == [{"name": "старый", "color": None}]


async def test_rename_tag_missing_source_tag_refuses_before_any_gate(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(tags=[])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    result = await s.rename_tag("нет-такого", "новый")
    assert "🛑" in result
    assert fake.calls == []


# ===========================================================================
# delete_tag — 10.1, 10.2
# ===========================================================================

async def test_delete_tag_plan_call_mutates_nothing_and_shows_carriers(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(
        tags=[{"name": "устарел", "color": None}],
        tasks_by_tag={"устарел": [{"id": "t1", "title": "Задача 1"},
                                  {"id": "t2", "title": "Задача 2"}]})
    monkeypatch.setattr(s, "ticktick_v2", fake)
    result = await s.delete_tag("устарел")
    assert "📋" in result
    assert "Задача 1" in result
    assert "Задача 2" in result
    assert fake.calls == []


async def test_delete_tag_execute_without_reply_is_refused(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(tags=[{"name": "устарел", "color": None}])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    plan = await s.delete_tag("устарел")
    mid = _extract_manifest_id(plan)
    result = await s.delete_tag("устарел", manifest_id=mid)
    assert "🛑" in result
    assert fake.calls == []
    assert fake.tags == [{"name": "устарел", "color": None}]


async def test_delete_tag_execute_with_consent_deletes_and_journals_carriers(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_MIN_CONSENT_GAP", 0)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = _FakeV2Tags(
        tags=[{"name": "устарел", "color": "#ABCDEF"}],
        tasks_by_tag={"устарел": [{"id": "t1", "title": "Задача 1"}]})
    monkeypatch.setattr(s, "ticktick_v2", fake)
    plan = await s.delete_tag("устарел")
    mid = _extract_manifest_id(plan)
    result = await s.delete_tag("устарел", manifest_id=mid, user_reply="да, удаляй")
    assert result.startswith("### ✅")
    assert "1" in result
    assert fake.calls == [("delete", "устарел")]

    recs = _journal_records(tmp_path)
    op_recs = [r for r in recs if r.get("op") == "tag_delete"]
    assert len(op_recs) == 1
    item = op_recs[0]["items"][0]
    # 10.2 — pre-snapshot carries the FULL carrier list, not just a count.
    assert item["carrier_tasks"] == [{"taskId": "t1", "title": "Задача 1"}]
    # ...and the tag's own pre-mutation fields (kind="tag" via _snapshot_of).
    assert item["snapshot"].get("color") == "#ABCDEF"
    assert op_recs[0]["actor"] == "human"
    assert op_recs[0]["user_reply"] == "да, удаляй"


async def test_delete_tag_missing_tag_refuses_before_any_gate(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(tags=[])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    result = await s.delete_tag("призрак")
    assert "🛑" in result
    assert fake.calls == []


# ===========================================================================
# 10.8 — WRITE/DESTRUCTIVE annotations
# ===========================================================================

def test_tag_tools_have_write_or_destructive_annotations():
    import asyncio
    tools = {t.name: t for t in asyncio.run(s.mcp.list_tools())}
    create = tools["create_tag"]
    rename = tools["rename_tag"]
    delete = tools["delete_tag"]
    assert create.annotations.readOnlyHint is False
    assert create.annotations.destructiveHint is False
    assert rename.annotations.readOnlyHint is False
    assert rename.annotations.destructiveHint is False
    assert delete.annotations.readOnlyHint is False
    assert delete.annotations.destructiveHint is True
