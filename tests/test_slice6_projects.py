"""create_project / update_project / archive_project / create_project_column:
the mutating-tool audit (docs/PIPELINE_ticktick_mcp.md §6) found these four
were the only project-group write methods with NO post-verify — they just
returned whatever the write call answered, with no independent fresh re-read
proving the mutation actually landed. This mirrors create_project_group's own
post-verify convention (server.py:5782, re-reads the live group list) applied
to each of the four. Output is also unified to the markdown template from
docs/DESIGN_output_format.md (### <emoji> header, body, 🧾 proof line).

No real network — the official (v1) and v2 clients are faked.

2026-08-05: create_project and create_project_column are now gated 🟡 (two
calls, same tool name — the removed tier-🟢 exemption, see
docs/DESIGN_approval_gate.md). Their tests now run the full plan->execute
cycle before asserting on the outcome that used to come straight back from a
single call."""
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


async def _gated_call(fn, *args, **kwargs):
    """Runs a gated tool's full plan->execute cycle and returns the
    execute-phase result."""
    preview = await fn(*args, **kwargs)
    assert "🛑" not in preview, f"plan phase unexpectedly refused: {preview!r}"
    mid = _extract_manifest_id(preview)
    return await fn(*args, manifest_id=mid, user_reply="да", **kwargs)


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------

class FakeOfficialCreate:
    def __init__(self, created, fresh=None, get_error=None):
        self._created = created
        self._fresh = fresh if fresh is not None else dict(created)
        self._get_error = get_error
        self.get_calls = 0

    def create_project(self, name, color="#F18181", view_mode="list"):
        return dict(self._created)

    def get_project(self, project_id):
        self.get_calls += 1
        if self._get_error:
            return {"error": self._get_error}
        return dict(self._fresh)


def _wire_official(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick", fake)


async def test_create_project_success_is_post_verified(monkeypatch):
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа", "color": "#F18181"})
    _wire_official(monkeypatch, fake)
    result = await _gated_call(s.create_project, "Работа")
    assert result.startswith("### ✅")
    assert "Работа" in result
    assert "🧾" in result
    assert fake.get_calls == 1


async def test_create_project_api_error_is_refused(monkeypatch):
    fake = FakeOfficialCreate({})
    fake.create_project = lambda name, color="#F18181", view_mode="list": {"error": "boom"}
    _wire_official(monkeypatch, fake)
    result = await _gated_call(s.create_project, "Работа")
    assert result.startswith("### ❌")
    assert "boom" in result
    assert fake.get_calls == 0


async def test_create_project_postverify_mismatch_is_unverified(monkeypatch):
    # TickTick's create response "succeeds" but the fresh re-read can't find
    # the project (id mismatch / not actually there) — must NOT claim ✅.
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"},
                              fresh={"id": "OTHER", "name": "??"})
    _wire_official(monkeypatch, fake)
    result = await _gated_call(s.create_project, "Работа")
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result


async def test_create_project_postverify_fetch_failure_is_unverified(monkeypatch):
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"}, get_error="rate limited")
    _wire_official(monkeypatch, fake)
    result = await _gated_call(s.create_project, "Работа")
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result


async def test_create_project_automation_key_bypasses_user_reply(monkeypatch):
    """Headless callers (tg-ai-assistant's Mini App / background pipeline)
    pass their own connection secret as automation_key on call #2 instead of
    a human user_reply — the tier-🟡 _gate_single wrapper must accept that
    the same way _gate_batch/create_tasks already do
    (references/automation-secrets.md §8, regression from commit 532e485
    removing the tier-🟢 exemption without adding automation_key here)."""
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"})
    _wire_official(monkeypatch, fake)
    preview = await s.create_project("Работа")
    mid = _extract_manifest_id(preview)
    result = await s.create_project("Работа", manifest_id=mid,
                                    automation_key=s.SECRET)
    assert result.startswith("### ✅")
    assert fake.get_calls == 1


async def test_create_project_wrong_automation_key_still_refused(monkeypatch):
    fake = FakeOfficialCreate({"id": "p1", "name": "Работа"})
    _wire_official(monkeypatch, fake)
    preview = await s.create_project("Работа")
    mid = _extract_manifest_id(preview)
    result = await s.create_project("Работа", manifest_id=mid,
                                    automation_key="not-the-real-secret")
    assert "🛑" in result
    assert fake.get_calls == 0


# ---------------------------------------------------------------------------
# update_project
# ---------------------------------------------------------------------------

class FakeOfficialUpdate:
    def __init__(self, fresh, update_resp=None, update_error=None):
        self._fresh = fresh
        self._update_resp = update_resp if update_resp is not None else dict(fresh)
        self._update_error = update_error
        self.get_calls = 0

    def update_project(self, project_id, name=None, color=None, view_mode=None):
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


async def test_update_project_success_is_post_verified(monkeypatch):
    fake = FakeOfficialUpdate({"id": "p1", "name": "Новое имя", "color": "#111111"})
    _wire_update(monkeypatch, fake)
    result = await s.update_project("Работа", "p1", name="Новое имя", color="#111111")
    assert result.startswith("### ✅")
    assert "Новое имя" in result
    assert "🧾" in result
    assert fake.get_calls == 1


async def test_update_project_refused_by_guard(monkeypatch):
    fake = FakeOfficialUpdate({"id": "p1", "name": "Работа"})
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: "🛑 Отказ — не та личность.")
    result = await s.update_project("Совсем другое", "p1", name="X")
    assert result.startswith("🛑")
    assert fake.get_calls == 0


async def test_update_project_postverify_catches_field_that_did_not_stick(monkeypatch):
    # API claims success but the fresh re-read shows the old name — TickTick
    # silently dropped the change; must be flagged, not reported as ✅.
    fake = FakeOfficialUpdate({"id": "p1", "name": "Старое имя"})
    _wire_update(monkeypatch, fake)
    result = await s.update_project("Старое имя", "p1", name="Новое имя")
    assert result.startswith("### ❌")
    assert "Новое имя" in result
    assert "Старое имя" in result


async def test_update_project_postverify_fetch_failure_is_unverified(monkeypatch):
    fake = FakeOfficialUpdate({"id": "p1", "name": "Работа"})
    fake.get_project = lambda project_id: {"error": "down"}
    _wire_update(monkeypatch, fake)
    result = await s.update_project("Работа", "p1", name="Новое имя")
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

    def archive_project(self, project_id, closed=True):
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


async def test_archive_project_success_is_post_verified(monkeypatch):
    projects = [{"id": "p1", "name": "Работа", "closed": False}]
    fake = FakeV2Archive(projects)
    _wire_archive(monkeypatch, fake, {"p1": "Работа"})
    result = await s.archive_project("Работа", "p1", archived=True)
    assert result.startswith("### ✅")
    assert "заархивирован" in result
    assert fake.force_calls == 1


async def test_archive_project_postverify_flags_stuck_flag(monkeypatch):
    # archive_project() "succeeds" but the flag never actually flips — the
    # fake here simulates that by leaving closed=False regardless.
    projects = [{"id": "p1", "name": "Работа", "closed": False}]
    fake = FakeV2Archive(projects)
    fake.archive_project = lambda project_id, closed=True: {}  # no-op, doesn't flip
    _wire_archive(monkeypatch, fake, {"p1": "Работа"})
    result = await s.archive_project("Работа", "p1", archived=True)
    assert result.startswith("### ❌")
    assert "расхождение" in result


async def test_archive_project_postverify_project_missing_is_unverified(monkeypatch):
    fake = FakeV2Archive([])  # project vanished from the live list
    _wire_archive(monkeypatch, fake, {"p1": "Работа"})
    result = await s.archive_project("Работа", "p1", archived=True)
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result


async def test_archive_project_api_rejection_is_refused(monkeypatch):
    fake = FakeV2Archive([{"id": "p1", "name": "Работа", "closed": False}],
                         archive_error="rejected")
    _wire_archive(monkeypatch, fake, {"p1": "Работа"})
    result = await s.archive_project("Работа", "p1", archived=True)
    assert result.startswith("### ❌")
    assert "rejected" in result


# ---------------------------------------------------------------------------
# create_project_column
# ---------------------------------------------------------------------------

class FakeV2Column:
    def __init__(self, create_error=None):
        self._create_error = create_error
        self.created_id = "col1"

    def create_column(self, project_id, name):
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


async def test_create_project_column_success_is_post_verified(monkeypatch):
    fake_v2 = FakeV2Column()
    fake_official = FakeOfficialColumns([{"id": "col1", "name": "В работе"}])
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    result = await _gated_call(s.create_project_column, "p1", "В работе", project_name="Работа")
    assert result.startswith("### ✅")
    assert "В работе" in result
    assert "🧾" in result


async def test_create_project_column_postverify_not_found_is_unverified(monkeypatch):
    fake_v2 = FakeV2Column()
    fake_official = FakeOfficialColumns([])  # new column doesn't show up
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    result = await _gated_call(s.create_project_column, "p1", "В работе", project_name="Работа")
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result


async def test_create_project_column_api_rejection_is_refused(monkeypatch):
    fake_v2 = FakeV2Column(create_error="rejected")
    fake_official = FakeOfficialColumns([])
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    result = await _gated_call(s.create_project_column, "p1", "В работе", project_name="Работа")
    assert result.startswith("### ❌")
    assert "rejected" in result


async def test_create_project_column_postverify_fetch_failure_is_unverified(monkeypatch):
    fake_v2 = FakeV2Column()
    fake_official = FakeOfficialColumns([], data_error="down")
    _wire_column(monkeypatch, fake_v2, fake_official, {"p1": "Работа"})
    result = await _gated_call(s.create_project_column, "p1", "В работе", project_name="Работа")
    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result
