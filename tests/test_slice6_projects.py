"""PLAN_retrofit.md ПАКЕТ 12 — gate + pre-snapshot + format for the seven
project/project-group write methods: create_project, update_project,
archive_project, create_project_group, delete_project_group,
move_project_to_group, create_project_column.

All seven now route through the shared `_gate_single` two-call gate
(STANDARD.md §3.1, references/gate.md, package 1's helper): call #1
(manifest_id/user_reply omitted) mutates NOTHING and returns a plan preview
carrying `manifest_id="<hex>"`; call #2 (manifest_id + affirmative
user_reply) actually mutates. Post-verify (independent fresh re-read) was
already in place for create_project/update_project/archive_project/
create_project_column from a previous session — those bodies are preserved,
just gated and (for create_project_column) format-unified via
`_tool_response`. create_project_group/delete_project_group/
move_project_to_group get the same `_tool_response` unification here for the
first time (п.12.6/12.7), and delete_project_group additionally gets a
pre-mutation journal snapshot (п.12.2) that it previously lacked entirely.

No real network — the official (v1) and v2 clients are faked."""
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------

class FakeOfficialCreate:
    def __init__(self, created, fresh=None, get_error=None):
        self._created = created
        self._fresh = fresh if fresh is not None else dict(created)
        self._get_error = get_error
        self.get_calls = 0
        self.create_calls = 0

    def create_project(self, name, color="#F18181", view_mode="list"):
        self.create_calls += 1
        return dict(self._created)

    def get_project(self, project_id):
        self.get_calls += 1
        if self._get_error:
            return {"error": self._get_error}
        return dict(self._fresh)


def _wire_official(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick", fake)


async def _plan_and_confirm_create_project(fake, name="Работа", **kwargs):
    preview = await s.create_project(name, **kwargs)
    mid = _extract_manifest_id(preview)
    return preview, await s.create_project(name, manifest_id=mid, user_reply="да", **kwargs)


async def test_create_project_call1_previews_nothing_created(monkeypatch):
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"})
    _wire_official(monkeypatch, fake)
    preview = await s.create_project("Работа")
    assert "manifest_id" in preview
    assert "«Работа»" in preview
    assert fake.create_calls == 0


async def test_create_project_call2_without_reply_is_refused_and_retryable(monkeypatch):
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"})
    _wire_official(monkeypatch, fake)
    preview = await s.create_project("Работа")
    mid = _extract_manifest_id(preview)
    refused = await s.create_project("Работа", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert fake.create_calls == 0
    result = await s.create_project("Работа", manifest_id=mid, user_reply="да, давай")
    assert fake.create_calls == 1
    assert "🛑" not in result


async def test_create_project_explicit_no_burns_the_manifest(monkeypatch):
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"})
    _wire_official(monkeypatch, fake)
    preview = await s.create_project("Работа")
    mid = _extract_manifest_id(preview)
    refused = await s.create_project("Работа", manifest_id=mid, user_reply="нет, погоди")
    assert "🛑" in refused
    dead = await s.create_project("Работа", manifest_id=mid, user_reply="да")
    assert "🛑" in dead
    assert fake.create_calls == 0


async def test_create_project_invalid_view_mode_is_refused_before_any_plan(monkeypatch):
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"})
    _wire_official(monkeypatch, fake)
    result = await s.create_project("Работа", view_mode="bogus")
    assert "🛑" in result
    assert "manifest_id" not in result
    assert fake.create_calls == 0


async def test_create_project_success_is_post_verified(monkeypatch):
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа", "color": "#F18181"})
    _wire_official(monkeypatch, fake)
    _, result = await _plan_and_confirm_create_project(fake)
    assert result.startswith("### ✅")
    assert "Работа" in result
    assert "🧾" in result
    assert fake.get_calls == 1


async def test_create_project_api_error_is_refused(monkeypatch):
    fake = FakeOfficialCreate({})
    fake.create_project = lambda name, color="#F18181", view_mode="list": {"error": "boom"}
    _wire_official(monkeypatch, fake)
    _, result = await _plan_and_confirm_create_project(fake)
    assert result.startswith("### ❌")
    assert "boom" in result
    assert fake.get_calls == 0


async def test_create_project_postverify_mismatch_is_unverified(monkeypatch):
    # TickTick's create response "succeeds" but the fresh re-read can't find
    # the project (id mismatch / not actually there) — must NOT claim ✅.
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"},
                              fresh={"id": "OTHER", "name": "??"})
    _wire_official(monkeypatch, fake)
    _, result = await _plan_and_confirm_create_project(fake)
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result


async def test_create_project_postverify_fetch_failure_is_unverified(monkeypatch):
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"}, get_error="rate limited")
    _wire_official(monkeypatch, fake)
    _, result = await _plan_and_confirm_create_project(fake)
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result


# ---------------------------------------------------------------------------
# update_project
# ---------------------------------------------------------------------------

class FakeOfficialUpdate:
    def __init__(self, fresh, update_resp=None, update_error=None):
        self._fresh = fresh
        self._update_resp = update_resp if update_resp is not None else dict(fresh)
        self._update_error = update_error
        self.get_calls = 0
        self.update_calls = 0

    def update_project(self, project_id, name=None, color=None, view_mode=None):
        self.update_calls += 1
        if self._update_error:
            return {"error": self._update_error}
        return dict(self._update_resp)

    def get_project(self, project_id):
        self.get_calls += 1
        return dict(self._fresh)


def _wire_update(monkeypatch, fake, names=None):
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: (names or {}))


async def _plan_and_confirm_update(project_name, project_id, **kwargs):
    preview = await s.update_project(project_name, project_id, **kwargs)
    mid = _extract_manifest_id(preview)
    return preview, await s.update_project(project_name, project_id,
                                           manifest_id=mid, user_reply="да", **kwargs)


async def test_update_project_call1_previews_nothing_updated(monkeypatch):
    fake = FakeOfficialUpdate({"id": "p1", "name": "Работа"})
    _wire_update(monkeypatch, fake)
    preview = await s.update_project("Работа", "p1", name="Новое имя")
    assert "manifest_id" in preview
    assert fake.update_calls == 0


async def test_update_project_success_is_post_verified(monkeypatch):
    fake = FakeOfficialUpdate({"id": "p1", "name": "Новое имя", "color": "#111111"})
    _wire_update(monkeypatch, fake)
    _, result = await _plan_and_confirm_update("Работа", "p1", name="Новое имя", color="#111111")
    assert result.startswith("### ✅")
    assert "Новое имя" in result
    assert "🧾" in result
    assert fake.get_calls == 1
    assert fake.update_calls == 1


async def test_update_project_refused_by_guard(monkeypatch):
    fake = FakeOfficialUpdate({"id": "p1", "name": "Работа"})
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: "🛑 Отказ — не та личность.")
    result = await s.update_project("Совсем другое", "p1", name="X")
    assert result.startswith("🛑")
    assert fake.get_calls == 0
    assert fake.update_calls == 0


async def test_update_project_nothing_to_change_is_refused(monkeypatch):
    fake = FakeOfficialUpdate({"id": "p1", "name": "Работа"})
    _wire_update(monkeypatch, fake)
    result = await s.update_project("Работа", "p1")
    assert "🛑" in result
    assert "manifest_id" not in result
    assert fake.update_calls == 0


async def test_update_project_blank_field_is_refused(monkeypatch):
    fake = FakeOfficialUpdate({"id": "p1", "name": "Работа"})
    _wire_update(monkeypatch, fake)
    result = await s.update_project("Работа", "p1", name="   ")
    assert "🛑" in result
    assert fake.update_calls == 0


async def test_update_project_postverify_catches_field_that_did_not_stick(monkeypatch):
    # API claims success but the fresh re-read shows the old name — TickTick
    # silently dropped the change; must be flagged, not reported as ✅.
    fake = FakeOfficialUpdate({"id": "p1", "name": "Старое имя"})
    _wire_update(monkeypatch, fake)
    _, result = await _plan_and_confirm_update("Старое имя", "p1", name="Новое имя")
    assert result.startswith("### ❌")
    assert "Новое имя" in result
    assert "Старое имя" in result


async def test_update_project_postverify_fetch_failure_is_unverified(monkeypatch):
    fake = FakeOfficialUpdate({"id": "p1", "name": "Работа"})
    fake.get_project = lambda project_id: {"error": "down"}
    _wire_update(monkeypatch, fake)
    _, result = await _plan_and_confirm_update("Работа", "p1", name="Новое имя")
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result


# ---------------------------------------------------------------------------
# archive_project
# ---------------------------------------------------------------------------

class FakeV2Archive:
    def __init__(self, projects, archive_error=None):
        self._projects = projects
        self._archive_error = archive_error
        self.force_calls = 0
        self.archive_calls = 0

    def archive_project(self, project_id, closed=True):
        self.archive_calls += 1
        if self._archive_error:
            raise RuntimeError(self._archive_error)
        for p in self._projects:
            if p.get("id") == project_id:
                p["closed"] = closed
        return {}

    def get_state(self, force=False):
        if force:
            self.force_calls += 1
        return {}

    def list_projects(self):
        return list(self._projects)


def _wire_archive(monkeypatch, fake, names):
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)


async def _plan_and_confirm_archive(project_name, project_id, **kwargs):
    preview = await s.archive_project(project_name, project_id, **kwargs)
    mid = _extract_manifest_id(preview)
    return preview, await s.archive_project(project_name, project_id,
                                            manifest_id=mid, user_reply="да", **kwargs)


async def test_archive_project_call1_previews_nothing_archived(monkeypatch):
    fake = FakeV2Archive([{"id": "p1", "name": "Работа", "closed": False}])
    _wire_archive(monkeypatch, fake, {"p1": "Работа"})
    preview = await s.archive_project("Работа", "p1", archived=True)
    assert "manifest_id" in preview
    assert fake.archive_calls == 0


async def test_archive_project_success_is_post_verified(monkeypatch):
    projects = [{"id": "p1", "name": "Работа", "closed": False}]
    fake = FakeV2Archive(projects)
    _wire_archive(monkeypatch, fake, {"p1": "Работа"})
    _, result = await _plan_and_confirm_archive("Работа", "p1", archived=True)
    assert result.startswith("### ✅")
    assert "заархивирован" in result
    assert fake.force_calls == 1
    assert fake.archive_calls == 1


async def test_archive_project_refused_by_guard(monkeypatch):
    fake = FakeV2Archive([{"id": "p1", "name": "Работа", "closed": False}])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: "🛑 Отказ — не та личность.")
    result = await s.archive_project("Совсем другое", "p1", archived=True)
    assert result.startswith("🛑")
    assert fake.archive_calls == 0


async def test_archive_project_postverify_flags_stuck_flag(monkeypatch):
    # archive_project() "succeeds" but the flag never actually flips — the
    # fake here simulates that by leaving closed=False regardless.
    projects = [{"id": "p1", "name": "Работа", "closed": False}]
    fake = FakeV2Archive(projects)
    fake.archive_project = lambda project_id, closed=True: {}  # no-op, doesn't flip
    _wire_archive(monkeypatch, fake, {"p1": "Работа"})
    _, result = await _plan_and_confirm_archive("Работа", "p1", archived=True)
    assert result.startswith("### ❌")
    assert "расхождение" in result


async def test_archive_project_postverify_project_missing_is_unverified(monkeypatch):
    fake = FakeV2Archive([])  # project vanished from the live list
    _wire_archive(monkeypatch, fake, {"p1": "Работа"})
    _, result = await _plan_and_confirm_archive("Работа", "p1", archived=True)
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result


async def test_archive_project_api_rejection_is_refused(monkeypatch):
    fake = FakeV2Archive([{"id": "p1", "name": "Работа", "closed": False}],
                         archive_error="rejected")
    _wire_archive(monkeypatch, fake, {"p1": "Работа"})
    _, result = await _plan_and_confirm_archive("Работа", "p1", archived=True)
    assert result.startswith("### ❌")
    assert "rejected" in result


# ---------------------------------------------------------------------------
# create_project_group
# ---------------------------------------------------------------------------

class FakeV2Groups:
    """Faked v2 client surface for create/delete/move-group + list_projects,
    shared by the three project-group tools' tests."""

    def __init__(self, groups=None, projects=None, create_error=None,
                 delete_error=None, move_error=None, new_group_id="g_new"):
        self._groups = list(groups or [])
        self._projects = list(projects or [])
        self._create_error = create_error
        self._delete_error = delete_error
        self._move_error = move_error
        self._new_group_id = new_group_id
        self.force_calls = 0
        self.create_calls = 0
        self.delete_calls = []
        self.move_calls = []

    def invalidate_cache(self):
        pass

    def get_state(self, force=False):
        if force:
            self.force_calls += 1
        return {}

    def list_project_groups(self):
        return list(self._groups)

    def list_projects(self):
        return list(self._projects)

    def create_project_group(self, name):
        self.create_calls += 1
        if self._create_error:
            raise RuntimeError(self._create_error)
        self._groups.append({"id": self._new_group_id, "name": name})
        return self._new_group_id

    def delete_project_group(self, group_id):
        self.delete_calls.append(group_id)
        if self._delete_error:
            return {"id2error": {group_id: self._delete_error}}
        self._groups = [g for g in self._groups if g.get("id") != group_id]
        return {}

    def move_project_to_group(self, project_id, group_id):
        self.move_calls.append((project_id, group_id))
        if self._move_error:
            raise RuntimeError(self._move_error)
        for p in self._projects:
            if p.get("id") == project_id:
                p["groupId"] = None if group_id == "NONE" else group_id
        return {}


def _wire_groups(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)


async def _plan_and_confirm_create_group(name):
    preview = await s.create_project_group(name)
    mid = _extract_manifest_id(preview)
    return preview, await s.create_project_group(name, manifest_id=mid, user_reply="да")


async def test_create_project_group_call1_previews_nothing_created(monkeypatch):
    fake = FakeV2Groups()
    _wire_groups(monkeypatch, fake)
    preview = await s.create_project_group("Личное")
    assert "manifest_id" in preview
    assert fake.create_calls == 0


async def test_create_project_group_success_is_post_verified(monkeypatch):
    fake = FakeV2Groups()
    _wire_groups(monkeypatch, fake)
    _, result = await _plan_and_confirm_create_group("Личное")
    assert result.startswith("### ✅")
    assert "Личное" in result
    assert "🧾" in result
    assert fake.create_calls == 1


async def test_create_project_group_api_error_is_refused(monkeypatch):
    fake = FakeV2Groups(create_error="boom")
    _wire_groups(monkeypatch, fake)
    _, result = await _plan_and_confirm_create_group("Личное")
    assert result.startswith("### ❌")
    assert "boom" in result


async def test_create_project_group_postverify_not_found_is_unverified(monkeypatch):
    fake = FakeV2Groups()
    # Sabotage: the group "creates" but never actually lands in the list.
    fake.create_project_group = lambda name: "ghost-id"
    _wire_groups(monkeypatch, fake)
    _, result = await _plan_and_confirm_create_group("Личное")
    assert result.startswith("### ⚠️")
    assert "НЕ подтвердилась" in result


async def test_create_project_group_call2_without_reply_is_refused_and_retryable(monkeypatch):
    fake = FakeV2Groups()
    _wire_groups(monkeypatch, fake)
    preview = await s.create_project_group("Личное")
    mid = _extract_manifest_id(preview)
    refused = await s.create_project_group("Личное", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert fake.create_calls == 0
    await s.create_project_group("Личное", manifest_id=mid, user_reply="да")
    assert fake.create_calls == 1


# ---------------------------------------------------------------------------
# delete_project_group
# ---------------------------------------------------------------------------

async def _plan_and_confirm_delete_group(name, gid):
    preview = await s.delete_project_group(name, gid)
    mid = _extract_manifest_id(preview)
    return preview, await s.delete_project_group(name, gid, manifest_id=mid, user_reply="да")


async def test_delete_project_group_unknown_id_is_refused_before_any_plan(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}])
    _wire_groups(monkeypatch, fake)
    result = await s.delete_project_group("Личное", "g_missing")
    assert "🛑" in result
    assert "manifest_id" not in result
    assert fake.delete_calls == []


async def test_delete_project_group_wrong_name_is_refused(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}])
    _wire_groups(monkeypatch, fake)
    result = await s.delete_project_group("Совсем другое", "g1")
    assert "🛑" in result
    assert "не той папки" in result
    assert fake.delete_calls == []


async def test_delete_project_group_call1_previews_nothing_deleted(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}])
    _wire_groups(monkeypatch, fake)
    preview = await s.delete_project_group("Личное", "g1")
    assert "manifest_id" in preview
    assert fake.delete_calls == []
    assert any(g.get("id") == "g1" for g in fake.list_project_groups())


async def test_delete_project_group_success_is_post_verified_with_presnapshot(monkeypatch, tmp_path, caplog):
    fake = FakeV2Groups(
        groups=[{"id": "g1", "name": "Личное"}],
        projects=[{"id": "p1", "name": "Проект А", "groupId": "g1"},
                  {"id": "p2", "name": "Проект Б", "groupId": "g1"}],
    )
    _wire_groups(monkeypatch, fake)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _, result = await _plan_and_confirm_delete_group("Личное", "g1")
    assert result.startswith("### ✅")
    assert fake.delete_calls == ["g1"]
    assert not any(g.get("id") == "g1" for g in fake.list_project_groups())
    # Pre-snapshot (п.12.2): a journal record with the group's own identity
    # AND the member projects it contained must exist BEFORE the delete
    # verdict is returned — this is the audit trail the old code never wrote.
    journal_path = tmp_path / "deletion_journal.jsonl"
    assert journal_path.exists()
    lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    records = [__import__("json").loads(line) for line in lines]
    pre = [r for r in records if r.get("op") == "group_delete"]
    assert pre, "no group_delete pre-snapshot record written"
    item = pre[0]["items"][0]
    assert item["snapshot"]["name"] == "Личное"
    member_names = {p["name"] for p in item["member_projects"]}
    assert member_names == {"Проект А", "Проект Б"}


async def test_delete_project_group_api_rejection_is_refused(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}], delete_error="rejected")
    _wire_groups(monkeypatch, fake)
    _, result = await _plan_and_confirm_delete_group("Личное", "g1")
    assert result.startswith("### ❌")
    assert "rejected" in result


async def test_delete_project_group_postverify_still_present_is_error(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}])
    # Sabotage: "delete" succeeds but the group never actually disappears.
    fake.delete_project_group = lambda group_id: {}
    _wire_groups(monkeypatch, fake)
    _, result = await _plan_and_confirm_delete_group("Личное", "g1")
    assert result.startswith("### ❌")
    assert "ВСЁ ЕЩЁ" in result


async def test_delete_project_group_explicit_no_burns_the_manifest(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}])
    _wire_groups(monkeypatch, fake)
    preview = await s.delete_project_group("Личное", "g1")
    mid = _extract_manifest_id(preview)
    refused = await s.delete_project_group("Личное", "g1", manifest_id=mid, user_reply="нет, стоп")
    assert "🛑" in refused
    dead = await s.delete_project_group("Личное", "g1", manifest_id=mid, user_reply="да")
    assert "🛑" in dead
    assert fake.delete_calls == []


# ---------------------------------------------------------------------------
# move_project_to_group
# ---------------------------------------------------------------------------

def _wire_move(monkeypatch, fake, names):
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)


async def _plan_and_confirm_move(project_name, project_id, group_id):
    preview = await s.move_project_to_group(project_name, project_id, group_id)
    mid = _extract_manifest_id(preview)
    return preview, await s.move_project_to_group(
        project_name, project_id, group_id, manifest_id=mid, user_reply="да")


async def test_move_project_to_group_refused_by_project_guard(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: "🛑 Отказ — не та личность.")
    result = await s.move_project_to_group("Совсем другое", "p1", "g1")
    assert result.startswith("🛑")
    assert fake.move_calls == []


async def test_move_project_to_group_dest_group_missing_is_refused(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}],
                        projects=[{"id": "p1", "name": "Проект А", "groupId": None}])
    _wire_move(monkeypatch, fake, {"p1": "Проект А"})
    result = await s.move_project_to_group("Проект А", "p1", "g_missing")
    assert "🛑" in result
    assert "manifest_id" not in result
    assert fake.move_calls == []


async def test_move_project_to_group_call1_previews_nothing_moved(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}],
                        projects=[{"id": "p1", "name": "Проект А", "groupId": None}])
    _wire_move(monkeypatch, fake, {"p1": "Проект А"})
    preview = await s.move_project_to_group("Проект А", "p1", "g1")
    assert "manifest_id" in preview
    assert fake.move_calls == []


async def test_move_project_to_group_success_is_post_verified(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}],
                        projects=[{"id": "p1", "name": "Проект А", "groupId": None}])
    _wire_move(monkeypatch, fake, {"p1": "Проект А"})
    _, result = await _plan_and_confirm_move("Проект А", "p1", "g1")
    assert result.startswith("### ✅")
    assert "Личное" in result
    assert fake.move_calls == [("p1", "g1")]


async def test_move_project_to_group_ungroup_success(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}],
                        projects=[{"id": "p1", "name": "Проект А", "groupId": "g1"}])
    _wire_move(monkeypatch, fake, {"p1": "Проект А"})
    _, result = await _plan_and_confirm_move("Проект А", "p1", "NONE")
    assert result.startswith("### ✅")
    assert "ungrouped" in result
    assert fake.move_calls == [("p1", "NONE")]


async def test_move_project_to_group_postverify_mismatch_is_error(monkeypatch):
    fake = FakeV2Groups(groups=[{"id": "g1", "name": "Личное"}],
                        projects=[{"id": "p1", "name": "Проект А", "groupId": None}])
    # Sabotage: the move "succeeds" but groupId never actually changes.
    fake.move_project_to_group = lambda project_id, group_id: {}
    _wire_move(monkeypatch, fake, {"p1": "Проект А"})
    _, result = await _plan_and_confirm_move("Проект А", "p1", "g1")
    assert result.startswith("### ❌")
    assert "НЕ переместился" in result


# ---------------------------------------------------------------------------
# create_project_column
# ---------------------------------------------------------------------------

class FakeV2Column:
    def __init__(self, create_error=None):
        self._create_error = create_error
        self.created_id = "col1"
        self.create_calls = 0

    def invalidate_cache(self):
        pass

    def create_column(self, project_id, name):
        self.create_calls += 1
        if self._create_error:
            raise RuntimeError(self._create_error)
        return self.created_id


class FakeOfficialColumns:
    def __init__(self, columns, data_error=None):
        self._columns = columns
        self._data_error = data_error

    def get_project_with_data(self, project_id):
        if self._data_error:
            return {"error": self._data_error}
        return {"columns": self._columns}


def _wire_column(monkeypatch, fake_v2, fake_official, names):
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    monkeypatch.setattr(s, "ticktick", fake_official)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)


async def _plan_and_confirm_column(project_id, name, project_name):
    preview = await s.create_project_column(project_id, name, project_name=project_name)
    mid = _extract_manifest_id(preview)
    return preview, await s.create_project_column(
        project_id, name, project_name=project_name, manifest_id=mid, user_reply="да")


async def test_create_project_column_missing_project_name_is_refused(monkeypatch):
    # п.12.3: project_name empty → identity-guard must not be silently
    # skipped — the call is refused outright, no plan, no mutation.
    fake_v2 = FakeV2Column()
    fake_official = FakeOfficialColumns([])
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    result = await s.create_project_column("p1", "В работе")
    assert "🛑" in result
    assert "manifest_id" not in result
    assert fake_v2.create_calls == 0


async def test_create_project_column_call1_previews_nothing_created(monkeypatch):
    fake_v2 = FakeV2Column()
    fake_official = FakeOfficialColumns([])
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    preview = await s.create_project_column("p1", "В работе", project_name="Работа")
    assert "manifest_id" in preview
    assert fake_v2.create_calls == 0


async def test_create_project_column_refused_by_guard(monkeypatch):
    fake_v2 = FakeV2Column()
    fake_official = FakeOfficialColumns([])
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    monkeypatch.setattr(s, "ticktick", fake_official)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: "🛑 Отказ — не та личность.")
    result = await s.create_project_column("p1", "В работе", project_name="Не то")
    assert result.startswith("🛑")
    assert fake_v2.create_calls == 0


async def test_create_project_column_success_is_post_verified(monkeypatch):
    fake_v2 = FakeV2Column()
    fake_official = FakeOfficialColumns([{"id": "col1", "name": "В работе"}])
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    _, result = await _plan_and_confirm_column("p1", "В работе", "Работа")
    assert result.startswith("### ✅")
    assert "В работе" in result
    assert "🧾" in result
    assert fake_v2.create_calls == 1


async def test_create_project_column_postverify_not_found_is_unverified(monkeypatch):
    fake_v2 = FakeV2Column()
    fake_official = FakeOfficialColumns([])  # new column doesn't show up
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    _, result = await _plan_and_confirm_column("p1", "В работе", "Работа")
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result


async def test_create_project_column_api_rejection_is_refused(monkeypatch):
    fake_v2 = FakeV2Column(create_error="rejected")
    fake_official = FakeOfficialColumns([])
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    _, result = await _plan_and_confirm_column("p1", "В работе", "Работа")
    assert result.startswith("### ❌")
    assert "rejected" in result


async def test_create_project_column_postverify_fetch_failure_is_unverified(monkeypatch):
    fake_v2 = FakeV2Column()
    fake_official = FakeOfficialColumns([], data_error="down")
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    _, result = await _plan_and_confirm_column("p1", "В работе", "Работа")
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result
