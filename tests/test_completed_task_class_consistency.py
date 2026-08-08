"""Дефект №2 (живая приёмка 2026-08-07): ПЯТЬ инструментов одного класса —
операции над задачей, которая уже завершена, — вели себя по-разному на ОДНОМ
И ТОМ ЖЕ входе.

    add_task_comment      → план построен, «id не среди открытых задач
    attach_file_to_task      (возможно, завершена) — название НЕ проверено»
    update_task_comment      и операция идёт
    delete_task_comment
    duplicate_task        → 🛑 ОТКАЗ

`_guard_task` во всех пяти возвращает ОДНО И ТО ЖЕ — `missing`. Расхождение
не в guard'е (он предсказуем и единообразен), а в решении на стороне
инструментов: четверо смягчали, пятый отказывал. Строгость при этом была
ОБРАТНА риску — `duplicate_task` создаёт КОПИЮ и ничего не портит, но
отказывал; изменяющие комментарии и вложения — работали.

ПОЧЕМУ ИМЕННО ТАКОЙ ТЕСТ. Поиск по коду расхождения не показывает: во всех
пяти местах вызван один и тот же guard, и всё выглядит единообразно.
Увидеть его можно только подав ОДИН И ТОТ ЖЕ вход во ВСЕ ПЯТЬ и потребовав
согласованного ответа — именно так его и нашли живой приёмкой. Поэтому файл
устроен как таблица инструментов, а не как пять отдельных тестов.

ЧТО ЗАФИКСИРОВАНО КАК ПРАВИЛЬНОЕ ПОВЕДЕНИЕ:
  1. задача завершена, но существует → операция ДОПУСТИМА у всех пяти
     (приложить чек к выполненному делу, дописать вывод, продублировать как
     шаблон — законные сценарии), с честной пометкой про завершённость;
     название при этом СВЕРЕНО — источник берётся под объект операции, как
     `restore_tasks` сверяется с КОРЗИНОЙ, а не с открытым пулом;
  2. задача завершена, но название НЕ совпало → 🛑 у всех пяти (защита от
     «не той задачи» не ослабляется завершённостью);
  3. задачи нет НИ В ОДНОМ источнике → 🛑 у всех пяти (раньше четверо
     строили план операции над несуществующим объектом — тот же класс
     дефекта, что чинит tests/test_missing_object_refused.py).

Через стенд `tests/read_stand.py`: настоящие клиенты, подменён только
транспорт. `_guard_task` и родня НЕ подменяются — иначе тест проверял бы
мок, а не фикс (ровно эта подмена и прятала расхождение в прежних тестах).
"""
import re

import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

GHOST_TASK = "deadbeefdeadbeefdeadbeef"
DONE_TITLE = "Купить бумагу"          # rs.COMPLETED[0], status=2, в P_WORK
COMMENT_ID = rs.COMMENT_ID

# Один вход — пять инструментов. Меняется ТОЛЬКО имя тула и его обязательные
# аргументы; задача во всех пяти одна и та же.
CLASS_TOOLS = ["add_task_comment", "attach_file_to_task", "update_task_comment",
               "delete_task_comment", "duplicate_task"]


def _args(tool: str, task_id: str, task_title: str) -> dict:
    common = {"task_id": task_id, "task_title": task_title}
    if tool == "add_task_comment":
        return {**common, "text": "готово, чек приложен", "project_id": rs.P_WORK}
    if tool == "attach_file_to_task":
        return {**common, "project_id": rs.P_WORK,
                "url": "https://example.com/чек.pdf", "filename": "чек.pdf"}
    if tool == "update_task_comment":
        return {**common, "text": "правка", "project_id": rs.P_WORK,
                "comment_id": COMMENT_ID}
    if tool == "delete_task_comment":
        return {**common, "project_id": rs.P_WORK, "comment_id": COMMENT_ID}
    if tool == "duplicate_task":
        return {**common, "summary": "как шаблон на следующий месяц"}
    raise AssertionError(tool)


@pytest.fixture(autouse=True)
def isolate_manifests():
    before, tombs = dict(s._MANIFESTS), dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _wire(monkeypatch):
    """Стенд, в котором завершённая задача РЕАЛЬНО существует: она в ленте
    завершённых v2 и читается точечно официальным API — но её нет в снапшоте
    открытых задач. Ровно положение дел из живой приёмки."""
    return rs.wire(monkeypatch, v1_tasks=list(rs.TASKS) + list(rs.COMPLETED))


async def _plan(tool: str, task_id: str, task_title: str) -> str:
    return await rs.call(tool, **_args(tool, task_id, task_title))


# ===========================================================================
# 1. Завершённая задача: пять ответов обязаны быть СОГЛАСОВАНЫ
# ===========================================================================

async def test_completed_task_is_allowed_by_the_whole_class(monkeypatch):
    """Главный тест файла. Один вход — пять инструментов; расхождение
    «четверо работают, пятый отказывает» обязано исчезнуть."""
    _wire(monkeypatch)

    answers = {t: await _plan(t, rs.TASK_COMPLETED, DONE_TITLE)
               for t in CLASS_TOOLS}

    refused = {t: a for t, a in answers.items() if "🛑" in a}
    assert not refused, (
        "класс ведёт себя несогласованно — часть инструментов отказывает на "
        f"том же входе, что остальные принимают:\n{refused}")
    for tool, answer in answers.items():
        assert "manifest_id" in answer, f"{tool} не построил план:\n{answer}"


async def test_completed_state_is_said_out_loud_by_every_tool(monkeypatch):
    """И согласованность формулировки: подтверждающий обязан узнать, что
    задача завершена, у ЛЮБОГО из пяти, а не у половины."""
    _wire(monkeypatch)

    for tool in CLASS_TOOLS:
        answer = await _plan(tool, rs.TASK_COMPLETED, DONE_TITLE)
        assert "завершена" in answer.lower(), (
            f"{tool} молчит о том, что задача завершена:\n{answer}")


async def test_completed_task_name_is_actually_verified(monkeypatch):
    """Название завершённой задачи ИЗВЕСТНО (официальный API его отдаёт), и
    прежняя пометка «название НЕ проверено» была неправдой наполовину.
    Проверка обязана состояться — а значит и ловить подлог."""
    _wire(monkeypatch)

    for tool in CLASS_TOOLS:
        answer = await _plan(tool, rs.TASK_COMPLETED, DONE_TITLE)
        assert "НЕ проверено" not in answer, (
            f"{tool} врёт, что название не проверено, хотя источник его "
            f"знает:\n{answer}")


async def test_wrong_title_on_completed_task_refused_by_every_tool(monkeypatch):
    """Завершённость НЕ ослабляет защиту от «не той задачи»: раз название
    теперь сверяется, подлог обязан ловиться у всех пяти."""
    _wire(monkeypatch)

    for tool in CLASS_TOOLS:
        answer = await _plan(tool, rs.TASK_COMPLETED, "Совсем другая задача")
        assert "🛑" in answer, f"{tool} принял ЧУЖОЕ название:\n{answer}"
        assert "manifest_id" not in answer, (
            f"{tool} всё же построил план на подлоге:\n{answer}")


# ===========================================================================
# 2. Вторая ось согласованности: объекта нет НИ В ОДНОМ источнике
# ===========================================================================

async def test_nonexistent_task_refused_by_every_tool(monkeypatch):
    """Раньше четверо строили план операции над несуществующей задачей (guard
    отдавал `missing`, они смягчали его до ⚠️). Теперь `missing` означает
    «не нашли ни среди открытых, ни среди завершённых/удалённых» — и это
    отказ у всех пяти, единообразно."""
    _wire(monkeypatch)

    for tool in CLASS_TOOLS:
        answer = await _plan(tool, GHOST_TASK, "Призрак")
        assert "🛑" in answer, (
            f"{tool} строит план операции над несуществующей задачей:\n{answer}")
        assert "manifest_id" not in answer, answer


# ===========================================================================
# 3. Не только план: операция реально ДОХОДИТ до TickTick
# ===========================================================================

async def test_completed_task_operation_actually_executes(monkeypatch):
    """План — половина дела: guard стоит и на ИСПОЛНЕНИИ (call #2), и если бы
    там осталась прежняя трактовка, комментарий к завершённой задаче не ушёл
    бы никуда. Здесь проверяется весь цикл до записи в TickTick.

    Подменяется РОВНО ТРАНСПОРТ (как во всём стенде): запись комментария
    добавляется в тот же journal вызовов, а любой незнакомый путь остаётся
    ошибкой, как у живого API."""
    v2, _v1, transport = _wire(monkeypatch)
    base_request = v2._request
    written = []

    def request(method, path, **kwargs):
        if method == "POST" and path.endswith("/comment"):
            written.append((path, (kwargs.get("json") or {}).get("title")))
            return dict(kwargs.get("json") or {})
        return base_request(method, path, **kwargs)

    v2._request = request

    plan = await rs.call("add_task_comment",
                         **_args("add_task_comment", rs.TASK_COMPLETED, DONE_TITLE))
    mid = re.search(r'manifest_id="([0-9a-f]+)"', plan)
    assert mid, plan
    result = await rs.call("add_task_comment",
                           **_args("add_task_comment", rs.TASK_COMPLETED, DONE_TITLE),
                           manifest_id=mid.group(1), user_reply="да")

    assert written, f"комментарий к завершённой задаче не ушёл в TickTick:\n{result}"
    assert written[0][0] == f"/project/{rs.P_WORK}/task/{rs.TASK_COMPLETED}/comment"
    assert "🛑" not in result, result
    assert "завершена" in result.lower(), (
        f"исполнение молчит о том, что задача завершена:\n{result}")
    del transport


# ===========================================================================
# 4. Счастливый путь не тронут
# ===========================================================================

async def test_open_task_unchanged_for_every_tool(monkeypatch):
    """Контроль: обычная открытая задача — план у всех пяти, без всякой
    пометки про завершённость."""
    _wire(monkeypatch)

    for tool in CLASS_TOOLS:
        answer = await _plan(tool, rs.TASK_ROOT, "Собрать отчёт")
        assert "🛑" not in answer, f"{tool}:\n{answer}"
        assert "manifest_id" in answer, f"{tool}:\n{answer}"
        assert "завершена" not in answer.lower(), (
            f"{tool} объявил открытую задачу завершённой:\n{answer}")
