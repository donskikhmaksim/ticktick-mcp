"""ЗАДАЧА В КОРЗИНЕ СЧИТАЕТСЯ ЖИВОЙ (живая приёмка 2026-08-07).

Корень. У удалённой задачи TickTick НЕ меняет поле `status` — оно остаётся
`0`, ровно как у открытой. Официальный Open API отдаёт такую задачу точечным
чтением как ни в чём не бывало (это же свойство уже задокументировано в
`_trash_state`: «carries no deletion flag anywhere»). А guard-фолбэк
`_official_task_snapshot` отсеивает только `status != 0` — то есть корзинная
задача проходит через него КАК ОТКРЫТАЯ. Дальше живут все последствия:

  * пять инструментов класса (комментарии/вложение/дублирование) строят план
    на удалённый объект как на живой и ни один не произносит слово «корзина»;
  * `update_tasks` помечает ⛔ «не будет применено» завершённую и подменённую
    строку, а корзинную — нет, и сводка врёт («2 из 4»).

Живьём это наблюдалось тремя прогонами с разбросом 50 минут при задаче,
пролежавшей в корзине почти сутки, — то есть это не окно рассогласования
кэшей, а систематическая слепота.

ПОЛИТИКА, ЗАФИКСИРОВАННАЯ ЗДЕСЬ (решена ОТДЕЛЬНО от завершённых задач):
операция над УДАЛЁННЫМ объектом — отказ у всего класса, в отличие от операции
над ЗАВЕРШЁННЫМ, которая законна. Причина в том, что эти два состояния
означают разное. Завершённая задача — нормальный конечный объект: приложить
к ней чек, дописать вывод, продублировать как шаблон — обычные сценарии, и
результат такой операции виден владельцу там же, где сама задача. Корзина —
это заявленное «я это убрал»: комментарий, дописанный в корзину, не увидит
никто, файл, приложенный туда, уедет вместе с задачей при очистке корзины
(TickTick чистит её сам), а «дубликат удалённого» почти всегда означает, что
человек хотел restore_tasks, а не копию. Поэтому здесь отказ с подсказкой
restore_tasks — сохранить объект, а не молча работать поверх удалённого.

Сервер УМЕЕТ отличать корзину и без новых источников: `get_task` печатает
«Status: IN TRASH (deleted)», а `find_task_any_state` возвращает не только
задачу, но и МЕСТО находки — «trash» уже известен, просто выбрасывался.

Через стенд tests/read_stand.py: подменён только транспорт. Официальный
транспорт при этом ЗНАЕТ корзинную задачу (`v1_tasks` включает TRASHED) —
это не поблажка тесту, а воспроизведение живого API, ради которого и
существует `_trash_state`.
"""
import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

TRASH_TITLE = "Старая затея"          # rs.TRASHED[0] — НЕ последняя в ленте
DONE_TITLE = "Купить бумагу"          # rs.COMPLETED[0]
OPEN_TITLE = "Собрать отчёт"          # rs.TASK_ROOT

CLASS_TOOLS = ["add_task_comment", "attach_file_to_task", "update_task_comment",
               "delete_task_comment", "duplicate_task"]


def _args(tool: str, task_id: str, task_title: str) -> dict:
    common = {"task_id": task_id, "task_title": task_title}
    if tool == "add_task_comment":
        return {**common, "text": "дописал вывод", "project_id": rs.P_HOME}
    if tool == "attach_file_to_task":
        return {**common, "project_id": rs.P_HOME,
                "content_base64": "0LDQsdCy", "filename": "чек.pdf"}
    if tool == "update_task_comment":
        return {**common, "text": "правка", "project_id": rs.P_HOME,
                "comment_id": rs.COMMENT_ID}
    if tool == "delete_task_comment":
        return {**common, "project_id": rs.P_HOME, "comment_id": rs.COMMENT_ID}
    if tool == "duplicate_task":
        return {**common, "summary": "как шаблон"}
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
    """Живое положение дел: официальный API знает и завершённую, и корзинную
    задачу (он не носит флага удаления), а снимок открытых задач — ни ту, ни
    другую."""
    return rs.wire(monkeypatch,
                   v1_tasks=list(rs.TASKS) + list(rs.COMPLETED) + list(rs.TRASHED))


# ===========================================================================
# 1. Guard: корзина — это НЕ «открыта»
# ===========================================================================

def test_guard_does_not_call_a_trashed_task_open(monkeypatch):
    """Корень дефекта в одной строке: `status` корзинной задачи равен 0, и
    фильтр «status != 0» её пропускает."""
    _wire(monkeypatch)

    g = s._guard_task(rs.TASK_TRASHED, TRASH_TITLE, rs.P_HOME)

    assert g.status != "ok", (
        "guard объявил УДАЛЁННУЮ задачу пригодной для мутации — дальше по "
        "коду её ничто не отличит от открытой")
    assert "корзин" in (g.message or "").lower(), (
        f"guard не называет причину своими словами: {g.message!r}")


def test_guard_of_the_class_separates_trash_from_completed(monkeypatch):
    """Два состояния — два ответа. Слить их в один статус значит либо
    запретить законную работу с завершённой задачей, либо разрешить работу
    поверх удалённой."""
    _wire(monkeypatch)

    trashed = s._guard_task_incl_completed(rs.TASK_TRASHED, TRASH_TITLE, rs.P_HOME)
    completed = s._guard_task_incl_completed(rs.TASK_COMPLETED, DONE_TITLE, rs.P_WORK)

    assert completed.status == "completed", completed.status
    assert trashed.status != completed.status, (
        "корзинная и завершённая задачи неразличимы для класса — политика "
        "для них не может быть разной")
    assert "корзин" in (trashed.message or "").lower(), trashed.message


def test_wrong_title_on_a_trashed_task_is_still_a_mismatch(monkeypatch):
    """Защита от «не той задачи» не растворяется в новом статусе: подлог
    названия остаётся подлогом, что бы ни было с состоянием объекта."""
    _wire(monkeypatch)

    g = s._guard_task_incl_completed(rs.TASK_TRASHED, "Совсем другая задача",
                                     rs.P_HOME)

    assert g.status == "mismatch", g.status


# ===========================================================================
# 2. Класс из пяти: отказ и слово «корзина» — у всех
# ===========================================================================

@pytest.mark.parametrize("tool", CLASS_TOOLS)
async def test_plan_on_a_trashed_task_is_refused(tool, monkeypatch):
    _wire(monkeypatch)

    answer = await rs.call_direct(tool, **_args(tool, rs.TASK_TRASHED, TRASH_TITLE))

    assert "🛑" in answer, f"{tool} строит план на УДАЛЁННУЮ задачу:\n{answer}"
    assert "manifest_id" not in answer, answer
    assert "корзин" in answer.lower(), (
        f"{tool} отказывает, но не произносит слово «корзина» — человек не "
        f"поймёт, что произошло с задачей:\n{answer}")
    assert "restore_tasks" in answer, (
        f"{tool} не подсказывает, как вернуть задачу:\n{answer}")


@pytest.mark.parametrize("tool", CLASS_TOOLS)
async def test_executor_on_a_trashed_task_is_refused(tool, monkeypatch):
    """Вторая линия: даже если план как-то построен (старый манифест, гонка,
    задача удалена ПОСЛЕ показа карточки) — исполнитель обязан отказать сам."""
    _wire(monkeypatch)
    impl = getattr(s, f"_{tool}_impl")

    if tool == "duplicate_task":
        out = await impl("как шаблон", rs.TASK_TRASHED, TRASH_TITLE)
    elif tool == "add_task_comment":
        out = await impl(TRASH_TITLE, "текст", rs.P_HOME, rs.TASK_TRASHED)
    elif tool == "attach_file_to_task":
        out = await impl(TRASH_TITLE, rs.TASK_TRASHED, rs.P_HOME,
                         content_base64="0LDQsdCy", filename="чек.pdf")
    elif tool == "update_task_comment":
        out = await impl(TRASH_TITLE, "правка", rs.P_HOME, rs.TASK_TRASHED,
                         rs.COMMENT_ID)
    else:
        out = await impl(TRASH_TITLE, rs.P_HOME, rs.TASK_TRASHED, rs.COMMENT_ID)

    assert "🛑" in out, f"{tool} выполнил операцию над удалённой задачей:\n{out}"
    assert "корзин" in out.lower(), out


async def test_completed_task_still_works_for_the_whole_class(monkeypatch):
    """Контроль политики: отказ введён ДЛЯ КОРЗИНЫ, а не для всего, что не
    открыто. Завершённая задача обязана остаться рабочей — иначе это не новая
    политика, а откат прошлого решения."""
    _wire(monkeypatch)

    for tool in CLASS_TOOLS:
        args = _args(tool, rs.TASK_COMPLETED, DONE_TITLE)
        args = {**args, "project_id": rs.P_WORK} if "project_id" in args else args
        answer = await rs.call_direct(tool, **args)
        assert "🛑" not in answer, f"{tool} на завершённой задаче:\n{answer}"
        assert "manifest_id" in answer, f"{tool}:\n{answer}"


# ===========================================================================
# 3. Батч: строка, которая заведомо не исполнится, обязана быть помечена
# ===========================================================================

def test_update_plan_marks_the_trashed_row_too(monkeypatch):
    """Живой прогон: батч из четырёх строк — открытая, завершённая,
    подменённая и корзинная. Первые три обработаны верно, корзинная прошла
    как живая, и сводка обещала применить больше, чем могла."""
    _wire(monkeypatch)

    check = s._check_plan_rows([
        {"taskId": rs.TASK_ROOT, "title": OPEN_TITLE},
        {"taskId": rs.TASK_TRASHED, "title": TRASH_TITLE},
        {"taskId": rs.TASK_COMPLETED, "title": DONE_TITLE},
        {"taskId": rs.TASK_KID, "title": "Совсем не та задача"},
    ])

    assert check.checked, "сверка не отработала — проверять нечего"
    marks = check.marks
    assert rs.TASK_TRASHED in marks, (
        "корзинная строка обещана к применению наравне с живой — согласие "
        f"берётся на то, чего не будет: {marks}")
    assert "корзин" in marks[rs.TASK_TRASHED].lower(), marks[rs.TASK_TRASHED]
    assert rs.TASK_ROOT not in marks, marks


# ===========================================================================
# 4. Цена: счастливый путь не платит за новую проверку
# ===========================================================================

def test_open_task_costs_no_extra_requests(monkeypatch):
    """Проверка корзины живёт на УЖЕ деградированном пути (задачи нет в
    снимке открытых). Открытая задача не имеет права платить за неё ни одним
    лишним запросом."""
    _v2, _v1, transport = _wire(monkeypatch)
    s._open_by_id(fresh=True)
    before = len(transport.calls)

    g = s._guard_task(rs.TASK_ROOT, OPEN_TITLE, rs.P_WORK, fresh=False)

    assert g.status == "ok"
    trash_calls = [c for c in transport.calls[before:] if "trash" in str(c[1])]
    assert not trash_calls, f"открытая задача полезла в корзину: {trash_calls}"


def test_trash_unreadable_does_not_block_open_work(monkeypatch):
    """Обратная сторона: если ленту корзины прочитать НЕ удалось, новая
    проверка не имеет права превращаться в отказ — иначе разовый сбой чтения
    корзины останавливает работу над задачами, которые в ней не лежат.
    Поведение при нечитаемой корзине остаётся ровно тем, каким было до этой
    правки (не хуже и не лучше), и это сказано вслух здесь, а не только в
    комментарии."""
    v2, _v1, _tr = _wire(monkeypatch)

    def boom(limit=50):
        raise RuntimeError("trash read failed")

    monkeypatch.setattr(v2, "get_trash", boom)
    monkeypatch.setattr(v2, "get_completed_tasks",
                        lambda **kw: list(rs.COMPLETED))
    v2._closed_lookup_cache = {}

    g = s._guard_task(rs.TASK_TRASHED, TRASH_TITLE, rs.P_HOME)

    assert g.status == "ok", (
        "сбой чтения корзины превратился в отказ по задаче — цена ошибки "
        f"чтения не должна быть такой: {g.status} / {g.message!r}")
