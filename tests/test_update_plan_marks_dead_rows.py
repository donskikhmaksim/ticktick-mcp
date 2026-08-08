"""План update_tasks обязан пометить строку, которая ЗАВЕДОМО не применится.

Дефект (живая приёмка 2026-08-07). Батч из пяти задач, одна из которых лежала
в корзине ещё ДО построения плана. Карточка подтверждения перечислила все
пять ОДИНАКОВО — ни намёка, что одна строка обречена. Отчёт после нажатия был
честен («НЕ применилось 1 — не найдена среди открытых»), но человек узнавал об
этом ПОСТФАКТУМ: подтверждал он операцию, часть которой не могла состояться.

Сверки живого состояния на фазе плана у update_tasks не было вовсе — только
на исполнении (`_update_tasks_impl` → `_guard_task`). У manual_triage такая
сверка есть с самого начала (пометка ПРОПУЩЕНО), то есть речь о пробеле в
одном туле, а не о новой политике.

Всё идёт через `tests/read_stand.py`: настоящие клиенты, подменён только
транспорт, тул зовётся ПО ИМЕНИ через реестр — проверяется тот же текст,
который увидит человек. Ни `_split_tasks_by_state`, ни `_open_by_id`, ни
`_describe_update_item` не подменяются: дефект живёт ровно на этом участке,
и мок здесь проверял бы мок.

Время: ни одной календарной привязки — план строится на смене приоритета,
поэтому ни «сегодня», ни «час назад» в фикстуре не участвуют.

ПОЗИЦИЯ ОБРЕЧЁННОЙ СТРОКИ В ФИКСТУРЕ — ЧАСТЬ ПРОВЕРКИ, А НЕ ПРОИЗВОЛ
(2026-08-07, найдено мутационной проверкой при слиянии). Сначала DEAD стоял
ПОСЛЕДНИМ, и тест зеленел даже с полностью удалённой построчной пометкой:
сводка «⚠️ Не применится строк: 1 из 5 — они помечены ⛔ выше» печатается под
списком, наивный разбор приклеивал её к последнему пункту, и `_marked()`
засчитывал её «⛔» как пометку строки. То есть тест, написанный ровно чтобы
отличить пометку В ПЛАНЕ от честности отчёта, принимал второе за первое.

Закрыто с двух сторон, потому что одной мало:
  1. `_plan_rows` обрывает пункт на пустой строке — сводка, отделённая от
     списка пустой строкой, больше не принадлежит ни одному пункту;
  2. DEAD стоит В СЕРЕДИНЕ — тогда даже разбор, снова ставший наивным,
     приклеит сводку к ЖИВОМУ пункту, и это увидит
     `test_live_rows_are_not_marked`, а не пропустит.

Общее правило для тестов на текст с многострочной разметкой: проверяемый
элемент нельзя ставить последним. Всё, что печатается ПОД списком (сводки,
предупреждения, инструкции), при любом наивном разборе склеивается с
последним пунктом — и проверка последнего пункта тихо перестаёт быть
проверкой.
"""
import re

import pytest

import ticktick_mcp.src.server as s
from tests.read_stand import (
    P_HOME, P_WORK, TASK_HIGH, TASK_MID, TASK_ROOT, TASK_TAGGED, TASK_TRASHED,
    call, wire)

# Названия — из снимка стенда; они должны совпадать с живыми, иначе строка
# уедет в mismatch по другой причине, и тест перестанет проверять корзину.
LIVE = [
    {"taskId": TASK_ROOT, "projectId": P_WORK, "title": "Собрать отчёт", "priority": 1},
    {"taskId": TASK_MID, "projectId": P_HOME, "title": "Записаться к врачу", "priority": 1},
    {"taskId": TASK_HIGH, "projectId": P_HOME, "title": "Оплатить страховку", "priority": 1},
    {"taskId": TASK_TAGGED, "projectId": P_HOME, "title": "Полить цветы", "priority": 1},
]
# Эта задача лежит в КОРЗИНЕ — в выборке открытых её нет, и исполнитель её
# отвергнет. План обязан сказать это ДО подтверждения.
DEAD = {"taskId": TASK_TRASHED, "projectId": P_HOME, "title": "Старая затея",
        "priority": 1}
# Обречённая строка — В СЕРЕДИНЕ, не в конце (см. шапку файла): под списком
# печатается сводка, и у последнего пункта она отбирает роль собственной
# пометки при любом наивном разборе.
BATCH = LIVE[:2] + [DEAD] + LIVE[2:]


@pytest.fixture(autouse=True)
def stand(monkeypatch):
    return wire(monkeypatch)


@pytest.fixture(autouse=True)
def _isolate_manifests():
    """`_MANIFESTS` — модульный глобал на всю сессию: работаем на чистом и
    возвращаем как было, чтобы соседние файлы не видели наших планов."""
    before, tombs = dict(s._MANIFESTS), dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _plan_rows(preview: str) -> dict:
    """{номер пункта: весь его текст, включая строки-продолжения}. Пометка
    может стоять как в самой строке, так и под ней — разбор не должен
    зависеть от того, как её оформили.

    ПУСТАЯ СТРОКА ЗАКАНЧИВАЕТ ПУНКТ. Без этого всё, что напечатано ПОД
    списком (сводка «не применится строк: N», инструкция «Покажи это
    пользователю»), доставалось последнему пункту — и проверка последнего
    пункта засчитывала чужой текст за его собственную пометку (см. шапку
    файла: ровно так этот тест и зеленел с удалённым фиксом)."""
    rows, current = {}, None
    for line in preview.splitlines():
        m = re.match(r"^(\d+)\.\s(.*)$", line)
        if m:
            current = int(m.group(1))
            rows[current] = m.group(2)
        elif not line.strip():
            current = None          # пункт кончился — дальше уже не его текст
        elif current is not None and not line.startswith("###"):
            if line.startswith(("Покажи это", "Манифест", "План")):
                current = None
                continue
            rows[current] += "\n" + line
    return rows


def _marked(text: str) -> bool:
    """Пометка «эта строка не применится» в любом разумном оформлении."""
    low = text.lower()
    return ("⛔" in text or "пропущ" in low or "не будет применен" in low
            or "не применится" in low)


async def test_plan_marks_the_row_whose_task_is_in_the_trash():
    preview = await call("update_tasks", summary="Понижаю приоритет у пяти задач",
                         tasks=BATCH)

    rows = _plan_rows(preview)
    assert len(rows) == 5, f"в плане не 5 пунктов:\n{preview}"
    dead_rows = [t for t in rows.values() if "Старая затея" in t]
    assert dead_rows, f"строки про задачу из корзины нет вовсе:\n{preview}"
    assert _marked(dead_rows[0]), (
        "строка про задачу, которой нет среди открытых, ничем не отличается "
        f"от исполнимых — человек подтверждает обречённую операцию:\n{preview}")


async def test_live_rows_are_not_marked():
    """Пометка обязана быть ИЗБИРАТЕЛЬНОЙ: пометить всё подряд — то же самое,
    что не помечать ничего."""
    preview = await call("update_tasks", summary="Понижаю приоритет у пяти задач",
                         tasks=BATCH)

    rows = _plan_rows(preview)
    for text in rows.values():
        if "Старая затея" in text:
            continue
        assert not _marked(text), f"живая задача помечена как обречённая:\n{text}"


async def test_the_plan_is_still_built_and_still_confirmable():
    """Пометка — не отказ: остальные четыре строки исполнимы, и план обязан
    остаться подтверждаемым (иначе одна мёртвая ссылка блокировала бы
    работу)."""
    preview = await call("update_tasks", summary="Понижаю приоритет у пяти задач",
                         tasks=BATCH)

    m = re.search(r"Манифест `([0-9a-f]+)`", preview)
    assert m, f"план не построен:\n{preview}"
    assert s._MANIFESTS[m.group(1)]["kind"] == "update"


async def test_mismatched_title_is_marked_too():
    """Вторая заведомо-неисполнимая форма: id жив, но указывает на ДРУГУЮ
    задачу — исполнитель отвергает такую строку тем же способом.

    Обречённая строка стоит ПЕРВОЙ по той же причине, что DEAD в BATCH:
    последним пунктом она собрала бы на себя сводку из-под списка и перестала
    бы что-либо проверять (см. шапку файла)."""
    preview = await call(
        "update_tasks", summary="Меняю приоритет",
        tasks=[{"taskId": TASK_MID, "projectId": P_HOME,
                "title": "Название, которого у этой задачи нет", "priority": 5},
               dict(LIVE[0])])

    rows = _plan_rows(preview)
    bad = [t for t in rows.values() if "Название, которого" in t]
    assert bad and _marked(bad[0]), (
        f"строка с разошедшимся названием не помечена:\n{preview}")


async def test_plan_survives_when_live_state_is_unavailable(monkeypatch):
    """Сверка — best-effort: недоступное живое состояние не имеет права
    ронять фазу плана (то же правило, что у превью тегов в set_task_tags).
    Строки не помечаются — фактов о них у сервера нет.

    ЧЕГО ЭТОТ ТЕСТ БОЛЬШЕ НЕ УТВЕРЖДАЕТ (круг 8): что план при этом молчит.
    Молчание и было дефектом — см. tests/test_plan_check_failure_is_visible.py,
    где проверяется, что сам СБОЙ СВЕРКИ виден человеку."""
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: None)

    preview = await call("update_tasks", summary="Понижаю приоритет",
                         tasks=BATCH)

    assert re.search(r"Манифест `([0-9a-f]+)`", preview), (
        f"план не построен, хотя недоступность состояния — не повод:\n{preview}")
    assert not any(_marked(t) for t in _plan_rows(preview).values())
