"""PLAN_retrofit.md ПАКЕТ 11 — gate + format for create_subtask / abandon_task
/ duplicate_task:

  11.1 — all three routed through `_gate_single` (used to mutate on the
         first call, no gate at all).
  11.2 — abandon_task / duplicate_task: container (project) check added to
         the identity-guard (was only id+title).
  11.3 — WRITE annotation on all three.
  11.4 — output wrapped in `_tool_response` (### header)."""
import ticktick_mcp.src.server as s


def _wire_common(monkeypatch, live, tmp_path, names=None):
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: names or {"p1": "Покупки", "p2": "Работа"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))


def _plan_manifest_id(plan_text: str) -> str:
    m = plan_text.split("Манифест `")[1]
    return m.split("`")[0]


# ---------------------------------------------------------------------------
# create_subtask
# ---------------------------------------------------------------------------

class _FakeOfficialClient:
    def __init__(self):
        self.calls = []

    def create_subtask(self, subtask_title, parent_task_id, project_id,
                       content=None, priority=0):
        self.calls.append((subtask_title, parent_task_id, project_id))
        return {"id": "new-sub", "title": subtask_title,
               "projectId": project_id, "parentId": parent_task_id}


def _wire_subtask(monkeypatch, live, tmp_path, names=None):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    _wire_common(monkeypatch, live, tmp_path, names)
    fake = _FakeOfficialClient()
    monkeypatch.setattr(s, "ticktick", fake)
    return fake


async def test_create_subtask_plan_call_creates_nothing(monkeypatch, tmp_path):
    live = {"p1t": {"id": "p1t", "title": "Родитель", "projectId": "proj1"}}
    fake = _wire_subtask(monkeypatch, live, tmp_path)

    out = await s.create_subtask(
        parent_task_title="Родитель", subtask_title="Дочка",
        parent_task_id="p1t", project_id="proj1")

    assert fake.calls == []
    assert "### 📋" in out
    assert "Манифест" in out


async def test_create_subtask_execute_creates_and_confirms(monkeypatch, tmp_path):
    live = {"p1t": {"id": "p1t", "title": "Родитель", "projectId": "proj1"}}
    fake = _wire_subtask(monkeypatch, live, tmp_path)

    plan = await s.create_subtask(
        parent_task_title="Родитель", subtask_title="Дочка",
        parent_task_id="p1t", project_id="proj1")
    mid = _plan_manifest_id(plan)

    # After execute, the fresh open-task pool includes the new subtask,
    # correctly parented — this is what post-verify re-reads.
    def _fresh_after(fresh=False):
        d = dict(live)
        d["new-sub"] = {"id": "new-sub", "title": "Дочка",
                        "projectId": "proj1", "parentId": "p1t"}
        return d
    monkeypatch.setattr(s, "_open_by_id", _fresh_after)

    out = await s.create_subtask(
        parent_task_title="Родитель", subtask_title="Дочка",
        parent_task_id="p1t", project_id="proj1",
        manifest_id=mid, user_reply="да")

    assert fake.calls == [("Дочка", "p1t", "proj1")]
    assert out.startswith("### ✅")
    assert "Создана" in out


async def test_create_subtask_execute_without_manifest_is_refused(monkeypatch, tmp_path):
    live = {"p1t": {"id": "p1t", "title": "Родитель", "projectId": "proj1"}}
    fake = _wire_subtask(monkeypatch, live, tmp_path)

    out = await s.create_subtask(
        parent_task_title="Родитель", subtask_title="Дочка",
        parent_task_id="p1t", project_id="proj1",
        manifest_id="nope", user_reply="да")

    assert fake.calls == []
    assert out.startswith("🛑")


async def test_create_subtask_identity_guard_mismatch_refuses(monkeypatch, tmp_path):
    live = {"p1t": {"id": "p1t", "title": "СОВСЕМ другая задача", "projectId": "proj1"}}
    fake = _wire_subtask(monkeypatch, live, tmp_path)

    out = await s.create_subtask(
        parent_task_title="Родитель", subtask_title="Дочка",
        parent_task_id="p1t", project_id="proj1")

    assert fake.calls == []
    assert out.startswith("🛑")


async def _annotations_of(tool_name):
    tools = await s.mcp.list_tools()
    return next(t.annotations for t in tools if t.name == tool_name)


async def test_create_subtask_is_write_annotated():
    ann = await _annotations_of("create_subtask")
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is False


# ---------------------------------------------------------------------------
# abandon_task
# ---------------------------------------------------------------------------

class _FakeV2Abandon:
    def __init__(self, live):
        self._live = live
        self.abandoned = []

    def abandon_task(self, task_id):
        self.abandoned.append(task_id)
        self._live.pop(task_id, None)
        return {}


def _wire_abandon(monkeypatch, live, tmp_path, names=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    _wire_common(monkeypatch, live, tmp_path, names)
    fake = _FakeV2Abandon(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


async def test_abandon_task_plan_call_marks_nothing(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire_abandon(monkeypatch, live, tmp_path)

    out = await s.abandon_task(summary="Отмечаю", task_id="t1",
                               task_title="Купить молоко")

    assert fake.abandoned == []
    assert "### 📋" in out


async def test_abandon_task_execute_marks_and_confirms(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire_abandon(monkeypatch, live, tmp_path)

    plan = await s.abandon_task(summary="Отмечаю", task_id="t1",
                                task_title="Купить молоко")
    mid = _plan_manifest_id(plan)

    out = await s.abandon_task(summary="Отмечаю", task_id="t1",
                               task_title="Купить молоко",
                               manifest_id=mid, user_reply="да")

    assert fake.abandoned == ["t1"]
    assert out.startswith("### ✅")


async def test_abandon_task_container_check_catches_wrong_project(monkeypatch, tmp_path):
    """11.2 — task id+title match, but it actually lives in a DIFFERENT
    project than the one supplied — must refuse, nothing touched."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p2"}}
    fake = _wire_abandon(monkeypatch, live, tmp_path)

    out = await s.abandon_task(summary="Отмечаю", task_id="t1",
                               task_title="Купить молоко",
                               project_name="Покупки")

    assert fake.abandoned == []
    assert out.startswith("🛑")
    assert "Работа" in out or "проект" in out


async def test_abandon_task_without_project_name_skips_container_check(monkeypatch, tmp_path):
    """No project_name supplied — container check is not armed (back-compat),
    same as before this package: only id+title verified."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p2"}}
    _wire_abandon(monkeypatch, live, tmp_path)

    out = await s.abandon_task(summary="Отмечаю", task_id="t1",
                               task_title="Купить молоко")

    assert "### 📋" in out


async def test_abandon_task_is_write_annotated():
    ann = await _annotations_of("abandon_task")
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is False


# ---------------------------------------------------------------------------
# duplicate_task
# ---------------------------------------------------------------------------

class _FakeV2Duplicate:
    def __init__(self, live):
        self._live = live
        self.duplicated = []

    def duplicate_task(self, task_id):
        self.duplicated.append(task_id)
        src = self._live[task_id]
        copy = dict(src, id="copy1", title=src["title"])
        self._live["copy1"] = copy
        return copy


def _wire_duplicate(monkeypatch, live, tmp_path, names=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    _wire_common(monkeypatch, live, tmp_path, names)
    fake = _FakeV2Duplicate(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


async def test_duplicate_task_plan_call_duplicates_nothing(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire_duplicate(monkeypatch, live, tmp_path)

    out = await s.duplicate_task(summary="Дублирую", task_id="t1",
                                 task_title="Купить молоко")

    assert fake.duplicated == []
    assert "### 📋" in out


async def test_duplicate_task_execute_duplicates_and_confirms(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire_duplicate(monkeypatch, live, tmp_path)

    plan = await s.duplicate_task(summary="Дублирую", task_id="t1",
                                  task_title="Купить молоко")
    mid = _plan_manifest_id(plan)

    out = await s.duplicate_task(summary="Дублирую", task_id="t1",
                                 task_title="Купить молоко",
                                 manifest_id=mid, user_reply="да")

    assert fake.duplicated == ["t1"]
    assert out.startswith("### ✅")
    assert "чек-лист" in out


async def test_duplicate_task_container_check_catches_wrong_project(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p2"}}
    fake = _wire_duplicate(monkeypatch, live, tmp_path)

    out = await s.duplicate_task(summary="Дублирую", task_id="t1",
                                 task_title="Купить молоко",
                                 project_name="Покупки")

    assert fake.duplicated == []
    assert out.startswith("🛑")


async def test_duplicate_task_is_write_annotated():
    ann = await _annotations_of("duplicate_task")
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is False
