"""СБОЙ СВЕРКИ ПЛАНА НЕ ИМЕЕТ ПРАВА ВЫГЛЯДЕТЬ КАК ЧИСТЫЙ ПЛАН (круг 8).

Дефект (живая приёмка 2026-08-07, по кнопкам). Сборка ⛔-пометок «не
применится» была обёрнута в try/except, и при сбое сверки с живым состоянием
возвращался ПУСТОЙ словарь. Пустой словарь означал одновременно две
несовместимые вещи — «сверка прошла, помечать нечего» и «сверить не удалось», —
и человек у кнопки видел их одинаково: план как полностью исполнимый.
Предупреждение уходило только в серверный лог, которого он не читает.

Воспроизведение было недетерминированным и доказано семью контрольными
прогонами: ОДИН И ТОТ ЖЕ батч из 4 задач в первый раз пришёл БЕЗ ⛔, при
повторе — С ⛔ и сводкой «Не применится строк: 1 из 4». Количество, проекты и
позиция строки как причина исключены. Сверка платит до двух запросов на каждую
строку вне снимка открытых, поэтому её сбой на батче — обычное дело, а не
экзотика.

Это ровно тот класс, ради которого шли семь кругов: сомнение проверки
неотличимо от факта. В вердикте ПОСЛЕ операции его вычистили в круге 7 (⚠️ —
сомнение проверки, ℹ️ — факт о состоянии); в превью ДО операции он остался и
там опаснее — человек жмёт «да», думая, что применится всё.

ЧТО ИМЕННО ЗАКРЫТО: при сбое сверки план строится (невозможность ПРОВЕРИТЬ ≠
невозможность ВЫПОЛНИТЬ, и разовый сбой чтения не должен превращаться в отказ
обслуживания), но НЕСЁТ ⚠️-строку о том, что исполнимость проверить не
удалось. Настоящая защита при этом стоит там же, где стояла, — исполнитель
перечитывает состояние сам и на недоступном отказывает по каждой строке.

КАК ПОДАЁТСЯ СБОЙ. Ломается НАСТОЯЩИЙ ИСТОЧНИК — транспорт v2 стенда: запрос
`/batch/check/0` (снимок открытых задач) кидает, как кинул бы живой HTTP по
таймауту. Ни `_check_plan_rows`, ни `_plan_live_check`, ни `_gate_batch` не
подменяются: мок на них проверял бы мок, а не починку.
"""
import re

import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

# Батч из живых задач: сбой сверки — единственная причина, по которой в плане
# может появиться сомнение. Позиции здесь не играют роли (проверяется текст
# ПОД списком), но строки всё равно живые, чтобы ⛔ было неоткуда взяться.
LIVE_ROWS = [
    {"taskId": rs.TASK_ROOT, "projectId": rs.P_WORK, "title": "Собрать отчёт"},
    {"taskId": rs.TASK_MID, "projectId": rs.P_HOME, "title": "Записаться к врачу"},
    {"taskId": rs.TASK_TAGGED, "projectId": rs.P_HOME, "title": "Полить цветы"},
]

# Как каждый из четырёх батч-мутаторов зовётся с этим же набором строк.
TOOLS = {
    "update_tasks": lambda rows: {"summary": "Меняю приоритет",
                                  "tasks": [dict(r, priority=1) for r in rows]},
    "complete_tasks": lambda rows: {"summary": "Закрываю", "tasks": rows},
    "move_tasks": lambda rows: {"summary": "Переношу", "tasks": rows,
                                "to_project_id": rs.P_WORK},
    "set_task_tags": lambda rows: {"summary": "Ставлю тег",
                                   "tasks": [dict(r, tags=["тег01"]) for r in rows]},
    "set_task_parent": lambda rows: {"summary": "Вкладываю", "tasks": rows,
                                     "parent_task_id": rs.TASK_HIGH_2,
                                     "project_id": rs.P_WORK,
                                     "parent_task_title": "Продлить домен"},
}


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


def _break_live_state(monkeypatch):
    """Живой сценарий сбоя: снимок открытых задач не читается.

    Ломается ТРАНСПОРТ (HTTP-слой), а не код сервера: `/batch/check/0` кидает
    так же, как кинул бы настоящий клиент при таймауте, и всё, что выше
    транспорта, отрабатывает боевое."""
    v2, _v1, transport = rs.wire(monkeypatch)
    real = transport.request

    def flaky(method, path, **kwargs):
        if path == "/batch/check/0":
            raise RuntimeError("check/0 timed out")
        return real(method, path, **kwargs)

    monkeypatch.setattr(transport, "request", flaky)
    monkeypatch.setattr(v2, "_request", flaky)
    v2._state_cache = None              # кэш снимка сброшен: пойдёт в транспорт
    v2._state_ts = 0.0
    return v2, transport


def _doubt_lines(preview: str) -> list:
    """Строки плана, которые говорят человеку «проверить не удалось».

    Ищутся по СМЫСЛУ, а не по дословному тексту: значок сомнения ⚠️ (канал
    круга 7 — сомнение проверки, в отличие от ℹ️ «факт») плюс слова про
    неудавшуюся проверку. Дословная формулировка может меняться, требование —
    нет."""
    out = []
    for line in preview.splitlines():
        low = line.lower()
        if "⚠️" in line and ("провер" in low or "сверить" in low) \
                and ("не удал" in low or "не получилось" in low):
            out.append(line)
    return out


# Чужие сомнения, которые тот же сбой чтения печатает В ТОТ ЖЕ план и по
# которым тест «сбой сверки строк виден» проходил бы, ничего не проверяя.
# Оба найдены мутационной проверкой (тест зеленел с полностью удалённым
# фиксом): у `set_task_tags` не читается список тегов, у `set_task_parent` не
# сверяется РОДИТЕЛЬ — и каждый печатает своё ⚠️.
_OTHER_DOUBTS = ("тег", "родител")


def _doubt_line(preview: str) -> str:
    """Сомнение именно в ИСПОЛНИМОСТИ СТРОК списка."""
    return next((ln for ln in _doubt_lines(preview)
                 if not any(w in ln.lower() for w in _OTHER_DOUBTS)), "")


@pytest.mark.parametrize("tool", list(TOOLS))
async def test_failed_check_is_said_out_loud(tool, monkeypatch):
    """Главное утверждение круга 8: сбой сверки виден ЧЕЛОВЕКУ, а не логу."""
    _break_live_state(monkeypatch)

    preview = await rs.call_direct(tool, **TOOLS[tool](LIVE_ROWS))

    assert _doubt_line(preview), (
        f"{tool}: сверить исполнимость не удалось, но план выглядит "
        f"полностью исполнимым — человек подтверждает его, думая, что "
        f"применится всё:\n{preview}")


@pytest.mark.parametrize("tool", list(TOOLS))
async def test_failed_check_still_builds_a_confirmable_plan(tool, monkeypatch):
    """Обратная сторона: невозможность ПРОВЕРИТЬ — не повод отказать в работе.
    Иначе разовый сбой чтения состояния останавливает всё подряд, включая
    полностью исправные задачи, — а «применилось не то» и без того невозможно:
    исполнитель на недоступном состоянии отказывает по каждой строке сам."""
    _break_live_state(monkeypatch)

    preview = await rs.call_direct(tool, **TOOLS[tool](LIVE_ROWS))

    assert re.search(r"Манифест `([0-9a-f]+)`", preview), (
        f"{tool}: сбой сверки превратился в отказ строить план:\n{preview}")


@pytest.mark.parametrize("tool", list(TOOLS))
async def test_a_healthy_plan_carries_no_doubt(tool, monkeypatch):
    """Контроль: предупреждение обязано быть ИЗБИРАТЕЛЬНЫМ. Строка «проверить
    не удалось» в каждом плане подряд — это шум, который через неделю
    перестают читать, и тогда она не значит ничего."""
    rs.wire(monkeypatch)

    preview = await rs.call_direct(tool, **TOOLS[tool](LIVE_ROWS))

    assert not _doubt_line(preview), (
        f"{tool}: сверка прошла успешно, а план всё равно жалуется:\n{preview}")


def _numbered_rows(preview: str) -> list:
    """Только сами пункты списка, без всего, что напечатано ПОД ним.

    Пустая строка заканчивает пункт — иначе сводки и предупреждения из-под
    списка достаются последнему пункту, и проверка «эта строка не помечена»
    начинает падать (или проходить) по чужому тексту. Тот же разбор и та же
    причина, что в tests/test_update_plan_marks_dead_rows.py."""
    rows, current = [], None
    for line in preview.splitlines():
        m = re.match(r"^(\d+)\.\s(.*)$", line)
        if m:
            rows.append(m.group(2))
            current = len(rows) - 1
        elif not line.strip():
            current = None
        elif current is not None:
            rows[current] += "\n" + line
    return rows


@pytest.mark.parametrize("tool", list(TOOLS))
async def test_failed_check_marks_no_row(tool, monkeypatch):
    """И не выдаёт незнание за знание с другой стороны: ⛔ — это заявление о
    ФАКТЕ «строка не применится», а фактов у сервера сейчас нет."""
    _break_live_state(monkeypatch)

    preview = await rs.call_direct(tool, **TOOLS[tool](LIVE_ROWS))

    rows = _numbered_rows(preview)
    assert len(rows) == len(LIVE_ROWS), f"{tool}: план не перечислил строки:\n{preview}"
    assert not [r for r in rows if "⛔" in r], (
        f"{tool}: сервер пометил строки, ничего о них не зная:\n{preview}")


async def test_unreadable_tag_list_does_not_invent_facts(monkeypatch):
    """Тот же класс, найденный этим же тестом рядом: превью `set_task_tags`
    печатает «тег не существует — будет создан», сверяясь со списком тегов
    аккаунта. Пока сбой чтения оставлял этот список ПУСТЫМ, пометка вставала
    на КАЖДЫЙ тег — в том числе на давно существующие. Незнание выдавалось за
    факт, причём за обратный действительности."""
    _break_live_state(monkeypatch)

    preview = await rs.call_direct(
"set_task_tags", summary="Ставлю тег",
        # «тег01» в аккаунте стенда ЕСТЬ (rs.TAGS) — «будет создан» про него
        # было бы прямой ложью.
        tasks=[dict(r, tags=["тег01"]) for r in LIVE_ROWS])

    assert "будет создан" not in preview, (
        f"сервер объявил существующий тег новым, ничего не прочитав:\n{preview}")
    assert [ln for ln in _doubt_lines(preview) if "тег" in ln.lower()], (
        f"о том, что список тегов не прочитался, человеку не сказали:\n{preview}")


async def test_a_readable_tag_list_still_flags_new_tags(monkeypatch):
    """Контроль: пометка «будет создан» никуда не делась там, где список
    тегов действительно прочитан и тега в нём действительно нет."""
    rs.wire(monkeypatch)

    preview = await rs.call_direct("set_task_tags", summary="Ставлю тег",
                            tasks=[dict(r, tags=["совсем-новый-тег"])
                                   for r in LIVE_ROWS])

    assert "будет создан" in preview, preview


async def test_exception_inside_the_check_is_also_visible(monkeypatch):
    """Вторая ветка того же сбоя: сверка не вернула «недоступно», а УПАЛА.

    Раньше оба пути вели в один и тот же молчаливый `return {}` — то есть
    исключение внутри сверки тоже выдавалось за чистый план. Падает здесь
    ИСТОЧНИК живого состояния (`_open_by_id`), а не сама проверяемая сверка."""
    rs.wire(monkeypatch)

    def boom(fresh=False):
        raise RuntimeError("state read exploded")

    monkeypatch.setattr(s, "_open_by_id", boom)

    preview = await rs.call_direct("update_tasks", summary="Меняю приоритет",
                            tasks=[dict(r, priority=1) for r in LIVE_ROWS])

    assert _doubt_line(preview), (
        f"исключение внутри сверки прошло молча:\n{preview}")
    assert re.search(r"Манифест `([0-9a-f]+)`", preview), preview
