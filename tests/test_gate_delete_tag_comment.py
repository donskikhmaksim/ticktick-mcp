"""Гейт-тесты на delete_tag и delete_task_comment — ночная QA-кампания нашла,
что оба исполнялись ОДНИМ вызовом, без plan→execute гейта вообще, хотя менее
деструктивные соседи (create_tag, add_task_comment, update_task_comment) уже
были гейтованы через _gate_single. Паттерн теста — как в
tests/test_tier0_gate_conversion.py: call #1 (без manifest_id) только строит
план и ничего не мутирует; call #2 (manifest_id + user_reply) реально
исполняет на "да" и жёстко отказывает (с инвалидацией манифеста) на "нет".

Identity-guard/existence-check/post-verify — уже существовавшая логика
(перенесена в _delete_tag_impl/_delete_task_comment_impl дословно) и
подробно не ретестируется здесь; фокус — на НОВОЙ обёртке гейта.

EXCEPTION (2026-08-07, group A of the def-116 follow-up): delete_task_comment
is DIFFERENT from delete_tag above — for delete_task_comment, identity-guard
is NOT "unchanged pre-existing logic confined to _x_impl" anymore. Before
this date the claim above was literally true for it too, and it was a bug:
`_guard_task` ran ONLY inside `_delete_task_comment_impl` (call #2,
execution), so the plan card shown on call #1 printed `task_title` straight
from the caller with ZERO verification against the live task the id actually
points at. `test_delete_task_comment_missing_identity_downgrades_to_warn`
below passed a `_guard_task` stand-in that never distinguished call #1 from
call #2 (same result on both calls) and only checked the outcome on call #2
— it would have happily let a "mismatch" stand-in build a plan carrying a
WRONG title too, and nothing in this file would have caught it. Live audit
(delete_habit/def-116, commit ea2a47c) found the same class of bug in a
sibling tool; this one is fixed the same way: `_guard_task` is now ALSO
called while BUILDING the plan (call #1) — a mismatched title refuses the
plan outright; a live-read hiccup does NOT block the plan (fail-open) but
warns honestly; the unchanged execution-side guard in `_delete_task_comment_
impl` still catches a real mismatch independently. See the new
`test_delete_task_comment_plan_identity_guard_*` tests below the existing
delete_task_comment ones — THOSE exercise the plan-phase check. delete_tag
is UNCHANGED by this follow-up — the "not re-tested here" claim above is
still accurate for it.

No real network — the v2 client is faked."""
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


def _ok_guard(*_a, **_k):
    return s._Guard("ok", project_id="p1", title="Купить молоко")


def _guard_sequence(*results):
    """`_guard_task` stand-in that returns `results` in order, one per call —
    see the identical helper (and its docstring) in
    tests/test_tier0_gate_conversion.py for the full rationale."""
    it = iter(results)

    def _stub(*_a, **_k):
        return next(it)
    return _stub


class FakeV2:
    """Минимальный фейк v2-клиента для delete_tag/delete_task_comment,
    мутирующий общее состояние — так post-verify (неизменённая логика) видит
    реальный эффект мутации."""

    def __init__(self, tags=None, tag_carriers=None, comments=None):
        self.tags = tags if tags is not None else []
        self.tag_carriers = tag_carriers or []
        self.comments = comments or {}
        self.calls = []

    # --- tags ---
    def get_state(self, force=False):
        return {}

    def get_tags(self):
        return list(self.tags)

    def get_tasks_by_tag(self, name):
        self.calls.append(("get_tasks_by_tag", name))
        return list(self.tag_carriers)

    def delete_tag(self, name):
        self.calls.append(("delete_tag", name))
        self.tags = [t for t in self.tags
                    if (t.get("name") or "").lower() != name.lower()]

    # --- comments ---
    def get_task_comments(self, project_id, task_id):
        return list(self.comments.get(task_id, []))

    def delete_task_comment(self, project_id, task_id, comment_id):
        self.calls.append(("delete_comment", task_id, comment_id))
        self.comments[task_id] = [
            c for c in self.comments.get(task_id, []) if c.get("id") != comment_id]


def _wire(monkeypatch, fake_v2, guard_task=True):
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    if guard_task:
        monkeypatch.setattr(s, "_guard_task", _ok_guard)


# ===========================================================================
# delete_tag
# ===========================================================================

async def test_delete_tag_call1_previews_nothing_deleted(monkeypatch):
    fake = FakeV2(tags=[{"name": "срочное"}])
    _wire(monkeypatch, fake)

    preview = await s.delete_tag("срочное")
    assert fake.calls == []
    assert any(t["name"] == "срочное" for t in fake.tags)
    assert "manifest_id" in preview
    assert "«срочное»" in preview


async def test_delete_tag_call2_empty_reply_refused_and_retryable(monkeypatch):
    fake = FakeV2(tags=[{"name": "срочное"}])
    _wire(monkeypatch, fake)

    preview = await s.delete_tag("срочное")
    mid = _extract_manifest_id(preview)

    refused = await s.delete_tag("срочное", manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert fake.calls == []
    assert any(t["name"] == "срочное" for t in fake.tags)

    result = await s.delete_tag("срочное", manifest_id=mid, user_reply="да")
    assert ("delete_tag", "срочное") in fake.calls
    assert not any(t["name"] == "срочное" for t in fake.tags)
    assert "🛑" not in result
    assert "✅" in result


async def test_delete_tag_explicit_no_refuses_and_burns_manifest(monkeypatch):
    fake = FakeV2(tags=[{"name": "срочное"}])
    _wire(monkeypatch, fake)

    preview = await s.delete_tag("срочное")
    mid = _extract_manifest_id(preview)

    refused = await s.delete_tag("срочное", manifest_id=mid, user_reply="нет, погоди")
    assert "🛑" in refused
    assert fake.calls == []
    assert any(t["name"] == "срочное" for t in fake.tags)

    # manifest is now dead — even a genuine "yes" afterwards must fail
    dead = await s.delete_tag("срочное", manifest_id=mid, user_reply="да")
    assert "🛑" in dead
    assert fake.calls == []
    assert any(t["name"] == "срочное" for t in fake.tags)


async def test_delete_tag_manifest_is_one_shot(monkeypatch):
    fake = FakeV2(tags=[{"name": "срочное"}])
    _wire(monkeypatch, fake)

    preview = await s.delete_tag("срочное")
    mid = _extract_manifest_id(preview)
    await s.delete_tag("срочное", manifest_id=mid, user_reply="да")
    calls_after_first = len(fake.calls)

    again = await s.delete_tag("срочное", manifest_id=mid, user_reply="да")
    assert "🛑" in again
    assert len(fake.calls) == calls_after_first


# ===========================================================================
# delete_task_comment
# ===========================================================================

async def test_delete_task_comment_call1_previews_nothing_deleted(monkeypatch):
    fake = FakeV2(comments={"t1": [{"id": "c1", "title": "не забыть"}]})
    _wire(monkeypatch, fake)

    preview = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1")
    assert fake.calls == []
    assert fake.comments["t1"]
    assert "manifest_id" in preview
    assert "«Купить молоко»" in preview


async def test_delete_task_comment_call2_empty_reply_refused_and_retryable(monkeypatch):
    fake = FakeV2(comments={"t1": [{"id": "c1", "title": "не забыть"}]})
    _wire(monkeypatch, fake)

    preview = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1")
    mid = _extract_manifest_id(preview)

    refused = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                          manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert fake.calls == []
    assert fake.comments["t1"]

    result = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                         manifest_id=mid, user_reply="да")
    assert ("delete_comment", "t1", "c1") in fake.calls
    assert fake.comments["t1"] == []
    assert "🛑" not in result
    # Регресс-тест дефекта №3 (2026-08-06, тот же класс бага, что нашёл живой
    # прогон create_project_group, манифест ea79556baf0f): удаление
    # комментария подтверждено post-verify (список перечитан, комментария
    # больше нет), но старый текст не начинался с ✅ ("Comment on '...'
    # deleted") — кнопочный вердикт был бы ложным "❓ НЕ подтверждено".
    assert s._auto_execute_report_is_success(result), result


async def test_delete_task_comment_missing_identity_downgrades_to_warn(monkeypatch):
    """Симметрично update_task_comment: когда identity-guard не смог сверить
    название (id не среди открытых задач), маркер — ⚠️, а не ✅, даже если
    само удаление комментария подтвердилось. ✅ значит «подтверждено
    ПОЛНОСТЬЮ» — раздавать его на неполной проверке нельзя."""
    fake = FakeV2(comments={"t1": [{"id": "c1", "title": "не забыть"}]})
    _wire(monkeypatch, fake, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard("missing", project_id="p1"))

    preview = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1")
    mid = _extract_manifest_id(preview)
    result = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                         manifest_id=mid, user_reply="да")

    assert fake.comments["t1"] == []
    assert result.startswith("⚠️"), result
    assert not s._auto_execute_report_is_success(result), result


async def test_delete_task_comment_explicit_no_refuses_and_burns_manifest(monkeypatch):
    fake = FakeV2(comments={"t1": [{"id": "c1", "title": "не забыть"}]})
    _wire(monkeypatch, fake)

    preview = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1")
    mid = _extract_manifest_id(preview)

    refused = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                          manifest_id=mid, user_reply="нет, стоп")
    assert "🛑" in refused
    assert fake.calls == []
    assert fake.comments["t1"]

    dead = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                       manifest_id=mid, user_reply="да")
    assert "🛑" in dead
    assert fake.calls == []
    assert fake.comments["t1"]


async def test_delete_task_comment_manifest_is_one_shot(monkeypatch):
    fake = FakeV2(comments={"t1": [{"id": "c1", "title": "не забыть"}]})
    _wire(monkeypatch, fake)

    preview = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1")
    mid = _extract_manifest_id(preview)
    await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                manifest_id=mid, user_reply="да")
    calls_after_first = len(fake.calls)

    again = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                        manifest_id=mid, user_reply="да")
    assert "🛑" in again
    assert len(fake.calls) == calls_after_first


# ---------------------------------------------------------------------------
# 2026-08-07: plan-phase identity-guard (def-116 follow-up, group A) — see
# the module docstring above and the equivalent block under
# attach_file_to_task/update_task_comment in test_tier0_gate_conversion.py
# for the shared rationale; same code shape, same _guard_task helper.
# ---------------------------------------------------------------------------

async def test_delete_task_comment_plan_identity_guard_blocks_wrong_title(
        monkeypatch):
    """id points at a REAL task ("Купить хлеб"), caller's task_title claims a
    DIFFERENT one ("Купить молоко") — before this fix, call #1 would have
    built and shown a plan card reading "Удаляю комментарий на «Купить
    молоко»" even though the id has nothing to do with that task, on an
    IRREVERSIBLE operation. Now the plan is refused outright."""
    fake = FakeV2(comments={"t1": [{"id": "c1", "title": "не забыть"}]})
    _wire(monkeypatch, fake, guard_task=False)
    monkeypatch.setattr(
        s, "_guard_task",
        lambda *a, **k: s._Guard(
            "mismatch", project_id="p1", title="Купить хлеб",
            message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'))

    result = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert "manifest_id" not in result, "план для несовпавшей пары строиться не должен"
    assert fake.comments["t1"]
    assert fake.calls == []


async def test_delete_task_comment_plan_read_failure_does_not_block_but_warns(
        monkeypatch):
    """A live-read hiccup while BUILDING the plan (call #1) must not block
    every comment deletion — fail-open here is cheaper than refusing
    everyone whose network is briefly flaky. The plan is still built,
    honestly warns that the title was not verified, and the real (unchanged)
    identity-guard on execution (call #2) does its normal job right after."""
    fake = FakeV2(comments={"t1": [{"id": "c1", "title": "не забыть"}]})
    _wire(monkeypatch, fake, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                  # call #1 (plan)
        s._Guard("ok", project_id="p1", title="Купить молоко"),   # call #2 (_impl)
    ))

    preview = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1")
    assert "🛑" not in preview, "временный сбой чтения не должен блокировать план"
    assert "НЕ удалось сверить" in preview
    mid = _extract_manifest_id(preview)

    result = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                         manifest_id=mid, user_reply="да")
    assert ("delete_comment", "t1", "c1") in fake.calls
    assert fake.comments["t1"] == []
    assert "🛑" not in result
    assert s._auto_execute_report_is_success(result), result


async def test_delete_task_comment_plan_read_failure_still_lets_execution_catch_a_real_mismatch(
        monkeypatch):
    """Same read failure on the plan as above, but this time the pair
    actually DOESN'T match. The plan-phase check couldn't run (so it warns
    instead of refusing), but the execution-phase guard inside
    `_delete_task_comment_impl` — untouched by this change — still catches
    the real mismatch: a network blip on planning must not weaken the
    protection at execution time, especially on an irreversible delete."""
    fake = FakeV2(comments={"t1": [{"id": "c1", "title": "не забыть"}]})
    _wire(monkeypatch, fake, guard_task=False)
    monkeypatch.setattr(s, "_guard_task", _guard_sequence(
        s._Guard("unavailable"),                                    # call #1 (plan)
        s._Guard("mismatch", project_id="p1", title="Купить хлеб",   # call #2 (_impl)
                message='id указывает на «Купить хлеб», а НЕ «Купить молоко»'),
    ))

    preview = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1")
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    result = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                         manifest_id=mid, user_reply="да")
    assert result.startswith("🛑")
    assert "«Купить хлеб»" in result
    assert fake.comments["t1"]
    assert fake.calls == []


async def test_delete_task_comment_automation_key_mismatch_is_refused_before_plan(
        monkeypatch):
    """Headless path (#118): a valid automation_key runs on the FIRST call,
    with no plan card and no Telegram button ever shown — so if the identity
    check only lived inside _gate_single/execution, a false name+id pair
    would sail through silently on a single valid key, deleting the WRONG
    comment. The check sits BEFORE _gate_single, so it applies here too:
    plan is refused, deletion never attempted, and exactly one guard read
    happens (the plan-stage one) — proving it is the PLAN stage refusing,
    not the execution stage (which would print a different message, "НЕ
    удалил комментарий", and would only be reached after a mutation
    attempt)."""
    fake = FakeV2(comments={"t1": [{"id": "c1", "title": "не забыть"}]})
    _wire(monkeypatch, fake, guard_task=False)
    calls = []

    def _stub(*a, **k):
        calls.append(1)
        return s._Guard("mismatch", project_id="p1", title="Купить хлеб",
                        message='id указывает на «Купить хлеб», а НЕ «Купить молоко»')
    monkeypatch.setattr(s, "_guard_task", _stub)
    monkeypatch.setattr(s, "SECRET", "test-secret")

    result = await s.delete_task_comment("Купить молоко", "p1", "t1", "c1",
                                         automation_key="test-secret")

    assert result.startswith("🛑 План НЕ построен")
    assert "«Купить хлеб»" in result
    assert fake.comments["t1"]
    assert fake.calls == []
    assert len(calls) == 1
