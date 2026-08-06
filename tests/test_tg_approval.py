"""Tests for the optional Telegram out-of-band approval layer
(ticktick_mcp/src/tg_approval.py + its hooks into server.py's
_require_consent / _maybe_tg_notify_plan). No real network, no real
Postgres — Telegram HTTP and the store are faked/monkeypatched.

Scope note: OFF by default (TG_APPROVAL_ENABLED unset) — every test that
doesn't explicitly enable it must observe BYTE-IDENTICAL behaviour to before
this layer existed (the compatibility invariant this whole feature is built
on, same as gmail-mcp's tg_approval.ts)."""
import time

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ===========================================================================
# md_to_telegram_html — pure formatting, no network
# ===========================================================================

def test_heading_and_bold_render():
    out = tg.md_to_telegram_html("### 📋 План удаления — 1\n\n- **«Купить молоко»** — Покупки")
    assert out == "<b>📋 План удаления — 1</b>\n\n- <b>«Купить молоко»</b> — Покупки"


def test_code_and_italic_render():
    out = tg.md_to_telegram_html("манифест `abc123` · _(диапазон сейчас пуст)_")
    assert out == "манифест <code>abc123</code> · <i>(диапазон сейчас пуст)</i>"


def test_intraword_underscore_is_never_italicized():
    """Real-world regression: a task/file name with underscores (e.g. a
    project or tag literally called `n8n_email_algo`) must NOT be partially
    turned into <i> — GFM's own "no intraword emphasis" rule, ported."""
    out = tg.md_to_telegram_html("«n8n_email_algo_report_2026-08-05.md»")
    assert "<i>" not in out
    assert "n8n_email_algo_report_2026-08-05.md" in out


def test_html_special_chars_are_escaped_before_tags_are_inserted():
    out = tg.md_to_telegram_html("<script>&test **bold**")
    assert out == "&lt;script&gt;&amp;test <b>bold</b>"


# ===========================================================================
# load_tg_approval_config — env parsing, fail-fast when misconfigured
# ===========================================================================

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TG_APPROVAL_ENABLED", raising=False)
    cfg = tg.load_tg_approval_config()
    assert cfg.enabled is False
    assert tg.enabled_for(cfg, "delete_tasks") is False


def test_enabled_without_bot_token_raises(monkeypatch):
    monkeypatch.setenv("TG_APPROVAL_ENABLED", "true")
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "123")
    with pytest.raises(RuntimeError, match="TG_BOT_TOKEN"):
        tg.load_tg_approval_config()


def test_enabled_with_everything_set_ok(monkeypatch):
    monkeypatch.setenv("TG_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("TG_BOT_TOKEN", "faketoken")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "123")
    monkeypatch.delenv("TG_APPROVAL_TOOLS", raising=False)
    cfg = tg.load_tg_approval_config()
    assert cfg.enabled is True
    assert cfg.server == "ticktick"
    assert cfg.tools_allowlist is None  # empty allowlist = every gated tool


def test_tools_allowlist_scopes_enabled_for(monkeypatch):
    monkeypatch.setenv("TG_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("TG_BOT_TOKEN", "faketoken")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "123")
    monkeypatch.setenv("TG_APPROVAL_TOOLS", "delete_tasks, execute_declutter")
    cfg = tg.load_tg_approval_config()
    assert tg.enabled_for(cfg, "delete_tasks") is True
    assert tg.enabled_for(cfg, "resume_declutter") is False


# ===========================================================================
# _require_consent — TG branch: byte-compatible when tool="" (default)
# ===========================================================================

def _fresh_manifest(mid="m1"):
    now = time.monotonic()
    return {"kind": "delete", "items": [{"taskId": "t1", "title": "X"}],
            "created": now - 10, "plan_shown_at": now - 10,
            "object_hash": s._manifest_object_hash("delete", ["t1"]),
            "summary": "test", "consumed": False}


def test_require_consent_without_tool_arg_ignores_tg_entirely(monkeypatch):
    """Regression guard for the compatibility invariant: `tool=""` (the
    default) must NEVER touch tg_approval, even when TG_APPROVAL_ENABLED=true
    globally — call sites that don't pass `tool` are unaffected."""
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    called = {"n": 0}
    monkeypatch.setattr(tg, "check_approval", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "pending")
    m = _fresh_manifest()
    cr = s._require_consent(action="delete", tier=2, manifest=m, user_reply="да",
                            object_ids=["t1"])
    assert cr.ok is True  # old behaviour: plain "да" is enough
    assert called["n"] == 0  # tg_approval.check_approval never called


def test_require_consent_with_tool_and_pending_approval_refuses(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "pending")
    m = _fresh_manifest()
    cr = s._require_consent(action="delete", tier=2, manifest=m, user_reply="да",
                            object_ids=["t1"], tool="delete_tasks", manifest_id="m1")
    assert cr.ok is False
    assert "Telegram" in cr.reason
    assert m["consumed"] is False  # still armed — user might tap the button soon


def test_require_consent_with_tool_and_rejected_approval_invalidates(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "rejected")
    m = _fresh_manifest()
    cr = s._require_consent(action="delete", tier=2, manifest=m, user_reply="да",
                            object_ids=["t1"], tool="delete_tasks", manifest_id="m1")
    assert cr.ok is False
    assert "Отклонено" in cr.reason
    assert m["consumed"] is True  # invalidated — a fresh plan is required


def test_require_consent_with_tool_and_approved_proceeds_to_timing_check(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "approved")
    m = _fresh_manifest()
    cr = s._require_consent(action="delete", tier=2, manifest=m, user_reply="да",
                            object_ids=["t1"], tool="delete_tasks", manifest_id="m1")
    assert cr.ok is True


def test_require_consent_tool_not_in_allowlist_skips_tg_check(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist={"resume_declutter"}, ttl_s=3600))
    called = {"n": 0}
    monkeypatch.setattr(tg, "check_approval", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "pending")
    m = _fresh_manifest()
    cr = s._require_consent(action="delete", tier=2, manifest=m, user_reply="да",
                            object_ids=["t1"], tool="delete_tasks", manifest_id="m1")
    assert cr.ok is True
    assert called["n"] == 0


# ===========================================================================
# _maybe_tg_notify_plan — plan-phase hook
# ===========================================================================

async def test_notify_plan_skipped_when_tool_not_enabled(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    called = {"n": 0}
    monkeypatch.setattr(tg, "notify_plan", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (True, ""))
    out = await s._maybe_tg_notify_plan("delete_tasks", "m1", "### план")
    assert out == "### план"  # untouched
    assert called["n"] == 0


async def test_notify_plan_success_appends_telegram_note(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    monkeypatch.setattr(tg, "notify_plan", lambda *a, **k: (True, ""))
    out = await s._maybe_tg_notify_plan("delete_tasks", "m1", "### план")
    assert "### план" in out
    assert "Telegram" in out


async def test_notify_plan_failure_invalidates_manifest_fail_closed(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    monkeypatch.setattr(tg, "notify_plan", lambda *a, **k: (False, "sendMessage failed"))
    s._MANIFESTS["m-fail-test"] = _fresh_manifest()
    out = await s._maybe_tg_notify_plan("delete_tasks", "m-fail-test", "### план")
    assert "🛑" in out
    assert "Не смог отправить" in out
    assert s._MANIFESTS["m-fail-test"]["consumed"] is True


# ===========================================================================
# Отправка плана не держит event loop
# ===========================================================================

async def test_plan_tool_sends_the_telegram_plan_off_the_event_loop(monkeypatch):
    """Внутри notify_plan — синхронный requests, паузы между кусками и сон на
    429 (до трёх минут на кусок в патологии). Прямой вызов из корутины держит
    event loop всё это время: зависший /health и заткнувшиеся MCP-сессии.

    Проверяется НАБЛЮДАЕМОЕ поведение, а не имя обёртки (до #115 тест ловил
    факт вызова `_run_blocking` — и был слеп ровно к тому, что случилось:
    обёртку вручную поставили три тула из двадцати двух, а тест этого не
    видел). Здесь посторонняя корутина обязана крутиться, пока идёт отправка.
    Полноценный сценарий с флуд-отказами Telegram — в
    tests/test_plan_send_nonblocking.py.
    """
    import asyncio
    import time as _time

    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id",
                        lambda fresh=False: {"t1": {"id": "t1", "title": "Купить молоко",
                                                    "projectId": "p1"}})
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})

    # Отправка занимает ощутимое время — как настоящая серия сообщений.
    monkeypatch.setattr(tg, "notify_plan",
                        lambda *a, **k: (_time.sleep(0.2), (True, ""))[1])

    beats = {"n": 0}

    async def _beat():
        while True:
            await asyncio.sleep(0.01)
            beats["n"] += 1

    hb = asyncio.create_task(_beat())
    try:
        out = await s.plan_task_deletion(
            "удалить одну", [{"taskId": "t1", "title": "Купить молоко"}])
    finally:
        hb.cancel()

    assert beats["n"] >= 5, ("event loop стоял на время отправки плана "
                             f"(посторонняя корутина провернулась {beats['n']} раз)")
    assert "План удаления" in out and "Telegram" in out


# ===========================================================================
# Поиск потерянных планов на уровне SQL (2026-08-06)
# ===========================================================================
#
# Postgres здесь фальшивый — проверяется не база, а ТЕКСТ запроса: что вторая
# ветка WHERE («подтверждено, но про потерю ещё не сообщали») действительно
# добавляется в ТОТ ЖЕ запрос, а не превращается во второй проход по базе, и
# что захват права сообщить — это одиночный атомарный UPDATE, а не «прочитал →
# отправил → пометил» (последнее при двух процессах на выкатке дало бы два
# одинаковых сообщения владельцу).

class _FakeCursor:
    def __init__(self, sink, rows):
        self.sink, self._rows = sink, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append((" ".join(sql.split()), params))

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, sink, rows):
        self.sink, self._rows = sink, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self.sink, self._rows)


class _FakePool:
    def __init__(self, sink, rows):
        self.sink, self._rows = sink, rows

    def getconn(self):
        return _FakeConn(self.sink, self._rows)

    def putconn(self, conn):
        pass


@pytest.fixture
def _fake_store(monkeypatch):
    sink, rows = [], []

    def _install(result_rows):
        rows[:] = result_rows
        monkeypatch.setattr(tg, "_pg_pool", _FakePool(sink, rows))
        return sink

    return _install


def test_batch_read_without_lost_scan_keeps_the_old_where(_fake_store):
    """Обычный проход поллера (сканирование потерь не запрошено) обязан
    остаться прежним запросом по списку id — иначе фича меняла бы горячий
    путь, который сейчас под приёмкой."""
    sink = _fake_store([])
    tg.get_tg_approvals(["a", "b"])
    sql, params = sink[0]
    assert "manifest_id = ANY(%s)" in sql
    assert "lost_notified_at IS NULL" not in sql
    assert params == (["a", "b"],)


def test_batch_read_with_lost_scan_adds_the_second_branch(_fake_store):
    """Строку, у которой манифеста в памяти НЕТ, поиск «от памяти» не увидит
    никогда — её id просто неоткуда взять. Поэтому вторая ветка WHERE и
    существует; и она обязана быть в ТОМ ЖЕ запросе (одно обращение к
    Postgres на проход)."""
    sink = _fake_store([])
    tg.get_tg_approvals(["a"], 1_700_000_000_000)
    assert len(sink) == 1, "второго прохода по базе быть не должно"
    sql, params = sink[0]
    assert "status = 'APPROVED'" in sql and "lost_notified_at IS NULL" in sql
    assert "expires_at > %s" in sql
    assert params == (["a"], 1_700_000_000_000)


def test_batch_read_scans_for_losses_even_with_no_live_manifests(_fake_store):
    """Пустая память — это состояние СРАЗУ ПОСЛЕ ПЕРЕЗАПУСКА, то есть ровно
    тогда, когда потерянные планы и появляются. Ранний выход по пустому
    списку id сделал бы потерю ненаблюдаемой."""
    sink = _fake_store([])
    tg.get_tg_approvals([], 1_700_000_000_000)
    assert len(sink) == 1


def test_batch_read_without_ids_and_without_scan_touches_nothing(_fake_store):
    sink = _fake_store([])
    assert tg.get_tg_approvals([]) == {}
    assert sink == []


def test_batch_read_exposes_the_fields_the_loss_detector_needs(_fake_store):
    _fake_store([("m1", "APPROVED", 111, "c1", 42, [7], 100, 105, [], None)])
    row = tg.get_tg_approvals(["m1"], 1)["m1"]
    assert row["decided_at"] == 105 and row["created_at"] == 100
    assert row["report_message_ids"] == [] and row["lost_notified_at"] is None
    # прежние поля на месте — их читает горячий путь автоисполнения
    assert row["status"] == "APPROVED" and row["message_id"] == 42
    assert row["extra_message_ids"] == [7]


def test_claim_lost_manifests_is_one_atomic_update(_fake_store):
    sink = _fake_store([("m1",)])
    assert tg.claim_lost_manifests(["m1", "m2"]) == ["m1"]
    sql, params = sink[0]
    assert sql.startswith("UPDATE tg_approvals SET lost_notified_at")
    assert "lost_notified_at IS NULL" in sql and "status = 'APPROVED'" in sql
    assert "server = 'ticktick'" in sql and "RETURNING manifest_id" in sql
    assert params[1] == ["m1", "m2"]


def test_claim_lost_manifests_without_store_is_silent(monkeypatch):
    monkeypatch.setattr(tg, "_pg_pool", None)
    assert tg.claim_lost_manifests(["m1"]) == []
    assert tg.claim_lost_manifests([]) == []


def test_clear_inline_keyboard_keeps_the_plan_text(monkeypatch):
    """Кнопки убираем, ТЕКСТ ПЛАНА оставляем: когда исполнять было нечего,
    план — единственное, что осталось у владельца от его же просьбы, и по
    нему он решает, что повторять."""
    calls = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: calls.append((method, body))
                        or {"ok": True})
    cfg = tg.TgApprovalConfig(enabled=True, bot_token="x", owner_chat_id="1",
                              server="ticktick", tools_allowlist=None, ttl_s=60)
    assert tg.clear_inline_keyboard(cfg, "c1", 42) is True
    method, body = calls[0]
    assert method == "editMessageReplyMarkup"
    assert "text" not in body
    assert body["reply_markup"] == {"inline_keyboard": []}


def test_clear_inline_keyboard_without_message_id_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: calls.append(1) or {"ok": True})
    cfg = tg.TgApprovalConfig(enabled=True, bot_token="x", owner_chat_id="1",
                              server="ticktick", tools_allowlist=None, ttl_s=60)
    assert tg.clear_inline_keyboard(cfg, "c1", None) is False
    assert calls == []


def test_owner_time_str_is_readable_and_never_raises():
    assert "время неизвестно" == tg.owner_time_str(None)
    assert "время неизвестно" == tg.owner_time_str("не число")
    assert tg.owner_time_str(1_700_000_000_000).startswith("2023-")
