"""Команда `/automation_key` и её три кнопки в Telegram (docs/TZ/
TZ_temp_automation_key.md §3.2/§3.3, тестовый план §6, пункт 4: «генерировать
/ отзывать может только владелец»).

Нет реальной сети, нет реального Postgres — Telegram HTTP и automation_key's
хранилище monkeypatch'нуты, та же дисциплина, что у
tests/test_own_bot_webhook.py (owner-only разбор callback_query/message)."""
import ticktick_mcp.src.automation_key as ak
import ticktick_mcp.src.tg_approval as tg


def _cfg(owner="1"):
    return tg.TgApprovalConfig(enabled=True, bot_token="own-token", owner_chat_id=owner,
                               server="ticktick", tools_allowlist=None, ttl_s=3600,
                               own_bot=True, webhook_secret="whsecret")


def _command_update(text="/automation_key", from_id="1", chat_id="1"):
    msg = {"text": text}
    if from_id is not None:
        msg["from"] = {"id": from_id}
    msg["chat"] = {"id": chat_id}
    return {"message": msg}


def _ak_callback_update(data="ak:show", from_id="1", chat_id="1", cq_id="cbq1"):
    cq = {"id": cq_id, "data": data}
    if from_id is not None:
        cq["from"] = {"id": from_id}
    cq["message"] = {"chat": {"id": chat_id}, "message_id": 42}
    return {"callback_query": cq}


# ═══════════════ /automation_key — только владелец видит меню ═══════════════

def test_command_from_owner_sends_the_three_button_menu(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(), _command_update())

    assert len(sent) == 1
    method, body = sent[0]
    assert method == "sendMessage"
    assert body["chat_id"] == "1"
    buttons = [b["callback_data"] for row in body["reply_markup"]["inline_keyboard"]
              for b in row]
    assert buttons == ["ak:show", "ak:off", "ak:status"]


def test_command_from_non_owner_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(owner="999"), _command_update(from_id="1"))

    assert sent == []


def test_command_without_from_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(), _command_update(from_id=None))

    assert sent == []


def test_unrelated_message_text_is_ignored(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(), _command_update(text="привет, бот"))

    assert sent == []


def test_command_with_bot_username_suffix_still_matches(monkeypatch):
    """Группы шлют `/automation_key@bot_name` — та же команда."""
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(), _command_update(text="/automation_key@maksim_mcp_bot"))

    assert len(sent) == 1


# ═══════════════ Кнопка «Показать ключ» — только владелец ═══════════════

def test_show_button_from_owner_generates_and_sends_the_token(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: calls.append(chat_id) or "raw-token-xyz")
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:show"))

    assert calls == ["1"], "generate_window не вызван (или вызван не для того чата)"
    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "raw-token-xyz" in send_calls[0]["text"]
    answer_calls = [b for m, b in sent if m == "answerCallbackQuery"]
    assert len(answer_calls) == 1


def test_show_button_from_non_owner_never_calls_generate_window(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: calls.append(chat_id) or "leaked-token")
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(owner="999"), _ak_callback_update(data="ak:show", from_id="1"))

    assert calls == [], "владелец НЕ подтверждён, но ключ всё равно сгенерирован"
    assert sent == [], "владелец НЕ подтверждён, но что-то ушло в Telegram"


def test_show_button_empty_token_reports_store_not_ready(monkeypatch):
    """generate_window вернул "" (хранилище не поднято) — владелец обязан
    получить внятное сообщение, а не пустой/сломанный текст."""
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: "")
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:show"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "не поднято" in send_calls[0]["text"] or "🛑" in send_calls[0]["text"]


# ═══════════════ Кнопка «Выключить» — только владелец ═══════════════

def test_off_button_from_owner_revokes(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "revoke_window", lambda chat_id: calls.append(chat_id) or True)
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:off"))

    assert calls == ["1"]
    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert "Окно выключено" in send_calls[0]["text"]


def test_off_button_from_non_owner_never_calls_revoke_window(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "revoke_window", lambda chat_id: calls.append(chat_id) or True)
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: {"ok": True})

    tg.handle_webhook(_cfg(owner="999"), _ak_callback_update(data="ak:off", from_id="1"))

    assert calls == [], "владелец НЕ подтверждён, но окно всё равно отозвано"


# ═══════════════ Кнопка «Статус» ═══════════════

def test_status_button_reports_no_window_ever_created(monkeypatch):
    monkeypatch.setattr(ak, "window_status", lambda chat_id: None)
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:status"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "ни разу не выдавалось" in send_calls[0]["text"]


def test_status_button_reports_active_window(monkeypatch):
    monkeypatch.setattr(ak, "window_status", lambda chat_id: {
        "active": True, "revoked": False, "expired": False,
        "created_at": 1000, "expires_at": 2000, "revoked_at": None,
        "created_by_chat": chat_id, "remaining_s": 3600,
    })
    sent = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: sent.append((method, body)) or {"ok": True})

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:status"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert "активно" in send_calls[0]["text"]


def test_status_button_from_non_owner_calls_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "window_status", lambda chat_id: calls.append(chat_id))
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: {"ok": True})

    tg.handle_webhook(_cfg(owner="999"), _ak_callback_update(data="ak:status", from_id="1"))

    assert calls == []


# ═══════ Приблуда для сверки: ak: не путается с a:/r: приблудами гейта ═══════

def test_ak_callback_does_not_trip_the_approval_decision_path(monkeypatch):
    """`ak:show` не должен матчиться `_CALLBACK_DATA_RE` (a:/r:) и вызывать
    consume_tg_decision — иначе кнопка меню случайно "подтверждала" бы
    несуществующий манифест с id "show"."""
    consume_calls = []
    monkeypatch.setattr(tg, "consume_tg_decision", lambda *a: consume_calls.append(a))
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: "tok")
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: {"ok": True})

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:show"))

    assert consume_calls == []
