"""Дефект (живая приёмка 2026-08-07): словарь кодов активности писался по
ПРЕДПОЛОЖЕНИЮ о названиях, а не по фактическому трафику TickTick.

Живой лог реальной задачи (get_task_activity, прод):

    2026-08-07 14:28:59  someone T_DONE  [web]
    2026-08-07 11:14:32  someone changed due date  none → 2026-08-06 (all-day)
    2026-08-07 11:14:28  someone renamed → "Отрисовать схему всей телефонии"
    2026-08-07 11:14:27  someone T_ASSIGN  [web]
    2026-08-07 11:14:19  someone created  [web]

То есть в середину человеческой фразы («someone T_DONE») вываливался сырой
код: словарь содержал `T_COMPLETE`, которого TickTick НЕ шлёт, и не содержал
`T_DONE`, который шлёт на каждое завершение. Приёмка видела там же
`T_COLUMN`, `T_ADD_FILE`, `T_DEL_FILE`; эта сессия добавила к списку
`T_ASSIGN`.

Второе требование — про СЛЕДУЮЩИЙ пропуск: незнакомый код обязан печататься
узнаваемой заглушкой вместе с самим кодом, чтобы дырку в словаре было видно
глазом, а не принимать за «действие с непонятным названием».
"""
import ticktick_mcp.src.server as s

# Коды, ФАКТИЧЕСКИ наблюдавшиеся в ответах TickTick (живая приёмка + эта
# сессия). Список расширять только по наблюдению, а не по догадке.
OBSERVED_CODES = ["T_DONE", "T_ASSIGN", "T_COLUMN", "T_ADD_FILE", "T_DEL_FILE",
                  "T_CREATE", "T_TITLE", "T_DUE", "T_MOVE", "T_DELETE"]


class FakeV2:
    def __init__(self, events):
        self._events = events

    def get_task_activity(self, project_id, task_id):
        return list(self._events)

    def get_state(self):
        return {"inboxId": "inbox1", "syncTaskBean": {"update": []}}

    def get_trash(self, limit=50):
        return [][:limit]


def _event(action):
    return {"action": action, "when": "2026-07-01T10:00:00+0000",
            "whoProfile": {"isMyself": True}, "deviceChannel": "web"}


async def test_observed_codes_never_print_raw(monkeypatch):
    """Ни один фактически наблюдавшийся код не должен долетать до вывода
    сырьём."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([_event(c) for c in OBSERVED_CODES]))

    out = await s.get_task_activity(task_id="t1", project_id="p1")

    for code in OBSERVED_CODES:
        assert code not in out, f"сырой код {code} в выводе:\n{out}"


async def test_completion_is_labelled_completed(monkeypatch):
    """T_DONE — это «завершено», самое частое событие в логе."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([_event("T_DONE")]))

    out = await s.get_task_activity(task_id="t1", project_id="p1")

    assert "completed" in out.lower(), out
    assert "T_DONE" not in out, out


async def test_unknown_code_is_a_visible_placeholder(monkeypatch):
    """Незнакомый код: и сам код виден (иначе пропуск не заметить), и рядом
    сказано, что сервер его не знает (иначе это читается как название
    действия)."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([_event("T_SOMETHING_NEW")]))

    out = await s.get_task_activity(task_id="t1", project_id="p1")

    assert "T_SOMETHING_NEW" in out, out
    assert "unrecognised" in out.lower() or "unknown" in out.lower(), out
    # Голый код без пояснения — ровно тот дефект, что чинится.
    assert "you T_SOMETHING_NEW" not in out, out


def test_dictionary_holds_no_invented_codes():
    """T_COMPLETE в словаре был выдумкой: TickTick его не шлёт. Словарь ведём
    по наблюдённому трафику."""
    assert "T_COMPLETE" not in s.ACTIVITY_ACTION_LABELS
    for code in OBSERVED_CODES:
        assert code in s.ACTIVITY_ACTION_LABELS, f"{code} наблюдался живьём, но не описан"
