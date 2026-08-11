"""Исчезновение сообщений гейта (docs/TZ/TZ_temp_automation_key.md §5,
тестовый план §6, пункты 11-14):

 11. Сообщение с планом НЕ удаляется, пока не пришёл ответ — даже если
     прошло больше минуты ожидания.
 12. Сообщение с планом удаляется примерно через минуту ПОСЛЕ ответа (не от
     момента отправки).
 13. Отчёт удаляется примерно через минуту после отправки.
 14. Сбой удаления (сообщение уже стёрто/чат недоступен) не роняет поллер и
     не переотправляет ошибку владельцу.

Нет реальной сети, нет реального Postgres — `_tg_call` и время
monkeypatch'нуты, та же дисциплина, что у остальных тестов tg_approval.py."""
import ticktick_mcp.src.tg_approval as tg


def _cfg():
    return tg.TgApprovalConfig(enabled=True, bot_token="x", owner_chat_id="1",
                               server="ticktick", tools_allowlist=None, ttl_s=3600)


def setup_function(_):
    """Список расписанных удалений — общий модульный список; каждый тест
    начинает с чистого листа, иначе соседние тесты видят чужие записи."""
    tg._SCHEDULED_DELETES.clear()


# ═══════════════ Механизм: schedule/sweep сами по себе ═══════════════

def test_schedule_then_sweep_deletes_after_the_delay(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(tg.time, "time", lambda: now[0])
    deleted = []
    monkeypatch.setattr(tg, "delete_message",
                        lambda cfg, chat_id, message_id: deleted.append((chat_id, message_id)) or True)

    tg.schedule_message_delete("42", 100)

    now[0] += 30  # меньше минуты
    assert tg.sweep_scheduled_deletes(_cfg()) == 0
    assert deleted == []

    now[0] += 40  # суммарно 70с — минута прошла
    assert tg.sweep_scheduled_deletes(_cfg()) == 1
    assert deleted == [("42", 100)]


def test_sweep_removes_entry_from_the_queue_regardless_of_outcome(monkeypatch):
    """Удалённая (или НЕ удалённая — не важно) запись покидает очередь: иначе
    поллер пытался бы удалить одно и то же сообщение на каждом тике вечно."""
    now = [1_000_000.0]
    monkeypatch.setattr(tg.time, "time", lambda: now[0])
    monkeypatch.setattr(tg, "delete_message", lambda cfg, c, m: False)  # "не вышло"

    tg.schedule_message_delete("42", 100)
    now[0] += 120
    assert tg.sweep_scheduled_deletes(_cfg()) == 0  # delete_message вернул False
    assert tg._SCHEDULED_DELETES == [], "запись осталась в очереди после свипа"


def test_schedule_with_missing_chat_or_message_is_a_noop():
    tg.schedule_message_delete(None, 100)
    tg.schedule_message_delete("42", None)
    assert tg._SCHEDULED_DELETES == []


def test_default_delay_is_about_one_minute():
    assert 55 <= tg._GATE_MESSAGE_DELETE_DELAY_S <= 65


# ═══════ 11. План НЕ удаляется, пока не пришёл ответ — даже после минуты ═══

def test_notify_plan_never_schedules_a_delete_by_itself(monkeypatch):
    """Отправка плана (notify_plan) — это МОМЕНТ ОТПРАВКИ, не ответ. Она не
    имеет права поставить сообщение в очередь на удаление вовсе, сколько бы
    времени потом ни прошло без ответа."""
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: {"ok": True, "result": {"message_id": 777}})
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "create_tg_approval", lambda *a, **k: None)
    monkeypatch.setattr(tg, "attach_plan_messages", lambda *a, **k: True)

    ok, err = tg.notify_plan(_cfg(), "m1", "план текст", "create_tag")

    assert ok, err
    assert tg._SCHEDULED_DELETES == [], (
        "notify_plan поставил сообщение в очередь на удаление — а должен "
        "был дождаться ответа")


def test_callback_before_decision_consumed_never_schedules_a_delete(monkeypatch):
    """Симулирует «прошла минута, кнопку ещё не нажали»: пока
    consume_tg_decision не вернул решение (строка уже не PENDING / чужой
    manifest_id), ничего в очередь не попадает."""
    monkeypatch.setattr(tg, "consume_tg_decision", lambda mid, status: None)
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: {"ok": True})

    tg.handle_webhook(_cfg(), {"callback_query": {
        "id": "cbq1", "from": {"id": "1"}, "data": "a:m1",
        "message": {"chat": {"id": "1"}, "message_id": 42},
    }})

    assert tg._SCHEDULED_DELETES == []


# ═════ 12. План удаляется ~через минуту ПОСЛЕ ответа (не от отправки) ═════

def test_button_press_schedules_delete_from_the_moment_of_the_press(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(tg.time, "time", lambda: now[0])
    monkeypatch.setattr(tg, "consume_tg_decision",
                        lambda mid, status: {"chat_id": "1", "message_id": 42})
    monkeypatch.setattr(tg, "clear_inline_keyboard", lambda *a: True)
    monkeypatch.setattr(tg, "get_tg_approval", lambda mid: None)
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: {"ok": True})

    # План "висел" 10 минут (send было бы в now-600) — таймер обязан
    # считаться от ЭТОГО момента (нажатия), не от отправки.
    tg.handle_webhook(_cfg(), {"callback_query": {
        "id": "cbq1", "from": {"id": "1"}, "data": "a:m1",
        "message": {"chat": {"id": "1"}, "message_id": 42},
    }})

    assert len(tg._SCHEDULED_DELETES) == 1
    chat_id, message_id, delete_after = tg._SCHEDULED_DELETES[0]
    assert (chat_id, message_id) == ("1", 42)
    assert delete_after - now[0] == tg._GATE_MESSAGE_DELETE_DELAY_S


def test_button_press_also_schedules_extra_message_ids(monkeypatch):
    """Длинный план (несколько кусков) — все его сообщения уходят на
    удаление, не только последнее (с кнопками)."""
    monkeypatch.setattr(tg, "consume_tg_decision",
                        lambda mid, status: {"chat_id": "1", "message_id": 42})
    monkeypatch.setattr(tg, "clear_inline_keyboard", lambda *a: True)
    monkeypatch.setattr(tg, "get_tg_approval",
                        lambda mid: {"chat_id": "1", "message_id": 42,
                                    "extra_message_ids": [40, 41]})
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: {"ok": True})

    tg.handle_webhook(_cfg(), {"callback_query": {
        "id": "cbq1", "from": {"id": "1"}, "data": "a:m1",
        "message": {"chat": {"id": "1"}, "message_id": 42},
    }})

    scheduled_ids = sorted(mid for _, mid, _ in tg._SCHEDULED_DELETES)
    assert scheduled_ids == [40, 41, 42]


def test_text_reply_via_require_consent_schedules_delete_when_tg_notified(monkeypatch):
    """Второй путь ответа — текстовое «да» (delete_project/слияние
    rename_tag, единственные два плана без авто-исполнителя, у которых
    текстовый путь после tg_notified ещё жив, см. consent._tg_button_only).
    Проверяется на самой `_schedule_tg_gate_message_delete` — прямом
    выражении требования ТЗ, без обвязки целого гейтованного тула."""
    from ticktick_mcp.src import consent as c

    monkeypatch.setattr(tg, "get_tg_approval",
                        lambda mid: {"chat_id": "1", "message_id": 55,
                                    "extra_message_ids": []})

    c._schedule_tg_gate_message_delete({"tg_notified": True}, "m-text")

    assert tg._SCHEDULED_DELETES == [("1", 55, tg._SCHEDULED_DELETES[0][2])]


def test_text_reply_without_tg_notified_schedules_nothing(monkeypatch):
    """Обычный чат-путь (план в Telegram не уходил) — нечего удалять, и
    `_schedule_tg_gate_message_delete` не должна лезть в базу вовсе."""
    from ticktick_mcp.src import consent as c

    called = []
    monkeypatch.setattr(tg, "get_tg_approval", lambda mid: called.append(mid))

    c._schedule_tg_gate_message_delete({"tg_notified": False}, "m-text")
    c._schedule_tg_gate_message_delete(None, "m-text")

    assert called == []
    assert tg._SCHEDULED_DELETES == []


# ═══════════ 13. Отчёт удаляется ~через минуту после ОТПРАВКИ ═══════════

def test_post_report_to_group_schedules_delete_immediately_at_send(monkeypatch):
    now = [2_000_000.0]
    monkeypatch.setattr(tg.time, "time", lambda: now[0])
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: {"ok": True, "result": {"message_id": 900}})
    monkeypatch.setattr(tg, "record_report_messages", lambda *a, **k: None)

    cfg = tg.TgApprovalConfig(enabled=True, bot_token="x", owner_chat_id="1",
                              server="ticktick", tools_allowlist=None, ttl_s=3600,
                              reports_chat_id="-100999")
    delivery = tg.post_report_to_group(cfg, "m1", "### ✅ Готово\nтекст отчёта",
                                       tool="complete_tasks", verdict="ok")

    assert delivery.ok
    assert len(tg._SCHEDULED_DELETES) == 1
    chat_id, message_id, delete_after = tg._SCHEDULED_DELETES[0]
    assert (chat_id, message_id) == ("-100999", 900)
    # СРАЗУ при отправке — таймер стартует ровно от now, не позже.
    assert delete_after - now[0] == tg._GATE_MESSAGE_DELETE_DELAY_S


# ═══════ 14. Сбой удаления не роняет поллер и не переотправляет ошибку ═══

def test_sweep_swallows_delete_failure_silently(monkeypatch):
    now = [3_000_000.0]
    monkeypatch.setattr(tg.time, "time", lambda: now[0])

    def _boom(cfg, chat_id, message_id):
        raise RuntimeError("chat unavailable")

    monkeypatch.setattr(tg, "delete_message", _boom)
    tg.schedule_message_delete("1", 5)
    now[0] += 120

    result = tg.sweep_scheduled_deletes(_cfg())  # не бросает

    assert result == 0
    assert tg._SCHEDULED_DELETES == [], "просроченная запись обязана уйти из очереди"


def test_sweep_never_sends_anything_to_the_owner(monkeypatch):
    """Сбой удаления — не повод писать владельцу «не смог удалить»: это
    молчаливая уборка, а не операция, о которой нужно отчитываться."""
    now = [3_000_000.0]
    monkeypatch.setattr(tg.time, "time", lambda: now[0])
    monkeypatch.setattr(tg, "delete_message", lambda cfg, c, m: False)
    sent = []
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, method, body: sent.append(1) or {"ok": True})
    tg.schedule_message_delete("1", 5)
    now[0] += 120

    tg.sweep_scheduled_deletes(_cfg())

    assert sent == []


async def test_poller_tick_survives_a_sweep_exception(monkeypatch):
    """Структурная защита на уровень выше: даже если sweep_scheduled_deletes
    сама бросит (а не просто вернёт 0), проход поллера обязан пережить это и
    продолжить искать/исполнять кандидатов кнопок — см. try/except вокруг
    вызова в `_tg_auto_execute_tick`."""
    from ticktick_mcp.src import server as s
    from ticktick_mcp.src import tg_auto_execute as tae

    monkeypatch.setattr(s, "_TG_CFG", _cfg())
    monkeypatch.setattr(tae, "_TG_CFG", _cfg())

    def _boom(cfg):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(tg, "sweep_scheduled_deletes", _boom)
    monkeypatch.setattr(tae, "_tg_auto_execute_pending", lambda: [])

    calls = {"get_tg_approvals": 0}

    def _fake_get_tg_approvals(*a, **k):
        calls["get_tg_approvals"] += 1
        return {}

    monkeypatch.setattr(tg, "get_tg_approvals", _fake_get_tg_approvals)

    await tae._tg_auto_execute_tick()  # не бросает

    assert calls["get_tg_approvals"] == 1, (
        "поллер остановился на сбое уборки сообщений, не дойдя до поиска кандидатов")
