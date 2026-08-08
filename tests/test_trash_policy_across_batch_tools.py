"""ПОЛИТИКА КОРЗИНЫ ОДИНАКОВА У ВСЕГО КЛАССА, А НЕ У ПЯТИ ИЗБРАННЫХ ТУЛОВ.

Круг 7 ввёл политику: операция над задачей в КОРЗИНЕ не выполняется, отказ
называет корзину и подсказывает `restore_tasks`. Живая приёмка подтвердила её
на пяти одиночных тулах класса (комментарии/вложение/дублирование) — 5 из 5
отказали побуквенно одинаковым текстом.

Дефект (живой прогон по кнопкам, 2026-08-07). `complete_tasks`, `move_tasks` и
`set_task_tags` этой политики не знали: ТОТ ЖЕ корзинный вход давал у них
обычный план — карточка «согласны?» на удалённую задачу, без ⛔, без слова
«корзина», в одном экране рядом с честной карточкой `update_tasks`.

ФАКТ, УСТАНОВЛЕННЫЙ ПО КОДУ (а не по поведению): guard на этих путях не
«промахивался» — сверки с живым состоянием на фазе ПЛАНА у них не было ВОВСЕ.
`complete_tasks`/`move_tasks`/`set_task_tags` строили превью из одних лишь
названий (`_plan_task_titles` + `_gate_batch`) и ничего не спрашивали о
состоянии задач. Исполнители при этом строку молча пропускали («не среди
открытых»), то есть данные не портились — но согласие бралось на операцию,
которой не будет, и человек не узнавал, что задача удалена.

СПЛОШНОЙ ПРОГОН ОДНОГО КОРЗИННОГО ВХОДА ПО ВСЕМ ТУЛАМ (тот же приём, что
дважды сработал раньше) добавил к трём найденным живьём четвёртый:
`create_attachment_upload_url` выдавал на корзинную задачу РАБОЧУЮ ссылку —
полномочие на запись файла в удалённый объект, ровно то, в чём отказывает
`attach_file_to_task`. Это был обход политики, а не её отсутствие в одном
месте, поэтому он закрыт здесь же.

РЕШЕНИЕ ДЛЯ БАТЧЕЙ (шире, чем «ещё один guard»): у списочного тула отказ
целиком неверен — одна мёртвая строка не должна отбирать исполнение у
остальных. Поэтому три исхода: часть строк обречена → ⛔ на них и ⚠️-сводка;
исполнимых строк не осталось → плана нет вовсе (это и есть одиночный корзинный
вход из живого прогона — он получает тот же отказ, что класс из пяти); всё
исполнимо → план как был.

Всё через tests/read_stand.py: настоящие клиенты, подменён только транспорт,
тулы зовутся ПО ИМЕНИ через реестр — проверяется тот самый текст, который
увидит человек.
"""
import re

import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

TRASH_TITLE = "Старая затея"          # rs.TRASHED[0] — НЕ последняя в ленте

# Батч-тулы класса. Значение — как позвать тул одним и тем же набором строк.
BATCH_TOOLS = {
    "update_tasks": lambda rows: {"summary": "Меняю приоритет",
                                  "tasks": [dict(r, priority=1) for r in rows]},
    "complete_tasks": lambda rows: {"summary": "Закрываю", "tasks": rows},
    "move_tasks": lambda rows: {"summary": "Переношу", "tasks": rows,
                                "to_project_id": rs.P_WORK},
    "set_task_tags": lambda rows: {"summary": "Ставлю тег",
                                   "tasks": [dict(r, tags=["тег01"]) for r in rows]},
    # Родитель здесь ЖИВОЙ намеренно: этот тул всегда сверял родителя и
    # никогда — сами вкладываемые задачи, поэтому корзинная задача в роли
    # РЕБЁНКА проходила как обычная (нашёл сплошной прогон, живой не поймал).
    "set_task_parent": lambda rows: {"summary": "Вкладываю", "tasks": rows,
                                     "parent_task_id": rs.TASK_HIGH_2,
                                     "project_id": rs.P_WORK,
                                     "parent_task_title": "Продлить домен"},
}

TRASHED_ROW = {"taskId": rs.TASK_TRASHED, "projectId": rs.P_HOME,
               "title": TRASH_TITLE}
# Живые строки для смешанного батча. Корзинная встанет В СЕРЕДИНУ (см. ниже).
LIVE_ROWS = [
    {"taskId": rs.TASK_ROOT, "projectId": rs.P_WORK, "title": "Собрать отчёт"},
    {"taskId": rs.TASK_MID, "projectId": rs.P_HOME, "title": "Записаться к врачу"},
    {"taskId": rs.TASK_TAGGED, "projectId": rs.P_HOME, "title": "Полить цветы"},
    {"taskId": rs.TASK_HIGH, "projectId": rs.P_HOME, "title": "Оплатить страховку"},
]
# ПОЗИЦИЯ ОБРЕЧЁННОЙ СТРОКИ — ЧАСТЬ ПРОВЕРКИ. Под списком печатается сводка
# «Не применится строк: 1 из 5», и у ПОСЛЕДНЕГО пункта она отбирает роль
# собственной пометки при любом наивном разборе: тест зеленел бы с полностью
# удалённой построчной пометкой. Поэтому корзинная строка стоит в середине.
MIXED = LIVE_ROWS[:2] + [TRASHED_ROW] + LIVE_ROWS[2:]


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before, tombs = dict(s._MANIFESTS), dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _wire(monkeypatch):
    """Живое положение дел: официальный API знает корзинную задачу (он не
    носит флага удаления), а снимок открытых — нет."""
    return rs.wire(monkeypatch,
                   v1_tasks=list(rs.TASKS) + list(rs.COMPLETED) + list(rs.TRASHED))


def _rows(preview: str) -> dict:
    """{номер пункта: его текст со строками-продолжения}. Пустая строка
    заканчивает пункт — иначе сводка из-под списка достаётся последнему
    пункту и подменяет собой его пометку."""
    out, current = {}, None
    for line in preview.splitlines():
        m = re.match(r"^(\d+)\.\s(.*)$", line)
        if m:
            current = int(m.group(1))
            out[current] = m.group(2)
        elif not line.strip():
            current = None
        elif current is not None:
            out[current] += "\n" + line
    return out


# ===========================================================================
# 1. Один корзинный вход — отказ, и он звучит одинаково у всех
# ===========================================================================

@pytest.mark.parametrize("tool", list(BATCH_TOOLS))
async def test_a_lone_trashed_row_gets_no_plan(tool, monkeypatch):
    """Ровно то, что видел живой прогон: карточка построена на задаче из
    корзины. Подтверждать в ней нечего — исполнитель отвергнет единственную
    строку, поэтому плана быть не должно."""
    _wire(monkeypatch)

    answer = await rs.call(tool, **BATCH_TOOLS[tool]([TRASHED_ROW]))

    assert "🛑" in answer, f"{tool} строит план на УДАЛЁННУЮ задачу:\n{answer}"
    assert "manifest_id" not in answer, answer


@pytest.mark.parametrize("tool", list(BATCH_TOOLS))
async def test_the_refusal_names_the_trash_and_the_way_back(tool, monkeypatch):
    """Отказ обязан сказать, ЧТО с задачей и КАК её вернуть. Без этого «не
    выполнено» читается как сбой сервера, а не как состояние объекта."""
    _wire(monkeypatch)

    answer = await rs.call(tool, **BATCH_TOOLS[tool]([TRASHED_ROW]))

    assert "корзин" in answer.lower(), (
        f"{tool} отказывает, но не произносит слово «корзина»:\n{answer}")
    assert "restore_tasks" in answer, (
        f"{tool} не подсказывает, как вернуть задачу:\n{answer}")


# ===========================================================================
# 2. Смешанный батч: помечается обречённая строка, а не весь план
# ===========================================================================

@pytest.mark.parametrize("tool", list(BATCH_TOOLS))
async def test_mixed_batch_marks_only_the_trashed_row(tool, monkeypatch):
    _wire(monkeypatch)

    preview = await rs.call(tool, **BATCH_TOOLS[tool](MIXED))

    rows = _rows(preview)
    assert len(rows) == len(MIXED), f"{tool}: план не перечислил всё:\n{preview}"
    dead = [t for t in rows.values() if TRASH_TITLE in t]
    assert dead and "⛔" in dead[0], (
        f"{tool}: корзинная строка неотличима от живых:\n{preview}")
    assert "корзин" in dead[0].lower(), (
        f"{tool}: пометка не называет причину своими словами:\n{dead[0]}")
    for text in rows.values():
        if TRASH_TITLE in text:
            continue
        assert "⛔" not in text, (
            f"{tool}: живая строка помечена как обречённая:\n{text}")


@pytest.mark.parametrize("tool", list(BATCH_TOOLS))
async def test_mixed_batch_is_still_confirmable(tool, monkeypatch):
    """Пометка — не отказ: четыре живые строки исполнимы, и план обязан
    остаться подтверждаемым, иначе одна удалённая задача блокирует работу."""
    _wire(monkeypatch)

    preview = await rs.call(tool, **BATCH_TOOLS[tool](MIXED))

    assert re.search(r"Манифест `([0-9a-f]+)`", preview), (
        f"{tool}: план не построен:\n{preview}")
    assert "не применится строк: 1 из 5" in preview.lower(), (
        f"{tool}: сводка не сказала, сколько строк обречено:\n{preview}")


@pytest.mark.parametrize("tool", list(BATCH_TOOLS))
async def test_a_healthy_batch_is_untouched(tool, monkeypatch):
    """Контроль: пометки не появляются там, где всё живое. Пометить всё
    подряд — то же самое, что не помечать ничего."""
    _wire(monkeypatch)

    preview = await rs.call(tool, **BATCH_TOOLS[tool](LIVE_ROWS))

    assert "⛔" not in preview, f"{tool}: помечены живые строки:\n{preview}"
    assert re.search(r"Манифест `([0-9a-f]+)`", preview), preview


# ===========================================================================
# 3. Ссылка на загрузку — тот же объект, та же политика
# ===========================================================================

async def test_upload_link_is_refused_for_a_trashed_task(monkeypatch):
    """`attach_file_to_task` отказывается класть файл в удалённую задачу — а
    ссылка на загрузку в ту же задачу выдавалась как ни в чём не бывало.
    Файл по ней уехал бы в корзину и исчез вместе с ней при очистке."""
    _wire(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.invalid")

    answer = await rs.call("create_attachment_upload_url",
                           task_id=rs.TASK_TRASHED, project_id=rs.P_HOME,
                           filename="чек.pdf", size_bytes=10)

    assert "🛑" in answer and "/ul/" not in answer, (
        f"выдана ссылка-полномочие на запись в УДАЛЁННУЮ задачу:\n{answer}")
    assert "корзин" in answer.lower() and "restore_tasks" in answer, answer


async def test_upload_link_executor_refuses_too(monkeypatch):
    """Вторая линия: по нажатию кнопки в Telegram исполнитель зовётся
    напрямую, минуя код тула, — и манифест мог быть построен ДО удаления."""
    _wire(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.invalid")

    out = await s._create_attachment_upload_url_impl(
        rs.TASK_TRASHED, rs.P_HOME, "чек.pdf")

    assert "🛑" in out and "/ul/" not in out, out
    assert "корзин" in out.lower(), out


async def test_upload_link_still_works_for_a_live_task(monkeypatch):
    """Контроль политики: отказ введён ДЛЯ КОРЗИНЫ, а не для всего подряд."""
    _wire(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.invalid")

    out = await s._create_attachment_upload_url_impl(
        rs.TASK_ROOT, rs.P_WORK, "чек.pdf")

    assert "/ul/" in out, f"ссылка на живую задачу не выдана:\n{out}"
