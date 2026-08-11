"""Команда `/automation_key` и кнопки `ak:*` в Telegram — ПОСЛЕ переезда
генерации в gmail-mcp (docs/TZ/TZ_automation_key_hub.md, 2026-08-11).

АДАПТАЦИЯ ПОД НОВЫЙ КОНТРАКТ (не подгонка под правку — сама команда сменила
поведение по решению владельца). До этого файла (см. git-историю) команда и
кнопки реально генерировали/листали/отзывали временные окна прямо в
ticktick-mcp. После консолидации ботов ticktick-mcp больше не держит
собственный вебхук — Telegram физически не может доставить сюда апдейт, а
владелец явно попросил единый механизм на всю экосистему MCP-серверов:
генерация/список/отзыв переехали ЦЕЛИКОМ в gmail-mcp (общая таблица со
`scope`, кнопки с выбором сервисов). Ticktick-mcp теперь ТОЛЬКО проверяет
присланный ключ (`automation_key.check_window`/`find_window`, покрыто
`tests/test_automation_key_windows.py`) — сама Telegram-точка входа здесь
осталась исключительно ради явного редиректа («такой команды больше нет
тут, она в gmail-mcp»), а не молчаливого игнора: владелец, наткнувшись на
старую привычку или старую кнопку в чате, обязан получить понятный ответ,
а не тишину.

Нет реальной сети — `_tg_call` monkeypatch'нут, та же дисциплина, что у
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


def _ak_callback_update(data="ak:new", from_id="1", chat_id="1", cq_id="cbq1"):
    cq = {"id": cq_id, "data": data}
    if from_id is not None:
        cq["from"] = {"id": from_id}
    cq["message"] = {"chat": {"id": chat_id}, "message_id": 42}
    return {"callback_query": cq}


def _sent_recorder(monkeypatch):
    sent = []

    def fake(cfg, method, body):
        sent.append((method, body))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 999}}
        return {"ok": True}

    monkeypatch.setattr(tg, "_tg_call", fake)
    return sent


def _no_lifecycle_calls(monkeypatch):
    """Приколачивает generate/list/revoke/revoke_all так, чтобы любой вызов
    провалил тест — редирект обязан отвечать текстом, НЕ трогая жизненный
    цикл окон (та часть теперь целиком у gmail-mcp)."""
    def boom(name):
        def _f(*a, **k):
            raise AssertionError(f"automation_key.{name} не должен вызываться "
                                 f"из ticktick-mcp Telegram-редиректа")
        return _f
    monkeypatch.setattr(ak, "generate_window", boom("generate_window"))
    monkeypatch.setattr(ak, "list_windows", boom("list_windows"))
    monkeypatch.setattr(ak, "revoke_window", boom("revoke_window"))
    monkeypatch.setattr(ak, "revoke_all_windows", boom("revoke_all_windows"))


# ═══════════════ /automation_key (текст) — редирект, только владелец ═══════

def test_command_from_owner_gets_redirect_to_gmail_mcp(monkeypatch):
    _no_lifecycle_calls(monkeypatch)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update())

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "gmail-mcp" in send_calls[0]["text"]


def test_command_with_any_argument_still_only_redirects(monkeypatch):
    """list/revoke/off/мусор — раньше это были разные под-команды, теперь
    ЛЮБОЙ аргумент после `/automation_key` ведёт к тому же редиректу, без
    разбора аргумента вообще (разбор аргументов переехал в gmail-mcp)."""
    for text in ("/automation_key list", "/automation_key revoke aaa111",
                "/automation_key off", "/automation_key blah"):
        _no_lifecycle_calls(monkeypatch)
        sent = _sent_recorder(monkeypatch)

        tg.handle_webhook(_cfg(), _command_update(text=text))

        send_calls = [b for m, b in sent if m == "sendMessage"]
        assert len(send_calls) == 1, f"неожиданно {len(send_calls)} сообщений для {text!r}"
        assert "gmail-mcp" in send_calls[0]["text"]


def test_command_from_non_owner_sends_nothing(monkeypatch):
    _no_lifecycle_calls(monkeypatch)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(owner="999"), _command_update(from_id="1"))

    assert sent == []


def test_command_without_from_sends_nothing(monkeypatch):
    _no_lifecycle_calls(monkeypatch)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(from_id=None))

    assert sent == []


def test_unrelated_message_text_is_ignored(monkeypatch):
    _no_lifecycle_calls(monkeypatch)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(text="привет, бот"))

    assert sent == []


def test_command_with_bot_username_suffix_still_matches(monkeypatch):
    """Группы шлют `/automation_key@bot_name` — команда распознаётся
    (регистрация вебхука на bot_name здесь не проверяется, это забота
    Telegram), редирект уходит как обычно."""
    _no_lifecycle_calls(monkeypatch)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(text="/automation_key@maksim_mcp_bot"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "gmail-mcp" in send_calls[0]["text"]


# ═══════════════ Кнопки ak:* — редирект через answerCallbackQuery ═══════════

def test_any_ak_button_from_owner_answers_with_redirect_no_sendmessage(monkeypatch):
    """Старая кнопка (`ak:new`/`ak:list`/`ak:offall`/`ak:revoke:<id>`), если
    она осталась висеть в чате от версии до переезда — нажатие отвечает
    ЧЕРЕЗ `answerCallbackQuery` (не через новое `sendMessage`), редирект в
    тексте всплывающей подсказки. Никакого вызова жизненного цикла окон."""
    for data in ("ak:new", "ak:list", "ak:offall", "ak:revoke:aaa111"):
        _no_lifecycle_calls(monkeypatch)
        sent = _sent_recorder(monkeypatch)

        tg.handle_webhook(_cfg(), _ak_callback_update(data=data))

        assert [b for m, b in sent if m == "sendMessage"] == [], f"{data!r}: лишнее sendMessage"
        answer_calls = [b for m, b in sent if m == "answerCallbackQuery"]
        assert len(answer_calls) == 1, f"{data!r}: ожидал ровно один answerCallbackQuery"
        assert "gmail-mcp" in answer_calls[0]["text"]


def test_ak_button_from_non_owner_does_nothing(monkeypatch):
    _no_lifecycle_calls(monkeypatch)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(owner="999"), _ak_callback_update(data="ak:new", from_id="1"))

    assert sent == [], "владелец НЕ подтверждён, но что-то ушло в Telegram"


# ═══════ Приблуда для сверки: ak: не путается с a:/r: приблудами гейта ═══════

def test_ak_callback_does_not_trip_the_approval_decision_path(monkeypatch):
    """`ak:new` не должен матчиться `_CALLBACK_DATA_RE` (a:/r:) и вызывать
    consume_tg_decision — иначе кнопка меню случайно "подтверждала" бы
    несуществующий манифест с id "new"."""
    consume_calls = []
    monkeypatch.setattr(tg, "consume_tg_decision", lambda *a: consume_calls.append(a))
    _no_lifecycle_calls(monkeypatch)
    _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:new"))

    assert consume_calls == []


def test_malformed_ak_callback_data_is_ignored(monkeypatch):
    """`ak:revoke:` без hex-id (или с мусором) не должен матчить
    `_AK_CALLBACK_RE` вовсе — не проваливается ни в один из известных action,
    ни путается с a:/r:."""
    sent = _sent_recorder(monkeypatch)
    consume_calls = []
    monkeypatch.setattr(tg, "consume_tg_decision", lambda *a: consume_calls.append(a))

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:revoke:not-hex!"))

    assert sent == []
    assert consume_calls == []
