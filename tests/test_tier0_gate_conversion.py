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

EXCEPTION (2026-08-07, group A of the def-116 follow-up): attach_file_to_task
and update_task_comment are DIFFERENT from the other 8 tools above — for
these two, identity-guard is NOT "unchanged pre-existing logic confined to
_x_impl" anymore. Before this date, the claim above was literally true for
them too, and it was a bug, not a feature: `_guard_task` ran ONLY inside
`_attach_file_to_task_impl`/`_update_task_comment_impl` (call #2, execution),
so the plan card shown on call #1 printed `task_title` straight from the
caller with ZERO verification against the live task the id actually points
at. The three tests that used to sit here
(test_attach_file_to_task_full_gate_cycle,
test_attach_file_to_task_missing_identity_refuses,
test_update_task_comment_full_gate_cycle,
test_update_task_comment_missing_identity_refuses — последние два носили
тогда имена ..._missing_identity_downgrades_to_warn и переписаны 2026-08-07,
см. дефект №2 в их собственных докстрингах) all passed
`_guard_task` stand-ins that never distinguished call #1 from call #2 — every
one of them would have happily built a plan carrying a WRONG title if the
stand-in had been "mismatch" instead of "ok"/"missing", and nothing below
this docstring proved otherwise. Live audit, not code review, is what
actually found the real-world defect (see delete_habit/def-116,
commit ea2a47c — same bug, different tool). Fixed the same way: `_guard_task`
is now ALSO called while BUILDING the plan (call #1, before the card is
shown) — a mismatched title refuses the plan outright (🛑, before anything is
displayed); a live-read hiccup does NOT block the plan (fail-open — a network
blip must not stop every attach/comment-edit), but the plan text says so
honestly, and the UNCHANGED execution-side guard in `_x_impl` still catches a
real mismatch independently. See the new
`test_attach_file_to_task_plan_identity_guard_*` and
`test_update_task_comment_plan_identity_guard_*` tests below the happy-path
ones for each tool — THOSE are what actually exercises the plan-phase check;
the older tests above them remain, unmodified in behaviour, to cover the
gate-wrapper mechanics they were originally written for.

Of the other 8 tools in this file, 6 got the SAME treatment in group B (next
paragraph); only create_project_group and create_tag are genuinely UNCHANGED
— the "not re-tested here" claim above is still accurate for those two alone.

EXCEPTION (2026-08-07, group B of the def-116 follow-up — see delete_habit,
commit ea2a47c, and group A: attach_file_to_task/update_task_comment/
delete_task_comment, merged into main just before this group): create_subtask
is DIFFERENT from the claim above — identity-guard is NOT "unchanged
pre-existing logic confined to _x_impl" for it anymore. Before this date the
claim was literally true, and it was a bug: `_guard_task` ran ONLY inside
`_create_subtask_impl` (call #2, execution), so the plan card shown on call
#1 printed `parent_task_title` straight from the caller with ZERO
verification against the live task the id actually points at.
`test_create_subtask_full_gate_cycle` never exercised a mismatched pair, so
it could not have caught this — it is unmodified below (still valid, still
covers only gate-wrapper mechanics). See the new
`test_create_subtask_plan_identity_guard_*` tests below the existing
create_subtask block — THOSE are what exercises the plan-phase check.

unset_task_parent is the SAME fix, same rationale, for the SUBTASK being
detached (task_id/task_title) — see the new
`test_unset_task_parent_plan_identity_guard_*` tests below its existing
block. task_id and the two existing tests above them
(test_unset_task_parent_full_gate_cycle,
test_unset_task_parent_confirmed_detaches) already monkeypatch `_guard_task`
to an unconditional "ok" stand-in (same result on every call), so they
remain valid and unmodified — they could not have distinguished "checked on
plan" from "checked only on execution" either way. SCOPE NOTE: this fix
covers ONLY the SUBTASK's own task_id↔task_title. parent_task_id↔
parent_task_title (the claimed PARENT) was a distinct, older gap — nothing
verified it anywhere, not even on execution, where only the RELATIONSHIP
(the subtask's live parentId == parent_task_id) was ever checked. It was
filed separately as def-126 and closed by its own branch, merged into main
right after this group; both guards now live side by side in
unset_task_parent, and the merged section below covers BOTH — including the
case where both warn at once, which is what keeps their two warnings from
silently collapsing into one. See the long comment above that section.

delete_project_group and move_project_to_group get the same treatment for
group_id↔group_name and project_id↔project_name respectively — see their own
dedicated comment blocks right above their new
`test_delete_project_group_plan_*`/`test_move_project_to_group_plan_*` tests
for the per-tool nuance (delete_project_group has no monkeypatchable guard
helper, reads `_live_groups()` directly; move_project_to_group reuses
`_guard_project`, whose binary ok/refuse outcome has no separate "unavailable"
branch to soften). add_task_comment gets the exact same _guard_task shape as
attach_file_to_task/update_task_comment/delete_task_comment in group A
(mismatch blocks, missing only warns — see its own comment block above its
new `test_add_task_comment_plan_*` tests). duplicate_task gets the same
_guard_task shape as create_subtask/unset_task_parent (mismatch AND missing
both block — its task_title argument is OPTIONAL, unlike every other tool in
this file, see its own comment block for how that interacts with the guard).
create_project_group and create_tag are UNCHANGED and deliberately excluded
from this whole follow-up: both create a BRAND-NEW object from a `name`
only — there is no caller-supplied id for an identity guard to cross-check
against anything.

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
    """`_guard_task`/`_guard_project` stand-in that returns `results` in
    order, one per call — used to simulate the plan-phase read (call #1) and
    the execution-phase read (call #2, inside `_x_impl`, unchanged)
    disagreeing: e.g. the plan read fails/times out (yields "unavailable")
    while the execution read a moment later succeeds and finds either a
    match or a real mismatch.

    NOT used by the unset_task_parent section, which now has TWO guards per
    phase and routes by id+phase instead — see `_unset_guard_router` there."""
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

    preview = await s.create_subtask.direct("Купить молоко", "Купить хлеб", "p1", "proj1")
    assert official.calls == []
    assert "manifest_id" in preview
    assert "«Купить хлеб»" in preview

    mid = _extract_manifest_id(preview)
    refused = await s.create_subtask.direct("Купить молоко", "Купить хлеб", "p1", "proj1",
                                     manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert official.calls == []

    result = await s.create_subtask.direct("Купить молоко", "Купить хлеб", "p1", "proj1",
                                     manifest_id=mid, user_reply="да")
    # Проект назначения — часть ожидания: именно он решает, в каком списке
    # окажется подзадача, и без него подмена проекта была ненаблюдаемой.
    assert official.calls == [("create_subtask", "Купить хлеб", "p1", "p1")]
    _assert_confirmed_success(result)

    again = await s.create_subtask.direct("Купить молоко", "Купить хлеб", "p1", "proj1",
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

    preview = await s.create_subtask.direct("Купить молоко", "Купить хлеб", "p1", "proj1")
    mid = _extract_manifest_id(preview)

    result = await s.create_subtask.direct("Купить молоко", "Купить хлеб", "p1", "proj1",
                                    manifest_id=mid, user_reply="да")

    assert "❌" in result, f"молчаливый отказ выдан за успех:\n{result}"
    assert "НЕ подтвердилось" in result


async def test_create_subtask_invalid_priority_refused_before_gate(monkeypatch):
    official = FakeOfficial()
    _wire(monkeypatch, fake_v2=FakeV2(), fake_official=official)
    result = await s.create_subtask.direct("Купить молоко", "Купить хлеб", "p1", "proj1",
                                    priority=99)
    assert "🛑" in result or "Invalid priority" in result
    assert official.calls == []


# ---------------------------------------------------------------------------
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group B) — the
# PARENT's task_id↔parent_task_title is now cross-checked BEFORE the plan is
# built, not only inside _create_subtask_impl on execution. See module
# docstring above for why the tests above this point could NOT have caught
# the old bug.
# ---------------------------------------------------------------------------

async def test_create_subtask_plan_identity_guard_blocks_wrong_parent_title(
        monkeypatch):
    """id points at a REAL task ("Купить хлеб"), caller's parent_task_title
    claims a DIFFERENT one ("Купить молоко") — before this fix, call #1
    would have built and shown a plan card reading "Создаю подзадачу ...
    под «Купить молоко»" even though the id has nothing to do with that
    task. Now the plan is refused outright, before any card is built, and
    the official client (which would perform the create) is never touched."""
    live = {"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}}
    official = FakeOfficial(live=live)
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, fake_official=official, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard(
            "mismatch", project_id="p1", title="Купить хлеб",
            message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'))

    result = await s.create_subtask.direct("Купить молоко", "Новый шаг", "t1", "p1")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert official.calls == []


async def test_create_subtask_plan_identity_guard_blocks_missing_parent(
        monkeypatch):
    """_create_subtask_impl treats a MISSING parent as a hard 🛑 too (not a
    soft warning, unlike e.g. add_task_comment) — the plan-phase transfer
    must reproduce that same severity, not soften it."""
    fake_v2 = FakeV2(live={})
    official = FakeOfficial(live={})
    _wire(monkeypatch, fake_v2=fake_v2, fake_official=official, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard("missing", project_id="p1",
                                 message="id … не среди открытых задач"))

    result = await s.create_subtask.direct("Купить молоко", "Новый шаг", "t-нет-такой", "p1")

    assert result.startswith("🛑 План НЕ построен")
    assert "manifest_id" not in result
    assert official.calls == []


async def test_create_subtask_plan_read_failure_does_not_block_but_warns(
        monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every subtask creation — fail-open here is cheaper than refusing
    everyone whose network is briefly flaky. The plan is still built,
    honestly warns that the parent's title was not verified, and the real
    (unchanged) identity-guard on execution (call #2) does its normal job
    right after."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    official = FakeOfficial(live=live)
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, fake_official=official, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                  # call #1 (plan)
        s._Guard("ok", project_id="p1", title="Купить молоко"),   # call #2 (_impl)
    ))

    preview = await s.create_subtask.direct("Купить молоко", "Новый шаг", "t1", "p1")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.create_subtask.direct("Купить молоко", "Новый шаг", "t1", "p1",
                                    manifest_id=mid, user_reply="да")
    assert official.calls == [("create_subtask", "Новый шаг", "t1", "p1")]
    _assert_confirmed_success(result)


async def test_create_subtask_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside
    `_create_subtask_impl` — untouched by this change — still catches the
    real mismatch: a network blip on planning must not weaken the
    protection at execution time."""
    live = {"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}}
    official = FakeOfficial(live=live)
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, fake_official=official, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                    # call #1 (plan)
        s._Guard("mismatch", project_id="p1", title="Купить хлеб",   # call #2 (_impl)
                message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'),
    ))

    preview = await s.create_subtask.direct("Купить молоко", "Новый шаг", "t1", "p1")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.create_subtask.direct("Купить молоко", "Новый шаг", "t1", "p1",
                                    manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Купить хлеб»" in result
    assert official.calls == []


async def test_create_subtask_automation_key_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false parent name+id
    pair would sail through silently on a single valid key. The check sits
    BEFORE _gate_single, so it applies here too: plan is refused, create
    never attempted, and exactly one guard read happens (the plan-stage
    one)."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    official = FakeOfficial()
    _wire(monkeypatch, fake_v2=fake_v2, fake_official=official, guard_task=False)
    calls = []

    def _stub(*a, **k):
        calls.append(1)
        return s._Guard("mismatch", project_id="p1", title="Купить хлеб",
                        message='id указывает на «Купить хлеб», а НЕ «Купить молоко»')
    monkeypatch.setattr(s, "_guard_task", _stub)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.create_subtask.direct("Купить молоко", "Новый шаг", "t1", "p1",
                                    automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert official.calls == []
    assert len(calls) == 1


# ===========================================================================
# unset_task_parent
# ===========================================================================

async def test_unset_task_parent_full_gate_cycle(monkeypatch):
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "_guard_task",
                        lambda *a, **k: s._Guard("ok", project_id="p1", title="Шаг 1"))

    preview = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")
    assert fake_v2.calls == []

    mid = _extract_manifest_id(preview)
    refused = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                        manifest_id=mid, user_reply="нет")
    assert "🛑" in refused
    assert live["c"]["parentId"] == "p"

    dead = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                     manifest_id=mid, user_reply="да")
    assert "🛑" in dead  # manifest was invalidated by the explicit "no" above
    assert live["c"]["parentId"] == "p"


async def test_unset_task_parent_confirmed_detaches(monkeypatch):
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "_guard_task",
                        lambda *a, **k: s._Guard("ok", project_id="p1", title="Шаг 1"))

    preview = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")
    mid = _extract_manifest_id(preview)
    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert ("unset_parent", "c") in fake_v2.calls
    _assert_confirmed_success(result)


# ---------------------------------------------------------------------------
# 2026-08-07: TWO independent plan-phase identity guards landed on
# unset_task_parent from two separate branches, and this section covers BOTH.
#
#   def-125 (group B of the def-116 follow-up) — task_id↔task_title of the
#   SUBTASK being detached. The check already existed inside
#   _unset_task_parent_impl but ran only on EXECUTION, after the owner had
#   already read (and possibly approved) the plan card. Now it also runs
#   BEFORE the plan is built. mismatch AND missing both refuse the plan (🛑)
#   — the executor treats both as 🛑 too, so moving it earlier changes no
#   severity; only "unavailable" (a live-read hiccup) is soft, fail-open with
#   a warning printed on the card.
#
#   def-126 — parent_task_id↔parent_task_title of the claimed PARENT. This
#   one was never checked ANYWHERE: _unset_task_parent_impl only verified the
#   RELATIONSHIP (the subtask's live parentId == parent_task_id), never the
#   parent's NAME against that id, so the card «Отцепляю «X» от родителя «Y»»
#   could carry any Y at all. Only a LIVE parent resolving to a different name
#   refuses the plan (🛑); missing/unavailable deliberately only warn, because
#   unset_task_parent SEVERS a link rather than creating one and a
#   completed/deleted parent is the ordinary reason to detach from it (unlike
#   set_task_parent, where a missing parent is refused outright — nesting
#   under a dead parent orphans the child). It also repeats itself inside
#   _unset_task_parent_impl as a second, independent line of defense, since up
#   to an hour can pass between the plan and the confirmation.
#
# MERGE NOTE (read before touching anything below). The two branches each
# added their own `describe_fn = ...` assignment, with the SAME name, in the
# same spot. Left as two consecutive assignments Python would silently keep
# only the last one and one of the two ⚠️ warnings would vanish from the card
# the owner actually reads — with no merge conflict on that line, and with
# every test of both branches still green, because each branch only ever
# asserted its OWN warning. They are now folded into ONE assignment that adds
# BOTH warnings (server.py), and
# `test_unset_task_parent_plan_warns_about_BOTH_child_and_parent` below is the
# test neither branch had: it is the only thing standing between that fold and
# a silent regression back into the broken shape. Do not delete it, and do not
# weaken it to assert just one of the two warnings.
#
# The two tests ABOVE this comment (test_unset_task_parent_full_gate_cycle,
# test_unset_task_parent_confirmed_detaches) monkeypatch `_guard_task` to an
# unconditional "ok" for every call and argument, so they can distinguish
# neither guard from its absence — they remain valid, and unmodified, for the
# gate-wrapper mechanics they were written for.
# ---------------------------------------------------------------------------


def _unset_guard_router(*, plan_child, plan_parent=None, impl_child=None,
                        impl_parent=None, child_id="c", parent_id="p",
                        counter=None):
    """`_guard_task` stand-in for unset_task_parent specifically.

    With both guards in place the tool asks `_guard_task` up to FOUR times per
    full cycle — subtask, then parent, while BUILDING the plan (call #1), and
    subtask, then parent again inside `_unset_task_parent_impl` on execution
    (call #2). A positional `_guard_sequence` cannot express that readably
    (and silently breaks the moment a guard returns early), so this stand-in
    routes by WHICH id it is asked about and by WHICH phase is asking: the
    execution-side calls pass `by_id=`, the plan-side ones do not.

    `counter`, when given a list, gets one entry appended per call — for the
    automation-key tests, which assert that the refusal happened before the
    gate rather than after some later read."""
    def _stub(task_id, *_a, **kw):
        if counter is not None:
            counter.append(task_id)
        execution = kw.get("by_id") is not None
        if task_id == child_id:
            g = impl_child if execution else plan_child
        elif task_id == parent_id:
            g = impl_parent if execution else plan_parent
        else:
            raise AssertionError(f"_guard_task asked about unexpected id {task_id!r}")
        assert g is not None, (
            f"_guard_task called for id={task_id!r} "
            f"({'execution' if execution else 'plan'} phase) but the test "
            "did not stage a result for that call")
        return g
    return _stub


def _g_ok(title):
    return s._Guard("ok", project_id="p1", title=title)


# --- def-125: the SUBTASK's own name -------------------------------------

async def test_unset_task_parent_plan_identity_guard_blocks_wrong_title(
        monkeypatch):
    """id points at a REAL task ("Шаг 2"), caller's task_title claims a
    DIFFERENT one ("Шаг 1") — before this fix, call #1 would have built and
    shown a plan card reading "Отцепляю «Шаг 1» от родителя ..." even though
    the id has nothing to do with that task. Now the plan is refused
    outright."""
    live = {"c": {"id": "c", "title": "Шаг 2", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        plan_child=s._Guard("mismatch", project_id="p1", title="Шаг 2",
                            message='id указывает на «Шаг 2», а НЕ «Шаг 1»'),
        # the parent guard must never be reached — the subtask check refuses
        # first, and staging nothing for it makes that an assertion, not luck
    ))

    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Шаг 2»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert live["c"]["parentId"] == "p"
    assert fake_v2.calls == []


async def test_unset_task_parent_plan_identity_guard_blocks_missing_task(
        monkeypatch):
    """_unset_task_parent_impl treats a MISSING task as a hard 🛑 too, not a
    soft warning — the plan-phase transfer must reproduce that same
    severity. (Contrast with a missing PARENT, which only warns — see
    test_unset_task_parent_plan_missing_parent_does_not_block_but_warns.)"""
    fake_v2 = FakeV2(live={})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        child_id="c-нет-такой",
        plan_child=s._Guard("missing", project_id="p1",
                            message="id … не среди открытых задач"),
    ))

    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c-нет-такой", "p", "p1")

    assert result.startswith("🛑 План НЕ построен")
    assert "manifest_id" not in result
    assert fake_v2.calls == []


async def test_unset_task_parent_plan_child_read_failure_does_not_block_but_warns(
        monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every detach — fail-open here is cheaper than refusing everyone whose
    network is briefly flaky. The plan is still built, honestly warns that
    the title was not verified, and the real (unchanged) identity-guard on
    execution (call #2) does its normal job right after.

    (Renamed on merge from ..._plan_read_failure_does_not_block_but_warns:
    def-126 brought a same-named test for the PARENT's read failure, and both
    cases have to stay covered — see
    test_unset_task_parent_plan_parent_read_failure_does_not_block_but_warns.)"""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        plan_child=s._Guard("unavailable"),
        plan_parent=_g_ok("Большой проект"),
        impl_child=_g_ok("Шаг 1"),
        impl_parent=_g_ok("Большой проект"),
    ))

    preview = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "Название задачи НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert ("unset_parent", "c") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_unset_task_parent_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside
    `_unset_task_parent_impl` — untouched by this change — still catches the
    real mismatch: a network blip on planning must not weaken the
    protection at execution time."""
    live = {"c": {"id": "c", "title": "Шаг 2", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        plan_child=s._Guard("unavailable"),
        plan_parent=_g_ok("Большой проект"),
        impl_child=s._Guard("mismatch", project_id="p1", title="Шаг 2",
                            message='id указывает на «Шаг 2», а НЕ «Шаг 1»'),
    ))

    preview = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Шаг 2»" in result
    assert live["c"]["parentId"] == "p"
    assert fake_v2.calls == []


async def test_unset_task_parent_automation_key_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false name+id pair
    would sail through silently on a single valid key. The check sits BEFORE
    _gate_single, so it applies here too."""
    live = {"c": {"id": "c", "title": "Шаг 2", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    calls = []
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        counter=calls,
        plan_child=s._Guard("mismatch", project_id="p1", title="Шаг 2",
                            message='id указывает на «Шаг 2», а НЕ «Шаг 1»'),
    ))
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                       automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Шаг 2»" in result
    assert live["c"]["parentId"] == "p"
    assert fake_v2.calls == []
    assert calls == ["c"], "проверка субтаска обязана стоять ДО гейта и оборвать всё на себе"


# --- def-126: the claimed PARENT's name -----------------------------------

async def test_unset_task_parent_plan_identity_guard_blocks_wrong_parent_title(
        monkeypatch):
    """parent_task_id points at a REAL task ("Большой проект"), caller's
    parent_task_title claims a DIFFERENT one ("Другой родитель") — before
    this fix, call #1 would have built and shown a plan card reading
    'Отцепляю «Шаг 1» от родителя «Другой родитель»' even though the id has
    nothing to do with that task. Now the plan is refused outright, before
    any card is built, and unset_task_parent (the v2 mutation) is never
    called. The SUBTASK's own guard says "ok" here, so the refusal can only
    have come from the parent check."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        plan_child=_g_ok("Шаг 1"),
        plan_parent=s._Guard(
            "mismatch", project_id="p1", title="Большой проект",
            message='id указывает на «Большой проект», а НЕ «Другой родитель»'),
    ))

    result = await s.unset_task_parent.direct("Шаг 1", "Другой родитель", "c", "p", "p1")

    assert result.startswith("🛑 План НЕ построен")
    assert "родитель по id" in result
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
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        plan_child=_g_ok("Шаг 1"),
        plan_parent=s._Guard("missing", project_id="p1"),
        impl_child=_g_ok("Шаг 1"),
        impl_parent=s._Guard("missing", project_id="p1"),
    ))

    preview = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")
    assert "🛑" not in preview, "родитель, не найденный среди открытых, не должен блокировать план"
    assert "возможно завершён/удалён" in preview
    mid = _extract_manifest_id(preview)

    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert ("unset_parent", "c") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_unset_task_parent_plan_parent_read_failure_does_not_block_but_warns(
        monkeypatch):
    """Same fail-open reasoning as the subtask's read-failure case, for the
    parent: a live-read hiccup while BUILDING the plan must not block every
    detach. The plan is still built, honestly warns that the PARENT's name
    was not verified, and the execution-side parent guard does its normal job
    right after."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        plan_child=_g_ok("Шаг 1"),
        plan_parent=s._Guard("unavailable"),
        impl_child=_g_ok("Шаг 1"),
        impl_parent=_g_ok("Большой проект"),
    ))

    preview = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "Имя родителя НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert ("unset_parent", "c") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_unset_task_parent_plan_read_failure_still_lets_execution_catch_a_real_parent_mismatch(
        monkeypatch):
    """Same read failure on the plan as
    test_unset_task_parent_plan_parent_read_failure_does_not_block_but_warns
    above, but this time the parent's real name actually DOESN'T match the
    claim. The plan-phase check couldn't run (so it warns instead of
    refusing), but the execution-phase guard inside
    `_unset_task_parent_impl` — the second, independent line of defense —
    still catches the real mismatch: a network blip on planning must not
    weaken the protection at execution time."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        plan_child=_g_ok("Шаг 1"),
        plan_parent=s._Guard("unavailable"),
        impl_child=_g_ok("Шаг 1"),
        impl_parent=s._Guard(
            "mismatch", project_id="p1", title="Другой родитель",
            message='id указывает на «Другой родитель», а НЕ «Большой проект»'),
    ))

    preview = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "родитель по id" in result
    assert "«Другой родитель»" in result
    assert live["c"]["parentId"] == "p"
    assert fake_v2.calls == []


async def test_unset_task_parent_automation_key_parent_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118) for the PARENT check: a valid automation_key runs
    on the FIRST call, with no plan card and no Telegram button ever shown —
    so if the parent identity check only lived inside _gate_single/execution,
    a false parent name+id pair would sail through silently on a single valid
    key. The check sits BEFORE _gate_single, so it applies here too."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    calls = []
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        counter=calls,
        plan_child=_g_ok("Шаг 1"),
        plan_parent=s._Guard(
            "mismatch", project_id="p1", title="Большой проект",
            message='id указывает на «Большой проект», а НЕ «Другой родитель»'),
    ))
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.unset_task_parent.direct("Шаг 1", "Другой родитель", "c", "p", "p1",
                                       automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Большой проект»" in result
    assert live["c"]["parentId"] == "p"
    assert fake_v2.calls == []
    assert calls == ["c", "p"], "обе плановые сверки обязаны стоять ДО гейта"


# --- def-125 + def-126 TOGETHER (the merge-fold regression test) -----------

async def test_unset_task_parent_plan_warns_about_BOTH_child_and_parent(
        monkeypatch):
    """THE test neither branch had, and the only one that fails if the two
    `describe_fn` assignments are ever folded back into two consecutive
    statements (the last one silently winning) or if either summand is
    dropped from the fold.

    Both soft outcomes at once: the subtask's name cannot be verified
    (a live-read hiccup — the ONLY soft outcome it has, since missing and
    mismatch both refuse the plan) AND the parent's name cannot be verified
    either. Neither blocks, so a plan card IS built — and it must carry BOTH
    warnings, because each one is a separate fact the owner needs before
    approving. Every other test in this section asserts one warning while the
    other is absent, so all of them stay green with half the fold missing."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        plan_child=s._Guard("unavailable"),
        plan_parent=s._Guard("unavailable"),
        impl_child=_g_ok("Шаг 1"),
        impl_parent=_g_ok("Большой проект"),
    ))

    preview = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")

    assert "🛑" not in preview, "два мягких предупреждения не должны блокировать план"
    assert "Название задачи НЕ удалось сверить" in preview, (
        "предупреждение про САМ отцепляемый субтаск (def-125) пропало из карточки")
    assert "Имя родителя НЕ удалось сверить" in preview, (
        "предупреждение про РОДИТЕЛЯ (def-126) пропало из карточки")
    # …and both on the SAME line, i.e. really from one describe_fn, not from
    # some other part of the card.
    plan_line = next(ln for ln in preview.splitlines() if "📋 План" in ln)
    assert "Название задачи НЕ удалось сверить" in plan_line
    assert "Имя родителя НЕ удалось сверить" in plan_line

    # the plan still works end-to-end after warning about both
    mid = _extract_manifest_id(preview)
    result = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1",
                                       manifest_id=mid, user_reply="да")
    assert ("unset_parent", "c") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_unset_task_parent_plan_warns_about_both_when_parent_is_merely_missing(
        monkeypatch):
    """Same fold, the other soft parent outcome: subtask unverifiable
    (read hiccup) + parent simply not among the open tasks
    (completed/deleted). Both warnings, one card, no block."""
    live = {"c": {"id": "c", "title": "Шаг 1", "projectId": "p1", "parentId": "p"}}
    fake_v2 = FakeV2(live=live)
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _unset_guard_router(
        plan_child=s._Guard("unavailable"),
        plan_parent=s._Guard("missing", project_id="p1"),
        impl_child=_g_ok("Шаг 1"),
        impl_parent=s._Guard("missing", project_id="p1"),
    ))

    preview = await s.unset_task_parent.direct("Шаг 1", "Большой проект", "c", "p", "p1")

    assert "🛑" not in preview
    assert "Название задачи НЕ удалось сверить" in preview
    assert "возможно завершён/удалён" in preview


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


# ---------------------------------------------------------------------------
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group B) —
# group_id↔group_name is now cross-checked BEFORE the plan is built, not
# only inside _delete_project_group_impl on execution. Unlike attach_file_
# to_task/update_task_comment (_guard_task), this method has no monkeypatch-
# able guard helper — it reads _live_groups() directly — so these tests
# shape the FAKE's live group list itself instead of stubbing a guard
# function.
# ---------------------------------------------------------------------------

async def test_delete_project_group_plan_identity_guard_blocks_wrong_name(
        monkeypatch):
    """id points at a REAL group ("Работа"), caller's group_name claims a
    DIFFERENT one ("Личное") — before this fix, call #1 would have built and
    shown a plan card reading "Удаляю папку проектов «Личное» ..." even
    though the id has nothing to do with that group. Now the plan is refused
    outright, before any card is built, and delete_group is never called."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Работа"}])
    _wire(monkeypatch, fake_v2=fake_v2)

    result = await s.delete_project_group("Личное", "g1")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Работа»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert fake_v2.calls == []
    assert any(g["id"] == "g1" for g in fake_v2.groups), "группа не должна быть тронута"


async def test_delete_project_group_plan_identity_guard_blocks_missing_group(
        monkeypatch):
    """The group_id isn't in the live list at all (already deleted/wrong id)
    — _delete_project_group_impl already refuses this with 🛑 on execution;
    the plan-phase transfer must refuse it just as hard, before the card."""
    fake_v2 = FakeV2(groups=[])
    _wire(monkeypatch, fake_v2=fake_v2)

    result = await s.delete_project_group("Личное", "g-нет-такой")

    assert result.startswith("🛑 План НЕ построен")
    assert "manifest_id" not in result
    assert fake_v2.calls == []


async def test_delete_project_group_plan_read_failure_does_not_block_but_warns(
        monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every deletion — fail-open here is cheaper than refusing everyone whose
    network is briefly flaky. The plan is still built, honestly warns that
    the name was not verified, and the real (unchanged) identity-guard on
    execution (call #2) does its normal job right after."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Личное"}])
    _wire(monkeypatch, fake_v2=fake_v2)
    calls = {"n": 0}
    real_live_groups = s._live_groups

    async def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("v2 read timed out")
        return await real_live_groups(*a, **k)
    monkeypatch.setattr(s, "_live_groups", _flaky)

    preview = await s.delete_project_group("Личное", "g1")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.delete_project_group("Личное", "g1", manifest_id=mid, user_reply="да")
    assert ("delete_group", "g1") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_delete_project_group_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside
    `_delete_project_group_impl` — untouched by this change — still catches
    the real mismatch: a network blip on planning must not weaken the
    protection at execution time, especially on an irreversible delete."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Работа"}])
    _wire(monkeypatch, fake_v2=fake_v2)
    calls = {"n": 0}
    real_live_groups = s._live_groups

    async def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("v2 read timed out")
        return await real_live_groups(*a, **k)
    monkeypatch.setattr(s, "_live_groups", _flaky)

    preview = await s.delete_project_group("Личное", "g1")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.delete_project_group("Личное", "g1", manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Работа»" in result
    assert fake_v2.calls == []
    assert any(g["id"] == "g1" for g in fake_v2.groups)


# ---------------------------------------------------------------------------
# 2026-08-07, живая приёмка: карточка удаления папки НЕ называла проекты
# внутри. Проверено дважды — папка с ОДНИМ проектом и папка с ДВУМЯ дают
# ПОБАЙТОВО ОДИНАКОВЫЙ текст:
#
#   📋 План — Удаляю папку проектов «X» (сами проекты останутся, просто без
#   папки)
#
# Ни числа, ни имён: карточка не отличает пустую папку от папки с
# содержимым. Отговорка «так нельзя» снимается соседним методом —
# plan_task_deletion для задачи с подзадачами разворачивает ВСЁ поддерево и
# показывает полный список на одобрение; ситуация идентична, действие над
# контейнером. При двух тестовых проектах это терпимо, при папке уровня
# «Active» с восемью боевыми — человек нажимает вслепую.
# ---------------------------------------------------------------------------

async def test_delete_group_plan_lists_the_projects_inside(monkeypatch):
    """Главный тест пункта: папка с ДВУМЯ проектами — оба имени обязаны
    быть в тексте плана."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Личное"}],
                     projects=[{"id": "p1", "name": "Дом", "groupId": "g1"},
                               {"id": "p2", "name": "Финансы", "groupId": "g1"},
                               {"id": "p3", "name": "Чужой", "groupId": "g2"},
                               {"id": "p4", "name": "Без папки", "groupId": None}])
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.delete_project_group("Личное", "g1")

    assert "«Дом»" in preview and "«Финансы»" in preview, (
        f"проекты внутри папки не названы:\n{preview}")
    assert "2" in preview, "число проектов внутри не показано"
    assert "«Чужой»" not in preview and "«Без папки»" not in preview, (
        "в карточку попали проекты из ДРУГОЙ папки")
    assert fake_v2.calls == []


async def test_delete_group_plan_distinguishes_one_project_from_two(monkeypatch):
    """Дословное воспроизведение приёмки: раньше эти два плана были
    побайтово одинаковы. Теперь они обязаны различаться."""
    one = FakeV2(groups=[{"id": "g1", "name": "Личное"}],
                 projects=[{"id": "p1", "name": "Дом", "groupId": "g1"}])
    _wire(monkeypatch, fake_v2=one)
    plan_one = (await s.delete_project_group("Личное", "g1")).split("_Манифест")[0]

    two = FakeV2(groups=[{"id": "g1", "name": "Личное"}],
                 projects=[{"id": "p1", "name": "Дом", "groupId": "g1"},
                           {"id": "p2", "name": "Финансы", "groupId": "g1"}])
    _wire(monkeypatch, fake_v2=two)
    plan_two = (await s.delete_project_group("Личное", "g1")).split("_Манифест")[0]

    assert plan_one != plan_two, (
        "папка с одним проектом и папка с двумя дают одинаковый текст:\n"
        f"{plan_one!r}")


async def test_delete_group_plan_says_when_the_folder_is_empty(monkeypatch):
    """Пустая папка обязана читаться как пустая, а не как «неизвестно»."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Личное"}], projects=[])
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.delete_project_group("Личное", "g1")

    assert "пуст" in preview.lower(), preview
    assert "manifest_id" in preview


async def test_delete_group_plan_caps_a_long_list(monkeypatch):
    """Папка уровня «Active»: список режется, но остаток назван числом —
    «и ещё N», а не молча обрезан."""
    many = [{"id": f"p{i}", "name": f"Проект {i}", "groupId": "g1"}
            for i in range(1, 16)]
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Личное"}], projects=many)
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.delete_project_group("Личное", "g1")

    assert "15" in preview, "общее число проектов внутри не названо"
    assert "и ещё" in preview, f"хвост списка обрезан молча:\n{preview}"
    assert "«Проект 1»" in preview


async def test_delete_group_plan_membership_read_failure_is_spoken_not_silent(
        monkeypatch):
    """Состав прочитать не удалось — карточка обязана сказать об этом, а не
    выглядеть как карточка пустой папки. План при этом строится (сверка
    имени папки уже прошла, а состав — справка, не гейт)."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Личное"}], projects=[])

    def _boom():
        raise RuntimeError("v2 упал")
    fake_v2.list_projects = _boom
    _wire(monkeypatch, fake_v2=fake_v2)

    preview = await s.delete_project_group("Личное", "g1")

    assert "manifest_id" in preview and "🛑" not in preview
    assert "не удалось" in preview.lower(), preview
    assert "пуст" not in preview.lower(), (
        "неудачное чтение состава выдано за пустую папку")


async def test_delete_project_group_automation_key_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false name+id pair
    would sail through silently on a single valid key, deleting the WRONG
    group. The check sits BEFORE _gate_single, so it applies here too."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Работа"}])
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.delete_project_group("Личное", "g1", automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Работа»" in result
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


# ---------------------------------------------------------------------------
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group B) —
# project_id↔project_name is now cross-checked BEFORE the plan is built, not
# only inside _move_project_to_group_impl on execution. Unlike _guard_task,
# _guard_project has a BINARY outcome (a refusal string, or None) — it does
# not distinguish "couldn't verify" from "genuinely doesn't match/exist",
# and that was ALREADY true at execution (require_known=True refuses both
# the same way). So there is no separate "soft warning" branch to add here
# on purpose: reproducing the exact same _guard_project call at plan time
# reproduces the exact same strictness, unconditionally — see
# test_move_project_to_group_plan_guard_refusal_blocks_even_when_reason_is_unavailable
# below, which exists specifically to prove that decision (not invent a
# softer path the original never had).
# ---------------------------------------------------------------------------

async def test_move_project_to_group_plan_identity_guard_blocks_wrong_name(
        monkeypatch):
    """project_id points at a REAL project ("Работа"), caller's
    project_name claims a DIFFERENT one ("Личное") — before this fix, call
    #1 would have built and shown a plan card even though the id has
    nothing to do with that project. Now the plan is refused outright,
    before any card is built, and move_project_to_group is never called."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Папка"}],
                     projects=[{"id": "p1", "groupId": None}])
    _wire(monkeypatch, fake_v2=fake_v2, guard_project=False)
    monkeypatch.setattr(
        s, "_guard_project",
        lambda *a, **k: ('🛑 Отказ — project_id указывает на «Работа», а НЕ '
                         '«Личное» (защита от «не того проекта»). Ничего не тронул.'))

    result = await s.move_project_to_group("Личное", "p1", "g1")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Работа»" in result
    assert "Ничего не изменено." in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert fake_v2.calls == []


async def test_move_project_to_group_plan_guard_refusal_blocks_even_when_reason_is_unavailable(
        monkeypatch):
    """_guard_project(require_known=True) refuses BOTH an unreadable/unknown
    id AND a genuine mismatch the exact same way (no distinct 'unavailable'
    branch exists in the function being reused) — that is pre-existing,
    unchanged behaviour, not something this transfer invents. Proves the
    transfer does NOT add a softer "read hiccup" carve-out that the
    original _move_project_to_group_impl never had: whatever _guard_project
    refuses, the plan refuses too, same strictness, just earlier."""
    fake_v2 = FakeV2(groups=[], projects=[])
    _wire(monkeypatch, fake_v2=fake_v2, guard_project=False)
    monkeypatch.setattr(
        s, "_guard_project",
        lambda *a, **k: ('🛑 Отказ — проект по id p1… не найден среди живых '
                         'проектов (или имена недоступны) — сверить личность '
                         'проекта нельзя. Ничего не тронул.'))

    result = await s.move_project_to_group("Работа", "p1", "g1")

    assert result.startswith("🛑 План НЕ построен")
    assert "manifest_id" not in result
    assert fake_v2.calls == []


async def test_move_project_to_group_automation_key_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false name+id pair
    would sail through silently on a single valid key. The check sits
    BEFORE _gate_single, so it applies here too: plan is refused, move never
    attempted, and exactly one guard call happens (the plan-stage one) —
    proving it is the PLAN stage refusing, not the execution stage (which
    would only be reached after _gate_single, and only after an approved
    manifest)."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Папка"}],
                     projects=[{"id": "p1", "groupId": None}])
    _wire(monkeypatch, fake_v2=fake_v2, guard_project=False)
    calls = []

    def _stub(*a, **k):
        calls.append(1)
        return ('🛑 Отказ — project_id указывает на «Работа», а НЕ «Личное» '
                '(защита от «не того проекта»). Ничего не тронул.')
    monkeypatch.setattr(s, "_guard_project", _stub)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.move_project_to_group("Личное", "p1", "g1",
                                           automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Работа»" in result
    assert fake_v2.calls == []
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 2026-08-07, живая приёмка: карточка называла ПАПКУ-НАЗНАЧЕНИЕ сырым id.
# Дословно:
#
#   📋 План — Перемещаю проект «__AUTOTEST__btn-tt-01-retest» в папку
#   id:c4d38a807dfe452e964e89b9
#
# Объект-источник (проект) резолвился и печатался по-человечески, объект-
# назначение (папка) — нет, хотя имя папки сервер знает: оно лежит в том же
# кэшированном v2-снапшоте, из которого читается list_project_groups, и
# _move_project_to_group_impl уже достаёт его на ИСПОЛНЕНИИ (dest_name) —
# то есть в отчёте после нажатия кнопки имя было, а в карточке ДО нажатия
# его не было. Ровно наоборот тому, что нужно человеку.
# ---------------------------------------------------------------------------

async def test_move_plan_names_the_destination_folder(monkeypatch):
    """Главный тест пункта: в карточке — имя папки, а не «id:g1»."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Активные"}],
                     projects=[{"id": "p1", "name": "Работа", "groupId": None}])
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})

    preview = await s.move_project_to_group("Работа", "p1", "g1")

    assert "«Активные»" in preview, f"папка не названа по имени:\n{preview}"
    assert "id:g1" not in preview, f"сырой id вместо имени:\n{preview}"
    assert fake_v2.calls == []


async def test_move_plan_says_out_loud_when_the_folder_name_is_unknown(monkeypatch):
    """Резолвинг не удался — молчать нельзя: карточка обязана сказать, что
    имя папки установить не удалось, и показать id. Молчаливый показ id и
    есть дефект, поэтому «просто id» тут недостаточно."""
    fake_v2 = FakeV2(groups=[], projects=[{"id": "p1", "groupId": None}])
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})

    preview = await s.move_project_to_group("Работа", "p1", "g-неизвестная")

    assert "не удалось" in preview.lower(), (
        f"неудачный резолвинг остался молчаливым:\n{preview}")
    assert "g-неизвестная" in preview, "при неизвестном имени id обязан быть виден"


async def test_move_plan_ungroup_wording_is_unchanged(monkeypatch):
    """group_id="NONE" — разгруппировка, резолвить нечего: формулировка
    «без папки» остаётся как была, без всяких «имя не удалось»."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Активные"}],
                     projects=[{"id": "p1", "groupId": "g1"}])
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})

    preview = await s.move_project_to_group("Работа", "p1", "NONE")

    assert "без папки" in preview
    assert "не удалось" not in preview.lower()


async def test_move_plan_folder_lookup_never_blocks_the_plan(monkeypatch):
    """Резолвинг имени папки — украшение карточки, не право вето: падение
    чтения групп не должно мешать построить план (сверка назначения на
    ИСПОЛНЕНИИ, где она и живёт, никуда не делась)."""
    fake_v2 = FakeV2(groups=[{"id": "g1", "name": "Активные"}],
                     projects=[{"id": "p1", "groupId": None}])
    _wire(monkeypatch, fake_v2=fake_v2)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})

    async def _boom(*a, **k):
        raise RuntimeError("v2 упал")
    monkeypatch.setattr(s, "_live_groups", _boom)

    preview = await s.move_project_to_group("Работа", "p1", "g1")

    assert "manifest_id" in preview and "🛑" not in preview
    assert "не удалось" in preview.lower()
    assert "g1" in preview


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


# ---------------------------------------------------------------------------
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group B) —
# task_id↔task_title now cross-checked BEFORE the plan is built, not only
# inside _add_task_comment_impl on execution. Same shape as
# attach_file_to_task/update_task_comment/delete_task_comment in group A
# (separate branch, not present here) — mismatch blocks the plan, missing
# only warns (matching _add_task_comment_impl's own severity on execution).
# ---------------------------------------------------------------------------

async def test_add_task_comment_plan_identity_guard_blocks_wrong_title(
        monkeypatch):
    """id points at a REAL task ("Купить хлеб"), caller's task_title claims
    a DIFFERENT one ("Купить молоко") — before this fix, call #1 would have
    built and shown a plan card reading "Добавляю комментарий к «Купить
    молоко»..." even though the id has nothing to do with that task. Now the
    plan is refused outright."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard(
            "mismatch", project_id="p1", title="Купить хлеб",
            message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'))

    result = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert fake_v2.calls == []


async def test_add_task_comment_plan_missing_identity_refuses(monkeypatch):
    """ПЕРЕПИСАН 2026-08-07 (дефект №2). Раньше этот тест назывался
    ..._does_not_block_but_warns и фиксировал прежнюю политику: `missing` →
    план строится с пометкой «название НЕ проверено», потому что «скорее
    всего задача просто завершена».

    Политика изменилась там, где ей и место — в решении КЛАССА операций
    (add/attach/update/delete comment + duplicate_task), см.
    `_guard_task_incl_completed`. Завершённая задача теперь опознаётся ПО
    ИМЕНИ через источник, где она живёт, и получает отдельный статус
    `completed` (операция разрешена, название сверено). Поэтому `missing`
    больше не значит «может быть, завершена» — он значит «не нашли НИ В
    ОДНОМ источнике», и строить на нём план операции над несуществующим
    объектом нельзя (тот же инвариант, что в
    tests/test_missing_object_refused.py).

    Согласованность всех пяти инструментов класса на одном входе проверяется
    на настоящих клиентах в tests/test_completed_task_class_consistency.py —
    подменять там `_guard_task`, как здесь, было бы проверкой мока."""
    fake_v2 = FakeV2(live={})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard("missing", project_id="p1",
                                 message="id … не среди открытых задач"))

    preview = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1")

    assert preview.startswith("🛑 План НЕ построен"), preview
    assert "manifest_id" not in preview, preview
    assert fake_v2.calls == []


async def test_add_task_comment_plan_read_failure_does_not_block_but_warns(
        monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every comment — fail-open here is cheaper than refusing everyone whose
    network is briefly flaky. The plan is still built, honestly warns that
    the title was not verified, and the real (unchanged) identity-guard on
    execution (call #2) does its normal job right after."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                  # call #1 (plan)
        s._Guard("ok", project_id="p1", title="Купить молоко"),   # call #2 (_impl)
    ))

    preview = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1",
                                      manifest_id=mid, user_reply="да")
    assert ("add_comment", "t1", "не забыть") in fake_v2.calls
    assert "🛑" not in result and "❌" not in result, result


async def test_add_task_comment_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside
    `_add_task_comment_impl` — untouched by this change — still catches the
    real mismatch: a network blip on planning must not weaken the protection
    at execution time."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                    # call #1 (plan)
        s._Guard("mismatch", project_id="p1", title="Купить хлеб",   # call #2 (_impl)
                message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'),
    ))

    preview = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1",
                                      manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Купить хлеб»" in result
    assert fake_v2.calls == []


async def test_add_task_comment_automation_key_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false name+id pair
    would sail through silently on a single valid key. The check sits BEFORE
    _gate_single, so it applies here too."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    calls = []

    def _stub(*a, **k):
        calls.append(1)
        return s._Guard("mismatch", project_id="p1", title="Купить хлеб",
                        message='id указывает на «Купить хлеб», а НЕ «Купить молоко»')
    monkeypatch.setattr(s, "_guard_task", _stub)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.add_task_comment("Купить молоко", "не забыть", "p1", "t1",
                                      automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert fake_v2.calls == []
    assert len(calls) == 1


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


async def test_attach_file_to_task_missing_identity_refuses(monkeypatch):
    """ПЕРЕПИСАН 2026-08-07 (дефект №2) — обоснование целиком в
    test_add_task_comment_plan_missing_identity_refuses выше. Коротко:
    `missing` больше не покрывает «возможно, завершена» (для этого есть
    статус `completed` с проверенным названием), а значит означает «объекта
    нет ни в одном источнике» — прикреплять к нему файл нечего."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard("missing", project_id="p1"))

    preview = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                          url="https://x/file.pdf")

    assert preview.startswith("🛑 План НЕ построен"), preview
    assert "manifest_id" not in preview, preview
    assert fake_v2.calls == []


async def test_attach_file_to_task_missing_source_refused_before_gate(monkeypatch):
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2)
    result = await s.attach_file_to_task("Купить молоко", "t1", "p1")
    assert "url or content_base64" in result
    assert fake_v2.calls == []


# ---------------------------------------------------------------------------
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group A) —
# task_id↔task_title now cross-checked BEFORE the plan is built, not only
# inside _attach_file_to_task_impl on execution. See module docstring above
# for why the tests above this point could NOT have caught the old bug.
# ---------------------------------------------------------------------------

async def test_attach_file_to_task_plan_identity_guard_blocks_wrong_title(
        monkeypatch):
    """The exact defect this follow-up closes: id points at a REAL task
    ("Купить хлеб"), but the caller's task_title claims a DIFFERENT one
    ("Купить молоко") — before this fix, call #1 would have built and shown
    a plan card reading "Прикрепляю ... к задаче «Купить молоко»" even
    though the id has nothing to do with that task. Now the plan is refused
    outright, before any card is built."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard(
            "mismatch", project_id="p1", title="Купить хлеб",
            message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'))

    result = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                         url="https://x/file.pdf")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert fake_v2.calls == []


async def test_attach_file_to_task_plan_read_failure_does_not_block_but_warns(
        monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every attach — fail-open here is cheaper than refusing everyone whose
    network is briefly flaky. The plan is still built, honestly warns that
    the title was not verified, and the real (unchanged) identity-guard on
    execution (call #2) does its normal job right after."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                  # call #1 (plan)
        s._Guard("ok", project_id="p1", title="Купить молоко"),   # call #2 (_impl)
    ))

    preview = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                          url="https://x/file.pdf")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                         url="https://x/file.pdf",
                                         manifest_id=mid, user_reply="да")
    assert ("attach", "t1") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_attach_file_to_task_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside
    `_attach_file_to_task_impl` — untouched by this change — still catches
    the real mismatch: a network blip on planning must not weaken the
    protection at execution time."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                    # call #1 (plan)
        s._Guard("mismatch", project_id="p1", title="Купить хлеб",   # call #2 (_impl)
                message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'),
    ))

    preview = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                          url="https://x/file.pdf")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                         url="https://x/file.pdf",
                                         manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Купить хлеб»" in result
    assert fake_v2.calls == []


async def test_attach_file_to_task_automation_key_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false name+id pair
    would sail through silently on a single valid key. The check sits BEFORE
    _gate_single, so it applies here too: plan is refused, upload never
    attempted, and exactly one guard read happens (the plan-stage one) —
    proving it is the PLAN stage refusing, not the execution stage (which
    would print a different message, "НЕ прикрепил", and would only be
    reached after a mutation attempt)."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    calls = []

    def _stub(*a, **k):
        calls.append(1)
        return s._Guard("mismatch", project_id="p1", title="Купить хлеб",
                        message='id указывает на «Купить хлеб», а НЕ «Купить молоко»')
    monkeypatch.setattr(s, "_guard_task", _stub)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.attach_file_to_task("Купить молоко", "t1", "p1",
                                         url="https://x/file.pdf",
                                         automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert fake_v2.calls == []
    assert len(calls) == 1


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

    preview = await s.duplicate_task.direct("Дублирую задачу", "t1", "Купить молоко")
    assert fake_v2.calls == []
    mid = _extract_manifest_id(preview)

    result = await s.duplicate_task.direct("Дублирую задачу", "t1", "Купить молоко",
                                    manifest_id=mid, user_reply="да")
    assert ("duplicate", "t1") in fake_v2.calls
    assert "t1-copy" in fake_v2.live
    _assert_confirmed_success(result)


# ---------------------------------------------------------------------------
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group B) —
# task_id↔task_title now cross-checked BEFORE the plan is built, not only
# inside _duplicate_task_impl on execution. task_title is OPTIONAL for this
# tool (unlike the others in this file) — the guard still runs unconditionally
# (an empty title always "agrees", but a MISSING id still refuses), exactly
# mirroring what _duplicate_task_impl already does on execution.
# ---------------------------------------------------------------------------

async def test_duplicate_task_plan_identity_guard_blocks_wrong_title(
        monkeypatch):
    """id points at a REAL task ("Купить хлеб"), caller's task_title claims
    a DIFFERENT one ("Купить молоко") — before this fix, call #1 would have
    built and shown a plan card summarising a duplicate of the WRONG task.
    Now the plan is refused outright, before any card is built."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard(
            "mismatch", project_id="p1", title="Купить хлеб",
            message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'))

    result = await s.duplicate_task.direct("Дублирую задачу", "t1", "Купить молоко")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert fake_v2.calls == []


async def test_duplicate_task_plan_identity_guard_blocks_missing_task(
        monkeypatch):
    """_duplicate_task_impl treats a MISSING task as a hard 🛑 too (not a
    soft warning, unlike e.g. add_task_comment) — the plan-phase transfer
    must reproduce that same severity."""
    fake_v2 = FakeV2(live={})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard("missing", project_id="",
                                 message="id … не среди открытых задач"))

    result = await s.duplicate_task.direct("Дублирую задачу", "t-нет-такой", "Купить молоко")

    assert result.startswith("🛑 План НЕ построен")
    assert "manifest_id" not in result
    assert fake_v2.calls == []


async def test_duplicate_task_plan_identity_guard_blocks_missing_task_without_title(
        monkeypatch):
    """task_title is OPTIONAL — omitting it must NOT disarm the "id exists
    among open tasks" half of the guard (only the NAME comparison is
    skipped when the title is empty; the existence check still runs, in
    both _guard_task and _duplicate_task_impl)."""
    fake_v2 = FakeV2(live={})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard("missing", project_id="",
                                 message="id … не среди открытых задач"))

    result = await s.duplicate_task.direct("Дублирую задачу", "t-нет-такой")

    assert result.startswith("🛑 План НЕ построен")
    assert "manifest_id" not in result
    assert fake_v2.calls == []


async def test_duplicate_task_plan_read_failure_does_not_block_but_warns(
        monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every duplicate — fail-open here is cheaper than refusing everyone whose
    network is briefly flaky. The plan is still built, honestly warns that
    the task was not verified, and the real (unchanged) identity-guard on
    execution (call #2) does its normal job right after."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                  # call #1 (plan)
        s._Guard("ok", project_id="p1", title="Купить молоко"),   # call #2 (_impl)
    ))

    preview = await s.duplicate_task.direct("Дублирую задачу", "t1", "Купить молоко")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.duplicate_task.direct("Дублирую задачу", "t1", "Купить молоко",
                                    manifest_id=mid, user_reply="да")
    assert ("duplicate", "t1") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_duplicate_task_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside
    `_duplicate_task_impl` — untouched by this change — still catches the
    real mismatch: a network blip on planning must not weaken the
    protection at execution time."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                    # call #1 (plan)
        s._Guard("mismatch", project_id="p1", title="Купить хлеб",   # call #2 (_impl)
                message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'),
    ))

    preview = await s.duplicate_task.direct("Дублирую задачу", "t1", "Купить молоко")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.duplicate_task.direct("Дублирую задачу", "t1", "Купить молоко",
                                    manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Купить хлеб»" in result
    assert fake_v2.calls == []


async def test_duplicate_task_automation_key_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false name+id pair
    would sail through silently on a single valid key, duplicating the WRONG
    task. The check sits BEFORE _gate_single, so it applies here too."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    calls = []

    def _stub(*a, **k):
        calls.append(1)
        return s._Guard("mismatch", project_id="p1", title="Купить хлеб",
                        message='id указывает на «Купить хлеб», а НЕ «Купить молоко»')
    monkeypatch.setattr(s, "_guard_task", _stub)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.duplicate_task.direct("Дублирую задачу", "t1", "Купить молоко",
                                    automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert fake_v2.calls == []
    assert len(calls) == 1


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


async def test_update_task_comment_missing_identity_refuses(monkeypatch):
    """ПЕРЕПИСАН 2026-08-07 (дефект №2) — обоснование целиком в
    test_add_task_comment_plan_missing_identity_refuses выше. Правка
    комментария на объекте, которого нет ни в одном источнике, теперь
    отказ — и, что важнее, комментарий при этом НЕ ТРОГАЕТСЯ."""
    fake_v2 = FakeV2(live={}, comments={"t1": [{"id": "c1", "title": "старый текст"}]})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard("missing", project_id="p1"))

    preview = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1")

    assert preview.startswith("🛑 План НЕ построен"), preview
    assert "manifest_id" not in preview, preview
    assert fake_v2.comments["t1"][0]["title"] == "старый текст"


# ---------------------------------------------------------------------------
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group A) — see
# the equivalent block under attach_file_to_task above for the shared
# rationale; same code shape, same _guard_task helper.
# ---------------------------------------------------------------------------

async def test_update_task_comment_plan_identity_guard_blocks_wrong_title(
        monkeypatch):
    """id points at a REAL task ("Купить хлеб"), caller's task_title claims a
    DIFFERENT one ("Купить молоко") — before this fix, call #1 would have
    built and shown a plan card reading "Правлю комментарий на «Купить
    молоко»..." even though the id has nothing to do with that task. Now the
    plan is refused outright."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}},
                     comments={"t1": [{"id": "c1", "title": "старый текст"}]})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard(
            "mismatch", project_id="p1", title="Купить хлеб",
            message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'))

    result = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert fake_v2.comments["t1"][0]["title"] == "старый текст"
    assert fake_v2.calls == []


async def test_update_task_comment_plan_read_failure_does_not_block_but_warns(
        monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every comment edit — fail-open here is cheaper than refusing everyone
    whose network is briefly flaky. The plan is still built, honestly warns
    that the title was not verified, and the real (unchanged) identity-guard
    on execution (call #2) does its normal job right after."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}},
                     comments={"t1": [{"id": "c1", "title": "старый текст"}]})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                  # call #1 (plan)
        s._Guard("ok", project_id="p1", title="Купить молоко"),   # call #2 (_impl)
    ))

    preview = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1",
                                         manifest_id=mid, user_reply="да")
    assert ("update_comment", "c1", "новый текст") in fake_v2.calls
    _assert_confirmed_success(result)


async def test_update_task_comment_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside
    `_update_task_comment_impl` — untouched by this change — still catches
    the real mismatch: a network blip on planning must not weaken the
    protection at execution time."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}},
                     comments={"t1": [{"id": "c1", "title": "старый текст"}]})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                    # call #1 (plan)
        s._Guard("mismatch", project_id="p1", title="Купить хлеб",   # call #2 (_impl)
                message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'),
    ))

    preview = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1",
                                         manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Купить хлеб»" in result
    assert fake_v2.comments["t1"][0]["title"] == "старый текст"
    assert fake_v2.calls == []


async def test_update_task_comment_automation_key_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false name+id pair
    would sail through silently on a single valid key. The check sits BEFORE
    _gate_single, so it applies here too: plan is refused, edit never
    attempted, and exactly one guard read happens (the plan-stage one) —
    proving it is the PLAN stage refusing, not the execution stage (which
    would print a different message, "НЕ изменил комментарий", and would
    only be reached after a mutation attempt)."""
    fake_v2 = FakeV2(live={"t1": {"id": "t1", "title": "Купить хлеб", "projectId": "p1"}},
                     comments={"t1": [{"id": "c1", "title": "старый текст"}]})
    _wire(monkeypatch, fake_v2=fake_v2, guard_task=False)
    calls = []

    def _stub(*a, **k):
        calls.append(1)
        return s._Guard("mismatch", project_id="p1", title="Купить хлеб",
                        message='id указывает на «Купить хлеб», а НЕ «Купить молоко»')
    monkeypatch.setattr(s, "_guard_task", _stub)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.update_task_comment("Купить молоко", "новый текст", "p1", "t1", "c1",
                                         automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert fake_v2.comments["t1"][0]["title"] == "старый текст"
    assert fake_v2.calls == []
    assert len(calls) == 1
