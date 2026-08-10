"""ЗАМОРОЗКА ТЕКСТОВ ОТКАЗА (2026-08-09, ZAHOD1.md 1.2.3, П11).

Зачем этот файл существует. П11 сворачивает в один помощник то, что было
написано в `server.py` двадцатью пятью копиями (охранник личности объекта) и
пятнадцатью (обёртка отказа по папке). У свёртки есть ровно один способ
навредить: незаметно ПЕРЕПИСАТЬ текст ответа. У сервера два живых
потребителя — телеграм-бот, разбирающий ответы регулярками вплоть до пробелов,
и агент в n8n; «привести к общему виду» три-пять формулировок значит сломать
их обоих, не уронив ни одного теста.

Поэтому ожидаемые строки в `EXPECTED` сняты ПРОГОНОМ НА НЕТРОНУТОМ ДЕРЕВЕ —
до единой правки `server.py` — и записаны сюда дословно, вместе с эмодзи,
кавычками-ёлочками, пробелами и точками. Тест сравнивает `==`, а не «есть
подстрока»: любое смягчение сравнения превращает заморозку в тавтологию.

ОТДЕЛЬНО ПРО ДВЕ РАЗНЫЕ ФОРМУЛИРОВКИ. На части площадок отказ по mismatch
звучит как «id это «X», а НЕ «Y»» (текст пишет сама площадка), на части — как
«id указывает на «X», а НЕ «Y»» (текст приходит из `_Guard.message`). Второй
вариант встречается чаще, и соблазн «привести к большинству» здесь надо
погасить: это не случайно разошедшиеся копии одного смысла, а разные тексты
разных команд. Обе формулировки заморожены ниже — если кто-то сведёт их к
одной, покраснеет ровно тот кейс, чей ответ он изменил.

ЧАСТИЧНЫЕ КЕЙСЫ (`EXPECTED_CONTAINS`). Три площадки отвечают не отказом, а
карточкой плана с предупреждением; в карточке есть `manifest_id`, который
меняется от прогона к прогону, поэтому заморожен дословно сам фрагмент
предупреждения, а сравнение — вхождение. Это единственное исключение, и оно
касается предупреждений, а не отказов.
"""
import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

# ─────────────────────────── постоянные стенда ───────────────────────────

WRONG = "Совсем другая задача"      # имя, которое НЕ совпадёт с живым
GHOST = "6a99ghostghostghost"       # id, которого нет ни в одной ленте
GHOST_TITLE = "Призрачная задача"
GHOST_PROJECT = "6a98ghostproject"
ROOT_TITLE = "Собрать отчёт"        # rs.TASK_ROOT, живая, в «Работа»
KID_TITLE = "Взять цифры у бухгалтерии"   # rs.TASK_KID, подзадача TASK_ROOT
TRASH_TITLE = "Старая затея"        # rs.TASK_TRASHED
DONE_TITLE = "Купить бумагу"        # rs.TASK_COMPLETED


@pytest.fixture(autouse=True)
def isolate_manifests():
    """Плановые команды при УСПЕХЕ кладут манифест в глобальный словарь.
    Здесь все кейсы отказные, но соседство с другими файлами не должно
    зависеть от порядка запуска."""
    before, tombs = dict(s._MANIFESTS), dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def wire(monkeypatch):
    """Живое положение дел: официальный API знает и завершённую, и корзинную
    задачу (он не носит флага удаления), снимок открытых — ни ту, ни другую.
    Ровно тот же стенд, на котором сняты замороженные строки."""
    return rs.wire(monkeypatch,
                   v1_tasks=list(rs.TASKS) + list(rs.COMPLETED) + list(rs.TRASHED))


async def _sync(fn, *args, **kwargs):
    """Мостик для площадок, которые не корутины: результат приводится к
    строке, чтобы таблица заморозки была однородной."""
    out = fn(*args, **kwargs)
    return out if isinstance(out, str) else repr(out)


# ─────────────────────────── сценарии ───────────────────────────
# Ключ — «команда/фаза/состояние». Значение — вызов, отдающий ответ сервера.

CASES = {
    # ── 1. create_subtask: план (родитель) ──────────────────────────────
    "create_subtask/план/mismatch": lambda: rs.call(
        "create_subtask", parent_task_title=WRONG, subtask_title="Пункт",
        parent_task_id=rs.TASK_ROOT, project_id=rs.P_WORK),
    "create_subtask/план/missing": lambda: rs.call(
        "create_subtask", parent_task_title=GHOST_TITLE, subtask_title="Пункт",
        parent_task_id=GHOST, project_id=rs.P_WORK),
    # Корзина здесь приходит не отдельным статусом, а `missing` (обычный
    # `_guard_task`, подзадача у удалённого родителя смысла не имеет) —
    # поэтому кейс НЕ входит в набор политики корзины ниже.
    "create_subtask/план/корзина_как_missing": lambda: rs.call(
        "create_subtask", parent_task_title=TRASH_TITLE, subtask_title="Пункт",
        parent_task_id=rs.TASK_TRASHED, project_id=rs.P_HOME),
    # ── 2. create_subtask: исполнение ───────────────────────────────────
    "create_subtask/исполнение/mismatch": lambda: s._create_subtask_impl(
        WRONG, "Пункт", rs.TASK_ROOT, rs.P_WORK),
    "create_subtask/исполнение/missing": lambda: s._create_subtask_impl(
        GHOST_TITLE, "Пункт", GHOST, rs.P_WORK),
    "create_subtask/исполнение/корзина_как_missing": lambda: s._create_subtask_impl(
        TRASH_TITLE, "Пункт", rs.TASK_TRASHED, rs.P_HOME),

    # ── 3. set_task_parent: план (родитель) ─────────────────────────────
    "set_task_parent/план/mismatch": lambda: rs.call(
        "set_task_parent", summary="Вложить",
        tasks=[{"taskId": rs.TASK_MID, "title": "Записаться к врачу"}],
        parent_task_id=rs.TASK_ROOT, project_id=rs.P_WORK,
        parent_task_title=WRONG),
    "set_task_parent/план/missing": lambda: rs.call(
        "set_task_parent", summary="Вложить",
        tasks=[{"taskId": rs.TASK_MID, "title": "Записаться к врачу"}],
        parent_task_id=GHOST, project_id=rs.P_WORK,
        parent_task_title=GHOST_TITLE),
    # ── 4. set_task_parent: исполнение ──────────────────────────────────
    "set_task_parent/исполнение/mismatch": lambda: s._set_task_parent_impl(
        "Вложить", [{"taskId": rs.TASK_MID, "title": "Записаться к врачу"}],
        rs.TASK_ROOT, rs.P_WORK, WRONG),
    "set_task_parent/исполнение/missing": lambda: s._set_task_parent_impl(
        "Вложить", [{"taskId": rs.TASK_MID, "title": "Записаться к врачу"}],
        GHOST, rs.P_WORK, GHOST_TITLE),

    # ── 5. unset_task_parent: план, САМА ЗАДАЧА ─────────────────────────
    "unset_task_parent/план/задача/mismatch": lambda: rs.call(
        "unset_task_parent", task_title=WRONG, parent_task_title=ROOT_TITLE,
        task_id=rs.TASK_KID, parent_task_id=rs.TASK_ROOT, project_id=rs.P_WORK),
    "unset_task_parent/план/задача/missing": lambda: rs.call(
        "unset_task_parent", task_title=GHOST_TITLE, parent_task_title=ROOT_TITLE,
        task_id=GHOST, parent_task_id=rs.TASK_ROOT, project_id=rs.P_WORK),
    # ── 6. unset_task_parent: план, РОДИТЕЛЬ ────────────────────────────
    "unset_task_parent/план/родитель/mismatch": lambda: rs.call(
        "unset_task_parent", task_title=KID_TITLE, parent_task_title=WRONG,
        task_id=rs.TASK_KID, parent_task_id=rs.TASK_ROOT, project_id=rs.P_WORK),
    # ── 7. unset_task_parent: исполнение, САМА ЗАДАЧА ───────────────────
    "unset_task_parent/исполнение/задача/mismatch": lambda: s._unset_task_parent_impl(
        WRONG, ROOT_TITLE, rs.TASK_KID, rs.TASK_ROOT, rs.P_WORK),
    "unset_task_parent/исполнение/задача/missing": lambda: s._unset_task_parent_impl(
        GHOST_TITLE, ROOT_TITLE, GHOST, rs.TASK_ROOT, rs.P_WORK),
    # ── 8. unset_task_parent: исполнение, РОДИТЕЛЬ ──────────────────────
    "unset_task_parent/исполнение/родитель/mismatch": lambda: s._unset_task_parent_impl(
        KID_TITLE, WRONG, rs.TASK_KID, rs.TASK_ROOT, rs.P_WORK),

    # ── 9. add_task_comment: план ───────────────────────────────────────
    "add_task_comment/план/mismatch": lambda: rs.call(
        "add_task_comment", task_title=WRONG, text="дописал вывод",
        project_id=rs.P_WORK, task_id=rs.TASK_ROOT),
    "add_task_comment/план/missing": lambda: rs.call(
        "add_task_comment", task_title=GHOST_TITLE, text="дописал вывод",
        project_id=rs.P_WORK, task_id=GHOST),
    "add_task_comment/план/trashed": lambda: rs.call(
        "add_task_comment", task_title=TRASH_TITLE, text="дописал вывод",
        project_id=rs.P_HOME, task_id=rs.TASK_TRASHED),
    # ── 10. add_task_comment: исполнение ────────────────────────────────
    "add_task_comment/исполнение/mismatch": lambda: s._add_task_comment_impl(
        WRONG, "дописал вывод", rs.P_WORK, rs.TASK_ROOT),
    "add_task_comment/исполнение/missing": lambda: s._add_task_comment_impl(
        GHOST_TITLE, "дописал вывод", rs.P_WORK, GHOST),
    "add_task_comment/исполнение/trashed": lambda: s._add_task_comment_impl(
        TRASH_TITLE, "дописал вывод", rs.P_HOME, rs.TASK_TRASHED),

    # ── 11. attach_file_to_task: план ───────────────────────────────────
    "attach_file_to_task/план/mismatch": lambda: rs.call(
        "attach_file_to_task", task_title=WRONG, task_id=rs.TASK_ROOT,
        project_id=rs.P_WORK, content_base64="0LDQsdCy", filename="чек.pdf"),
    "attach_file_to_task/план/missing": lambda: rs.call(
        "attach_file_to_task", task_title=GHOST_TITLE, task_id=GHOST,
        project_id=rs.P_WORK, content_base64="0LDQsdCy", filename="чек.pdf"),
    "attach_file_to_task/план/trashed": lambda: rs.call(
        "attach_file_to_task", task_title=TRASH_TITLE, task_id=rs.TASK_TRASHED,
        project_id=rs.P_HOME, content_base64="0LDQsdCy", filename="чек.pdf"),
    # ── 12. attach_file_to_task: исполнение ─────────────────────────────
    "attach_file_to_task/исполнение/mismatch": lambda: s._attach_file_to_task_impl(
        WRONG, rs.TASK_ROOT, rs.P_WORK, content_base64="0LDQsdCy",
        filename="чек.pdf"),
    "attach_file_to_task/исполнение/missing": lambda: s._attach_file_to_task_impl(
        GHOST_TITLE, GHOST, rs.P_WORK, content_base64="0LDQsdCy",
        filename="чек.pdf"),
    "attach_file_to_task/исполнение/trashed": lambda: s._attach_file_to_task_impl(
        TRASH_TITLE, rs.TASK_TRASHED, rs.P_HOME, content_base64="0LDQsdCy",
        filename="чек.pdf"),

    # ── 13. abandon_task: план ──────────────────────────────────────────
    "abandon_task/план/mismatch": lambda: rs.call(
        "abandon_task", summary="Отказаться", task_id=rs.TASK_ROOT,
        task_title=WRONG),
    "abandon_task/план/missing": lambda: rs.call(
        "abandon_task", summary="Отказаться", task_id=GHOST,
        task_title=GHOST_TITLE),
    # То же, что у create_subtask: `abandon_task` живёт на обычном
    # `_guard_task`, и корзина приходит к нему как `missing`.
    "abandon_task/план/корзина_как_missing": lambda: rs.call(
        "abandon_task", summary="Отказаться", task_id=rs.TASK_TRASHED,
        task_title=TRASH_TITLE),
    # ── 14. abandon_task: исполнение ────────────────────────────────────
    "abandon_task/исполнение/mismatch": lambda: s._abandon_task_impl(
        "Отказаться", rs.TASK_ROOT, WRONG),
    "abandon_task/исполнение/missing": lambda: s._abandon_task_impl(
        "Отказаться", GHOST, GHOST_TITLE),
    "abandon_task/исполнение/корзина_как_missing": lambda: s._abandon_task_impl(
        "Отказаться", rs.TASK_TRASHED, TRASH_TITLE),

    # ── 15. duplicate_task: план ────────────────────────────────────────
    "duplicate_task/план/mismatch": lambda: rs.call(
        "duplicate_task", summary="как шаблон", task_id=rs.TASK_ROOT,
        task_title=WRONG),
    "duplicate_task/план/missing": lambda: rs.call(
        "duplicate_task", summary="как шаблон", task_id=GHOST,
        task_title=GHOST_TITLE),
    "duplicate_task/план/trashed": lambda: rs.call(
        "duplicate_task", summary="как шаблон", task_id=rs.TASK_TRASHED,
        task_title=TRASH_TITLE),
    # ── 16. duplicate_task: исполнение ──────────────────────────────────
    "duplicate_task/исполнение/mismatch": lambda: s._duplicate_task_impl(
        "как шаблон", rs.TASK_ROOT, WRONG),
    "duplicate_task/исполнение/missing": lambda: s._duplicate_task_impl(
        "как шаблон", GHOST, GHOST_TITLE),
    "duplicate_task/исполнение/trashed": lambda: s._duplicate_task_impl(
        "как шаблон", rs.TASK_TRASHED, TRASH_TITLE),

    # ── 17. update_task_comment: план ───────────────────────────────────
    "update_task_comment/план/mismatch": lambda: rs.call(
        "update_task_comment", task_title=WRONG, text="правка",
        project_id=rs.P_WORK, task_id=rs.TASK_ROOT, comment_id=rs.COMMENT_ID),
    "update_task_comment/план/missing": lambda: rs.call(
        "update_task_comment", task_title=GHOST_TITLE, text="правка",
        project_id=rs.P_WORK, task_id=GHOST, comment_id=rs.COMMENT_ID),
    "update_task_comment/план/trashed": lambda: rs.call(
        "update_task_comment", task_title=TRASH_TITLE, text="правка",
        project_id=rs.P_HOME, task_id=rs.TASK_TRASHED, comment_id=rs.COMMENT_ID),
    # ── 18. update_task_comment: исполнение ─────────────────────────────
    "update_task_comment/исполнение/mismatch": lambda: s._update_task_comment_impl(
        WRONG, "правка", rs.P_WORK, rs.TASK_ROOT, rs.COMMENT_ID),
    "update_task_comment/исполнение/missing": lambda: s._update_task_comment_impl(
        GHOST_TITLE, "правка", rs.P_WORK, GHOST, rs.COMMENT_ID),
    "update_task_comment/исполнение/trashed": lambda: s._update_task_comment_impl(
        TRASH_TITLE, "правка", rs.P_HOME, rs.TASK_TRASHED, rs.COMMENT_ID),

    # ── 19. delete_task_comment: план ───────────────────────────────────
    "delete_task_comment/план/mismatch": lambda: rs.call(
        "delete_task_comment", task_title=WRONG, project_id=rs.P_WORK,
        task_id=rs.TASK_ROOT, comment_id=rs.COMMENT_ID),
    "delete_task_comment/план/missing": lambda: rs.call(
        "delete_task_comment", task_title=GHOST_TITLE, project_id=rs.P_WORK,
        task_id=GHOST, comment_id=rs.COMMENT_ID),
    "delete_task_comment/план/trashed": lambda: rs.call(
        "delete_task_comment", task_title=TRASH_TITLE, project_id=rs.P_HOME,
        task_id=rs.TASK_TRASHED, comment_id=rs.COMMENT_ID),
    # ── 20. delete_task_comment: исполнение ─────────────────────────────
    "delete_task_comment/исполнение/mismatch": lambda: s._delete_task_comment_impl(
        WRONG, rs.P_WORK, rs.TASK_ROOT, rs.COMMENT_ID),
    "delete_task_comment/исполнение/missing": lambda: s._delete_task_comment_impl(
        GHOST_TITLE, rs.P_WORK, GHOST, rs.COMMENT_ID),
    "delete_task_comment/исполнение/trashed": lambda: s._delete_task_comment_impl(
        TRASH_TITLE, rs.P_HOME, rs.TASK_TRASHED, rs.COMMENT_ID),

    # ── 21. update_tasks: пакетная площадка охранника ───────────────────
    "update_tasks/исполнение/mismatch": lambda: s._update_tasks_impl(
        "Правка", [{"taskId": rs.TASK_ROOT, "title": WRONG, "priority": 5}]),
    "update_tasks/исполнение/missing": lambda: s._update_tasks_impl(
        "Правка", [{"taskId": GHOST, "title": GHOST_TITLE, "priority": 5}]),
    "update_tasks/исполнение/trashed": lambda: s._update_tasks_impl(
        "Правка", [{"taskId": rs.TASK_TRASHED, "title": TRASH_TITLE,
                    "priority": 5}]),
    # ── 22. complete_tasks: пакетная площадка охранника ─────────────────
    "complete_tasks/исполнение/mismatch": lambda: s._complete_tasks_impl(
        "Закрыть", [{"taskId": rs.TASK_ROOT, "title": WRONG}]),
    "complete_tasks/исполнение/missing": lambda: s._complete_tasks_impl(
        "Закрыть", [{"taskId": GHOST, "title": GHOST_TITLE}]),

    # ── 23. create_tasks: внешний родитель (пакет) ──────────────────────
    "create_tasks/исполнение/родитель/mismatch": lambda: s._create_tasks_impl(
        "Создаю", [{"title": "Новая", "project_id": rs.P_WORK,
                    "parent_id": rs.TASK_ROOT, "parent_title": WRONG}]),
    "create_tasks/исполнение/родитель/missing": lambda: s._create_tasks_impl(
        "Создаю", [{"title": "Новая", "project_id": rs.P_WORK,
                    "parent_id": GHOST, "parent_title": GHOST_TITLE}]),
    # ── 24. plan_task_creation: внешний родитель (пакет, план) ──────────
    "plan_task_creation/план/родитель/mismatch": lambda: s.plan_task_creation(
        "Создаю", [{"title": "Новая", "project_id": rs.P_WORK,
                    "parent_id": rs.TASK_ROOT, "parent_title": WRONG}]),
    "plan_task_creation/план/родитель/missing": lambda: s.plan_task_creation(
        "Создаю", [{"title": "Новая", "project_id": rs.P_WORK,
                    "parent_id": GHOST, "parent_title": GHOST_TITLE}]),

    # ── 25-30. Постраничный вывод: offset за концом списка ──────────────
    "get_project_tasks/страница_за_концом": lambda: rs.call(
        "get_project_tasks", project_id=rs.P_WORK, limit=5, offset=900),
    "get_tasks_by_priority/страница_за_концом": lambda: rs.call(
        "get_tasks_by_priority", priority_id=5, limit=2, offset=900),
    "get_all_tasks/страница_за_концом": lambda: rs.call(
        "get_all_tasks", limit=5, offset=900),
    "get_inbox_tasks/страница_за_концом": lambda: rs.call(
        "get_inbox_tasks", limit=5, offset=900),
    "run_filter/страница_за_концом": lambda: rs.call(
        "run_filter", filter="Только срочное", limit=5, offset=900),
    "get_changes/страница_за_концом": lambda: rs.call(
        "get_changes", since="2026-03-13", until="2026-03-15", limit=5,
        offset=900),

    # ── 31-45. Обёртка отказа по ПАПКЕ (_guard_project) ─────────────────
    "delete_project/mismatch": lambda: rs.call(
        "delete_project", project_name="Не тот проект", project_id=rs.P_WORK),
    "delete_project/unknown": lambda: rs.call(
        "delete_project", project_name="Работа", project_id=GHOST_PROJECT),
    "move_tasks/исполнение/mismatch": lambda: s._move_tasks_impl(
        "Перенести", [{"taskId": rs.TASK_ROOT, "title": ROOT_TITLE}],
        rs.P_HOME, "Не тот проект"),
    "move_project_to_group/план/mismatch": lambda: rs.call(
        "move_project_to_group", project_name="Не тот проект",
        project_id=rs.P_WORK, group_id="grp-1"),
    "move_project_to_group/исполнение/mismatch": lambda: s._move_project_to_group_impl(
        "Не тот проект", rs.P_WORK, "grp-1"),
    "restore_tasks/исполнение/папка_неизвестна": lambda: s._restore_tasks_impl(
        "Вернуть", [{"taskId": rs.TASK_TRASHED, "title": TRASH_TITLE}],
        GHOST_PROJECT),
    "update_project/план/mismatch": lambda: rs.call(
        "update_project", project_name="Не тот проект", project_id=rs.P_WORK,
        name="Работа-2"),
    "update_project/исполнение/mismatch": lambda: s._update_project_impl(
        "Не тот проект", rs.P_WORK, name="Работа-2"),
    "archive_project/план/mismatch": lambda: rs.call(
        "archive_project", project_name="Не тот проект", project_id=rs.P_WORK,
        archived=True),
    "archive_project/план/разархивировать/mismatch": lambda: rs.call(
        "archive_project", project_name="Не тот проект", project_id=rs.P_ARCH,
        archived=False),
    "archive_project/исполнение/mismatch": lambda: s._archive_project_impl(
        "Не тот проект", rs.P_WORK, True),
    "archive_project/исполнение/разархивировать/mismatch": lambda: s._archive_project_impl(
        "Не тот проект", rs.P_ARCH, False),
    "create_project_column/план/mismatch": lambda: rs.call(
        "create_project_column", project_id=rs.P_WORK, name="Новая колонка",
        project_name="Не тот проект"),
    "create_project_column/исполнение/mismatch": lambda: s._create_project_column_impl(
        rs.P_WORK, "Новая колонка", "Не тот проект"),
    "create_tasks/исполнение/папка_неизвестна": lambda: s._create_tasks_impl(
        "Создаю", [{"title": "Новая", "project_id": GHOST_PROJECT,
                    "project_name": "Призрачный проект"}]),
    # Пятнадцатая площадка обёртки по папке — разбор назначения в triage.
    # Она не возвращает отказ наружу, а вплетает его в свою причину, поэтому
    # заморожена именно причина.
    "triage_destination/mismatch": lambda: _sync(
        s._resolve_triage_destination,
        {"to_project_id": rs.P_WORK, "to_project": "Не тот проект"},
        {rs.P_WORK: "Работа", rs.P_HOME: "Дом"}),
}

# Ответ обязан СОВПАСТЬ дословно. Снято на нетронутом дереве (см. докстринг).
EXPECTED = {
    'abandon_task/исполнение/mismatch': '🛑 НЕ отметил — id это «Собрать отчёт», а НЕ «Совсем другая задача». Ничего не тронул.',
    'abandon_task/исполнение/missing': '🛑 НЕ отметил — «Призрачная задача» не среди открытых задач (завершена/удалена/неверный id). Ничего не тронул.',
    'abandon_task/исполнение/корзина_как_missing': '🛑 НЕ отметил — «Старая затея» не среди открытых задач (завершена/удалена/неверный id). Ничего не тронул.',
    'abandon_task/план/mismatch': '🛑 План НЕ построен — id это «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'abandon_task/план/missing': '🛑 План НЕ построен — «Призрачная задача» не среди открытых задач (завершена/удалена/неверный id). Ничего не изменено.',
    'abandon_task/план/корзина_как_missing': '🛑 План НЕ построен — «Старая затея» не среди открытых задач (завершена/удалена/неверный id). Ничего не изменено.',
    'add_task_comment/исполнение/mismatch': '🛑 НЕ добавил комментарий — id это «Собрать отчёт», а НЕ «Совсем другая задача». Ничего не тронул.',
    'add_task_comment/исполнение/missing': '🛑 НЕ добавил комментарий — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не тронул.',
    'add_task_comment/исполнение/trashed': '🛑 НЕ добавил комментарий — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не тронул.',
    'add_task_comment/план/mismatch': '🛑 План НЕ построен — id это «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'add_task_comment/план/missing': '🛑 План НЕ построен — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не изменено.',
    'add_task_comment/план/trashed': '🛑 План НЕ построен — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не изменено.',
    'archive_project/исполнение/mismatch': '🛑 Отказ — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не тронул.',
    'archive_project/исполнение/разархивировать/mismatch': '🛑 Отказ — project_id указывает на «Архив», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не тронул.',
    'archive_project/план/mismatch': '🛑 План НЕ построен — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не изменено.',
    'archive_project/план/разархивировать/mismatch': '🛑 План НЕ построен — project_id указывает на «Архив», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не изменено.',
    'attach_file_to_task/исполнение/mismatch': '🛑 НЕ прикрепил — id это «Собрать отчёт», а НЕ «Совсем другая задача». Ничего не тронул.',
    'attach_file_to_task/исполнение/missing': '🛑 НЕ прикрепил — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не тронул.',
    'attach_file_to_task/исполнение/trashed': '🛑 НЕ прикрепил — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не тронул.',
    'attach_file_to_task/план/mismatch': '🛑 План НЕ построен — id указывает на «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'attach_file_to_task/план/missing': '🛑 План НЕ построен — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не изменено.',
    'attach_file_to_task/план/trashed': '🛑 План НЕ построен — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не изменено.',
    'complete_tasks/исполнение/mismatch': '🛑 НЕ завершил «Совсем другая задача» — id указывает на «Собрать отчёт», а НЕ «Совсем другая задача»',
    'complete_tasks/исполнение/missing': '↷ «Призрачная задача» — не среди открытых (уже завершена/удалена/неверный id), пропущено',
    'create_project_column/исполнение/mismatch': '🛑 Отказ — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не тронул.',
    'create_project_column/план/mismatch': '🛑 План НЕ построен — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не изменено.',
    'create_subtask/исполнение/mismatch': '🛑 НЕ создал подзадачу — родитель по id это «Собрать отчёт», а НЕ «Совсем другая задача». Ничего не тронул.',
    'create_subtask/исполнение/missing': '🛑 НЕ создал подзадачу — родитель «Призрачная задача» не среди открытых задач (завершён/удалён/неверный id). Ничего не тронул.',
    'create_subtask/исполнение/корзина_как_missing': '🛑 НЕ создал подзадачу — родитель «Старая затея» не среди открытых задач (завершён/удалён/неверный id). Ничего не тронул.',
    'create_subtask/план/mismatch': '🛑 План НЕ построен — родитель по id это «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'create_subtask/план/missing': '🛑 План НЕ построен — родитель «Призрачная задача» не среди открытых задач (завершён/удалён/неверный id). Ничего не изменено.',
    'create_subtask/план/корзина_как_missing': '🛑 План НЕ построен — родитель «Старая затея» не среди открытых задач (завершён/удалён/неверный id). Ничего не изменено.',
    'create_tasks/исполнение/папка_неизвестна': 'Ошибки (1):\n#1 «Новая»: 🛑 Отказ — проект по id 6a98ghostpro… не найден среди живых проектов (или имена недоступны) — сверить личность проекта нельзя. Ничего не тронул.',
    'create_tasks/исполнение/родитель/mismatch': 'Ошибки (1):\n#1 «Новая»: 🛑 родитель по id это «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи») — подзадача НЕ создаётся. Ничего не изменено.',
    'create_tasks/исполнение/родитель/missing': 'Ошибки (1):\n#1 «Новая»: 🛑 родитель «Призрачная задача» (6a99ghos…) не среди открытых задач (завершён/удалён/в корзине) — подзадача НЕ создаётся, иначе она легла бы отдельной задачей в корне. Ничего не изменено.',
    'delete_project/mismatch': '🛑 Отказ — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не тронул.',
    'delete_project/unknown': '🛑 Отказ — проект по id 6a98ghostpro… не найден среди живых проектов (или имена недоступны) — сверить личность проекта нельзя. Ничего не тронул.',
    'delete_task_comment/исполнение/mismatch': '🛑 НЕ удалил комментарий — id это «Собрать отчёт», а НЕ «Совсем другая задача». Ничего не тронул.',
    'delete_task_comment/исполнение/missing': '🛑 НЕ удалил комментарий — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не тронул.',
    'delete_task_comment/исполнение/trashed': '🛑 НЕ удалил комментарий — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не тронул.',
    'delete_task_comment/план/mismatch': '🛑 План НЕ построен — id указывает на «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'delete_task_comment/план/missing': '🛑 План НЕ построен — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не изменено.',
    'delete_task_comment/план/trashed': '🛑 План НЕ построен — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не изменено.',
    'duplicate_task/исполнение/mismatch': '🛑 НЕ дублировал — id это «Собрать отчёт», а НЕ «Совсем другая задача». Ничего не тронул.',
    'duplicate_task/исполнение/missing': '🛑 НЕ дублировал — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не тронул.',
    'duplicate_task/исполнение/trashed': '🛑 НЕ дублировал — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не тронул.',
    'duplicate_task/план/mismatch': '🛑 План НЕ построен — id это «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'duplicate_task/план/missing': '🛑 План НЕ построен — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не изменено.',
    'duplicate_task/план/trashed': '🛑 План НЕ построен — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не изменено.',
    'get_all_tasks/страница_за_концом': 'All open tasks: 14 top-level tasks total, but offset=900 is past the end (valid offsets are 0-13).',
    'get_changes/страница_за_концом': 'Изменений с 2026-03-13 по 2026-03-15: всего 2, но offset=900 уже за концом ленты (последняя страница начинается с offset=0).',
    'get_inbox_tasks/страница_за_концом': 'Inbox: 2 task(s) (2 top-level), but offset=900 is past the end (valid offsets are 0-1).',
    'get_project_tasks/страница_за_концом': "Project 'Работа' has 12 tasks, but offset=900 is past the end (last page starts at offset=10).",
    'get_tasks_by_priority/страница_за_концом': "Tasks that are 'priority 'High (5)'': 3 total, but offset=900 is past the end (last page starts at offset=2).",
    'move_project_to_group/исполнение/mismatch': '🛑 Отказ — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не тронул.',
    'move_project_to_group/план/mismatch': '🛑 План НЕ построен — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не изменено.',
    'move_tasks/исполнение/mismatch': '🛑 Отказ — project_id указывает на «Дом», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не тронул.',
    'plan_task_creation/план/родитель/mismatch': '### 📋 План создания — 0\n🛑 Плана нет — ни одна строка сверку не прошла, манифест НЕ создан, подтверждать нечего.\n🛑 **Исключены 1:** #1 «Новая»: родитель по id это «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи») — подзадача НЕ создаётся\nНичего не изменено.',
    'plan_task_creation/план/родитель/missing': '### 📋 План создания — 0\n🛑 Плана нет — ни одна строка сверку не прошла, манифест НЕ создан, подтверждать нечего.\n🛑 **Исключены 1:** #1 «Новая»: родитель «Призрачная задача» (6a99ghos…) не среди открытых задач (завершён/удалён/в корзине) — подзадача НЕ создаётся, иначе она легла бы отдельной задачей в корне\nНичего не изменено.',
    'restore_tasks/исполнение/папка_неизвестна': '🛑 Отказ — проект по id 6a98ghostpro… не найден среди живых проектов (или имена недоступны) — сверить личность проекта нельзя. Ничего не тронул.',
    'run_filter/страница_за_концом': "Filter 'Только срочное' — 3 task(s) (3 top-level), but offset=900 is past the end (valid offsets are 0-2).",
    'set_task_parent/исполнение/mismatch': '🛑 НЕ вложил — родитель по id это «Собрать отчёт», а НЕ «Совсем другая задача». Ничего не тронул.',
    'set_task_parent/исполнение/missing': '🛑 НЕ вложил — родитель «Призрачная задача» не среди открытых задач (завершён/удалён/неверный id) — вложение под мёртвого родителя осиротит задачи. Ничего не тронул.',
    'set_task_parent/план/mismatch': '🛑 План НЕ построен — родитель по id это «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'set_task_parent/план/missing': '🛑 План НЕ построен — родитель «Призрачная задача» не среди открытых задач (завершён/удалён/неверный id) — вложение под мёртвого родителя осиротит задачи. Ничего не изменено.',
    'triage_destination/mismatch': "('', '', 'проект назначения не подтверждён — Отказ — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не тронул.')",
    'unset_task_parent/исполнение/задача/mismatch': '🛑 НЕ отцепил — id это «Взять цифры у бухгалтерии», а НЕ «Совсем другая задача». Ничего не тронул.',
    'unset_task_parent/исполнение/задача/missing': '🛑 НЕ отцепил — «Призрачная задача» не среди открытых задач (завершена/удалена/неверный id). Ничего не тронул.',
    'unset_task_parent/исполнение/родитель/mismatch': '🛑 НЕ отцепил — родитель по id это «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не тронул.',
    'unset_task_parent/план/задача/mismatch': '🛑 План НЕ построен — id это «Взять цифры у бухгалтерии», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'unset_task_parent/план/задача/missing': '🛑 План НЕ построен — «Призрачная задача» не среди открытых задач (завершена/удалена/неверный id). Ничего не изменено.',
    'unset_task_parent/план/родитель/mismatch': '🛑 План НЕ построен — родитель по id это «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'update_project/исполнение/mismatch': '🛑 Отказ — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не тронул.',
    'update_project/план/mismatch': '🛑 План НЕ построен — project_id указывает на «Работа», а НЕ «Не тот проект» (защита от «не того проекта»). Ничего не изменено.',
    'update_task_comment/исполнение/mismatch': '🛑 НЕ изменил комментарий — id это «Собрать отчёт», а НЕ «Совсем другая задача». Ничего не тронул.',
    'update_task_comment/исполнение/missing': '🛑 НЕ изменил комментарий — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не тронул.',
    'update_task_comment/исполнение/trashed': '🛑 НЕ изменил комментарий — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не тронул.',
    'update_task_comment/план/mismatch': '🛑 План НЕ построен — id указывает на «Собрать отчёт», а НЕ «Совсем другая задача» (защита от «не той задачи»). Ничего не изменено.',
    'update_task_comment/план/missing': '🛑 План НЕ построен — id 6a99ghos… не найден ни среди открытых задач, ни среди завершённых/удалённых (неверный id или задача слишком старая для этих выборок). Ничего не изменено.',
    'update_task_comment/план/trashed': '🛑 План НЕ построен — задача «Старая затея» лежит В КОРЗИНЕ (удалена): операция над удалённым объектом не выполняется — верните её через restore_tasks, и тогда повторите. Ничего не изменено.',
    'update_tasks/исполнение/mismatch': '🛑 НЕ обновил «Совсем другая задача» — id указывает на «Собрать отчёт», а НЕ «Совсем другая задача»',
    'update_tasks/исполнение/missing': '🛑 НЕ обновил «Призрачная задача» — id 6a99ghos… не среди открытых задач (завершена/удалена/неверный id)',
    'update_tasks/исполнение/trashed': '🛑 НЕ обновил «Старая затея» — задача «Старая затея» лежит В КОРЗИНЕ (удалена) — верните её через restore_tasks, прежде чем работать с ней',
}

# Ответ обязан СОДЕРЖАТЬ замороженный фрагмент: карточка плана несёт
# случайный manifest_id, дословно сравнивать её нельзя.
PARTIAL_CASES = {
    # unset_task_parent: родитель не среди открытых — это НЕ отказ, а
    # предупреждение в карточке (обычный повод отцеплять именно от него).
    "unset_task_parent/план/родитель/missing": lambda: rs.call(
        "unset_task_parent", task_title=KID_TITLE, parent_task_title=GHOST_TITLE,
        task_id=rs.TASK_KID, parent_task_id=GHOST, project_id=rs.P_WORK),
    # add_task_comment на ЗАВЕРШЁННОЙ задаче: операция законна, карточка
    # обязана сказать о состоянии объекта вслух.
    "add_task_comment/план/completed": lambda: rs.call(
        "add_task_comment", task_title=DONE_TITLE, text="дописал вывод",
        project_id=rs.P_WORK, task_id=rs.TASK_COMPLETED),
    "duplicate_task/план/completed": lambda: rs.call(
        "duplicate_task", summary="как шаблон", task_id=rs.TASK_COMPLETED,
        task_title=DONE_TITLE),
}

EXPECTED_CONTAINS = {
    'add_task_comment/план/completed': ' ℹ️ задача ЗАВЕРШЕНА (не среди открытых) — операция над ней допустима, название сверено с живым состоянием.',
    'duplicate_task/план/completed': ' ℹ️ задача ЗАВЕРШЕНА (не среди открытых) — операция над ней допустима, название сверено с живым состоянием.',
    'unset_task_parent/план/родитель/missing': ' ⚠️ Родитель не среди открытых задач (возможно завершён/удалён) — имя не сверено; связь перепроверится при подтверждении.',
}


# Ветка «живое состояние прочитать не удалось»: на ПЛАНЕ это не отказ, а
# предупреждение в карточке (разовый сбой чтения не имеет права блокировать
# работу — исполнение перепроверит и остаётся последней линией). Три разных
# текста на разных площадках; свёртка обязана сохранить все три.
UNAVAILABLE_CASES = {
    "create_subtask/план/unavailable": lambda: rs.call(
        "create_subtask", parent_task_title=ROOT_TITLE, subtask_title="Пункт",
        parent_task_id=rs.TASK_ROOT, project_id=rs.P_WORK),
    "unset_task_parent/план/unavailable": lambda: rs.call(
        "unset_task_parent", task_title=KID_TITLE, parent_task_title=ROOT_TITLE,
        task_id=rs.TASK_KID, parent_task_id=rs.TASK_ROOT, project_id=rs.P_WORK),
    "add_task_comment/план/unavailable": lambda: rs.call(
        "add_task_comment", task_title=ROOT_TITLE, text="дописал вывод",
        project_id=rs.P_WORK, task_id=rs.TASK_ROOT),
    "abandon_task/план/unavailable": lambda: rs.call(
        "abandon_task", summary="Отказаться", task_id=rs.TASK_ROOT,
        task_title=ROOT_TITLE),
    "duplicate_task/план/unavailable": lambda: rs.call(
        "duplicate_task", summary="как шаблон", task_id=rs.TASK_ROOT,
        task_title=ROOT_TITLE),
    "set_task_parent/план/unavailable": lambda: rs.call(
        "set_task_parent", summary="Вложить",
        tasks=[{"taskId": rs.TASK_MID, "title": "Записаться к врачу"}],
        parent_task_id=rs.TASK_ROOT, project_id=rs.P_WORK,
        parent_task_title=ROOT_TITLE),
}

EXPECTED_UNAVAILABLE = {
    'abandon_task/план/unavailable': ' ⚠️ Задачу НЕ удалось сверить с живым состоянием (чтение не удалось) — сверка повторится при подтверждении, и расхождение остановит исполнение.',
    'add_task_comment/план/unavailable': ' ⚠️ Название задачи НЕ удалось сверить с живым состоянием (чтение не удалось) — сверка повторится при подтверждении, и расхождение остановит исполнение.',
    'create_subtask/план/unavailable': ' ⚠️ Название родительской задачи НЕ удалось сверить с живым состоянием (чтение не удалось) — сверка повторится при подтверждении, и расхождение остановит исполнение.',
    'duplicate_task/план/unavailable': ' ⚠️ Задачу НЕ удалось сверить с живым состоянием (чтение не удалось) — сверка повторится при подтверждении, и расхождение остановит исполнение.',
    'set_task_parent/план/unavailable': '⚠️ Название родительской задачи НЕ удалось сверить с живым состоянием (чтение не удалось) — сверка повторится при подтверждении, и расхождение остановит исполнение.',
    'unset_task_parent/план/unavailable': ' ⚠️ Название задачи НЕ удалось сверить с живым состоянием (чтение не удалось) — сверка повторится при подтверждении, и расхождение остановит исполнение. ⚠️ Имя родителя НЕ удалось сверить с живым состоянием (чтение не удалось) — сверка повторится при подтверждении.',
}


def blind(monkeypatch):
    """Живое состояние не читается вовсе — ровно то, что видит guard при
    сбое чтения снимка открытых задач."""
    wire(monkeypatch)
    monkeypatch.setattr(s, "_open_by_id", lambda **kw: None)


# ─────────────────────────── сами проверки ───────────────────────────

@pytest.mark.parametrize("case", sorted(CASES))
async def test_refusal_text_is_frozen(case, monkeypatch):
    """Ответ сервера совпадает с замороженным ДОСЛОВНО — до пробела."""
    wire(monkeypatch)
    got = await CASES[case]()
    assert got == EXPECTED[case], (
        f"\nкейс:      {case}\nбыло:      {EXPECTED[case]!r}\nстало:     {got!r}")


@pytest.mark.parametrize("case", sorted(PARTIAL_CASES))
async def test_warning_text_is_frozen(case, monkeypatch):
    """Карточка плана несёт замороженный фрагмент предупреждения."""
    wire(monkeypatch)
    got = await PARTIAL_CASES[case]()
    assert EXPECTED_CONTAINS[case] in got, (
        f"\nкейс:      {case}\nждали:     {EXPECTED_CONTAINS[case]!r}\n"
        f"получили:  {got!r}")


@pytest.mark.parametrize("case", sorted(UNAVAILABLE_CASES))
async def test_unverified_note_is_frozen(case, monkeypatch):
    """Текст «сверить не удалось» — тоже ответ сервера, и он тоже заморожен."""
    blind(monkeypatch)
    got = await UNAVAILABLE_CASES[case]()
    assert EXPECTED_UNAVAILABLE[case] in got, (
        f"\nкейс:      {case}\nждали:     {EXPECTED_UNAVAILABLE[case]!r}\n"
        f"получили:  {got!r}")


TRASHED_CASES = sorted(k for k in CASES if k.endswith("/trashed"))


@pytest.mark.parametrize("case", TRASHED_CASES)
async def test_trashed_task_is_refused_on_every_gated_tool(case, monkeypatch):
    """Политика корзины — у ВСЕГО класса, а не у одной команды.

    Отдельный тест поверх той же таблицы: если ветку `trashed` снять из
    общего помощника, обязаны покраснеть ВСЕ площадки сразу. Один упавший
    кейс вместо всех означает, что помощник внедрён не везде — то есть
    свёртка сделана наполовину."""
    wire(monkeypatch)
    got = await CASES[case]()
    assert "🛑" in got, f"{case}: операция над УДАЛЁННОЙ задачей не отказана:\n{got}"
    assert "корзин" in got.lower(), f"{case}: отказ не называет причину:\n{got}"
    assert "restore_tasks" in got, f"{case}: не сказано, как вернуть:\n{got}"


def test_every_scenario_has_a_frozen_answer():
    """Заморозка без строки — не заморозка. Кейс, для которого забыли снять
    ответ, обязан быть виден как красный тест, а не пропущен молча."""
    assert set(CASES) == set(EXPECTED), (
        f"без ожидаемой строки: {sorted(set(CASES) - set(EXPECTED))}; "
        f"лишние: {sorted(set(EXPECTED) - set(CASES))}")
    assert set(PARTIAL_CASES) == set(EXPECTED_CONTAINS), (
        f"без фрагмента: {sorted(set(PARTIAL_CASES) - set(EXPECTED_CONTAINS))}")
    assert set(UNAVAILABLE_CASES) == set(EXPECTED_UNAVAILABLE), (
        f"без фрагмента: "
        f"{sorted(set(UNAVAILABLE_CASES) - set(EXPECTED_UNAVAILABLE))}")
    assert len(TRASHED_CASES) >= 10, (
        f"политика корзины проверяется на {len(TRASHED_CASES)} площадках — "
        "меньше, чем их есть; откатная проверка перестанет что-либо ловить")
