"""build_recurrence_rule: UNTIL строится в таймзоне владельца, а не в UTC (def-D2).

Корень: `"UNTIL=" + until.replace("-", "") + "T000000Z"` брал календарную дату
владельца и объявлял её ПОЛНОЧЬЮ UTC. Для America/Los_Angeles «до 31 августа»
превращалось в 17:00 30 августа по местному — повторы обрывались почти на
семь часов раньше, чем ожидал человек, и никакой пометки об этом не было.
Таймзона владельца в этом проекте — жёсткое правило (никогда не UTC).

Даты в фикстурах либо явно-фиксированные (функция не смотрит на часы), либо
считаются от datetime.now(_USER_TZ) — см. тест про «завтра» ниже.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import ticktick_mcp.src.server as s

LA = ZoneInfo("America/Los_Angeles")


async def test_until_is_end_of_day_in_owner_timezone(monkeypatch):
    monkeypatch.setattr(s, "_USER_TZ", LA)

    out = await s.build_recurrence_rule("WEEKLY", by_day=["TU"], until="2026-08-31")

    # 31 августа 23:59:59 по PDT (UTC-7) == 1 сентября 06:59:59 UTC.
    assert "UNTIL=20260901T065959Z" in out, out
    # Именно этого быть не должно: полночь UTC = 17:00 30 августа по местному.
    assert "UNTIL=20260831T000000Z" not in out


async def test_until_in_utc_zone_covers_the_whole_named_day(monkeypatch):
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("UTC"))

    out = await s.build_recurrence_rule("DAILY", until="2026-08-31")

    # Даже в UTC «до 31 августа» обязано включать сам 31-й день целиком.
    assert "UNTIL=20260831T235959Z" in out, out


async def test_until_accepts_relative_word_resolved_on_the_server_clock(monkeypatch):
    monkeypatch.setattr(s, "_USER_TZ", LA)

    out = await s.build_recurrence_rule("DAILY", until="завтра")

    # Фикстура считается от реальных часов, а не от календарной константы —
    # иначе она молча протухнет через N дней (правило репозитория).
    expected_local = datetime.now(LA).date() + timedelta(days=1)
    expected = datetime(expected_local.year, expected_local.month, expected_local.day,
                        23, 59, 59, tzinfo=LA).astimezone(ZoneInfo("UTC"))
    assert f"UNTIL={expected.strftime('%Y%m%dT%H%M%SZ')}" in out, out


async def test_unparseable_until_is_rejected_not_turned_into_garbage():
    out = await s.build_recurrence_rule("WEEKLY", until="31.08.2026")

    assert "RRULE:" not in out, f"мусорная дата уехала в правило: {out!r}"
    assert "31.08.2026" in out


async def test_until_and_count_together_are_rejected():
    # RFC 5545: UNTIL и COUNT не могут стоять в одном правиле.
    out = await s.build_recurrence_rule("WEEKLY", count=5, until="2026-08-31")

    assert "RRULE:" not in out, f"взаимоисключающие параметры прошли: {out!r}"
