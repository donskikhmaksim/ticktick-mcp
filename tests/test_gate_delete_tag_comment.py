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

No real network — the v2 client is faked."""
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


def _ok_guard(*_a, **_k):
    return s._Guard("ok", project_id="p1", title="Купить молоко")


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
