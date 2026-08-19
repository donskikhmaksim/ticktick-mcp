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

QA-раунд 2 (2026-08-19) — переименования оказалось мало: поле totalCheckIns
не просто не различает успех/провал, оно ещё и УСТАРЕВАЕТ (живьём: чек-ин
сразу после записи не попадал в него при следующем get_habits — «done: 0»;
delete_habit печатал «успешных было 3», хотя get_habit_checkins непосредственно
перед этим показывал 4 done-записи). Правка ниже: get_habits больше НЕ читает
totalCheckIns вообще — «done» теперь считается по-настоящему, ЖИВЫМ batch-
чтением check-in'ов (тем же вызовом get_habit_checkins использует), поэтому
FakeV2.get_habits() ниже больше не эмулирует «плохое» поле — счёт целиком
идёт из CHECKINS.
"""
import ticktick_mcp.src.server as s

HABIT = {"id": "h1", "name": "Растяжка", "goal": 1.0, "unit": "Count",
         "type": "Boolean", "repeatRule": "RRULE:FREQ=WEEKLY;INTERVAL=1;TT_TIMES=3",
         # Значение поля больше не читается get_habits вовсе (см. QA-раунд 2
         # выше) — оставлено намеренно РАСХОДЯЩИМСЯ с CHECKINS (18 против 3
         # реальных done), чтобы тест ниже доказывал, что именно расхождение
         # НЕ просачивается наружу.
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
    """«total check-ins» — неправда: провалы и пропуски туда не попадают.
    Обновлено QA-раунд 2: теперь ещё и число берётся из ЖИВЫХ записей (3
    done среди CHECKINS), а НЕ из устаревшего HABIT["totalCheckIns"]=18 —
    расхождение специально заложено в фикстуру, чтобы это доказать."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_habits()

    assert "done: 3" in out, out
    assert "18" not in out, out
    assert "total check-ins" not in out.lower(), out
    # Названо честно: это реальный счёт, а не счётчик TickTick.
    assert "actual" in out.lower(), out


async def test_habits_done_count_is_recomputed_not_stale(monkeypatch):
    """Живой QA-баг (2026-08-19): checkin_habit(status=2) сразу за которым
    get_habits показывал done: 0 — поле TickTick подвисает. Проверяем, что
    get_habits вообще не смотрит в totalCheckIns: FakeV2 ниже отдаёт habit с
    totalCheckIns=0 (как сразу после чек-ина), но CHECKINS всё равно
    показывают 3 done — именно 3 и должно попасть в вывод."""
    habit_stale = dict(HABIT, totalCheckIns=0)
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(habits=[habit_stale]))

    out = await s.get_habits()

    assert "done: 3" in out, out
    assert "done: 0" not in out, out


async def test_habits_done_count_falls_back_to_unknown_on_read_failure(monkeypatch):
    """Пересчёт стоит одного лишнего запроса — если он упал, честнее
    показать «?», чем откатиться на устаревшее totalCheckIns."""
    class RaisingV2(FakeV2):
        def get_habit_checkins(self, habit_ids, after_stamp):
            self.asked_after_stamp = after_stamp
            raise RuntimeError("network blip")

    monkeypatch.setattr(s, "ticktick_v2", RaisingV2())

    out = await s.get_habits()

    assert "done: ?" in out, out
    assert "18" not in out, out


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
