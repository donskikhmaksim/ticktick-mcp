"""Дефект (живая приёмка 2026-08-07) — «всего отметок» у привычки считает
только УСПЕШНЫЕ.

Живьём: `get_habits` печатал про привычку «Растяжка» строку

    goal: 1.0 Count | type: Boolean | total check-ins: 18

а `get_habit_checkins` за тот же период отдал 31 запись: 18 выполнено,
12 провалено, 1 пропущено. Слово «total» читается как «все отметки», по факту
это счётчик успехов — человек, оценивающий свою регулярность, получает
завышенную картину (18 из 18 вместо 18 из 31).

Там же, в самом списке отметок, две мелочи, которые ломают ровно то, ради
чего его открывают:
  * дата печаталась внутренним кодом TickTick — «20260529» вместо 2026-05-29;
  * записи шли в том порядке, в каком их отдал API, то есть вперемешку
    (20260529 → 20260531 → 20260530 → 20260605), и серию нельзя было
    посчитать глазами.
"""
import ticktick_mcp.src.server as s

HABIT = {"id": "h1", "name": "Растяжка", "goal": 1.0, "unit": "Count",
         "type": "Boolean", "repeatRule": "RRULE:FREQ=WEEKLY;INTERVAL=1;TT_TIMES=3",
         # Ровно то, что отдаёт живой API: 18 при 31 фактической отметке.
         "totalCheckIns": 18}

# Порядок специально «как из API» — вперемешку, как в живом ответе.
CHECKINS = [
    {"checkinStamp": 20260529, "status": 2, "value": 1.0, "goal": 1.0},
    {"checkinStamp": 20260531, "status": 2, "value": 1.0, "goal": 1.0},
    {"checkinStamp": 20260530, "status": 2, "value": 1.0, "goal": 1.0},
    {"checkinStamp": 20260601, "status": 1, "value": 0.0, "goal": 1.0},
    {"checkinStamp": 20260528, "status": 0, "value": 0.0, "goal": 1.0},
]


class FakeV2:
    def __init__(self, habits=None, checkins=None):
        self._habits = habits if habits is not None else [HABIT]
        self._checkins = checkins if checkins is not None else CHECKINS

    def get_habits(self):
        return [dict(h) for h in self._habits]

    def get_habit_checkins(self, habit_ids, after_stamp):
        self.asked_after_stamp = after_stamp
        return {habit_ids[0]: [dict(c) for c in self._checkins]}


async def test_habits_do_not_call_successes_a_total(monkeypatch):
    """«total check-ins» — неправда: провалы и пропуски туда не попадают."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_habits()

    assert "18" in out, out
    assert "total check-ins" not in out.lower(), out
    # Названо, что счётчик про УСПЕШНЫЕ отметки, и сказано, где смотреть все.
    assert "done" in out.lower(), out
    assert "get_habit_checkins" in out, out


async def test_checkin_dates_are_human_readable(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_habit_checkins("Растяжка", "h1", "2026-05-01")

    assert "2026-05-29" in out, out
    assert "20260529" not in out, out


async def test_checkins_are_sorted_by_date(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_habit_checkins("Растяжка", "h1", "2026-05-01")

    dates = [ln.split(":")[0].lstrip("- ").strip()
             for ln in out.splitlines() if ln.startswith("- ")]
    assert dates == sorted(dates), f"порядок не по датам: {dates}"
    assert len(dates) == len(CHECKINS), out


async def test_checkin_header_breaks_the_count_down(monkeypatch):
    """Заголовок обязан различать успехи и всё остальное — иначе «(31)»
    прочитают как «31 раз сделал»."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_habit_checkins("Растяжка", "h1", "2026-05-01")

    header = out.splitlines()[0]
    assert "5" in header, header          # всего записей
    assert "3" in header, header          # выполнено
    assert "done" in header.lower(), header
    assert "failed" in header.lower(), header
