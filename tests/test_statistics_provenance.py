"""Дефект-расследование (живая приёмка 2026-08-07/08): `get_statistics`
стабильно расходится с независимой сверкой, и по выводу нельзя понять, чьи
это вообще числа и за какие сутки.

Что установлено живьём (прод, аккаунт владельца):

  * get_statistics: «Completed today: 2 | yesterday: 6 | total: 908»;
  * get_changes за те же дни (метки ленты — UTC) показал СЕМЬ завершений с
    датой 2026-08-07: 09:10, 18:14, 18:15, 18:16, 18:16, 21:04, 21:28;
  * get_task_activity одной из них дал T_DONE в 14:28:59
    America/Los_Angeles = 21:28 UTC — то есть метки ленты действительно UTC.

Ни UTC-сутки (7 завершений), ни сутки владельца America/Los_Angeles (те же 7)
не дают 2. Решающее наблюдение — естественный эксперимент на переходе полуночи
UTC: повторный вызов уже ПОСЛЕ 00:00 UTC 2026-08-08 вернул те же «today: 2 |
yesterday: 6». Считай TickTick по UTC, «today» обнулилось бы (после полуночи
завершений не было). Значит окно «сегодня» у статистики — сутки ЧУЖОЙ зоны:
граница лежит между 18:16 и 21:04 UTC, то есть зона аккаунта TickTick со
смещением примерно UTC+3…+5, и в это окно попали ровно два завершения.

Вывод: числа приходят от TickTick сырыми, наш код их не искажает — он лишь
молчал о том, ЧЕЙ это счёт и в какой зоне нарезаны сутки, из-за чего его
сверяли с get_changes (а тот режет сутки по UTC) и каждый раз получали
расхождение.

Эти тесты фиксируют обе половины вывода: числа не пересчитываются, и
происхождение названо вслух.
"""
import ticktick_mcp.src.server as s


class FakeV2:
    def __init__(self, stats):
        self._stats = stats

    def get_statistics(self):
        return dict(self._stats)


STATS = {"score": 5780, "level": 6, "todayCompleted": 2,
         "yesterdayCompleted": 6, "totalCompleted": 908}


async def test_numbers_are_passed_through_untouched(monkeypatch):
    """Контроль: сервер ничего не пересчитывает — печатает ровно то, что
    прислал TickTick. Если однажды кто-то начнёт «чинить» расхождение
    арифметикой, этот тест обязан упасть."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(STATS))

    out = await s.get_statistics()

    assert "2" in out and "6" in out and "908" in out, out
    assert "5780" in out and "6" in out, out


async def test_output_names_whose_numbers_these_are(monkeypatch):
    """Вывод обязан сказать: счётчики — сервиса TickTick, «сегодня» нарезано
    по зоне его аккаунта (не UTC и не зона этого сервера), и с лентой
    get_changes они сходиться не обязаны."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(STATS))

    out = await s.get_statistics()

    low = out.lower()
    assert "ticktick" in low, out
    assert "зон" in low, out                      # названа нарезка суток по зоне
    assert s._USER_TZ.key.lower() in low, out     # и что это НЕ зона сервера
    assert "get_changes" in low, out              # с чем именно нельзя сверять


async def test_missing_fields_do_not_become_zeroes(monkeypatch):
    """Отсутствующее поле — «нет данных», а не «ноль завершённых»: ноль здесь
    читается как факт («сегодня ничего не сделал»), которого источник не
    сообщал."""
    monkeypatch.setattr(s, "ticktick_v2", FakeV2({"score": 10, "level": 1}))

    out = await s.get_statistics()

    assert "None" not in out, out
    assert "0" not in out.split("\n")[1], out
