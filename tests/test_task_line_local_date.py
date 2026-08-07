"""Дефект: списки печатали дату дедлайна СЫРЫМ срезом UTC-строки.

`format_task_line()` брала `str(task["dueDate"])[:10]`, а `format_task()`
печатала dueDate/startDate целиком как пришло — обе БЕЗ конверсии в `_USER_TZ`,
хотя вся фильтрация (`_task_due_local_date`, overdue / due-today / due-in-N)
считает именно в зоне владельца. Для дедлайна около полуночи расхождение —
ровно календарный день:

    23:59 America/Los_Angeles  ==  06:59 UTC СЛЕДУЮЩЕГО дня

Живьём это выглядело как противоречие в одном выводе: `get_overdue_tasks`
(рубрика «просрочено») печатала «due <сегодня>». Классификация была верной,
врал текст.

Фикстуры считаются от `datetime.now(timezone.utc)` / реальных часов зоны —
календарных констант тут нет НАМЕРЕННО: захардкоженная дата протухает молча
через N дней и продолжает зеленеть (правило репозитория, мина уже была).

Момент дедлайна берётся около ПОЛУНОЧИ по местному времени — только там баг
виден. Тест, взявший полдень, был бы зелёным и на сломанном коде.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import ticktick_mcp.src.server as s

LA = "America/Los_Angeles"   # −07/−08: локальный вечер уже «завтра» по UTC
MSK = "Europe/Moscow"        # +03: локальное раннее утро ещё «вчера» по UTC


def _near_midnight(zone: str, hour: int, minute: int):
    """Момент `hour:minute` завтрашнего ЛОКАЛЬНОГО дня в зоне `zone`.

    Возвращает (строка_как_её_отдаёт_TickTick, локальная_дата, utc_дата).
    Считается от реальных часов (`datetime.now(tz)`), без календарных
    констант. «Завтра» — чтобы момент не зависел от того, в какую секунду
    суток запустили тест.
    """
    tz = ZoneInfo(zone)
    day = (datetime.now(tz) + timedelta(days=1)).date()
    local_dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    utc_dt = local_dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000"), local_dt.date(), utc_dt.date()


def test_fixture_actually_straddles_midnight():
    """Страховка на саму фикстуру: если локальная и UTC даты совпали, тест
    ниже ничего не проверяет. 23:59 в LA (−07/−08) — всегда следующий день по
    UTC; 00:30 в Москве (+03) — всегда предыдущий."""
    _, local_d, utc_d = _near_midnight(LA, 23, 59)
    assert utc_d == local_d + timedelta(days=1)
    _, local_d, utc_d = _near_midnight(MSK, 0, 30)
    assert utc_d == local_d - timedelta(days=1)


def test_task_line_late_night_la_prints_local_day(monkeypatch):
    """Дедлайн 23:59 по Лос-Анджелесу печатается днём владельца, а не днём UTC."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    due, local_d, utc_d = _near_midnight(LA, 23, 59)
    line = s.format_task_line({"id": "t1", "projectId": "p1",
                               "title": "Deliver the door", "dueDate": due})
    assert f"due {local_d.isoformat()}" in line, line
    assert utc_d.isoformat() not in line, line


def test_task_line_early_morning_moscow_prints_local_day(monkeypatch):
    """Зеркальный знак смещения: 00:30 по Москве — это ещё вчера по UTC.
    Сырой срез уводил день НАЗАД, локальная конверсия обязана держать оба
    направления, а не только отрицательные офсеты."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(MSK))
    due, local_d, utc_d = _near_midnight(MSK, 0, 30)
    line = s.format_task_line({"id": "t1", "projectId": "p1",
                               "title": "Позвонить в банк", "dueDate": due})
    assert f"due {local_d.isoformat()}" in line, line
    assert utc_d.isoformat() not in line, line


@pytest.mark.parametrize("zone", [LA, MSK, "UTC"])
def test_task_line_all_day_is_verbatim(zone, monkeypatch):
    """Регресс #36: all-day дедлайн — ЗОНОНЕЗАВИСИМАЯ календарная дата.
    Её нельзя .astimezone()'ить; читается буквально в любой зоне.
    Этот тест ловит «починку» конверсией всего подряд."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(zone))
    day = (datetime.now(ZoneInfo(zone)) + timedelta(days=2)).date()
    for task in ({"dueDate": day.isoformat(), "isAllDay": True},
                 {"dueDate": day.isoformat() + "T00:00:00.000+0000", "isAllDay": True},
                 {"dueDate": day.isoformat()}):
        line = s.format_task_line({**task, "id": "t1", "projectId": "p1", "title": "x"})
        assert f"due {day.isoformat()}" in line, (zone, task, line)


def test_task_line_unparseable_due_does_not_crash(monkeypatch):
    """Мусор в dueDate не должен ронять список — печатаем что есть."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    line = s.format_task_line({"id": "t1", "projectId": "p1",
                               "title": "x", "dueDate": "не дата"})
    assert "due " in line


def test_format_task_due_line_is_local_day(monkeypatch):
    """`format_task()` (детальный вид) — тот же дефект: строка Due Date
    показывала UTC-инстант, то есть чужой календарный день."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    due, local_d, utc_d = _near_midnight(LA, 23, 59)
    out = s.format_task({"id": "t1", "projectId": "p1", "title": "x", "dueDate": due})
    assert f"Due Date: {local_d.isoformat()}" in out, out
    assert utc_d.isoformat() not in out, out


def test_format_task_start_date_is_local_day(monkeypatch):
    """startDate печатается тем же форматтером и той же строкой кода —
    правдивость обеих дат в одном выводе не может расходиться."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    start, local_d, utc_d = _near_midnight(LA, 23, 30)
    out = s.format_task({"id": "t1", "projectId": "p1", "title": "x", "startDate": start})
    assert f"Start Date: {local_d.isoformat()}" in out, out
    assert utc_d.isoformat() not in out, out


def test_format_task_keeps_clock_time_visible(monkeypatch):
    """День — по владельцу, но само ВРЕМЯ дедлайна не должно пропасть:
    «23:59» — это половина смысла «около полуночи»."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    due, _, _ = _near_midnight(LA, 23, 59)
    out = s.format_task({"id": "t1", "projectId": "p1", "title": "x", "dueDate": due})
    assert "23:59" in out, out


@pytest.mark.parametrize("zone", [LA, MSK, "UTC"])
def test_format_task_all_day_verbatim(zone, monkeypatch):
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(zone))
    day = (datetime.now(ZoneInfo(zone)) + timedelta(days=2)).date()
    out = s.format_task({"id": "t1", "projectId": "p1", "title": "x",
                         "dueDate": day.isoformat(), "isAllDay": True})
    assert f"Due Date: {day.isoformat()}" in out, out
