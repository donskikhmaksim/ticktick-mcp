"""Команда `/automation_key` и её кнопки в Telegram.

Базовый контракт (владелец-only) — docs/TZ/TZ_temp_automation_key.md
§3.2/§3.3, тестовый план §6, пункт 4: «генерировать/отзывать может только
владелец». Многооконный контракт (без аргумента = сразу генерирует
ОЧЕРЕДНОЕ окно; list/revoke <id>/off) — docs/TZ/TZ_multi_automation_windows.md
«Команды в Telegram».

Нет реальной сети, нет реального Postgres — Telegram HTTP и automation_key's
хранилище monkeypatch'нуты, та же дисциплина, что у
tests/test_own_bot_webhook.py (owner-only разбор callback_query/message).

АДАПТАЦИЯ ПОД НОВЫЙ КОНТРАКТ (не подгонка — сама команда сменила поведение).
Раньше `/automation_key` без аргумента и кнопка `ak:show` ТОЛЬКО показывали
меню трёх кнопок («Показать ключ» / «Выключить» / «Статус») — генерация была
ОТДЕЛЬНЫМ шагом (нажатием кнопки). Аддендум прямо требует: команда без
аргумента (и кнопка «Новый ключ», `ak:new`) сразу генерируют ОЧЕРЕДНОЕ окно
и присылают токен — «Статус» одного окна перестал быть осмысленным понятием
при множестве окон и заменён «Списком» (`ak:list`/`list`). Тесты, проверявшие
старое меню-без-генерации и кнопку/команду «Статус», переписаны под новое
поведение; тесты владелец-only дисциплины (кто может жать/писать) и
owner-only разбора остались по смыслу теми же, только под новые имена
действий."""
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
    """`_tg_call` заглушка, которая копит (method, body) и возвращает
    успешный ответ sendMessage с фиктивным message_id — нужен, чтобы
    `_ak_do_generate`'s `schedule_message_delete` не падал на отсутствующем
    `result`."""
    sent = []

    def fake(cfg, method, body):
        sent.append((method, body))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 999}}
        return {"ok": True}

    monkeypatch.setattr(tg, "_tg_call", fake)
    return sent


# ═══════════════ /automation_key без аргумента — только владелец ═══════════

def test_command_from_owner_generates_and_sends_the_token(monkeypatch):
    """Без аргумента команда теперь СРАЗУ генерирует — не просто открывает
    меню (TZ_multi_automation_windows.md)."""
    calls = []
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: calls.append(chat_id) or "raw-token-xyz")
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update())

    assert calls == ["1"], "generate_window не вызван (или вызван не для того чата)"
    send_calls = [b for m, b in sent if m == "sendMessage"]
    # Токен-сообщение + меню для дальнейших действий.
    assert len(send_calls) == 2
    assert "raw-token-xyz" in send_calls[0]["text"]
    buttons = [b["callback_data"] for row in send_calls[1]["reply_markup"]["inline_keyboard"]
              for b in row]
    assert buttons == ["ak:new", "ak:list", "ak:offall"]


def test_command_from_non_owner_sends_nothing(monkeypatch):
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(owner="999"), _command_update(from_id="1"))

    assert sent == []


def test_command_without_from_sends_nothing(monkeypatch):
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(from_id=None))

    assert sent == []


def test_unrelated_message_text_is_ignored(monkeypatch):
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(text="привет, бот"))

    assert sent == []


def test_command_with_bot_username_suffix_still_matches(monkeypatch):
    """Группы шлют `/automation_key@bot_name` — та же команда."""
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: "")  # хранилище не поднято
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(text="/automation_key@maksim_mcp_bot"))

    assert len(sent) == 1  # "не поднято" — единственное сообщение, без меню


def test_command_with_unknown_argument_reports_a_hint(monkeypatch):
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(text="/automation_key blah"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "Не понял аргумент" in send_calls[0]["text"]


# ═══════════════ Кнопка/команда «Новый ключ» — только владелец ═══════════════

def test_new_button_from_owner_generates_and_sends_the_token(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: calls.append(chat_id) or "raw-token-xyz")
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:new"))

    assert calls == ["1"], "generate_window не вызван (или вызван не для того чата)"
    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert "raw-token-xyz" in send_calls[0]["text"]
    answer_calls = [b for m, b in sent if m == "answerCallbackQuery"]
    assert len(answer_calls) == 1


def test_new_button_does_not_disturb_other_already_generated_windows(monkeypatch):
    """`generate_window` в новом контракте — INSERT, не UPSERT: этот тест
    проверяет только то, что обработчик передаёт chat_id и не пытается сам
    что-то отзывать/трогать перед генерацией (сама изоляция окон друг от
    друга — automation_key.py's ответственность, покрыта
    tests/test_automation_key_windows.py)."""
    calls = []

    def fake_generate(chat_id):
        calls.append(chat_id)
        return f"token-{len(calls)}"

    monkeypatch.setattr(ak, "generate_window", fake_generate)
    revoke_calls = []
    monkeypatch.setattr(ak, "revoke_window", lambda *a, **k: revoke_calls.append(a) or False)
    monkeypatch.setattr(ak, "revoke_all_windows", lambda *a, **k: revoke_calls.append(a) or 0)
    _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:new"))
    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:new"))

    assert calls == ["1", "1"]
    assert revoke_calls == [], "генерация НЕ должна вызывать revoke ни разу"


def test_new_button_from_non_owner_never_calls_generate_window(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: calls.append(chat_id) or "leaked-token")
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(owner="999"), _ak_callback_update(data="ak:new", from_id="1"))

    assert calls == [], "владелец НЕ подтверждён, но ключ всё равно сгенерирован"
    assert sent == [], "владелец НЕ подтверждён, но что-то ушло в Telegram"


def test_new_button_empty_token_reports_store_not_ready(monkeypatch):
    """generate_window вернул "" (хранилище не поднято) — владелец обязан
    получить внятное сообщение, а не пустой/сломанный текст."""
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: "")
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:new"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "не поднято" in send_calls[0]["text"] or "🛑" in send_calls[0]["text"]


def test_new_button_schedules_the_token_message_for_10s_deletion(monkeypatch):
    """TZ_multi_automation_windows.md: сообщение со свежим токеном
    самоудаляется через 10с, не через дефолтные 60с гейта."""
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: "raw-token-xyz")
    scheduled = []
    monkeypatch.setattr(tg, "schedule_message_delete",
                        lambda chat_id, message_id, delay_s=None: scheduled.append(
                            (chat_id, message_id, delay_s)))
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: (
        {"ok": True, "result": {"message_id": 555}} if method == "sendMessage" else {"ok": True}))

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:new"))

    assert len(scheduled) == 1
    chat_id, message_id, delay_s = scheduled[0]
    assert chat_id == "1"
    assert message_id == 555
    assert delay_s == 10.0, f"ожидал delay_s=10, получил {delay_s!r}"


# ═══════════════ Кнопка/команда «Выключить всё» — только владелец ═══════════

def test_offall_button_from_owner_revokes_everything(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "revoke_all_windows", lambda chat_id: calls.append(chat_id) or 2)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:offall"))

    assert calls == ["1"]
    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert "Выключено окон: 2" in send_calls[0]["text"]


def test_offall_button_with_nothing_active_reports_that(monkeypatch):
    monkeypatch.setattr(ak, "revoke_all_windows", lambda chat_id: 0)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:offall"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert "и так не было" in send_calls[0]["text"]


def test_off_text_command_revokes_everything(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "revoke_all_windows", lambda chat_id: calls.append(chat_id) or 1)
    _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(text="/automation_key off"))

    assert calls == ["1"]


def test_offall_button_from_non_owner_never_calls_revoke_all_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "revoke_all_windows", lambda chat_id: calls.append(chat_id) or 1)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(owner="999"), _ak_callback_update(data="ak:offall", from_id="1"))

    assert calls == [], "владелец НЕ подтверждён, но окна всё равно погашены"
    assert sent == [], "владелец НЕ подтверждён, но что-то ушло в Telegram"


# ═══════════════ Кнопка/команда «Список» ═══════════════

def test_list_button_reports_no_windows(monkeypatch):
    monkeypatch.setattr(ak, "list_windows", lambda chat_id: [])
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:list"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1
    assert "нет" in send_calls[0]["text"]


def test_list_button_reports_active_windows_with_per_row_revoke_buttons(monkeypatch):
    monkeypatch.setattr(ak, "list_windows", lambda chat_id: [
        {"window_id": "aaa111", "label": None, "created_at": 1000,
         "expires_at": 2000, "created_by_chat": "1", "remaining_s": 3600},
        {"window_id": "bbb222", "label": "чат Б", "created_at": 1500,
         "expires_at": 2500, "created_by_chat": "1", "remaining_s": 7200},
    ])
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:list"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert "aaa111" in send_calls[0]["text"]
    assert "bbb222" in send_calls[0]["text"] and "чат Б" in send_calls[0]["text"]
    buttons = [b["callback_data"] for row in send_calls[0]["reply_markup"]["inline_keyboard"]
              for b in row]
    assert "ak:revoke:aaa111" in buttons
    assert "ak:revoke:bbb222" in buttons


def test_list_text_command_works(monkeypatch):
    monkeypatch.setattr(ak, "list_windows", lambda chat_id: [])
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(text="/automation_key list"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1


def test_list_button_from_non_owner_calls_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "list_windows", lambda chat_id: calls.append(chat_id) or [])
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(owner="999"), _ak_callback_update(data="ak:list", from_id="1"))

    assert calls == []
    assert sent == [], "владелец НЕ подтверждён, но что-то ушло в Telegram"


# ═══════════════ Кнопка/команда «Отозвать <id>» ═══════════════

def test_revoke_button_from_owner_revokes_that_specific_window(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "revoke_window", lambda window_id, chat_id: calls.append((window_id, chat_id)) or True)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:revoke:aaa111"))

    assert calls == [("aaa111", "1")]
    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert "aaa111" in send_calls[0]["text"] and "выключено" in send_calls[0]["text"]


def test_revoke_button_reports_when_window_is_already_gone(monkeypatch):
    monkeypatch.setattr(ak, "revoke_window", lambda window_id, chat_id: False)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _ak_callback_update(data="ak:revoke:aaa111"))

    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert "не найдено" in send_calls[0]["text"] or "неактивно" in send_calls[0]["text"]


def test_revoke_text_command_with_id_works(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "revoke_window", lambda window_id, chat_id: calls.append((window_id, chat_id)) or True)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(text="/automation_key revoke aaa111"))

    assert calls == [("aaa111", "1")]
    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert len(send_calls) == 1 and "aaa111" in send_calls[0]["text"]


def test_revoke_text_command_without_id_asks_for_one(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "revoke_window", lambda *a: calls.append(a) or True)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(), _command_update(text="/automation_key revoke"))

    assert calls == []
    send_calls = [b for m, b in sent if m == "sendMessage"]
    assert "id" in send_calls[0]["text"]


def test_revoke_button_from_non_owner_never_calls_revoke_window(monkeypatch):
    calls = []
    monkeypatch.setattr(ak, "revoke_window", lambda window_id, chat_id: calls.append((window_id, chat_id)) or True)
    sent = _sent_recorder(monkeypatch)

    tg.handle_webhook(_cfg(owner="999"), _ak_callback_update(data="ak:revoke:aaa111", from_id="1"))

    assert calls == []
    assert sent == [], "владелец НЕ подтверждён, но что-то ушло в Telegram"


# ═══════ Приблуда для сверки: ak: не путается с a:/r: приблудами гейта ═══════

def test_ak_callback_does_not_trip_the_approval_decision_path(monkeypatch):
    """`ak:new` не должен матчиться `_CALLBACK_DATA_RE` (a:/r:) и вызывать
    consume_tg_decision — иначе кнопка меню случайно "подтверждала" бы
    несуществующий манифест с id "new"."""
    consume_calls = []
    monkeypatch.setattr(tg, "consume_tg_decision", lambda *a: consume_calls.append(a))
    monkeypatch.setattr(ak, "generate_window", lambda chat_id: "tok")
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
