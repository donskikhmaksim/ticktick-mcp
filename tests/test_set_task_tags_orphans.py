"""set_task_tags orphan-tag fix.

Bug (found by the overnight QA campaign): set_task_tags writes a raw tag
label straight onto a task via /batch/task. TickTick actually keeps tags in
TWO places — the account's own tag list (/batch/tag, what
list_tags/create_tag/delete_tag see) and the raw label string on the task
(/batch/task). Writing a brand-new label without registering it in the
account tag list first creates an ORPHAN tag: invisible to list_tags,
undeletable via delete_tag, but still attached to the task.

Fix implemented (variant б from the QA ticket): set_task_tags now registers
any not-yet-existing tag through the same account-level path create_tag uses
(ticktick_v2.create_tag) BEFORE writing it onto any task, and post-verifies
the registration itself (a fresh get_tags() read) rather than trusting the
200 from create_tag. If registration can't be confirmed for a tag, that
specific task-change is skipped entirely (fail-closed — see module docstring
in server.py's _set_task_tags_impl for why a partial/truncated tag list is
NOT sent instead).

These tests are pure-logic: a fake v2 client backs get_state/get_tags/
create_tag/batch_update_tasks with in-memory state, mirroring the pattern in
tests/test_slice1_real_gates.py's _FakeV2.
"""
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


class _FakeTagsV2:
    """Fake v2 client covering exactly what set_task_tags touches: the
    account tag list (get_state/get_tags/create_tag) and per-task tags
    (batch_update_tasks), backed by plain in-memory state so a fresh read
    after a mutation reflects it (real post-verify, not a stub echo)."""

    def __init__(self, live, tags=None, poison_tags=None):
        self.live = live
        self.tags = tags if tags is not None else []
        # tag names that silently fail to register (create_tag call goes
        # through with no exception, but the tag never actually appears —
        # simulates the API accepting-and-dropping, same class of bug as the
        # task-write path this whole fix is modeled on)
        self.poison_tags = set(poison_tags or ())
        self.calls = []

    def invalidate_cache(self):
        pass

    def get_state(self, force=False):
        return {"tags": self.tags}

    def get_tags(self):
        return self.tags

    def create_tag(self, name, color=None):
        """Двойник настоящего TickTickV2Client.create_tag: он приводит имя к
        нижнему регистру и НЕ срезает решётку — срезать её обязан вызывающий.

        Раньше двойник делал `name.lstrip("#").lower()`, то есть брал на себя
        работу сервера: убери нормализацию из server.py — и тесты остались бы
        зелёными, а в TickTick уехал бы тег-фантом «#работа» (по решётке они
        не совпадают, и такой тег не виден в списке и не удаляется)."""
        stored = name.lower()
        self.calls.append(("create_tag", stored))
        if stored in self.poison_tags:
            return {}  # "succeeds" but never actually registers
        if not any((t.get("name") or "").lower() == stored for t in self.tags):
            self.tags.append({"name": stored, "label": name, "color": color})
        return {}

    def batch_update_tasks(self, changes):
        self.calls.append(("update", changes))
        for c in changes:
            t = self.live.setdefault(c["taskId"], {"id": c["taskId"]})
            if "tags" in c:
                t["tags"] = c["tags"]
        return {}


def _wire(monkeypatch, live, tags=None, poison_tags=None, names=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: names or {"p1": "Работа"})
    fake = _FakeTagsV2(live, tags, poison_tags)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


# ===========================================================================
# 1. New tag: flagged in the plan preview, registered before the task write,
#    and the registration is itself post-verified (visible in get_tags()).
# ===========================================================================

async def test_new_tag_flagged_in_preview_as_will_be_created(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1", "tags": []}}
    fake = _wire(monkeypatch, live, tags=[])

    preview = await s.set_task_tags(
        "Ставлю тег", [{"taskId": "t1", "title": "Купить молоко", "tags": ["новый"]}])
    assert fake.calls == []  # call #1 is read-only, nothing mutated
    assert "будет создан" in preview
    assert "новый" in preview


async def test_new_tag_registered_before_task_write_and_post_verified(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1", "tags": []}}
    fake = _wire(monkeypatch, live, tags=[])

    preview = await s.set_task_tags(
        "Ставлю тег", [{"taskId": "t1", "title": "Купить молоко", "tags": ["новый"]}])
    mid = _extract_manifest_id(preview)
    result = await s.set_task_tags("Ставлю тег", manifest_id=mid, user_reply="да")

    # 1. The tag is now a REAL account tag (root-cause fix, not just a
    #    warning) — visible via get_tags(), the same thing list_tags reads.
    assert any(t.get("name") == "новый" for t in fake.tags)
    # 2. create_tag ran BEFORE batch_update_tasks (registration precedes the
    #    task write, so the task never carries an unregistered label even
    #    transiently).
    kinds = [c[0] for c in fake.calls]
    assert kinds.index("create_tag") < kinds.index("update")
    # 3. The task actually carries the tag.
    assert live["t1"]["tags"] == ["новый"]
    # 4. The response says so, in Russian, and is not a hard refusal.
    assert "🛑" not in result
    assert "Новые теги зарегистрированы" in result
    assert "новый" in result


async def test_already_existing_tag_is_not_re_registered(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1", "tags": []}}
    fake = _wire(monkeypatch, live, tags=[{"name": "дом", "label": "дом"}])

    preview = await s.set_task_tags(
        "Ставлю тег", [{"taskId": "t1", "title": "Купить молоко", "tags": ["дом"]}])
    assert "будет создан" not in preview
    mid = _extract_manifest_id(preview)
    result = await s.set_task_tags("Ставлю тег", manifest_id=mid, user_reply="да")

    assert ("create_tag", "дом") not in fake.calls
    assert live["t1"]["tags"] == ["дом"]
    assert "Новые теги зарегистрированы" not in result


# ===========================================================================
# 2. Registration failure: fail-closed, no orphan is ever created — the
#    whole per-task change is skipped rather than a truncated tag list sent.
# ===========================================================================

async def test_failed_registration_skips_the_task_entirely(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1",
                   "tags": ["старый"]}}
    fake = _wire(monkeypatch, live, tags=[{"name": "старый", "label": "старый"}],
                poison_tags={"призрак"})

    preview = await s.set_task_tags(
        "Ставлю тег",
        [{"taskId": "t1", "title": "Купить молоко", "tags": ["призрак"]}])
    mid = _extract_manifest_id(preview)
    result = await s.set_task_tags("Ставлю тег", manifest_id=mid, user_reply="да")

    # The poison tag never made it into the account tag list...
    assert not any(t.get("name") == "призрак" for t in fake.tags)
    # ...and — the actual orphan-prevention guarantee — never got written
    # onto the task either. The task's ORIGINAL tags are untouched, not
    # wiped to [] and not left with a phantom "призрак" label.
    assert live["t1"]["tags"] == ["старый"]
    update_calls = [c for c in fake.calls if c[0] == "update"]
    assert update_calls == []  # batch_update_tasks was never even called
    assert "🛑" in result
    assert "призрак" in result
    assert "Купить молоко" in result


async def test_partial_failure_only_skips_the_affected_task(monkeypatch):
    live = {
        "t1": {"id": "t1", "title": "Задача A", "projectId": "p1", "tags": []},
        "t2": {"id": "t2", "title": "Задача B", "projectId": "p1", "tags": []},
    }
    _wire(monkeypatch, live, tags=[], poison_tags={"призрак"})

    preview = await s.set_task_tags("Ставлю теги", [
        {"taskId": "t1", "title": "Задача A", "tags": ["хороший"]},
        {"taskId": "t2", "title": "Задача B", "tags": ["призрак"]},
    ])
    mid = _extract_manifest_id(preview)
    result = await s.set_task_tags("Ставлю теги", manifest_id=mid, user_reply="да")

    assert live["t1"]["tags"] == ["хороший"]  # good tag applied normally
    assert live["t2"]["tags"] == []  # untouched — bad tag never written
    assert "Задача A" in result and "хороший" in result
    assert "Задача B" in result  # shows up in the skipped-tasks report
    assert "🛑" in result


# ===========================================================================
# 3. Post-verify honesty: the report reflects the REAL live state, not the
#    request — a task that ends up with the wrong tag set is reported ❌,
#    never silently smoothed over into a ✅/🏷 success line.
# ===========================================================================

async def test_task_write_mismatch_after_registration_is_reported_as_failure(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1", "tags": []}}
    fake = _wire(monkeypatch, live, tags=[])

    # Sabotage the task write itself (independent of tag registration): make
    # batch_update_tasks a no-op so the live state never actually changes,
    # like a silently-dropped TickTick write.
    def _noop_update(changes):
        fake.calls.append(("update", changes))
        return {}
    fake.batch_update_tasks = _noop_update

    preview = await s.set_task_tags(
        "Ставлю тег", [{"taskId": "t1", "title": "Купить молоко", "tags": ["новый"]}])
    mid = _extract_manifest_id(preview)
    result = await s.set_task_tags("Ставлю тег", manifest_id=mid, user_reply="да")

    # Registration still succeeded and is reported...
    assert any(t.get("name") == "новый" for t in fake.tags)
    assert "Новые теги зарегистрированы" in result
    # ...but the task-level write is honestly reported as NOT applied, since
    # live tags never actually changed.
    assert "❌" in result
    assert live["t1"]["tags"] == []


async def test_hash_and_case_are_normalised_by_the_server_not_by_the_double(
        monkeypatch):
    """Ни один тест файла не подавал тег с решёткой или заглавной буквой —
    все входы («новый», «дом») уже были нормализованы руками. Нормализацию
    при этом делал ещё и двойник, так что убери её из server.py — и всё
    осталось бы зелёным, а в TickTick уехал бы тег-фантом «#работа»:
    в списке тегов он не виден, через delete_tag не удаляется, а на задаче
    висит."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1",
                   "tags": []}}
    fake = _wire(monkeypatch, live, tags=[])

    preview = await s.set_task_tags(
        "Ставлю тег",
        [{"taskId": "t1", "title": "Купить молоко", "tags": ["#Работа"]}])
    mid = _extract_manifest_id(preview)
    await s.set_task_tags("Ставлю тег", manifest_id=mid, user_reply="да")

    registered = [c[1] for c in fake.calls if c[0] == "create_tag"]
    assert registered == ["работа"], (
        f"в аккаунт зарегистрирован тег-фантом: {registered}")
    written = [c[1] for c in fake.calls if c[0] == "update"]
    assert written and written[0][0]["tags"] == ["работа"], (
        f"на задачу записан ненормализованный тег: {written}")
    assert [t["name"] for t in fake.tags] == ["работа"]
