"""Карточка подтверждения обязана НАЗЫВАТЬ объект, а не показывать его id.

Класс дефекта. Describe-функции гейта печатали `t.get('title') or taskId` —
то есть при не переданном названии в позицию НАЗВАНИЯ, в кавычки, попадали
24 hex-символа. Человек читает это как имя объекта и физически не может
сверить, тот ли объект: id глазами не сверяет никто. То же с проектом
назначения (`to_project_name or to_project_id`) и с родительской задачей
(`parent_task_title or parent_task_id`).

Правило, выведенное живой приёмкой 2026-08-07: идентификатор РЯДОМ с именем
— нормально; идентификатор ВМЕСТО имени — дефект. А если имя установить не
удалось, это надо сказать ВСЛУХ, а не показывать id молча.

Почему тест требует ОТСУТСТВИЯ сырого id, а не только наличия имени: иначе
правка «имя добавили, id оставили вместо» (или рядом, в кавычках) прошла бы
проверку на наличие имени и ничего бы не изменила для читающего.

Стенд — `tests/read_stand.py`: настоящие клиенты, подменён только HTTP,
инструменты зовутся по имени через реестр MCP (ровно то, что уедет клиенту).
"""
import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as stand

GHOST = "6a99ghostghostghost0001"     # такого объекта в аккаунте нет вовсе
NO_NAME = "УСТАНОВИТЬ НЕ УДАЛОСЬ"     # общая формулировка «имя неизвестно»


@pytest.fixture(autouse=True)
def _stand(monkeypatch):
    return stand.wire(monkeypatch)


def _assert_named(text: str, name: str, *raw_ids: str):
    """Имя названо, и ни один сырой id в карточке не появился."""
    assert name in text, text
    for rid in raw_ids:
        assert rid not in text, f"сырой id {rid} остался в карточке:\n{text}"


# ─────────────────────── батч-инструменты (_gate_batch) ───────────────────────
#
# Во всех батч-тестах проверяемая строка идёт ПЕРВОЙ, а под ней есть ещё
# одна: всё, что печатается под списком (сводки, предупреждения, инструкция
# модели), при наивном разборе склеивается с последним пунктом — и проверка
# тихо перестаёт быть проверкой.

async def test_complete_tasks_card_names_the_task():
    text = await stand.call(
        "complete_tasks", summary="Закрываю задачи",
        tasks=[{"taskId": stand.TASK_ROOT},
               {"taskId": stand.TASK_MID, "title": "Записаться к врачу"}])

    _assert_named(text, "Собрать отчёт", stand.TASK_ROOT)


async def test_update_tasks_card_names_the_task():
    text = await stand.call(
        "update_tasks", summary="Правлю задачи",
        tasks=[{"taskId": stand.TASK_ROOT, "priority": 5},
               {"taskId": stand.TASK_MID, "title": "Записаться к врачу",
                "priority": 1}])

    _assert_named(text, "Собрать отчёт", stand.TASK_ROOT)


async def test_move_tasks_card_names_both_the_task_and_the_destination():
    text = await stand.call(
        "move_tasks", summary="Переношу задачи",
        tasks=[{"taskId": stand.TASK_ROOT},
               {"taskId": stand.TASK_MID, "title": "Записаться к врачу"}],
        to_project_id=stand.P_HOME)

    # Проект назначения — тот же класс: «→ 6a21home» человек не сверит.
    _assert_named(text, "Собрать отчёт", stand.TASK_ROOT, stand.P_HOME)
    assert "Дом" in text, text


async def test_set_task_tags_card_names_the_task():
    text = await stand.call(
        "set_task_tags", summary="Проставляю теги",
        tasks=[{"taskId": stand.TASK_ROOT, "tags": ["тег01"]},
               {"taskId": stand.TASK_MID, "title": "Записаться к врачу",
                "tags": ["тег02"]}])

    _assert_named(text, "Собрать отчёт", stand.TASK_ROOT)


async def test_set_task_parent_card_names_the_parent():
    """Живое название родителя здесь УЖЕ прочитано identity-guard'ом строкой
    выше — и выбрасывалось, а печатался id."""
    text = await stand.call(
        "set_task_parent", summary="Вкладываю задачи",
        tasks=[{"taskId": stand.TASK_MID, "title": "Записаться к врачу"},
               {"taskId": stand.TASK_HIGH, "title": "Оплатить страховку"}],
        parent_task_id=stand.TASK_ROOT, project_id=stand.P_WORK)

    _assert_named(text, "Собрать отчёт", stand.TASK_ROOT)


async def test_restore_tasks_card_names_the_task_from_the_trash():
    text = await stand.call(
        "restore_tasks", summary="Восстанавливаю",
        tasks=[{"taskId": stand.TASK_TRASHED},
               {"taskId": "6a49trash", "title": "Черновик письма"}])

    _assert_named(text, "Старая затея", stand.TASK_TRASHED)


# ───────────────── одиночные инструменты (_gate_single) ─────────────────

async def test_abandon_task_card_names_the_task():
    text = await stand.call("abandon_task", summary="",
                            task_id=stand.TASK_ROOT)

    _assert_named(text, "Собрать отчёт", stand.TASK_ROOT)


async def test_duplicate_task_card_names_the_task():
    text = await stand.call("duplicate_task", summary="",
                            task_id=stand.TASK_ROOT)

    _assert_named(text, "Собрать отчёт", stand.TASK_ROOT)


# ────────── имя установить не удалось — сказать ВСЛУХ, не молчать ──────────

async def test_unknown_task_name_is_stated_out_loud_not_shown_as_an_id():
    text = await stand.call(
        "complete_tasks", summary="Закрываю задачи",
        tasks=[{"taskId": GHOST},
               {"taskId": stand.TASK_MID, "title": "Записаться к врачу"}])

    # id тут показать МОЖНО — но только вместе с прямым признанием, что
    # название неизвестно. Молчаливый показ id и был дефектом.
    assert NO_NAME in text, text
    assert GHOST in text, text


async def test_unknown_destination_project_is_stated_out_loud(monkeypatch):
    text = await stand.call(
        "move_tasks", summary="Переношу задачи",
        tasks=[{"taskId": stand.TASK_ROOT, "title": "Собрать отчёт"},
               {"taskId": stand.TASK_MID, "title": "Записаться к врачу"}],
        to_project_id=GHOST)

    assert NO_NAME in text, text
    assert GHOST in text, text


# ─────────────────────────── manual_triage ───────────────────────────

async def test_manual_triage_card_names_the_task():
    """У manual_triage живое название берётся при сверке плана — а фолбэком
    в позиции имени стоял task_id."""
    text = await stand.call(
        "manual_triage", summary="Разбираю",
        operations=[
            {"op": "complete", "task_id": stand.TASK_ROOT,
             "title": "Собрать отчёт", "said": "отчёт готов, закрой"},
            {"op": "complete", "task_id": stand.TASK_MID,
             "title": "Записаться к врачу", "said": "к врачу сходил"},
        ])

    _assert_named(text, "Собрать отчёт", stand.TASK_ROOT)


def test_describe_triage_op_never_puts_an_id_in_the_name_slot():
    """Прямой вызов описателя строки плана manual_triage.

    Через сам инструмент этот случай недостижим: пустой `title` он отвергает
    целым планом раньше, чем дойдёт до описателя. Поэтому фолбэк на
    `task_id` в позиции имени был МЁРТВЫМ — но именно как мина: ослабь
    когда-нибудь ту валидацию, и карточка молча начнёт называть задачу
    номером. Проверяется здесь напрямую, раз через тул не достать."""
    line = s._describe_triage_op({"op": "complete", "task_id": GHOST,
                                  "said": "закрой эту"})

    assert NO_NAME in line, line
    assert f"«{GHOST}»" not in line, line


async def test_manual_triage_skipped_row_is_named_not_numbered():
    """Строка ПРОПУЩЕНО (задачи нет среди открытых) тоже обязана называть
    задачу. Пустой title сюда не доходит вовсе — manual_triage отказывает
    раньше, целым планом, — поэтому проверяется достижимый случай."""
    text = await stand.call(
        "manual_triage", summary="Разбираю",
        operations=[
            {"op": "complete", "task_id": GHOST, "title": "Призрак",
             "said": "закрой эту"},
            {"op": "complete", "task_id": stand.TASK_MID,
             "title": "Записаться к врачу", "said": "к врачу сходил"},
        ])

    assert "ПРОПУЩЕНО" in text, text
    _assert_named(text, "Призрак", GHOST)


# ─────────────────────── get_task_info (READONLY) ───────────────────────

def _state_with_named_people():
    tasks = [dict(t) for t in stand.TASKS]
    for t in tasks:
        if t["id"] == stand.TASK_ASSIGNED_OPEN:
            t["creator"] = stand.MEMBER_OTHER      # создала не владелец, а Ирина
            t["columnId"] = stand.COL_B
    return stand.build_state(tasks=tasks)


def _line_with(text: str, prefix: str) -> str:
    return next(ln for ln in text.splitlines() if ln.strip().startswith(prefix))


async def test_get_task_info_names_assignee_creator_and_column(monkeypatch):
    """Здесь, в отличие от карточек подтверждения, id рядом с именем ПОЛЕЗЕН
    (userId кладут в поле assignee, columnId — в create/update). Проверяется
    поэтому не отсутствие id, а то, что он не стоит ВМЕСТО имени: в каждой
    из трёх строк обязано быть человеческое имя."""
    stand.wire(monkeypatch, state=_state_with_named_people())

    text = await stand.call("get_task_info", task_id=stand.TASK_ASSIGNED_OPEN)

    assert "Ирина" in _line_with(text, "assignee:"), text
    assert "Ирина" in _line_with(text, "created:"), text     # автор задачи
    assert "В работе" in _line_with(text, "columnId:"), text  # раздел (колонка)


async def test_get_task_info_says_so_when_a_person_cannot_be_named(monkeypatch):
    tasks = [dict(t) for t in stand.TASKS]
    for t in tasks:
        if t["id"] == stand.TASK_ASSIGNED_OPEN:
            t["assignee"] = "999888"      # такого участника в проекте нет
    stand.wire(monkeypatch, state=stand.build_state(tasks=tasks))

    text = await stand.call("get_task_info", task_id=stand.TASK_ASSIGNED_OPEN)

    assert NO_NAME in text, text
    assert "999888" in text, text
