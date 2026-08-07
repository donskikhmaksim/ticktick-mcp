"""Дефект (вторая половина той же истории, что и test_task_line_local_date.py).

Коммит f85fc76 перевёл на зону владельца ТОЛЬКО два форматтера —
`format_task_line()` и `format_task()`. Все остальные места, которые рисуют
дату СВОИМ кодом, остались на сыром UTC. Живой ретест на проде поймал это на
одной и той же задаче «Deliver the door»:

    get_task / список →  2026-08-06        (зона владельца, после f85fc76)
    get_task_info     →  2026-08-08T21:00:00.000+0000  →  читается как 08-07

Раньше все источники врали ОДИНАКОВО, и по ним хотя бы можно было сверяться
между собой. После половинчатого фикса врёт один — и без третьего источника
невозможно понять, какой прав. Это хуже исходного состояния, поэтому здесь
проверяется не только «каждое место печатает локальный день», но и отдельно
СОГЛАСОВАННОСТЬ трёх инструментов на ОДНОЙ задаче.

Проверяемые места (`ticktick_mcp/src/server.py`):
  1. get_task_info           — start / due
  2. get_task_info           — created / last modified / completed
  3. _task_activity_fallback — те же три штампа
  4. get_task_activity       — `when` каждого события лога
  5. get_task_activity       — dueDateBefore → dueDate у события T_DUE
  6. _verify_item            — «срок …» в отчёте о созданной задаче
  7. _dc_analyze             — "due" во флаге flag_obsolete (печатает
                               plan_declutter)

ФИКСТУРЫ БЕЗ КАЛЕНДАРНЫХ КОНСТАНТ. Момент считается от `datetime.now(tz)`
(правило репозитория: захардкоженная дата протухает молча и продолжает
зеленеть). Время берётся ОКОЛО ПОЛУНОЧИ по местному — только там локальный и
UTC-день расходятся; тест, взявший полдень, был бы зелёным и на сломанном
коде. Отдельный тест-страховка ниже это утверждение проверяет.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import ticktick_mcp.src.server as s

LA = "America/Los_Angeles"   # −07/−08: поздний вечер здесь — уже «завтра» по UTC
MSK = "Europe/Moscow"        # +03: раннее утро здесь — ещё «вчера» по UTC


def _at_local(zone: str, hour: int, minute: int, day_offset: int = 1):
    """Момент `hour:minute` ЛОКАЛЬНОГО дня, сдвинутого на `day_offset` суток.

    Возвращает (строка_как_её_отдаёт_TickTick, локальная_дата, utc_дата).
    Считается от реальных часов (`datetime.now(tz)`) — календарных констант
    нет намеренно.
    """
    tz = ZoneInfo(zone)
    day = (datetime.now(tz) + timedelta(days=day_offset)).date()
    local_dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    utc_dt = local_dt.astimezone(timezone.utc)
    return (utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            local_dt.date(), utc_dt.date())


def test_fixture_actually_straddles_midnight():
    """Страховка на саму фикстуру: если локальная и UTC даты совпадут, все
    тесты ниже перестанут что-либо проверять и останутся зелёными."""
    for offset in (-90, -1, 0, 1):
        _, local_d, utc_d = _at_local(LA, 23, 59, offset)
        assert utc_d == local_d + timedelta(days=1), (offset, local_d, utc_d)
        _, local_d, utc_d = _at_local(MSK, 0, 30, offset)
        assert utc_d == local_d - timedelta(days=1), (offset, local_d, utc_d)


def _days(text: str):
    """Все календарные дни YYYY-MM-DD, встреченные в тексте."""
    return set(re.findall(r"\d{4}-\d{2}-\d{2}", text))


def _line(text: str, needle: str) -> str:
    for ln in text.splitlines():
        if needle in ln:
            return ln
    raise AssertionError(f"строки с «{needle}» нет в выводе:\n{text}")


# --------------------------------------------------------------------------
# Двойники клиентов. Ровно те методы, которых касаются проверяемые функции.
# --------------------------------------------------------------------------

class FakeV2:
    def __init__(self, task=None, activity=None, trash=None, inbox_id="inbox_me"):
        self._task = task
        self._activity = activity or []
        self._trash = trash or []
        self._inbox_id = inbox_id

    def get_state(self, force=False):
        return {"inboxId": self._inbox_id,
                "projectProfiles": [{"id": "p1", "name": "Работа"}],
                "syncTaskBean": {"update": [self._task] if self._task else []}}

    def get_trash(self, limit=50):
        return list(self._trash)[:limit]

    def get_task_activity(self, project_id, task_id):
        return list(self._activity)


class FakeV1:
    def __init__(self, task):
        self._task = task

    def get_task(self, project_id, task_id):
        return dict(self._task)


# ==========================================================================
# Место 1 — get_task_info: start / due
# ==========================================================================

async def test_task_info_due_is_owner_calendar_day(monkeypatch):
    """Живой случай: due 23:59 по Лос-Анджелесу печатался сырым UTC-моментом
    (`...T06:59:00.000+0000`), то есть чужим календарным днём."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    due, local_d, utc_d = _at_local(LA, 23, 59)
    task = {"id": "t1", "projectId": "p1", "title": "Deliver the door",
            "dueDate": due, "status": 0}
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(task=task))

    out = await s.get_task_info("t1")
    due_line = _line(out, "due:")

    assert local_d.isoformat() in due_line, due_line
    assert utc_d.isoformat() not in due_line, due_line
    assert "+0000" not in due_line, due_line


async def test_task_info_start_is_owner_calendar_day(monkeypatch):
    """startDate рисуется соседней строкой того же кода — обе даты в одном
    выводе не имеют права расходиться по правдивости."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    start, local_d, utc_d = _at_local(LA, 23, 30)
    task = {"id": "t1", "projectId": "p1", "title": "x", "startDate": start}
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(task=task))

    out = await s.get_task_info("t1")
    start_line = _line(out, "start:")

    assert local_d.isoformat() in start_line, start_line
    assert utc_d.isoformat() not in start_line, start_line


async def test_task_info_moscow_direction(monkeypatch):
    """Зеркальный знак смещения: 00:30 по Москве — ещё вчера по UTC. Сырой
    вывод уводил день НАЗАД; конверсия обязана держать оба направления."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(MSK))
    due, local_d, utc_d = _at_local(MSK, 0, 30)
    task = {"id": "t1", "projectId": "p1", "title": "Позвонить в банк",
            "dueDate": due}
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(task=task))

    due_line = _line(await s.get_task_info("t1"), "due:")
    assert local_d.isoformat() in due_line, due_line
    assert utc_d.isoformat() not in due_line, due_line


async def test_task_info_keeps_clock_time_visible(monkeypatch):
    """День — по владельцу, но само время дедлайна пропасть не должно:
    «23:59» — половина смысла «около полуночи»."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    due, _, _ = _at_local(LA, 23, 59)
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(
        task={"id": "t1", "projectId": "p1", "title": "x", "dueDate": due}))
    assert "23:59" in _line(await s.get_task_info("t1"), "due:")


@pytest.mark.parametrize("zone", [LA, MSK, "UTC"])
async def test_task_info_all_day_is_verbatim(zone, monkeypatch):
    """Регресс #36: all-day дедлайн — ЗОНОНЕЗАВИСИМАЯ календарная дата, её
    нельзя .astimezone()'ить. Тест ловит «починку» конверсией всего подряд."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(zone))
    day = (datetime.now(ZoneInfo(zone)) + timedelta(days=2)).date()
    task = {"id": "t1", "projectId": "p1", "title": "x", "isAllDay": True,
            "dueDate": day.isoformat() + "T00:00:00.000+0000",
            "startDate": day.isoformat() + "T00:00:00.000+0000"}
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(task=task))

    out = await s.get_task_info("t1")
    assert day.isoformat() in _line(out, "due:"), out
    assert day.isoformat() in _line(out, "start:"), out
    assert "all-day" in _line(out, "due:").lower(), out


# ==========================================================================
# Место 2 — get_task_info: created / last modified / completed
# ==========================================================================

async def test_task_info_stamps_are_local_and_name_the_zone(monkeypatch):
    """Штампы печатались сырым UTC и БЕЗ указания зоны — читатель не мог
    даже понять, что перед ним не его время."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    created, c_local, c_utc = _at_local(LA, 23, 10, -30)
    modified, m_local, m_utc = _at_local(LA, 23, 20, -3)
    completed, k_local, k_utc = _at_local(LA, 23, 40, -1)
    task = {"id": "t1", "projectId": "p1", "title": "x", "creator": "me",
            "createdTime": created, "modifiedTime": modified,
            "completedTime": completed, "status": 2}
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(task=task))

    out = await s.get_task_info("t1")
    for needle, local_d, utc_d in (("created:", c_local, c_utc),
                                   ("last modified:", m_local, m_utc),
                                   ("completed:", k_local, k_utc)):
        ln = _line(out, needle)
        assert local_d.isoformat() in ln, ln
        assert utc_d.isoformat() not in ln, ln
        assert LA in ln, ln


async def test_task_info_stamps_of_all_day_task_are_still_instants(monkeypatch):
    """Ловушка на реализацию: у all-day задачи стоит isAllDay=True, но
    createdTime/modifiedTime — ВСЕГДА моменты времени, а не календарные даты.
    Переиспользование all-day-ветки для штампов напечатало бы UTC-день
    создания как «зононезависимый» и вернуло бы ровно тот же дефект."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo(LA))
    created, c_local, c_utc = _at_local(LA, 23, 10, -30)
    day = (datetime.now(ZoneInfo(LA)) + timedelta(days=2)).date()
    task = {"id": "t1", "projectId": "p1", "title": "x", "isAllDay": True,
            "dueDate": day.isoformat(), "createdTime": created,
            "creator": "me"}
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(task=task))

    ln = _line(await s.get_task_info("t1"), "created:")
    assert c_local.isoformat() in ln, ln
    assert c_utc.isoformat() not in ln, ln
