"""build_reminder: отрицательный ввод — ошибка, а не молчаливый ноль (def-D1).

Корень: `if minutes_before <= 0: return "TRIGGER:PT0S"` сваливал ЛЮБОЕ
отрицательное значение в «напомнить ровно в момент события» и возвращал это
как нормальный результат. Вызывающий просил «за 30 минут», ошибся знаком —
и получал синтаксически валидный триггер без единого слова о подмене.
Метод обязан либо отказать, либо честно пометить; здесь выбран отказ.
"""
import ticktick_mcp.src.server as s


async def test_negative_minutes_is_rejected_not_silently_zeroed():
    out = await s.build_reminder(-30)

    # Главное: НЕ выдать валидный триггер на ошибочный ввод.
    assert "TRIGGER" not in out, f"негативный ввод молча стал триггером: {out!r}"
    # И назвать в ответе само значение, чтобы ошибка была видна.
    assert "-30" in out


async def test_zero_minutes_means_at_the_event_time():
    # Граничный случай «за 0 минут» — документированное поведение, остаётся.
    assert await s.build_reminder(0) == "TRIGGER:PT0S"
    assert await s.build_reminder() == "TRIGGER:PT0S"


async def test_three_days_before():
    # Граничный случай «за 3 дня» — целые сутки сворачиваются в дни.
    assert await s.build_reminder(3 * 24 * 60) == "TRIGGER:-P3D"


async def test_hours_and_minutes_forms():
    assert await s.build_reminder(60) == "TRIGGER:-PT1H"
    assert await s.build_reminder(90) == "TRIGGER:-PT90M"
    assert await s.build_reminder(15) == "TRIGGER:-PT15M"
