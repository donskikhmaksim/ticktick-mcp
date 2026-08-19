"""После «🔴 Отклонить» в личке обязан остаться честный терминальный след.

Дефект (найден разведкой 2026-08-19): при отказе владелец видел только
эфемерный тост `answerCallbackQuery` («Отклонено», исчезает через секунды) и
снятые кнопки на НЕизменённом тексте плана — в чате не оставалось никакого
следа, что план отменён. Докстринг `clear_inline_keyboard` при этом обещал,
что «объяснение приходит отдельным сообщением», но в reject-ветке такого
сообщения никто не слал (оно есть только на lost-plan пути).

Фикс: reject-ветка `_handle_callback_query` редактирует исходное сообщение
плана (`mark_rejected_in_owner_chat` → тот же транспорт, что у approve-итога,
`summarize_in_owner_chat`): терминальная строка «🛑 Отклонено — ничего не
сделано» ПЕРВОЙ, ниже — сам отклонённый план для справки. Один вызов
`editMessageText` меняет текст И снимает кнопки; если правка не удалась —
резервное снятие кнопок прежним `clear_inline_keyboard`.

Все тесты работают при `_pg_pool = None` (store не готов) СОЗНАТЕЛЬНО: это
одновременно регресс-тест второй находки — `get_tg_approval`, позванная из
вебхука (6d36dbe, таймеры удаления кусков плана), падала AttributeError без
гарда `store_ready()`, то есть нажатие кнопки роняло вебхук в конфигурации
без Postgres. Никакой реальной сети/БД — та же дисциплина, что у
tests/test_own_bot_webhook.py.
"""
import ticktick_mcp.src.tg_approval as tg


def _cfg(owner="1"):
    return tg.TgApprovalConfig(enabled=True, bot_token="own-token",
                               owner_chat_id=owner, server="ticktick",
                               tools_allowlist=None, ttl_s=3600,
                               own_bot=True, webhook_secret="whsecret")


def _cq_update(data="r:m1", from_id="1", chat_id="1", message_id=42,
               cq_id="cbq1", text=None, include_message=True):
    cq = {"id": cq_id, "from": {"id": from_id}, "data": data}
    if include_message:
        msg = {"chat": {"id": chat_id}, "message_id": message_id}
        if text is not None:
            msg["text"] = text
        cq["message"] = msg
    return {"callback_query": cq}


def _wire(monkeypatch, edit_ok=True):
    """Фейковый Telegram: собирает все вызовы `_tg_call`, store выключен."""
    calls = []

    def fake_tg_call(cfg, method, body):
        calls.append((method, body))
        if method == "editMessageText" and not edit_ok:
            return {"ok": False, "description": "message can't be edited"}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(tg, "_tg_call", fake_tg_call)
    monkeypatch.setattr(tg, "_pg_pool", None)  # store_ready() → False
    monkeypatch.setattr(tg, "consume_tg_decision",
                        lambda mid, status: {"chat_id": "1", "message_id": 42})
    return calls


def _edits(calls):
    return [b for m, b in calls if m == "editMessageText"]


def _answers(calls):
    return [b for m, b in calls if m == "answerCallbackQuery"]


def test_reject_edits_plan_message_with_terminal_mark(monkeypatch):
    """Главный след: reject редактирует сообщение плана терминальной строкой
    (а не только снимает кнопки), и правка сама снимает кнопки."""
    calls = _wire(monkeypatch)
    tg.handle_webhook(_cfg(), _cq_update(data="r:m1", text="какой-то план"))
    edits = _edits(calls)
    assert edits, "reject не отредактировал сообщение плана — следа не осталось"
    body = edits[0]
    assert "Отклонено — ничего не сделано" in body["text"]
    assert body["reply_markup"] == {"inline_keyboard": []}
    assert body["chat_id"] == "1" and body["message_id"] == 42
    # тост остаётся тостом — последним, после следа
    assert _answers(calls) and _answers(calls)[-1]["text"] == "Отклонено"


def test_reject_mark_keeps_plan_text_for_reference(monkeypatch):
    """Отклонённый план сохраняется под терминальной строкой: владелец,
    передумавший через полминуты, должен видеть, ЧТО он отклонил."""
    calls = _wire(monkeypatch)
    plan = "План удаления 3 задач из проекта Работа"
    tg.handle_webhook(_cfg(), _cq_update(data="r:m1", text=plan))
    body = _edits(calls)[0]
    assert plan in body["text"]
    # терминальная строка стоит РАНЬШЕ плана (заголовок первым)
    assert body["text"].find("Отклонено") < body["text"].find(plan)


def test_reject_mark_does_not_look_like_success(monkeypatch):
    """Формулировка не должна читаться как успех: без ✅, без «подтверждено»,
    без «затронуто объектов»."""
    calls = _wire(monkeypatch)
    tg.handle_webhook(_cfg(), _cq_update(data="r:m1", text="план"))
    text = _edits(calls)[0]["text"]
    low = text.lower()
    assert "✅" not in text
    assert "подтвержд" not in low
    assert "затронуто" not in low


def test_reject_without_plan_text_still_leaves_the_mark(monkeypatch):
    """callback_query без текста сообщения (теоретически возможно) — след всё
    равно остаётся, просто без хвоста «для справки»."""
    calls = _wire(monkeypatch)
    tg.handle_webhook(_cfg(), _cq_update(data="r:m1", text=None))
    assert _edits(calls), "без текста плана след пропал вовсе"
    assert "Отклонено — ничего не сделано" in _edits(calls)[0]["text"]


def test_reject_long_plan_is_capped_to_one_message(monkeypatch):
    """Очень длинный план обрезается с «…»: правка обязана влезть в один
    editMessageText (4096), иначе Telegram ответит 400 и следа не будет."""
    calls = _wire(monkeypatch)
    plan = "х" * 10_000
    tg.handle_webhook(_cfg(), _cq_update(data="r:m1", text=plan))
    body = _edits(calls)[0]
    assert "…" in body["text"]
    assert len(body["text"]) <= tg.TELEGRAM_TEXT_LIMIT


def test_reject_edit_failure_falls_back_to_clearing_buttons(monkeypatch):
    """Правка не удалась (сообщение старше 48ч, стёрто руками) — кнопки всё
    равно снимаются прежним editMessageReplyMarkup, чтобы не вводили в
    заблуждение."""
    calls = _wire(monkeypatch, edit_ok=False)
    tg.handle_webhook(_cfg(), _cq_update(data="r:m1", text="план"))
    methods = [m for m, _ in calls]
    assert "editMessageText" in methods
    assert "editMessageReplyMarkup" in methods, \
        "editMessageText упал, а кнопки так и остались висеть"


def test_approve_path_is_untouched(monkeypatch):
    """Approve ведёт себя как раньше: только снятие кнопок; текст плана НЕ
    трогается (итог в него впишет поллер после исполнения и перепроверки)."""
    calls = _wire(monkeypatch)
    tg.handle_webhook(_cfg(), _cq_update(data="a:m1", text="план"))
    assert _edits(calls) == []
    assert any(m == "editMessageReplyMarkup" for m, _ in calls)
    assert _answers(calls)[-1]["text"] == "Подтверждено"


def test_mark_rejected_without_message_id_is_a_quiet_false(monkeypatch):
    """message_id нет — некуда писать: False без исключений (та же дисциплина,
    что у summarize_in_owner_chat)."""
    calls = _wire(monkeypatch)
    assert tg.mark_rejected_in_owner_chat(_cfg(), "1", None, "план") is False
    assert _edits(calls) == []


def test_get_tg_approval_without_store_returns_none(monkeypatch):
    """Регресс-тест второй находки: `get_tg_approval` зовётся прямо из
    вебхука (таймеры удаления кусков плана, 6d36dbe) и обязана переживать
    неготовый store, а не падать AttributeError на `_pg_pool.getconn()`."""
    monkeypatch.setattr(tg, "_pg_pool", None)
    assert tg.get_tg_approval("m1") is None
