"""restore_tasks: задача, прошедшая путь «завершена → удалена → восстановлена»,
возвращается из корзины ЗАВЕРШЁННОЙ и в открытый пул не попадает — вердикт
обязан быть ✅, а не «❌ НЕ восстановлено».

Корень. Пост-проверка (`_restore_tasks_impl` и ветка `restore` в
`_verify_item`) искала восстановленную задачу ТОЛЬКО среди открытых. Для
завершённой это заведомо промах: TickTick честно вернул её из корзины, но в
`/batch/check/0` (снимок открытых) она не появляется никогда. Ирония в том,
что identity-guard в теле той же функции смотрит В КОРЗИНУ — правильный
источник выбран строкой выше и не использован строкой ниже.

Цена дефекта не косметическая: по правилам приёмки «❌» означает «есть
расхождения — это НЕ успех», и человек получал красный вердикт об удавшейся
операции, после чего шёл перепроверять руками то, что сработало.

Стенд — `tests/read_stand.py`: НАСТОЯЩИЙ v2-клиент, подменён только HTTP.
Восстановление в стенде возвращает задачу в том статусе, в каком её удаляли
(завершённую — в ленту завершённых, а не в открытые), поэтому проверяется
поведение сервера, а не доброта двойника.
"""
import json

import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as stand

T_DONE = "6a70donetrash"      # завершена, потом удалена, потом восстановлена
T_OPEN = "6a71opentrash"      # обычная (открытая) — для контраста в одном батче


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # Пост-проверка ждёт появления задачи среди открытых по расписанию
    # _POSTVERIFY_RETRY_DELAYS_S (до ~9 c). Тест не должен реально спать.
    monkeypatch.setattr(s.time, "sleep", lambda *a, **k: None)


def _trash():
    return [
        {"id": T_DONE, "projectId": stand.P_WORK, "title": "Оплатить счёт за март",
         "status": 2, "completedTime": stand._stamp(stand.TODAY)},
        {"id": T_OPEN, "projectId": stand.P_HOME, "title": "Полить фикус",
         "status": 0},
    ]


def _wire(monkeypatch, trash=None):
    return stand.wire(monkeypatch,
                      v2_kwargs={"trash": _trash() if trash is None else trash})


# ─────────────────── сам исполнитель: вердикт об успехе ───────────────────

async def test_restored_completed_task_is_reported_as_success(monkeypatch):
    _wire(monkeypatch)

    result = await s._restore_tasks_impl(
        "Восстанавливаю",
        [{"taskId": T_DONE, "title": "Оплатить счёт за март"}])

    assert "НЕ восстановлено" not in result, result
    assert "❌" not in result, result
    assert "Оплатить счёт за март" in result
    # Статус обязан быть НАЗВАН: задача вернулась, но осталась завершённой —
    # молча засчитать это как «снова среди открытых» было бы неправдой.
    assert "завершён" in result.lower()


async def test_restored_completed_task_leaves_the_trash_for_real(monkeypatch):
    """Страховка от «фикса», который просто перестал бы проверять: задача
    обязана реально уйти из корзины и найтись среди завершённых."""
    v2, _v1, _tr = _wire(monkeypatch)

    await s._restore_tasks_impl(
        "Восстанавливаю",
        [{"taskId": T_DONE, "title": "Оплатить счёт за март"}])

    assert all(x.get("id") != T_DONE for x in v2.get_trash(500))
    found, where = v2.find_task_any_state(T_DONE)
    assert where == "completed"
    assert found.get("projectId") == stand.P_WORK


async def test_open_and_completed_tasks_restored_together_are_both_confirmed(
        monkeypatch):
    """Смешанный батч: обычная задача возвращается в открытые, завершённая —
    в ленту завершённых. Ни одна не должна получить ❌."""
    _wire(monkeypatch)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [
            {"taskId": T_DONE, "title": "Оплатить счёт за март"},
            {"taskId": T_OPEN, "title": "Полить фикус"},
        ])

    assert "❌" not in result, result
    assert "Оплатить счёт за март" in result
    assert "Полить фикус" in result


# ─────────────── независимая перепроверка: ветка restore ───────────────

def test_verify_item_confirms_restore_of_a_task_that_stayed_completed(monkeypatch):
    v2, _v1, _tr = stand.wire(monkeypatch, v2_kwargs={"trash": []})
    # Задача лежит в ленте завершённых и НЕ среди открытых — ровно то
    # состояние, в котором её оставляет восстановление из корзины.
    item = {"taskId": stand.TASK_COMPLETED, "title": "Купить бумагу",
            "expect": {"projectId": stand.P_WORK}}
    del v2

    status, line = s._verify_item("restore", item, {}, {stand.P_WORK: "Работа"})

    assert status == "ok", line
    assert "✅" in line
    assert "завершён" in line.lower()


def test_verify_item_still_flags_a_task_that_never_left_the_trash(monkeypatch):
    """Обратная сторона: если задача так и осталась в корзине, вердикт
    по-прежнему ❌. Фикс не имеет права делать успехом всё подряд."""
    stand.wire(monkeypatch)
    item = {"taskId": stand.TASK_TRASHED, "title": "Старая затея",
            "expect": {"projectId": stand.P_HOME}}

    status, line = s._verify_item("restore", item, {}, {stand.P_HOME: "Дом"})

    assert status == "bad", line
    assert "❌" in line
    assert "корзин" in line.lower()


def test_verify_item_still_flags_a_task_found_nowhere(monkeypatch):
    stand.wire(monkeypatch)
    item = {"taskId": "6a99ghost", "title": "Призрак",
            "expect": {"projectId": stand.P_HOME}}

    status, line = s._verify_item("restore", item, {}, {stand.P_HOME: "Дом"})

    assert status == "bad", line
    assert "❌" in line


# ─────────────────────── отчёт operation_report ───────────────────────

async def test_operation_report_does_not_call_a_successful_restore_a_discrepancy(
        monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _wire(monkeypatch)

    # Проверяемая задача идёт ПЕРВОЙ намеренно: под списком вердиктов отчёт
    # печатает «Итог» и «Статус операции», и проверка последнего пункта
    # склеивалась бы с ними при наивном разборе.
    await s._restore_tasks_impl(
        "Восстанавливаю", [
            {"taskId": T_DONE, "title": "Оплатить счёт за март"},
            {"taskId": T_OPEN, "title": "Полить фикус"},
        ])

    rec = json.loads(
        (tmp_path / "deletion_journal.jsonl").read_text(encoding="utf-8").strip())
    report = s._build_operation_report(rec["record"])

    assert "Итог: ✅ 2 подтверждено, ⚠️ 0 не проверено, ❌ 0 расхождений" in report
    assert "Статус операции: ✅" in report
