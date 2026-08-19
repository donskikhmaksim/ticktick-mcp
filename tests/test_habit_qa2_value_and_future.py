"""QA-раунд 2 (2026-08-19), живой прогон привычек — checkin_habit, баги 4 и 5:

4. checkin_habit не проверял value против типа привычки — для boolean
   (да/нет) habit с goal=1 value=99 молча записывалось как «выполнено,
   99.0/1.0», хотя для да/нет-привычки это бессмысленно.
5. checkin_habit тихо принимал дату в будущем без единого предупреждения.
"""
import re
from datetime import datetime

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


async def _checkin(*args, **kwargs):
    """Полный цикл плана и исполнения checkin_habit; отдаёт результат #2
    (тот же паттерн, что в test_slice9_habits.py)."""
    preview = await s.checkin_habit(*args, **kwargs)
    assert "🛑" not in preview, f"plan phase unexpectedly refused: {preview!r}"
    mid = _extract_manifest_id(preview)
    return await s.checkin_habit(*args, manifest_id=mid, user_reply="да", **kwargs)


class FakeHabitsV2:
    """Как FakeHabitsV2 в test_slice9_habits.py, плюс поле "type" на каждой
    привычке — нужно для проверки value против boolean/quantitative."""

    def __init__(self, habits, checkins=None):
        self._habits = habits
        self._checkins = {k: list(v) for k, v in (checkins or {}).items()}
        self.checkin_habit_calls = 0

    def get_habits(self):
        return self._habits

    def get_habit_checkins(self, ids, after_stamp):
        out = {}
        for hid in ids:
            out[hid] = [e for e in self._checkins.get(hid, [])
                        if e["checkinStamp"] > after_stamp]
        return out

    def checkin_habit(self, habit_id, date=None, status=2, value=None, goal=1.0):
        self.checkin_habit_calls += 1
        stamp = (int(date.replace("-", "")) if date
                 else int(datetime.now().strftime("%Y%m%d")))
        entries = self._checkins.setdefault(habit_id, [])
        v = value if value is not None else (goal if status == 2 else 0.0)
        entries.append({"checkinStamp": stamp, "status": status,
                        "value": v, "goal": goal})
        return {"ok": True}


HABITS = [
    {"id": "h1", "name": "Медитация", "goal": 1.0, "type": "Boolean"},
    {"id": "h2", "name": "Бег", "goal": 3.0, "type": "Real"},
]


def _wire(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)


# ───────────────── баг 4: value против типа привычки ─────────────────

async def test_checkin_habit_boolean_habit_rejects_unrelated_value(monkeypatch):
    """Живой репро: boolean-привычка (goal=1) + value=99 раньше записывалось
    как «выполнено, значение 99.0/1.0»."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-07-28", value=99)

    assert result.startswith("🛑")
    assert fake.checkin_habit_calls == 0


async def test_checkin_habit_boolean_habit_allows_value_matching_goal(monkeypatch):
    """value, совпадающий с goal (обычный «выполнено» для да/нет), не
    отказывается — ограничение бьёт только по БЕССМЫСЛЕННЫМ значениям."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-07-28", value=1)

    assert result.startswith("### ✅")
    assert fake.checkin_habit_calls == 1


async def test_checkin_habit_boolean_habit_allows_no_value(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-07-28")

    assert result.startswith("### ✅")
    assert fake.checkin_habit_calls == 1


async def test_checkin_habit_quantitative_habit_keeps_arbitrary_value(monkeypatch):
    """Ограничение — только для boolean; количественные привычки как раньше
    принимают любое числовое value."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _checkin("Бег", "h2", date="2026-07-28", value=5.5)

    assert result.startswith("### ✅")
    assert fake.checkin_habit_calls == 1


# ───────────────── баг 5: предупреждение о дате в будущем ─────────────────

async def test_checkin_habit_future_date_is_accepted_but_warned(monkeypatch):
    """Живой репро: date="2026-12-31" при "сегодня" 2026-08-19 принималось
    молча. Не блокируем (backfill в прошлое — обычное дело, будущее может
    быть осмысленным), но предупреждаем в ответе."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-12-31")

    assert result.startswith("### ✅")
    assert "будущ" in result.lower()
    assert fake.checkin_habit_calls == 1


async def test_checkin_habit_past_date_has_no_future_warning(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-07-28")

    assert "будущ" not in result.lower()


async def test_checkin_habit_today_has_no_future_warning(monkeypatch):
    """date=None (сегодня) не может быть «будущим» по определению — helper
    вообще не должен вызываться, но проверяем результат, а не внутренности."""
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1")

    assert "будущ" not in result.lower()
