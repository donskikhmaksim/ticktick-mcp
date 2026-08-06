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
(get_tg_approvals/check_approval/get_tg_approval/notify_plan) and the Telegram
HTTP call are monkeypatched throughout, same convention as test_tg_approval.py.

2026-08-06: поллер больше НЕ читает статусы поштучно (check_approval на каждый
живой манифест + ещё get_tg_approval на одобренный) — он делает ОДИН пакетный
запрос tg_approval.get_tg_approvals(ids) на весь проход. Поэтому тесты поллера
здесь патчат именно её (хелпер `_approvals` ниже), а не check_approval;
одиночные функции остались нетронуты и продолжают обслуживать чат-путь."""
import time

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


@pytest.fixture(autouse=True)
def _clean_lost_cache():
    """`_TG_LOST_CLEARED` — модульный кэш «этот манифест на самом деле
    исполнялся, в журнале есть запись». Он живёт всё время процесса, поэтому
    без сброса один тест мог бы молча погасить обнаружение в другом."""
    s._TG_LOST_CLEARED.clear()
    yield
    s._TG_LOST_CLEARED.clear()


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
        # ПОЛНАЯ доставка: 1 кусок из 1. С 2026-08-06 полнота выражается
        # полями ReportDelivery, а не непустотой списка id — частичная
        # доставка больше не может притвориться успехом.
        return tg.ReportDelivery([1001], 1, 1, True)

    def _summarize(cfg, chat_id, message_id, short_md):
        private.append((chat_id, message_id, short_md))
        return True  # правка принята Telegram — сводка вписана

    monkeypatch.setattr(tg, "post_report_to_group", _post, raising=False)
    monkeypatch.setattr(tg, "summarize_in_owner_chat", _summarize, raising=False)
    return group, private


_DB_STATUS = {"approved": "APPROVED", "pending": "PENDING", "rejected": "REJECTED"}


def _approvals(monkeypatch, status_of, chat_id="c1", message_id=None,
               extra_message_ids=None, lost_rows=None):
    """Подменяет ПАКЕТНОЕ чтение статусов (единственный путь поллера в базу).
    `status_of(mid)` возвращает "approved"/"pending"/"rejected"/"none"; "none"
    = строки в tg_approvals нет вовсе, поэтому ключ в словарь не попадает.

    `extra_message_ids` — id ПРЕДЫДУЩИХ кусков длинного плана. Поле обязано
    быть в фейке ровно потому же, почему оно есть в настоящем
    get_tg_approvals: без него уборка сирот в личке (_cleanup_plan_leftovers)
    была бы «зелёной» в тестах и мёртвой в проде.

    `lost_rows` (2026-08-06) — строки, которые настоящий запрос возвращает по
    ВТОРОЙ ветке WHERE: подтверждённые кнопкой, но без живого манифеста
    («сервис перезапустился между планом и нажатием»). Их id заведомо нет в
    `mids`, поэтому фейк обязан уметь их отдавать — иначе поиск потерянных
    планов был бы непроверяемым."""
    def _fake(mids, lost_scan_since_ms=None):
        out = {}
        for mid in mids:
            st = status_of(mid)
            if st == "none":
                continue
            out[mid] = {"status": _DB_STATUS[st],
                        "expires_at": tg._now_ms() + 3_600_000,
                        "chat_id": chat_id, "message_id": message_id,
                        "extra_message_ids": list(extra_message_ids or []),
                        "created_at": tg._now_ms(), "decided_at": None,
                        "report_message_ids": [], "lost_notified_at": None}
        if lost_scan_since_ms is not None:
            out.update(lost_rows or {})
        return out
    monkeypatch.setattr(tg, "get_tg_approvals", _fake)


def test_find_candidates_empty_when_tg_disabled(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    now = time.monotonic()
    s._MANIFESTS["cand1"] = {"kind": "delete", "consumed": False, "created": now,
                             "items": [{"taskId": "t1", "title": "X"}]}
    _approvals(monkeypatch, lambda mid: "approved")
    assert s._find_tg_auto_execute_candidates() == []


def test_find_candidates_skips_unapproved(monkeypatch):
    _enable_tg(monkeypatch)
    now = time.monotonic()
    s._MANIFESTS["cand2"] = {"kind": "delete", "consumed": False, "created": now,
                             "items": [{"taskId": "t1", "title": "X"}]}
    _approvals(monkeypatch, lambda mid: "pending")
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
    _approvals(monkeypatch, lambda mid: "approved" if mid == "cand3" else "none")
    ids = {c["manifest_id"] for c in s._find_tg_auto_execute_candidates()}
    assert "cand3" not in ids


def test_find_candidates_skips_kind_with_no_registered_tool(monkeypatch):
    _enable_tg(monkeypatch)
    now = time.monotonic()
    # Раньше здесь стоял kind="create" как пример НЕ проведённого через
    # Telegram вида манифеста. С 2026-08-06 создание задач проведено
    # (_AUTO_EXECUTE_TOOL_FOR_KIND["create"] = "create_tasks"), поэтому
    # берём kind, у которого авто-исполнителя действительно нет:
    # "delete_project" — его план в Telegram уходит, но по кнопке сервер сам
    # ничего не удаляет, модель обязана повторить вызов тула.
    s._MANIFESTS["cand4"] = {"kind": "delete_project", "consumed": False,
                             "created": now}
    _approvals(monkeypatch, lambda mid: "approved" if mid == "cand4" else "none")
    ids = {c["manifest_id"] for c in s._find_tg_auto_execute_candidates()}
    assert "cand4" not in ids


def test_find_candidates_respects_allowlist(monkeypatch):
    _enable_tg(monkeypatch, allowlist={"execute_declutter"})
    now = time.monotonic()
    s._MANIFESTS["cand5"] = {"kind": "delete", "consumed": False, "created": now,
                             "items": [{"taskId": "t1", "title": "X"}]}
    _approvals(monkeypatch, lambda mid: "approved" if mid == "cand5" else "none")
    ids = {c["manifest_id"] for c in s._find_tg_auto_execute_candidates()}
    assert "cand5" not in ids  # delete_tasks not in allowlist


def test_find_candidates_finds_approved_delete(monkeypatch):
    _enable_tg(monkeypatch)
    now = time.monotonic()
    s._MANIFESTS["cand6"] = {"kind": "delete", "consumed": False, "created": now,
                             "items": [{"taskId": "t1", "title": "X"}]}
    _approvals(monkeypatch, lambda mid: "approved", message_id=99)
    out = [c for c in s._find_tg_auto_execute_candidates() if c["manifest_id"] == "cand6"]
    assert len(out) == 1
    assert out[0]["tool"] == "delete_tasks"
    assert out[0]["chat_id"] == "c1"
    assert out[0]["message_id"] == 99
    # старая строка без колонки extra — не повод падать
    assert out[0]["extra_message_ids"] == []


def test_find_candidates_carries_extra_plan_chunk_ids(monkeypatch):
    """id предыдущих кусков длинного плана обязаны доехать до кандидата —
    иначе после исполнения их некому удалить из лички (наш reaper строки
    APPROVED не трогает, а gmail-mcp знает только про message_id)."""
    _enable_tg(monkeypatch)
    s._MANIFESTS["cand7"] = {"kind": "delete", "consumed": False,
                             "created": time.monotonic(),
                             "items": [{"taskId": "t1", "title": "X"}]}
    _approvals(monkeypatch, lambda mid: "approved" if mid == "cand7" else "none",
               message_id=99, extra_message_ids=[96, 97, 98])
    out = [c for c in s._find_tg_auto_execute_candidates()
           if c["manifest_id"] == "cand7"]
    assert out[0]["extra_message_ids"] == [96, 97, 98]


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
    # Пакетное чтение статусов (единственный путь поллера в базу с 2026-08-06)
    # + ДВА получателя итога: группа-архив (полный отчёт) и личка (сводка).
    _approvals(monkeypatch, lambda m: "approved" if m == mid else "none", message_id=7)
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


def test_tick_verifies_tools_that_journal_under_their_own_record_id(
        monkeypatch, tmp_path):
    """Независимая перепроверка обязана работать для ЛЮБОГО инструмента, а не
    только для delete_tasks (прямое требование Максима).

    Поймано аудитом слияния 2026-08-06: `_verified_auto_execute_report` зовёт
    `_build_operation_report(manifest_id)`, а тот ищет записи журнала по
    `record`/`manifest`. Совпадение с manifest_id было ТОЛЬКО у delete_tasks —
    остальные исполнители пишут через `_op_journal` со своим «<op>-<hex>», и
    после расширения кнопки на 22 тула у всех новых перепроверка возвращала
    «В журнале нет записей», а вердикт был вечным "unverified". Лечится
    меткой `tg_manifest`, которую поллер выставляет на время исполнения."""
    import asyncio

    _enable_tg(monkeypatch)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    monkeypatch.setattr(s, "_v2_project_names", lambda: {})

    mid = "e2e-generic-1"
    s._MANIFESTS[mid] = {
        "kind": "delete", "consumed": False, "created": time.monotonic(),
        "object_hash": s._manifest_object_hash("delete", ["t1"]),
        "summary": "test", "items": [{"taskId": "t1", "title": "X",
                                      "projectId": "p1", "snapshot": {}}],
    }

    async def _fake_impl(manifest_id, m):
        # Ровно то, что делает любой обычный исполнитель: пишет журнал под
        # СВОИМ record_id и возвращает текст со ссылкой на него.
        rid = s._op_journal("complete", [{"taskId": "t1", "title": "Купить молоко"}],
                            "test")
        assert not rid.startswith(mid)      # id журнала ≠ id манифеста
        return "✅ Закрыто 1/1: «Купить молоко»\n" + s._report_line(rid)

    monkeypatch.setattr(s._AUTO_EXECUTORS["delete_tasks"], "execute", _fake_impl)
    _approvals(monkeypatch, lambda m: "approved" if m == mid else "none", message_id=7)
    group, private = _fake_report_sinks(monkeypatch)

    asyncio.run(s._tg_auto_execute_tick())

    assert len(group) == 1
    assert group[0]["verdict"] == "ok"      # было "unverified" до правки
    assert "Купить молоко" in group[0]["report_md"]
    assert "В журнале нет записей" not in group[0]["report_md"]
    # Метка снята: журнал, написанный ПОСЛЕ тика, к этому манифесту не липнет.
    s._op_journal("complete", [{"taskId": "t2", "title": "Посторонняя"}], "x")
    assert "Посторонняя" not in s._build_operation_report(mid)


def test_tick_skips_when_no_candidates(monkeypatch):
    import asyncio
    _enable_tg(monkeypatch)
    _approvals(monkeypatch, lambda m: "none")
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
    _approvals(monkeypatch, lambda m: "approved" if m == mid else "none", message_id=7)

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


# ===========================================================================
# «Подтверждено кнопкой, но исполнять нечего» (2026-08-06)
# ===========================================================================
#
# Сценарий поломки, ради которого всё это существует: владелец получил план с
# кнопками → сервис перезапустился (автодеплой из main) → владелец нажал ✅ →
# вебхук соседнего сервиса честно поставил APPROVED → манифеста в памяти уже
# нет → раньше наступала ТИШИНА: reap_expired строки APPROVED не трогает,
# сообщение с кнопками висело вечно, отчёта не было, и владелец не узнавал,
# что операция не состоялась. С режимом «только кнопка» такой план вдобавок
# стал принципиально неисполнимым.
#
# Ниже проверяются оба направления лжи по отдельности: и «промолчали про
# потерю», и (что важнее) «сказали «не выполнено» про выполненное».

_HOUR_MS = 3_600_000


def _lost_row(*, decided_ago_ms=10 * 60 * 1000, expires_in_ms=30 * 60 * 1000,
              chat_id="c1", message_id=42, report_message_ids=None,
              lost_notified_at=None, status="APPROVED", extra=None):
    """Строка tg_approvals в том виде, в каком её отдаёт get_tg_approvals."""
    now = tg._now_ms()
    return {"status": status, "expires_at": now + expires_in_ms,
            "chat_id": chat_id, "message_id": message_id,
            "extra_message_ids": list(extra or []),
            "created_at": now - decided_ago_ms,
            "decided_at": now - decided_ago_ms,
            "report_message_ids": list(report_message_ids or []),
            "lost_notified_at": lost_notified_at}


def _lost(rows, **kw):
    return s._tg_lost_manifest_rows(rows, now_ms=tg._now_ms(), **kw)


def test_lost_rows_catches_approved_row_without_a_manifest():
    """Базовый случай: строка одобрена, манифеста в памяти нет, времени
    прошло достаточно — это и есть потерянный план."""
    out = _lost({"gone-1": _lost_row()}, is_known=lambda mid: False)
    assert [c["manifest_id"] for c in out] == ["gone-1"]
    assert out[0]["chat_id"] == "c1" and out[0]["message_id"] == 42


def test_lost_rows_wait_out_the_grace_window():
    """Между записью APPROVED и ближайшим тиком поллера есть законное окно
    (тик — 10 секунд плюс поездка в Postgres). Паниковать в нём нельзя:
    манифест жив, его вот-вот исполнят."""
    out = _lost({"fresh-1": _lost_row(decided_ago_ms=3_000)},
                is_known=lambda mid: False)
    assert out == []


def test_lost_rows_ignore_a_manifest_the_process_still_knows():
    """Манифест мог быть исполнен долю секунды назад и вычищен из _MANIFESTS
    (_prune_manifests сносит consumed). Надгробие про него помнит — и такой
    случай НЕ должен выглядеть как потеря."""
    out = _lost({"done-1": _lost_row()}, is_known=lambda mid: True)
    assert out == []


def test_lost_rows_ignore_rows_whose_report_was_already_published():
    """report_message_ids заполняются только после публикации отчёта в архив,
    то есть после реального исполнения. Такую строку мог оставить и ДРУГОЙ
    процесс (старый контейнер во время выкатки) — для нас это доказательство,
    что операция состоялась."""
    out = _lost({"done-2": _lost_row(report_message_ids=[500, 501])},
                is_known=lambda mid: False)
    assert out == []


def test_lost_rows_report_each_loss_only_once():
    """Отметка в БД — единственное, что переживает перезапуск. Без неё поллер
    писал бы владельцу одно и то же каждые 10 секунд до конца TTL."""
    out = _lost({"told-1": _lost_row(lost_notified_at=tg._now_ms())},
                is_known=lambda mid: False)
    assert out == []


def test_lost_rows_ignore_ancient_rows():
    """Исполненные строки живут в таблице до уборки соседним сервером. Без
    ограничения по возрасту первый же тик после рестарта «открыл» бы их все
    заново и завалил владельца сообщениями о давно сделанном."""
    out = _lost({"old-1": _lost_row(expires_in_ms=-5 * _HOUR_MS)},
                is_known=lambda mid: False)
    assert out == []


def test_lost_rows_ignore_non_approved_rows():
    for status in ("PENDING", "REJECTED", "EXPIRED"):
        assert _lost({"x": _lost_row(status=status)},
                     is_known=lambda mid: False) == []


def test_lost_rows_use_the_real_memory_check_by_default():
    """is_known по умолчанию — настоящая память процесса: живой манифест в
    _MANIFESTS потерянным не считается, что бы ни было в строке."""
    s._MANIFESTS["alive-1"] = {"kind": "delete", "consumed": False,
                               "created": time.monotonic()}
    assert _lost({"alive-1": _lost_row()}) == []
    assert [c["manifest_id"] for c in _lost({"nomem-1": _lost_row()})] == ["nomem-1"]


def test_journal_mentions_manifests_finds_executed_ones(monkeypatch, tmp_path):
    """Самая надёжная проверка «а не исполнялось ли оно на самом деле»:
    журнал мутаций — файл на диске, он ПЕРЕЖИВАЕТ перезапуск процесса, в
    отличие от манифестов и надгробий."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    s._journal_write({"ts": "x", "manifest": "did-run", "op": "delete",
                      "items": []})
    s._journal_write({"ts": "x", "record": "delete-abc",
                      "tg_manifest": "did-run-2", "op": "delete", "items": []})
    assert s._journal_mentions_manifests({"did-run", "never-ran"}) == {"did-run"}
    assert s._journal_mentions_manifests({"did-run-2"}) == {"did-run-2"}


def test_journal_mentions_manifests_survives_a_missing_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path / "нет-такого"))
    assert s._journal_mentions_manifests({"any"}) == set()


def _lost_sinks(monkeypatch, claimed=None):
    """Подменяет всё, чем аварийная ветка общается с миром: захват строки в
    Postgres, снятие кнопок, сообщение в личку и публикацию в архив."""
    dm, archive, cleared, claims = [], [], [], []

    def _claim(ids):
        claims.append(list(ids))
        return list(ids) if claimed is None else list(claimed)

    monkeypatch.setattr(tg, "claim_lost_manifests", _claim, raising=False)
    monkeypatch.setattr(tg, "clear_inline_keyboard",
                        lambda cfg, chat, mid: cleared.append((chat, mid)) or True,
                        raising=False)
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, md, **kw: dm.append((chat, md))
                        or tg.SendResult(True, [1], "", 1))
    monkeypatch.setattr(tg, "post_report_to_group",
                        lambda cfg, mid, md, *, tool, verdict: archive.append(
                            (mid, md, tool, verdict))
                        or tg.ReportDelivery([2], 1, 1, True), raising=False)
    return dm, archive, cleared, claims


def test_announce_tells_the_owner_and_the_archive(monkeypatch, tmp_path):
    """Текст владельцу обязан сказать три вещи простыми словами: нажатие
    дошло, операция НЕ выполнена, что делать дальше. В архив тот же факт
    уходит отдельным вердиктом "lost" — не «ошибка исполнения» (исполнения не
    было вовсе)."""
    _enable_tg(monkeypatch)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    dm, archive, cleared, _claims = _lost_sinks(monkeypatch)

    told = s._announce_lost_manifests([
        {"manifest_id": "gone-9", "chat_id": "c1", "message_id": 42,
         "extra_message_ids": [], "decided_at": tg._now_ms() - 60_000}])

    assert told == 1
    assert cleared == [("c1", 42)], "кнопки обязаны перестать вводить в заблуждение"
    chat, text = dm[0]
    assert chat == "c1"
    assert "НЕ выполнена" in text and "перезапуск" in text
    assert "ещё раз" in text
    mid, md, _tool, verdict = archive[0]
    assert mid == "gone-9" and verdict == "lost"
    assert "НЕ выполнена" in md


def test_announce_keeps_quiet_about_plans_that_actually_ran(monkeypatch, tmp_path):
    """САМАЯ ОПАСНАЯ ошибка этой фичи — сказать «не выполнено» про
    выполненное: владелец повторит операцию, и мутация случится дважды.
    Журнал мутаций переживает рестарт, поэтому он здесь и решает."""
    _enable_tg(monkeypatch)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    s._journal_write({"ts": "x", "tg_manifest": "ran-1", "op": "delete",
                      "items": []})
    dm, archive, _cleared, claims = _lost_sinks(monkeypatch)

    told = s._announce_lost_manifests([
        {"manifest_id": "ran-1", "chat_id": "c1", "message_id": 42,
         "extra_message_ids": [], "decided_at": tg._now_ms() - 60_000}])

    assert told == 0
    assert dm == [] and archive == [] and claims == []
    # и второй раз журнал уже не перечитывается — запомнили в процессе
    assert "ran-1" in s._TG_LOST_CLEARED


def test_announce_says_nothing_when_another_process_claimed_the_row(monkeypatch,
                                                                    tmp_path):
    """При выкатке нового билда какое-то время работают ДВА процесса. Право
    сообщить забирает атомарный UPDATE в Postgres; кто не выиграл — молчит,
    иначе владелец получил бы два одинаковых сообщения об одной потере."""
    _enable_tg(monkeypatch)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    dm, archive, _cleared, _claims = _lost_sinks(monkeypatch, claimed=[])

    told = s._announce_lost_manifests([
        {"manifest_id": "gone-10", "chat_id": "c1", "message_id": 1,
         "extra_message_ids": [], "decided_at": tg._now_ms() - 60_000}])

    assert told == 0 and dm == [] and archive == []


def test_announce_claims_the_row_before_telling_anyone(monkeypatch, tmp_path):
    """Порядок «сначала пометить в БД, потом отправить» — сознательный: не
    доставленное уведомление хуже, чем повторяющееся вечно каждые 10 секунд
    (последнее Максим запретил прямо)."""
    _enable_tg(monkeypatch)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    order = []
    monkeypatch.setattr(tg, "claim_lost_manifests",
                        lambda ids: order.append("claim") or list(ids),
                        raising=False)
    monkeypatch.setattr(tg, "clear_inline_keyboard", lambda *a, **k: True,
                        raising=False)
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, md, **kw: order.append("dm")
                        or tg.SendResult(True, [1], "", 1))
    monkeypatch.setattr(tg, "post_report_to_group",
                        lambda *a, **k: tg.ReportDelivery([2], 1, 1, True),
                        raising=False)

    s._announce_lost_manifests([
        {"manifest_id": "gone-11", "chat_id": "c1", "message_id": 1,
         "extra_message_ids": [], "decided_at": tg._now_ms() - 60_000}])

    assert order == ["claim", "dm"]


def test_announce_survives_a_failing_notification(monkeypatch, tmp_path):
    """Best-effort целиком: сбой уведомления по одному плану не имеет права
    ни бросить наружу, ни отменить разбор остальных."""
    _enable_tg(monkeypatch)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    dm, _archive, _cleared, _claims = _lost_sinks(monkeypatch)
    calls = {"n": 0}

    def _flaky(cfg, chat, md, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Telegram отвалился")
        dm.append((chat, md))
        return tg.SendResult(True, [1], "", 1)

    monkeypatch.setattr(tg, "send_message_chunked", _flaky)
    told = s._announce_lost_manifests([
        {"manifest_id": "gone-12", "chat_id": "c1", "message_id": 1,
         "extra_message_ids": [], "decided_at": tg._now_ms() - 60_000},
        {"manifest_id": "gone-13", "chat_id": "c1", "message_id": 2,
         "extra_message_ids": [], "decided_at": tg._now_ms() - 60_000}])

    assert told == 2  # оба разобраны
    assert len(dm) == 1  # второй дошёл, первый честно упал в лог


def test_a_plan_executed_through_the_chat_path_is_not_called_lost():
    """Планы без авто-исполнителя (delete_project, слияние в rename_tag)
    исполняет ПОВТОРНЫЙ вызов инструмента после нажатия кнопки. У них нет ни
    надгробия от поллера, ни отчёта в архиве, ни метки в журнале — то есть
    ровно ноль признаков «исполнялось». Манифест при этом гаснет за секунды, а
    подтверждённая строка живёт часами: без следа от `_prune_manifests` каждая
    такая успешная операция через пару минут выглядела бы потерянной, и
    владелец получал бы «не выполнено» про выполненное."""
    s._MANIFESTS["chat-done-1"] = {"kind": "delete_project", "consumed": True,
                                   "created": time.monotonic()}
    s._prune_manifests()
    assert "chat-done-1" not in s._MANIFESTS
    assert s._tg_manifest_is_known("chat-done-1")
    assert _lost({"chat-done-1": _lost_row()}) == []


def test_a_plan_that_merely_expired_is_still_reported_as_lost():
    """А вот просроченный план исполнить нельзя НИКАК — про него сказать надо,
    поэтому следа он не оставляет."""
    s._MANIFESTS["stale-1"] = {"kind": "delete", "consumed": False,
                               "created": time.monotonic() - s._MANIFEST_TTL - 1}
    s._prune_manifests()
    assert not s._tg_manifest_is_known("stale-1")
    assert [c["manifest_id"] for c in _lost({"stale-1": _lost_row()})] == ["stale-1"]


def test_auto_executed_tombstone_is_not_overwritten_by_pruning():
    """Причина «исполнено кнопкой» несёт ответ модели (_manifest_gone_msg) —
    затирать её общей отметкой при уборке нельзя."""
    s._MANIFESTS["btn-1"] = {"kind": "delete", "consumed": False,
                             "created": time.monotonic()}
    s._consume_manifest_for_auto_execute("btn-1")
    s._prune_manifests()
    assert s._MANIFEST_TOMBSTONES["btn-1"]["reason"] == "tg_auto_executed"


def test_tick_notices_a_plan_lost_to_a_restart(monkeypatch, tmp_path):
    """Сквозной сценарий: память пуста (как СРАЗУ ПОСЛЕ ПЕРЕЗАПУСКА), а в
    Postgres лежит подтверждённая строка. Раньше тик в этом состоянии выходил
    первой же строкой («нет живых манифестов — в базу не идём»), и потеря была
    ненаблюдаемой по построению."""
    import asyncio

    _enable_tg(monkeypatch)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    dm, archive, cleared, _claims = _lost_sinks(monkeypatch)
    _approvals(monkeypatch, lambda mid: "none",
               lost_rows={"restart-1": _lost_row()})

    asyncio.run(s._tg_auto_execute_tick())

    assert [c[0] for c in dm] == ["c1"]
    assert archive and archive[0][3] == "lost"
    assert cleared == [("c1", 42)]


def test_tick_still_executes_when_the_lost_branch_explodes(monkeypatch, tmp_path):
    """Аварийная ветка ходит в файл, в Postgres и в Telegram — её сбой не
    имеет права ни уронить поллер, ни помешать исполнению обычных кандидатов."""
    import asyncio

    _enable_tg(monkeypatch)
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    monkeypatch.setattr(s, "ticktick_v2", _FakeV2Delete(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})
    monkeypatch.setattr(s, "_tg_lost_manifest_rows",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("бум")))

    mid = "e2e-del-lostboom"
    s._MANIFESTS[mid] = {
        "kind": "delete", "consumed": False, "created": time.monotonic(),
        "object_hash": s._manifest_object_hash("delete", ["t1"]),
        "summary": "test", "items": [{
            "taskId": "t1", "projectId": "p1", "title": "Купить молоко",
            "project": "Покупки", "snapshot": {}}],
    }
    _approvals(monkeypatch, lambda m: "approved" if m == mid else "none",
               message_id=7)
    group, private = _fake_report_sinks(monkeypatch)

    asyncio.run(s._tg_auto_execute_tick())  # must not raise

    assert "t1" not in live and len(group) == 1 and len(private) == 1


# ===========================================================================
# Двойная публикация итога — теперь невозможна КОНСТРУКТИВНО
# ===========================================================================

def test_no_second_report_when_publishing_the_success_outcome_raises(monkeypatch):
    """Исключение внутри `_publish_auto_execute_outcome` на УСПЕШНОМ пути
    ловится общим `except` тика — и тот раньше публиковал ВТОРОЙ отчёт «🛑
    ошибка исполнения» про операцию, которая на самом деле выполнена и уже
    отчиталась. В архив ложилась прямая ложь. Держалось это только внутренними
    try/except, то есть дисциплиной; теперь держит отметка «итог уже
    публиковался», которую нельзя забыть поставить."""
    import asyncio

    _enable_tg(monkeypatch)
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    monkeypatch.setattr(s, "ticktick_v2", _FakeV2Delete(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})

    mid = "e2e-del-double"
    s._MANIFESTS[mid] = {
        "kind": "delete", "consumed": False, "created": time.monotonic(),
        "object_hash": s._manifest_object_hash("delete", ["t1"]),
        "summary": "test", "items": [{
            "taskId": "t1", "projectId": "p1", "title": "Купить молоко",
            "project": "Покупки", "snapshot": {}}],
    }
    _approvals(monkeypatch, lambda m: "approved" if m == mid else "none",
               message_id=7)
    group, private = _fake_report_sinks(monkeypatch)
    # Падение ПОСЛЕ того, как полный отчёт уже ушёл в группу: сводка в личку
    # строится позже публикации в архив.
    monkeypatch.setattr(s, "_short_auto_execute_summary",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("сводка не собралась")))

    asyncio.run(s._tg_auto_execute_tick())  # must not raise

    assert "t1" not in live, "операция реально выполнена"
    assert len(group) == 1, "второго отчёта быть не должно"
    assert group[0]["verdict"] != "failed", "успешную операцию нельзя объявить провалом"
    assert private == []
