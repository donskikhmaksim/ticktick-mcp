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

import pytest

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


def _ok_guard(*_a, **_k):
    return s._Guard("ok", project_id="p1", title="Купить молоко")


def _guard_sequence(*results):
    """`_guard_task` stand-in that returns `results` in order, one per call —
    used below (def-126) to simulate the plan-phase parent read (call #1)
    disagreeing with what the execution-phase reads (call #2, inside
    _unset_task_parent_impl, unchanged) find a moment later: e.g. the plan
    read fails/times out (yields "unavailable") while the execution read
    either confirms the claimed name or catches a real mismatch."""
    it = iter(results)

    def _stub(*_a, **_k):
        return next(it)
    return _stub


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
        """Записывает РОВНО то, что передал вызывающий.

        Раньше здесь стояло `None if group_id == "NONE" else group_id`, то
        есть двойник САМ делал нормализацию, которую обязан делать клиент. На
        этом и держался баг #100: клиент отправлял в TickTick буквальную
        строку "NONE", группы с таким id не существует, разгруппировка не
        срабатывала НИКОГДА — а тест был зелёный, потому что контракт из
        докстринга соблюдал только фейк.

        Семантику самой разгруппировки проверять здесь нечем (двойник стоит
        НА МЕСТЕ клиента, то есть ниже того слоя, где она живёт) — она
        проверяется сквозным тестом через настоящий клиент в
        tests/test_silent_failures.py."""
        self.calls.append(("move_group", project_id, group_id))
        for p in self.projects:
            if p.get("id") == project_id:
                p["groupId"] = group_id

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
    """Двойник официального клиента.

    `live` — то же изменяемое состояние, что у FakeV2: созданная подзадача
    обязана стать ВИДНОЙ среди открытых задач, иначе настоящий post-verify
    вернёт «❌ Создание НЕ подтвердилось». Раньше двойник её никуда не
    записывал, и «успешный» тест этого тула был зелёным на явном отказе —
    критерий `"🛑" not in result` к post-verify нечувствителен.

    `project_id` пишется в журнал вызовов намеренно: именно он решает, в
    каком списке окажется подзадача, и без него подмена проекта была
    ненаблюдаемой.
    """

    def __init__(self, live=None):
        self.calls = []
        self.live = live if live is not None else {}

    def create_subtask(self, subtask_title, parent_task_id, project_id,
                       content=None, priority=0):
        self.calls.append(("create_subtask", subtask_title, parent_task_id,
                           project_id))
        task = {"id": "sub1", "title": subtask_title, "projectId": project_id,
                "parentId": parent_task_id}
        self.live[task["id"]] = task
        return dict(task)


def _assert_confirmed_success(result: str):
    """Критерий успеха для тулов этого файла.

    Раньше здесь стояло `assert "🛑" not in result`, и это НЕ проверка успеха:
    отказ гейта печатается через 🛑, а провал ПОСЛЕДУЮЩЕЙ проверки факта —
    через ❌ («создал, но среди открытых задач не вижу»). Тест оставался
    зелёным ровно на том исходе, ради которого post-verify и написан, — и
    один из тулов файла (create_subtask) в этом состоянии и находился: его
    «успешный» прогон возвращал «❌ Создание НЕ подтвердилось».

    Требуется явный след ПЕРЕЧИТАННОГО факта («проверено»/✅), а не молчание.
    ⚠️ не запрещается: у некоторых тулов это информационная приписка про
    границы операции (что именно не переносится в копию), а не отказ."""
    assert "🛑" not in result, result
    assert "❌" not in result, f"post-verify не подтвердил операцию:\n{result}"
    assert "провер" in result.lower() or "✅" in result, (
        f"в ответе нет следа перечитанного факта — операция объявлена "
        f"успешной без подтверждения:\n{result}")


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
    # ОДНО состояние на оба клиента: подзадачу создаёт официальный клиент, а
    # перечитывает её post-verify через v2. Пока состояния были разными,
    # созданная подзадача не появлялась среди открытых, и «успешный» прогон
    # возвращал «❌ Создание НЕ подтвердилось» — при зелёном тесте.
    live = {"p1": {"id": "p1", "title": "Купить молоко", "projectId": "p1"}}
    official = FakeOfficial(live=live)
    fake_v2 = FakeV2(live=live)
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
    # Проект назначения — часть ожидания: именно он решает, в каком списке
    # окажется подзадача, и без него подмена проекта была ненаблюдаемой.
    assert official.calls == [("create_subtask", "Купить хлеб", "p1", "p1")]
    _assert_confirmed_success(result)

    again = await s.create_subtask("Купить молоко", "Купить хлеб", "p1", "proj1",
                                    manifest_id=mid, user_reply="да")
    assert "🛑" in again
    assert len(official.calls) == 1


async def test_create_subtask_reports_a_silent_refusal_instead_of_success(
        monkeypatch):
    """Молчаливый отказ TickTick: HTTP 200 с id, но задачи в списке открытых
    нет. Тул обязан сказать «НЕ подтвердилось», а не рапортовать успех.

    Тест новый: раньше двойник вообще не умел «создать» задачу так, чтобы её
    было видно, поэтому ЛЮБОЙ прогон возвращал этот самый отказ — и различить
    молчаливый провал от нормального успеха было нечем."""
    live = {"p1": {"id": "p1", "title": "Купить молоко", "projectId": "p1"}}
    official = FakeOfficial(live=live)
    # ответ есть, эффекта нет — ровно то, что делает молчаливый отказ
    official.create_subtask = lambda subtask_title, parent_task_id, project_id, \
        content=None, priority=0: {"id": "sub1", "title": subtask_title}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, fake_official=official)

    preview = await s.create_subtask("Купить молоко", "Купить хлеб", "p1", "proj1")
    mid = _extract_manifest_id(preview)

    result = await s.create_subtask("Купить молоко", "Купить хлеб", "p1", "proj1",
                                    manifest_id=mid, user_reply="да")

    assert "❌" in result, f"молчаливый отказ выдан за успех:\n{result}"
    assert "НЕ подтвердилось" in result


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
    _assert_confirmed_success(result)


# ---------------------------------------------------------------------------
# 2026-08-07 (def-126): parent_task_id↔parent_task_title (the claimed PARENT,
# NOT the subtask being detached) is now cross-checked BEFORE the plan is
# built. Before this fix, NOTHING verified it at all — only the RELATIONSHIP
# (live parentId of the subtask == parent_task_id) was checked, never the
# parent's NAME against that id. The two tests above this point monkeypatch
# `_guard_task` to an unconditional "ok" (same result on every call, for
# every argument) so they could not have caught either the old bug or a
# regression here — they remain unmodified.
#
# Strictness note (see server.py docstring/comments for the full rationale):
# mismatch (a LIVE parent with a DIFFERENT name than claimed) blocks the plan
# (🛑) — same severity as set_task_parent's parent guard. But missing/
# unavailable (parent not resolvable live) does NOT block here, unlike
# set_task_parent: unset_task_parent SEVERS a link rather than creating one,
# and a parent that is completed/deleted (hence unresolvable live) is the
# ordinary, expected reason to want to detach from it — the subtask's own
# live parentId is still independently checked against parent_task_id in
# _unset_task_parent_impl either way, so the id-level relationship is never
# taken on faith. The execution-side (second-line) guard for the SAME check
# is exercised separately, in its own commit/tests below this one.
# ---------------------------------------------------------------------------

async def test_unset_task_parent_plan_identity_guard_blocks_wrong_parent_title(
        monkeypatch):
    """parent_task_id points at a REAL task ("Большой проект"), caller's
    parent_task_title claims a DIFFERENT one ("Другой родитель") — before
    this fix, call #1 would have built and shown a plan card reading
    'Отцепляю «Шаг 1» от родителя «Другой родитель»' even though the id has
    nothing to do with that task. Now the plan is refused outright, before
    any card is built, and unset_task_parent (the v2 mutation) is never
    called."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard(
            "mismatch", project_id="p1", title="Большой проект",
            message='id указывает на «Большой проект», а НЕ «Другой родитель»'))

    result = await s.unset_task_parent("Шаг 1", "Другой родитель", "c", "p", "p1")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Большой проект»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert live["c"]["parentId"] == "p"
    assert fake_v2.calls == []


async def test_unset_task_parent_plan_missing_parent_does_not_block_but_warns(
        monkeypatch):
    """Deliberately DIFFERENT severity from set_task_parent's own missing-
    parent case (which refuses the plan outright): a parent that doesn't
    resolve live (completed/deleted) is the NORMAL reason to detach from it,
    so the plan is still built — it just honestly warns that the parent's
    name could not be verified — and the detach (id-based, verified via the
    relationship check further down) still goes through on confirmation."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("missing", project_id="p1"),               # call #1 (plan, parent)
        s._Guard("ok", project_id="p1", title="Шаг 1"),      # call #2 (impl, subtask)
        s._Guard("ok", project_id="p1", title="Большой проект"),  # call #2 (impl, parent)
    ))

    preview = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1")
    assert "🛑" not in preview, "родитель, не найденный среди открытых, не должен блокировать план"
    assert "возможно завершён/удалён" in preview
    mid = _extract_manifest_id(preview)

    result = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert ("unset_parent", "c") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_unset_task_parent_plan_read_failure_does_not_block_but_warns(
        monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every detach — fail-open here is cheaper than refusing everyone whose
    network is briefly flaky. The plan is still built, honestly warns that
    the parent's name was not verified, and the real (unchanged) identity-
    guard on execution (call #2) does its normal job right after."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                  # call #1 (plan, parent)
        s._Guard("ok", project_id="p1", title="Шаг 1"),            # call #2 (impl, subtask)
        s._Guard("ok", project_id="p1", title="Большой проект"),   # call #2 (impl, parent)
    ))

    preview = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert ("unset_parent", "c") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_unset_task_parent_automation_key_parent_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the parent
    identity check only lived inside _gate_single/execution, a false parent
    name+id pair would sail through silently on a single valid key. The
    check sits BEFORE _gate_single, so it applies here too."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    calls = []

    def _stub(*a, **k):
        calls.append(1)
        return s._Guard("mismatch", project_id="p1", title="Большой проект",
                        message='id указывает на «Большой проект», а НЕ «Другой родитель»')
    monkeypatch.setattr(s, "_guard_task", _stub)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.unset_task_parent("Шаг 1", "Другой родитель", "c", "p", "p1",
                                       automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Большой проект»" in result
    assert live["c"]["parentId"] == "p"
    assert fake_v2.calls == []
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 2026-08-07 (def-126), continued: the SAME parent_task_id↔parent_task_title
# check, now as the SECOND, independent line of defense right before the
# actual detach in _unset_task_parent_impl — up to an hour can pass between
# the plan (call #1) and the confirmation (call #2), so state can change in
# between. Exercised specifically via a plan-phase read failure (the ONLY
# way to reach execution with the parent's name still unverified — a plan-
# phase mismatch above refuses outright and never reaches this code at all).
# ---------------------------------------------------------------------------

async def test_unset_task_parent_plan_read_failure_still_lets_execution_catch_a_real_parent_mismatch(
        monkeypatch):
    """Same read failure on the plan as
    test_unset_task_parent_plan_read_failure_does_not_block_but_warns above,
    but this time the parent's real name actually DOESN'T match the claim.
    The plan-phase check couldn't run (so it warns instead of refusing), but
    the execution-phase guard inside `_unset_task_parent_impl` — the second,
    independent line of defense added by this commit — still catches the
    real mismatch: a network blip on planning must not weaken the protection
    at execution time."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                       # call #1 (plan, parent)
        s._Guard("ok", project_id="p1", title="Шаг 1"),                # call #2 (impl, subtask)
        s._Guard("mismatch", project_id="p1", title="Другой родитель",  # call #2 (impl, parent)
                message='id указывает на «Другой родитель», а НЕ «Большой проект»'),
    ))

    preview = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.unset_task_parent("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Другой родитель»" in result
    assert live["c"]["parentId"] == "p"
    assert fake_v2.calls == []


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
    _assert_confirmed_success(result)
    # Регресс-тест дефекта №3 (2026-08-06, живой прогон, манифест
    # ea79556baf0f): группа создалась реально, но кнопочный вердикт был
    # ложным «❓ НЕ подтверждено», потому что self-report не начинался с ✅
    # (single-gate тул, журнала мутаций для групп нет — верится только
    # ведущему ✅ собственного отчёта, см. _auto_execute_report_is_success).
    assert s._auto_execute_report_is_success(result), result

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
    _assert_confirmed_success(result)
    # Тот же класс бага, что и у create_project_group (дефект №3) — не
    # журналируется, self-report был без ведущего ✅.
    assert s._auto_execute_report_is_success(result), result


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
    _assert_confirmed_success(result)
    # Тот же класс бага, что и у create_project_group (дефект №3).
    assert s._auto_execute_report_is_success(result), result


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
    # ВНИМАНИЕ: критерий здесь слабее, чем у соседних тулов, и это не небрежность
    # теста, а факт про сам тул: `_add_task_comment_impl` не перечитывает
    # комментарии после записи, поэтому подтверждать нечем. Строгий критерий
    # означал бы, что тест утверждает больше, чем сервер проверил.
    assert "🛑" not in result and "❌" not in result, result


@pytest.mark.skip(reason="НЕ РЕАЛИЗОВАНО: add_task_comment не перечитывает "
                         "комментарии после записи, поэтому «Comment added» — "
                         "это эхо запроса, а не подтверждённый факт. Молчаливый "
                         "отказ TickTick (HTTP 200 + пустой результат) тул "
                         "объявит успехом. Добавление post-verify меняет "
                         "поведение инструмента — решение владельца, а не "
                         "правка теста.")
async def test_add_task_comment_confirms_the_comment_is_really_there(monkeypatch):
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко",
                                 "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2)
    # двойник, который «принял» запись, но ничего не сохранил — ровно то, чем
    # TickTick отвечает при молчаливом отказе
    fake_v2.add_task_comment = lambda project_id, task_id, text: None

    preview = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1")
    mid = _extract_manifest_id(preview)
    result = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1",
                                      manifest_id=mid, user_reply="да")

    assert "❌" in result or "⚠️" in result


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
    _assert_confirmed_success(result)
    # Тот же класс бага, что и у create_project_group (дефект №3): вложение
    # реально прикреплено и post-verify его видит, но старый текст не начинался
    # с ✅ ("Attached '...' to '...'"), поэтому кнопочный вердикт был ложным.
    assert s._auto_execute_report_is_success(result), result


async def test_attach_file_to_task_missing_identity_downgrades_to_warn(monkeypatch):
    """Симметрично update_task_comment/delete_task_comment: identity-guard не
    смог сверить название → ⚠️, даже если вложение реально прикрепилось и
    post-verify его видит. ✅ значит «подтверждено ПОЛНОСТЬЮ» (output-
    format.md §7.2) — название здесь не проверено, значит не полностью."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard("missing", project_id="p1"))

    preview = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                          url="https://x/file.pdf")
    mid = _extract_manifest_id(preview)
    result = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                         url="https://x/file.pdf",
                                         manifest_id=mid, user_reply="да")

    assert ("attach", "t1") in fake_v2.calls
    assert result.startswith("⚠️"), result
    assert not s._auto_execute_report_is_success(result), result


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
    _assert_confirmed_success(result)

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
    _assert_confirmed_success(result)


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
    _assert_confirmed_success(result)
    # Тот же класс бага, что и у create_project_group (дефект №3): правка
    # подтверждена post-verify (комментарий перечитан, текст совпал), но
    # старый текст не начинался с ✅ ("Comment on '...' updated").
    assert s._auto_execute_report_is_success(result), result


async def test_update_task_comment_missing_identity_downgrades_to_warn(monkeypatch):
    """Когда identity-guard НЕ смог сверить название (id не среди открытых
    задач — например, задача завершена), маркер обязан быть ⚠️, а не ✅: сама
    правка комментария подтверждена, но НЕ ВСЁ подтверждено (название — нет).
    ✅ по замороженной легенде (output-format.md §7.2) значит «подтверждено
    ПОЛНОСТЬЮ» — раздача его здесь была бы новой дырой того же типа."""
    fake_v2 = FakeV2(live={}, comments={"t1": [{"id": "c1", "title": "старый текст"}]})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard("missing", project_id="p1"))

    preview = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1")
    mid = _extract_manifest_id(preview)
    result = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1",
                                         manifest_id=mid, user_reply="да")

    assert fake_v2.comments["t1"][0]["title"] == "новый текст"
    assert result.startswith("⚠️"), result
    assert not s._auto_execute_report_is_success(result), result
