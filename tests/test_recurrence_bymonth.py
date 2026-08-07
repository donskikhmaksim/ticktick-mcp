"""build_recurrence_rule: BYMONTHDAY / BYMONTH / BYSETPOS и честные пометки (def-D3).

Корень: билдер знал только FREQ/INTERVAL/BYDAY/COUNT/UNTIL. «Каждое 31-е
число», «последний день месяца», «второй вторник марта» — типовые повторы
(платежи, дни рождения) — им было не построить в принципе, а `by_day=["2TU"]`
при YEARLY без BYMONTH молча означало «второй вторник ГОДА». Правило,
синтаксически валидное, но смыслово не то, опаснее падающего: теперь такие
случаи либо строятся честно, либо помечаются предупреждением, либо
отвергаются. Плюс `interval` перестал молча подтягиваться до 1.

Контракт вывода: ПЕРВАЯ строка — всегда чистый RRULE (её и передают в
repeat_flag), предупреждения идут ниже.
"""
import ticktick_mcp.src.server as s


def _rule(out: str) -> str:
    """Первая строка вывода — сам RRULE, без пометок."""
    return out.splitlines()[0]


async def test_monthly_on_the_31st_is_buildable_and_warns_about_short_months():
    out = await s.build_recurrence_rule("MONTHLY", by_month_day=[31])

    assert _rule(out) == "RRULE:FREQ=MONTHLY;INTERVAL=1;BYMONTHDAY=31"
    # Февраль/апрель/июнь/сентябрь/ноябрь 31-го не имеют — повтор там просто
    # не случится. Это должно быть сказано вслух, а не оставлено сюрпризом.
    assert "⚠" in out
    assert "-1" in out  # подсказка про «последний день месяца»


async def test_last_day_of_month():
    out = await s.build_recurrence_rule("MONTHLY", by_month_day=[-1])

    assert _rule(out) == "RRULE:FREQ=MONTHLY;INTERVAL=1;BYMONTHDAY=-1"
    # «Последний день месяца» — точное правило, предупреждать не о чем.
    assert "⚠" not in out


async def test_every_two_weeks_on_tuesday():
    out = await s.build_recurrence_rule("WEEKLY", interval=2, by_day=["TU"])

    assert _rule(out) == "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"
    assert "⚠" not in out


async def test_second_tuesday_of_march():
    out = await s.build_recurrence_rule("YEARLY", by_month=[3], by_day=["2TU"])

    assert _rule(out) == "RRULE:FREQ=YEARLY;INTERVAL=1;BYMONTH=3;BYDAY=2TU"
    assert "⚠" not in out


async def test_yearly_ordinal_byday_without_bymonth_is_flagged():
    # Без BYMONTH это «второй вторник ГОДА» — почти никогда не то, что имели в виду.
    out = await s.build_recurrence_rule("YEARLY", by_day=["2TU"])

    assert _rule(out) == "RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2TU"
    assert "⚠" in out
    assert "by_month" in out  # предупреждение подсказывает, чего не хватает


async def test_bysetpos_last_workday_of_month():
    out = await s.build_recurrence_rule(
        "MONTHLY", by_day=["MO", "TU", "WE", "TH", "FR"], by_set_pos=[-1])

    assert _rule(out) == "RRULE:FREQ=MONTHLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"


async def test_bysetpos_alone_is_rejected():
    # RFC 5545: BYSETPOS работает только вместе с другим BY-правилом.
    out = await s.build_recurrence_rule("MONTHLY", by_set_pos=[-1])

    assert "RRULE:" not in out, f"BYSETPOS без BY-правила прошёл: {out!r}"


async def test_invalid_by_month_day_is_rejected():
    for bad in ([0], [32], [-40]):
        out = await s.build_recurrence_rule("MONTHLY", by_month_day=bad)
        assert "RRULE:" not in out, f"by_month_day={bad} прошёл: {out!r}"


async def test_invalid_by_month_is_rejected():
    out = await s.build_recurrence_rule("YEARLY", by_month=[13])
    assert "RRULE:" not in out, f"by_month=13 прошёл: {out!r}"


async def test_invalid_by_day_token_is_rejected():
    out = await s.build_recurrence_rule("WEEKLY", by_day=["MOND"])
    assert "RRULE:" not in out, f"мусорный by_day прошёл: {out!r}"
    assert "MOND" in out


async def test_zero_or_negative_interval_is_rejected_not_silently_one():
    for bad in (0, -3):
        out = await s.build_recurrence_rule("WEEKLY", interval=bad)
        assert "RRULE:" not in out, f"interval={bad} молча стал 1: {out!r}"


async def test_ordinal_by_day_is_documented():
    # Докстринг — часть контракта инструмента: порядковые префиксы и то, что
    # by_day работает не только для WEEKLY, раньше не были описаны.
    doc = s.build_recurrence_rule.__doc__
    assert "2TU" in doc or "-1FR" in doc
    assert "BYMONTHDAY" in doc
