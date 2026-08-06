"""Tests for the TG auto-execute layer added 2026-08-05 (Maksim: "нажал
кнопку в Telegram — должно сразу исполниться на бэке, не ждать повторного
вызова моделью") — the Python port of gmail-mcp's consent.ts's
tryAutoExecute/TG_AUTO_REPLY_MARKER and http.ts's runAutoExecutePoller.

Covers:
  - tg_approval.try_auto_execute (pure, callback-based — no Postgres/network)
  - tg_approval.report_auto_execution_result (Telegram editMessageText, faked)
  - server.py's candidate finder (_find_tg_auto_execute_candidates),
    the atomic consume step (_consume_manifest_for_auto_execute), and the
    registered executors for delete_tasks / execute_declutter
    (_tg_auto_execute_tick, end to end with fake TickTick clients)

No real network, no real Postgres — tg_approval's Postgres-backed functions
(check_approval/get_tg_approval/notify_plan) and the Telegram HTTP call are
monkeypatched throughout, same convention as test_tg_approval.py."""
import time

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


@pytest.fixture(autouse=True)
def _clean_manifests():
    """_MANIFESTS is a module-level global shared across the whole test
    session — _prune_manifests() (called by _find_tg_auto_execute_candidates)
    assumes every entry has a "created" key, so a leftover entry without one
    (from an unrelated test elsewhere in the suite, or a previous test in
    this file) breaks EVERY later call. Snapshot/restore keeps this file
    self-contained regardless of run order."""
    before = dict(s._MANIFESTS)
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)


# ===========================================================================
# tg_approval.try_auto_execute — pure logic, fake callbacks
# ===========================================================================

def _manifest(consumed=False, object_hash="h1", tool_ok=True):
    return {"kind": "delete", "consumed": consumed, "object_hash": object_hash,
            "items": [{"taskId": "t1", "title": "X"}]}


def test_try_auto_execute_happy_path_consumes_and_returns_manifest():
    m = _manifest()
    consumed_calls = []

    def consume(mid):
        m["consumed"] = True
        consumed_calls.append(mid)
        return m

    result = tg.try_auto_execute(
        manifest_id="m1", tool="delete_tasks",
        get_manifest=lambda mid: m,
        consume_manifest=consume,
        rehash=lambda mm: "h1",
    )
    assert result is m
    assert consumed_calls == ["m1"]


def test_try_auto_execute_missing_manifest_returns_none():
    result = tg.try_auto_execute(
        manifest_id="ghost", tool="delete_tasks",
        get_manifest=lambda mid: None,
        consume_manifest=lambda mid: (_ for _ in ()).throw(AssertionError("must not be called")),
        rehash=lambda mm: "h1",
    )
    assert result is None


def test_try_auto_execute_already_consumed_returns_none():
    m = _manifest(consumed=True)
    result = tg.try_auto_execute(
        manifest_id="m1", tool="delete_tasks",
        get_manifest=lambda mid: m,
        consume_manifest=lambda mid: (_ for _ in ()).throw(AssertionError("must not be called")),
        rehash=lambda mm: "h1",
    )
    assert result is None


def test_try_auto_execute_binding_mismatch_refuses_without_consuming():
    m = _manifest(object_hash="h1")
    consume_called = {"n": 0}

    def consume(mid):
        consume_called["n"] += 1
        return m

    result = tg.try_auto_execute(
        manifest_id="m1", tool="delete_tasks",
        get_manifest=lambda mid: m,
        consume_manifest=consume,
        rehash=lambda mm: "DIFFERENT",  # live state drifted since planning
    )
    assert result is None
    assert consume_called["n"] == 0  # never consumed — nothing executed


def test_try_auto_execute_wrong_tool_tag_refuses():
    m = dict(_manifest(), _auto_tool="execute_declutter")
    result = tg.try_auto_execute(
        manifest_id="m1", tool="delete_tasks",
        get_manifest=lambda mid: m,
        consume_manifest=lambda mid: (_ for _ in ()).throw(AssertionError("must not be called")),
        rehash=lambda mm: "h1",
    )
    assert result is None


def test_try_auto_execute_consume_race_returns_none():
    """Two poller ticks (or a tick racing a model-driven execute call) both
    see the manifest as live; only one consume_manifest call may "win" — the
    store-side callback is what enforces that (a real one is an atomic flip),
    modelled here as returning None the 2nd time."""
    m = _manifest()
    calls = {"n": 0}

    def consume(mid):
        calls["n"] += 1
        if calls["n"] > 1:
            return None
        return m

    first = tg.try_auto_execute(manifest_id="m1", tool="delete_tasks",
                                get_manifest=lambda mid: m,
                                consume_manifest=consume, rehash=lambda mm: "h1")
    second = tg.try_auto_execute(manifest_id="m1", tool="delete_tasks",
                                 get_manifest=lambda mid: m,
                                 consume_manifest=consume, rehash=lambda mm: "h1")
    assert first is m
    assert second is None


# ===========================================================================
# tg_approval.report_auto_execution_result — Telegram editMessageText, faked
# ===========================================================================

_CFG = tg.TgApprovalConfig(enabled=True, bot_token="x", owner_chat_id="1",
                           server="ticktick", tools_allowlist=None, ttl_s=3600)


def test_report_auto_execution_result_calls_edit_message_text(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: calls.append((method, body)) or {"ok": True})
    tg.report_auto_execution_result(_CFG, "chat1", 42, "### ✅ Готово")
    assert len(calls) == 1
    method, body = calls[0]
    assert method == "editMessageText"
    assert body["chat_id"] == "chat1"
    assert body["message_id"] == 42
    assert body["reply_markup"] == {"inline_keyboard": []}
    assert "Готово" in body["text"]


def test_report_auto_execution_result_no_message_id_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: calls.append(1) or {"ok": True})
    tg.report_auto_execution_result(_CFG, "chat1", None, "### ✅ Готово")
    assert calls == []


def test_report_auto_execution_result_tg_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: {"ok": False, "description": "boom"})
    # must not raise
    tg.report_auto_execution_result(_CFG, "chat1", 42, "### ✅ Готово")


# ===========================================================================
# server.py — _consume_manifest_for_auto_execute (the atomic step)
# ===========================================================================

def test_consume_manifest_for_auto_execute_one_shot():
    s._MANIFESTS["cx1"] = {"kind": "delete", "consumed": False, "created": time.monotonic()}
    first = s._consume_manifest_for_auto_execute("cx1")
    assert first is not None
    assert s._MANIFESTS["cx1"]["consumed"] is True
    second = s._consume_manifest_for_auto_execute("cx1")
    assert second is None


def test_consume_manifest_for_auto_execute_unknown_id():
    assert s._consume_manifest_for_auto_execute("does-not-exist") is None


# ===========================================================================
# server.py — _find_tg_auto_execute_candidates
# ===========================================================================

def _enable_tg(monkeypatch, allowlist=None):
    fields = dict(enabled=True, bot_token="x", owner_chat_id="1",
                  server="ticktick", tools_allowlist=allowlist, ttl_s=3600)
    # TgApprovalConfig прирастает полями (reports_chat_id/reap_enabled,
    # 2026-08-06) — подставляем их только если они реально объявлены, чтобы
    # тест не ломался ни на старой, ни на новой версии конфига.
    extra = {"reports_chat_id": "-100999", "reap_enabled": True}
    known = getattr(tg.TgApprovalConfig, "__dataclass_fields__", {})
    fields.update({k: v for k, v in extra.items() if k in known})
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(**fields))


def _fake_report_sinks(monkeypatch):
    """Подменяет ДВА получателя итога автоисполнения: группу «MCP Отчёты»
    (полный отчёт) и личку владельца (короткая сводка). raising=False — эти
    функции появляются в tg_approval параллельной правкой, тест не должен
    зависеть от порядка слияния веток."""
    group, private = [], []

    def _post(cfg, manifest_id, report_md, *, tool, verdict):
        group.append({"manifest_id": manifest_id, "report_md": report_md,
                      "tool": tool, "verdict": verdict})
        return [1001]  # id доставленных сообщений — непустой список = ok

    def _summarize(cfg, chat_id, message_id, short_md):
        private.append((chat_id, message_id, short_md))

    monkeypatch.setattr(tg, "post_report_to_group", _post, raising=False)
    monkeypatch.setattr(tg, "summarize_in_owner_chat", _summarize, raising=False)
    return group, private


def test_find_candidates_empty_when_tg_disabled(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    now = time.monotonic()
    s._MANIFESTS["cand1"] = {"kind": "delete", "consumed": False, "created": now,
                             "items": [{"taskId": "t1", "title": "X"}]}
    monkeypatch.setattr(tg, "check_approval", lambda mid: "approved")
    assert s._find_tg_auto_execute_candidates() == []


def test_find_candidates_skips_unapproved(monkeypatch):
    _enable_tg(monkeypatch)
    now = time.monotonic()
    s._MANIFESTS["cand2"] = {"kind": "delete", "consumed": False, "created": now,
                             "items": [{"taskId": "t1", "title": "X"}]}
    monkeypatch.setattr(tg, "check_approval", lambda mid: "pending")
    assert s._find_tg_auto_execute_candidates() == []


def test_find_candidates_skips_consumed(monkeypatch):
    # Note: _MANIFESTS is a shared module-level global across the whole test
    # session, so assertions below filter to OUR OWN planted id rather than
    # asserting the full candidate list is empty — an unrelated leftover
    # manifest from another test file (running earlier in the same session)
    # would otherwise make this test flaky depending on run order.
    _enable_tg(monkeypatch)
    now = time.monotonic()
    s._MANIFESTS["cand3"] = {"kind": "delete", "consumed": True, "created": now,
                             "items": [{"taskId": "t1", "title": "X"}]}
    # Only claim "approved" for OUR planted id — a leftover manifest from
    # another test would otherwise also read as approved and, since it's not
    # actually gated (store_ready() is False in this process), trip a real
    # tg_approval.get_tg_approval() Postgres call.
    monkeypatch.setattr(tg, "check_approval", lambda mid: "approved" if mid == "cand3" else "none")
    ids = {c["manifest_id"] for c in s._find_tg_auto_execute_candidates()}
    assert "cand3" not in ids


def test_find_candidates_skips_kind_with_no_registered_tool(monkeypatch):
    _enable_tg(monkeypatch)
    now = time.monotonic()
    s._MANIFESTS["cand4"] = {"kind": "create", "consumed": False, "created": now}
    monkeypatch.setattr(tg, "check_approval", lambda mid: "approved" if mid == "cand4" else "none")
    ids = {c["manifest_id"] for c in s._find_tg_auto_execute_candidates()}
    assert "cand4" not in ids


def test_find_candidates_respects_allowlist(monkeypatch):
    _enable_tg(monkeypatch, allowlist={"execute_declutter"})
    now = time.monotonic()
    s._MANIFESTS["cand5"] = {"kind": "delete", "consumed": False, "created": now,
                             "items": [{"taskId": "t1", "title": "X"}]}
    monkeypatch.setattr(tg, "check_approval", lambda mid: "approved" if mid == "cand5" else "none")
    ids = {c["manifest_id"] for c in s._find_tg_auto_execute_candidates()}
    assert "cand5" not in ids  # delete_tasks not in allowlist


def test_find_candidates_finds_approved_delete(monkeypatch):
    _enable_tg(monkeypatch)
    now = time.monotonic()
    s._MANIFESTS["cand6"] = {"kind": "delete", "consumed": False, "created": now,
                             "items": [{"taskId": "t1", "title": "X"}]}
    monkeypatch.setattr(tg, "check_approval", lambda mid: "approved")
    monkeypatch.setattr(tg, "get_tg_approval",
                        lambda mid: {"chat_id": "c1", "message_id": 99})
    out = [c for c in s._find_tg_auto_execute_candidates() if c["manifest_id"] == "cand6"]
    assert len(out) == 1
    assert out[0]["tool"] == "delete_tasks"
    assert out[0]["chat_id"] == "c1"
    assert out[0]["message_id"] == 99


# ===========================================================================
# server.py — end-to-end tick: delete_tasks
# ===========================================================================

class _FakeV2Delete:
    def __init__(self, live):
        self.live = live

    def get_state(self, force=False):
        pass

    def get_open_tasks(self):
        return list(self.live.values())

    def batch_delete_tasks(self, items):
        for it in items:
            self.live.pop(it["taskId"], None)
        return {}

    def invalidate_cache(self):
        pass


def test_tick_auto_executes_approved_delete_and_reports(monkeypatch):
    import asyncio

    _enable_tg(monkeypatch)
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    monkeypatch.setattr(s, "ticktick_v2", _FakeV2Delete(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})

    mid = "e2e-del-1"
    s._MANIFESTS[mid] = {
        "kind": "delete", "consumed": False, "created": time.monotonic(),
        "object_hash": s._manifest_object_hash("delete", ["t1"]),
        "summary": "test", "items": [{
            "taskId": "t1", "projectId": "p1", "title": "Купить молоко",
            "project": "Покупки", "snapshot": {},
        }],
    }
    monkeypatch.setattr(tg, "check_approval", lambda m: "approved" if m == mid else "none")
    monkeypatch.setattr(tg, "get_tg_approval",
                        lambda m: {"chat_id": "c1", "message_id": 7})
    group, private = _fake_report_sinks(monkeypatch)

    asyncio.run(s._tg_auto_execute_tick())

    assert s._MANIFESTS[mid]["consumed"] is True
    assert "t1" not in live  # actually deleted
    # ПОЛНЫЙ отчёт уходит в группу-архив, в личку — только короткая сводка
    # (2026-08-06: раньше обе роли играло одно editMessageText).
    assert len(group) == 1
    assert group[0]["manifest_id"] == mid and group[0]["tool"] == "delete_tasks"
    assert "Что сделал исполнитель" in group[0]["report_md"]
    assert len(private) == 1
    chat_id, message_id, short = private[0]
    assert chat_id == "c1" and message_id == 7
    assert "🛑" not in short
    assert "MCP Отчёты" in short


def test_tick_skips_when_no_candidates(monkeypatch):
    import asyncio
    _enable_tg(monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda m: "none")
    group, private = _fake_report_sinks(monkeypatch)
    asyncio.run(s._tg_auto_execute_tick())
    assert group == [] and private == []


def test_tick_reports_error_without_crashing(monkeypatch):
    import asyncio

    _enable_tg(monkeypatch)
    mid = "e2e-del-err"
    s._MANIFESTS[mid] = {
        "kind": "delete", "consumed": False, "created": time.monotonic(),
        "object_hash": s._manifest_object_hash("delete", ["t1"]),
        "summary": "test", "items": [{
            "taskId": "t1", "projectId": "p1", "title": "X", "project": "P",
            "snapshot": {},
        }],
    }
    monkeypatch.setattr(tg, "check_approval", lambda m: "approved" if m == mid else "none")
    monkeypatch.setattr(tg, "get_tg_approval",
                        lambda m: {"chat_id": "c1", "message_id": 7})

    async def boom(manifest_id, m):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(s._AUTO_EXECUTORS["delete_tasks"], "execute", boom)
    group, private = _fake_report_sinks(monkeypatch)

    asyncio.run(s._tg_auto_execute_tick())  # must not raise
    # Ошибка исполнения идёт по тому же пути: полный текст — в группу как
    # "failed", короткая сводка — в личку.
    assert len(group) == 1
    assert group[0]["verdict"] == "failed"
    assert "🛑" in group[0]["report_md"]
    assert "kaboom" in group[0]["report_md"]
    assert len(private) == 1
    assert "🛑" in private[0][2]
    # the manifest was consumed by try_auto_execute BEFORE execute() ran and
    # raised — no retry storm on a manifest that already "used" its one shot
    assert s._MANIFESTS[mid]["consumed"] is True
