"""QA-раунд 2 (2026-08-19), живой прогон привычек — get_habit_checkins,
баги 2 и 3:

2. get_habit_checkins не сверял habit_id с habit_name (identity guard) —
   при несовпадающей паре тихо отдавал чужую историю check-in'ов, подписанную
   ЗАПРОШЕННЫМ (неверным) именем. checkin_habit/delete_habit уже делают эту
   сверку через _names_agree — здесь применяется тот же helper.
3. Некорректный after_date ронял сырое питоновское исключение
   ("invalid literal for int() with base 10: '...'") вместо человекочитаемой
   ошибки, какая уже была у checkin_habit для его `date`. Обе теперь
   используют общий _validate_habit_date.
"""
import ticktick_mcp.src.server as s


class FakeHabitsV2:
    """Минимальная заглушка: только то, чего касается get_habit_checkins —
    список привычек (с двумя записями, чтобы был реальный «не тот id») и
    check-in'ы, плюс опциональный сбой get_habits() для теста fail-closed."""

    def __init__(self, habits, checkins=None, raise_on_get_habits=False):
        self._habits = habits
        self._checkins = {k: list(v) for k, v in (checkins or {}).items()}
        self._raise_on_get_habits = raise_on_get_habits
        self.get_habits_calls = 0

    def get_habits(self):
        self.get_habits_calls += 1
        if self._raise_on_get_habits:
            raise RuntimeError("network blip")
        return self._habits

    def get_habit_checkins(self, ids, after_stamp):
        out = {}
        for hid in ids:
            out[hid] = [e for e in self._checkins.get(hid, [])
                        if e["checkinStamp"] > after_stamp]
        return out


HABITS = [
    {"id": "h1", "name": "Медитация", "goal": 1.0, "type": "Boolean"},
    {"id": "h2", "name": "Бег", "goal": 3.0, "type": "Real"},
]


def _wire(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)


# ───────────────── баг 2: identity guard у get_habit_checkins ─────────────────

async def test_get_habit_checkins_blocks_id_name_mismatch(monkeypatch):
    """Живой репро: get_habit_checkins(habit_name="Медитация", habit_id=<id
    привычки "Бег">) раньше тихо отдавал историю «Бега» под именем
    «Медитация». Теперь — отказ, и чужие данные не просачиваются вообще."""
    fake = FakeHabitsV2(HABITS, checkins={
        "h2": [{"checkinStamp": 20260601, "status": 2, "value": 3.0, "goal": 3.0}],
    })
    _wire(monkeypatch, fake)

    result = await s.get_habit_checkins("Медитация", "h2", "2026-01-01")

    assert result.startswith("🛑")
    assert "«Бег»" in result
    assert "«Медитация»" in result
    assert "2026-06-01" not in result and "20260601" not in result


async def test_get_habit_checkins_unknown_id_refused(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await s.get_habit_checkins("Медитация", "h-нет-такой", "2026-01-01")

    assert result.startswith("🛑")


async def test_get_habit_checkins_matching_pair_still_works(monkeypatch):
    """Guard не должен ломать нормальный путь: совпадающая пара работает
    как раньше."""
    fake = FakeHabitsV2(HABITS, checkins={
        "h1": [{"checkinStamp": 20260601, "status": 2, "value": 1.0, "goal": 1.0}],
    })
    _wire(monkeypatch, fake)

    result = await s.get_habit_checkins("Медитация", "h1", "2026-01-01")

    assert "🛑" not in result
    assert "2026-06-01" in result


async def test_get_habit_checkins_read_failure_fails_closed(monkeypatch):
    """У read-only get_habit_checkins проверка ровно ОДНА (в отличие от
    delete_habit, где план-фаза может фейлиться открытым, потому что
    исполнение всё равно перепроверяет ещё раз безусловно) — сбой чтения
    списка привычек не должен молча пропускать непроверенную пару, даже
    если пара на самом деле верная."""
    fake = FakeHabitsV2(HABITS, raise_on_get_habits=True)
    _wire(monkeypatch, fake)

    result = await s.get_habit_checkins("Медитация", "h1", "2026-01-01")

    assert "не удалось сверить" in result.lower()
    assert "check-ins for" not in result.lower()


# ───────────────── баг 3: человекочитаемая ошибка формата даты ─────────────────

async def test_get_habit_checkins_invalid_date_format_is_human_readable(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await s.get_habit_checkins("Медитация", "h1", "не-дата")

    assert result.startswith("🛑")
    assert "YYYY-MM-DD" in result
    assert "invalid literal" not in result
    assert fake.get_habits_calls == 0, "формат даты проверяется ДО сетевых вызовов"
