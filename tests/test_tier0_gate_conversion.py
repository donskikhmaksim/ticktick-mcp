"""2026-08-05: Maksim removed the tier-🟢 exemption ("создание тоже должно
быть через флоу план→экзек, всё что не только читает — через этот флоу").
These 10 tools used to mutate on a SINGLE call, with zero test coverage of
that fact: create_subtask, unset_task_parent, create_project_group,
delete_project_group, move_project_to_group, add_task_comment,
attach_file_to_task, create_tag, duplicate_task, update_task_comment.
(create_project, create_project_column and checkin_habit — the other 3 of
the original 15 — already had test files; those were updated in place to the
new two-call shape instead of being duplicated here.)

Each is now a TWO-call tool (same name, mirroring update_tasks/_gate_batch):
call #1 (no manifest_id) previews and mutates NOTHING; call #2 (manifest_id +
user_reply) is checked by _require_consent(tier=1, ...) and only then runs
the real mutation, exactly once (one-shot manifest).

Per docs/testing-deploy.md §13: each tool below gets a happy path, a refusal
path (empty/negative user_reply — proving the external client is untouched),
and a one-shot edge case. Identity-guard/post-verify internals are UNCHANGED
pre-existing logic (only moved into a `_x_impl` helper verbatim) and are not
re-tested here; `_guard_task`/`_guard_project` are monkeypatched to a plain
"ok" stand-in so these tests isolate the NEW gate wrapper behaviour.

No real network — the v2/official clients are faked."""
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


def _ok_guard(*_a, **_k):
    return s._Guard("ok", project_id="p1", title="Купить молоко")


class FakeV2:
    """One fake covering every v2 call touched by the 10 tools below,
    mutating a shared `live`/`groups`/`tags`/`comments` state so post-verify
    (unchanged pre-existing logic) sees the effect of a real mutation."""

    def __init__(self, live=None, groups=None, projects=None, tags=None,
                 comments=None):
        self.live = live or {}
        self.groups = groups if groups is not None else []
        self.projects = projects if projects is not None else []
        self.tags = tags if tags is not None else []
        self.comments = comments or {}
        self.calls = []

    # --- shared state plumbing ---
    def get_state(self, force=False):
        return {}

    def get_open_tasks(self):
        return list(self.live.values())

    def invalidate_cache(self):
        pass

    # --- create_subtask uses the OFFICIAL client instead, see FakeOfficial ---

    # --- unset_task_parent ---
    def unset_task_parent(self, task_id, parent_id, project_id):
        self.calls.append(("unset_parent", task_id))
        self.live[task_id]["parentId"] = None
        return {}

    # --- project groups ---
    def create_project_group(self, name):
        self.calls.append(("create_group", name))
        gid = f"g-{len(self.groups) + 1}"
        self.groups.append({"id": gid, "name": name})
        return gid

    def list_project_groups(self):
        return list(self.groups)

    def delete_project_group(self, group_id):
        self.calls.append(("delete_group", group_id))
        self.groups = [g for g in self.groups if g.get("id") != group_id]
        return {}

    def move_project_to_group(self, project_id, group_id):
        self.calls.append(("move_group", project_id, group_id))
        for p in self.projects:
            if p.get("id") == project_id:
                p["groupId"] = None if group_id == "NONE" else group_id

    def list_projects(self):
        return list(self.projects)

    # --- comments ---
    def add_task_comment(self, project_id, task_id, text):
        self.calls.append(("add_comment", task_id, text))
        self.comments.setdefault(task_id, []).append(
            {"id": "c1", "title": text})

    def update_task_comment(self, project_id, task_id, comment_id, text):
        self.calls.append(("update_comment", comment_id, text))
        for c in self.comments.get(task_id, []):
            if c.get("id") == comment_id:
                c["title"] = text

    def get_task_comments(self, project_id, task_id):
        return list(self.comments.get(task_id, []))

    # --- attachments ---
    def upload_attachment(self, project_id, task_id, url=None,
                          content_base64=None, filename=None):
        self.calls.append(("attach", task_id))
        atts = self.live[task_id].setdefault("attachments", [])
        atts.append({"fileName": filename or "file.txt"})
        return {"fileName": filename or "file.txt", "size": 123}

    # --- tags ---
    def create_tag(self, name, color=None):
        self.calls.append(("create_tag", name))
        self.tags.append({"name": name})

    def get_tags(self):
        return list(self.tags)

    # --- duplicate ---
    def duplicate_task(self, task_id):
        self.calls.append(("duplicate", task_id))
        src = self.live[task_id]
        copy = dict(src, id=f"{task_id}-copy", title=src.get("title"))
        self.live[copy["id"]] = copy
        return copy


class FakeOfficial:
    def __init__(self):
        self.calls = []

    def create_subtask(self, subtask_title, parent_task_id, project_id,
                       content=None, priority=0):
        self.calls.append(("create_subtask", subtask_title, parent_task_id))
        return {"id": "sub1", "title": subtask_title}


def _wire(monkeypatch, fake_v2=None, fake_official=None, guard_task=True,
         guard_project=True):
    if fake_v2 is not None:
        monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    if fake_official is not None:
        monkeypatch.setattr(s, "ticktick", fake_official)
    if guard_task:
        monkeypatch.setattr(s, "_guard_task", _ok_guard)
    if guard_project:
        monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)


# ===========================================================================
# create_subtask
# ===========================================================================

async def test_create_subtask_full_gate_cycle(monkeypatch):
    official = FakeOfficial()
    fake_v2 = FakeV2(live={"p1": {"id": "p1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, fake_official=official)

    preview = await s.create_subtask("Купить молоко", "Купить хлеб", "p1", "proj1")
    assert official.calls == []
    assert "manifest_id" in preview
    assert "«Купить хлеб»" in preview

    mid = _extract_manifest_id(preview)
    refused = await s.create_subtask("Купить молоко", "Купить хлеб", "p1", "proj1",
                                     manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert official.calls == []

    result = await s.create_subtask("Купить молоко", "Купить хлеб", "p1", "proj1",
                                     manifest_id=mid, user_reply="да")
    assert official.calls == [("create_subtask", "Купить хлеб", "p1")]
    assert "🛑" not in result

    again = await s.create_subtask("Купить молоко", "Купить хлеб", "p1", "proj1",
                                    manifest_id=mid, user_reply="да")
    assert "🛑" in again
    assert len(official.calls) == 1


async def test_create_subtask_invalid_priority_refused_before_gate(monkeypatch):
    official = FakeOfficial()
    _wire(monkeypatch, fake_v2=FakeV2(), fake_official=official)
    result = await s.create_subtask("Купить молоко", "Купить хлеб", "p1", "proj1",
                                    priority=99)
    assert "🛑" in result or "Invalid priority" in result
    assert official.calls == []


# ===========================================================================
# unset_task_parent
# ===========================================================================

async def test_unset_task_parent_full_gate_cycle(monkeypatch):
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "_guard_task",
                        lambda *a, **k: s._Guard("ok", project_id="p1", title="Шаг 1"))

    preview = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1")
    assert fake_v2.calls == []

    mid = _extract_manifest_id(preview)
    refused = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1",
                                        manifest_id=mid, user_reply="нет")
    assert "🛑" in refused
    assert live["c"]["parentId"] == "p"

    dead = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1",
                                     manifest_id=mid, user_reply="да")
    assert "🛑" in dead  # manifest was invalidated by the explicit "no" above
    assert live["c"]["parentId"] == "p"


async def test_unset_task_parent_confirmed_detaches(monkeypatch):
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "_guard_task",
                        lambda *a, **k: s._Guard("ok", project_id="p1", title="Шаг 1"))

    preview = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1")
    mid = _extract_manifest_id(preview)
    result = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert ("unset_parent", "c") in fake_v2.calls
    assert "🛑" not in result


# ===========================================================================
# create_project_group
# ===========================================================================

async def test_create_project_group_full_gate_cycle(monkeypatch):
    fake_v2 = FakeV2()
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.create_project_group("Личное")
    assert fake_v2.calls == []
    mid = _extract_manifest_id(preview)

    refused = await s.create_project_group("Личное", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert fake_v2.calls == []

    result = await s.create_project_group("Личное", manifest_id=mid, user_reply="да")
    assert ("create_group", "Личное") in fake_v2.calls
    assert "🛑" not in result

    again = await s.create_project_group("Личное", manifest_id=mid, user_reply="да")
    assert "🛑" in again


# ===========================================================================
# delete_project_group
# ===========================================================================

async def test_delete_project_group_full_gate_cycle(monkeypatch):
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Личное"}])
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.delete_project_group("Личное", "g1")
    assert fake_v2.calls == []
    mid = _extract_manifest_id(preview)

    result = await s.delete_project_group("Личное", "g1", manifest_id=mid, user_reply="да")
    assert ("delete_group", "g1") in fake_v2.calls
    assert not any(g["id"] == "g1" for g in fake_v2.groups)
    assert "🛑" not in result


async def test_delete_project_group_unknown_manifest_refused(monkeypatch):
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Личное"}])
    _wire(monkeypatch, fake_v2=fake_v2)
    result = await s.delete_project_group("Личное", "g1",
                                          manifest_id="ghost", user_reply="да")
    assert "🛑" in result
    assert fake_v2.calls == []


# ===========================================================================
# move_project_to_group
# ===========================================================================

async def test_move_project_to_group_full_gate_cycle(monkeypatch):
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Личное"}],
                     projects=[{"id": "p1", "groupId": None}])
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})

    preview = await s.move_project_to_group("Работа", "p1", "g1")
    assert fake_v2.calls == []
    mid = _extract_manifest_id(preview)

    result = await s.move_project_to_group("Работа", "p1", "g1",
                                           manifest_id=mid, user_reply="да")
    assert ("move_group", "p1", "g1") in fake_v2.calls
    assert fake_v2.projects[0]["groupId"] == "g1"
    assert "🛑" not in result


# ===========================================================================
# add_task_comment
# ===========================================================================

async def test_add_task_comment_full_gate_cycle(monkeypatch):
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1")
    assert fake_v2.calls == []
    mid = _extract_manifest_id(preview)

    refused = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1",
                                       manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert fake_v2.calls == []

    result = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1",
                                      manifest_id=mid, user_reply="да")
    assert ("add_comment", "t1", "не забыть") in fake_v2.calls
    assert "🛑" not in result


# ===========================================================================
# attach_file_to_task
# ===========================================================================

async def test_attach_file_to_task_full_gate_cycle(monkeypatch):
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                          url="https://x/file.pdf")
    assert fake_v2.calls == []
    mid = _extract_manifest_id(preview)

    result = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                         url="https://x/file.pdf",
                                         manifest_id=mid, user_reply="да")
    assert ("attach", "t1") in fake_v2.calls
    assert "🛑" not in result


async def test_attach_file_to_task_missing_source_refused_before_gate(monkeypatch):
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2)
    result = await s.attach_file_to_task("Купить молоко", "t1", "p1")
    assert "url or content_base64" in result
    assert fake_v2.calls == []


# ===========================================================================
# create_tag
# ===========================================================================

async def test_create_tag_full_gate_cycle(monkeypatch):
    fake_v2 = FakeV2()
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.create_tag("срочное")
    assert fake_v2.calls == []
    mid = _extract_manifest_id(preview)

    result = await s.create_tag("срочное", manifest_id=mid, user_reply="да")
    assert ("create_tag", "срочное") in fake_v2.calls
    assert "🛑" not in result

    again = await s.create_tag("срочное", manifest_id=mid, user_reply="да")
    assert "🛑" in again


# ===========================================================================
# duplicate_task
# ===========================================================================

async def test_duplicate_task_full_gate_cycle(monkeypatch):
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.duplicate_task("Дублирую задачу", "t1", "Купить молоко")
    assert fake_v2.calls == []
    mid = _extract_manifest_id(preview)

    result = await s.duplicate_task("Дублирую задачу", "t1", "Купить молоко",
                                    manifest_id=mid, user_reply="да")
    assert ("duplicate", "t1") in fake_v2.calls
    assert "t1-copy" in fake_v2.live
    assert "🛑" not in result


# ===========================================================================
# update_task_comment
# ===========================================================================

async def test_update_task_comment_full_gate_cycle(monkeypatch):
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}},
                     comments={"t1": [{"id": "c1", "title": "старый текст"}]})
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1")
    assert fake_v2.calls == []
    mid = _extract_manifest_id(preview)

    result = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1",
                                         manifest_id=mid, user_reply="да")
    assert ("update_comment", "c1", "новый текст") in fake_v2.calls
    assert fake_v2.comments["t1"][0]["title"] == "новый текст"
    assert "🛑" not in result
