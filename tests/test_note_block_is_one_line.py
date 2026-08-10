"""1.1.6 — Д13: `_unarmed_note` обязана возвращать не более одной строки.

Живой дефект: `_unarmed_note` (`ticktick_mcp/src/server.py`) собирает список
из двух возможных жалоб — ⚠️ «выполнено БЕЗ сверки названия» (сверка не
состоялась, потому что название не передали — `loose`) и ℹ️ «опознано ПО id»
(имени нет ни у вызывающего, ни у живой задачи — `by_id`) — и раньше склеивала
их через `"\n".join(notes)`. Три потребителя (`_update_tasks_impl`,
`_complete_tasks_impl`, `_move_tasks_impl`) печатают результат как ОДИН
элемент своего списка строк ответа; внешний бот разбирает ответ построчно, и
вторая строка вместо одной — незаявленное расхождение замороженного формата.

Главный способ подделки (см. `docs/TZ/ZAHOD1.md`, п. 1.1.6): склеить через
`" ".join()` вместо `"\n"` — число строк станет верным, но если при этом
значок ⚠️ выбирается механически «от первой фразы» (а не по СОДЕРЖИМОМУ),
законное опознание по `id` без единой непроверенной строки снова понижает
вердикт исполнителя через `_EXEC_WARN_MARKERS`
(`ticktick_mcp/src/tg_auto_execute.py`), который ищет ⚠️ где угодно в
самоотчёте. Поэтому второй тест ниже отдельно проверяет ЧИСТЫЙ `by_id`-случай
без единого ⚠️.
"""
import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_auto_execute as tae


def test_update_tasks_prints_at_most_one_note_line():
    """Вход с одной несверенной (`loose`) и одной безымянной (`by_id`)
    задачей — в ответе ровно одна строка примечания, и она начинается с ⚠️
    (самый строгий из двух случаев ведёт)."""
    found = [
        {"taskId": "t1", "title": "Купить молоко", "armed": False,
         "by_id": False},
        {"taskId": "t2", "title": "(без названия: 📎 1 файл)", "armed": False,
         "by_id": True},
    ]
    note = s._unarmed_note(found)

    assert "\n" not in note
    lines = [ln for ln in note.split("\n") if ln]
    assert len(lines) == 1
    assert note.startswith("⚠️")
    # Обе жалобы несут разную информацию — ни одна не выброшена при склейке.
    assert "выполнено БЕЗ сверки названия" in note
    assert "опознано ПО id" in note


def test_by_id_only_note_does_not_lower_the_verdict():
    """Вход только с `by_id` (пустой `loose`) — итоговая строка НЕ содержит
    ⚠️ (начинается с ℹ️), и `_EXEC_WARN_MARKERS` по ней не срабатывает: это
    законное опознание по id, а не сомнение в проверке."""
    found = [
        {"taskId": "t2", "title": "(без названия: 📎 1 файл)", "armed": False,
         "by_id": True},
    ]
    note = s._unarmed_note(found)

    assert "\n" not in note
    assert "⚠️" not in note
    assert note.startswith("ℹ️")

    assert not any(mark in note for mark in tae._EXEC_WARN_MARKERS)
