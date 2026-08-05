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

def test_notify_plan_skipped_when_tool_not_enabled(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    called = {"n": 0}
    monkeypatch.setattr(tg, "notify_plan", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (True, ""))
    out = s._maybe_tg_notify_plan("delete_tasks", "m1", "### план")
    assert out == "### план"  # untouched
    assert called["n"] == 0


def test_notify_plan_success_appends_telegram_note(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    monkeypatch.setattr(tg, "notify_plan", lambda *a, **k: (True, ""))
    out = s._maybe_tg_notify_plan("delete_tasks", "m1", "### план")
    assert "### план" in out
    assert "Telegram" in out


def test_notify_plan_failure_invalidates_manifest_fail_closed(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))
    monkeypatch.setattr(tg, "notify_plan", lambda *a, **k: (False, "sendMessage failed"))
    s._MANIFESTS["m-fail-test"] = _fresh_manifest()
    out = s._maybe_tg_notify_plan("delete_tasks", "m-fail-test", "### план")
    assert "🛑" in out
    assert "Не смог отправить" in out
    assert s._MANIFESTS["m-fail-test"]["consumed"] is True
