"""План move_tasks против ЖИВОГО состояния (ночной QA 2026-08-19, баги A1/A2).

A1 (остаток на main): строки-задачи план уже сверяет (круг 8,
`_plan_live_check`), а вот НАЗНАЧЕНИЕ — нет: call #1 с мусорным или чужим
`to_project_id` строил план без единого отказа («⚠️ НАЗВАНИЕ СПИСКА
УСТАНОВИТЬ НЕ УДАЛОСЬ» — это констатация, не отказ), и человек ставил «да»
под операцией, которой исполнитель гарантированно откажет ЦЕЛИКОМ
(`_guard_project_or_refuse(..., require_known=True)` в `_move_tasks_impl`).
План на заведомо обречённую операцию — просьба нажать «да» впустую, поэтому
несуществующее назначение обязано давать «🛑 План НЕ построен» уже на фазе
плана — тот же исход, что «исполнимых строк не осталось» у `_plan_live_check`.

A2: авто-строка плана печатала только «→ <куда>» — откуда задача уезжает,
видно не было вовсе (только если вызывающий сам напишет это в summary), а
перемещение в ТОТ ЖЕ проект (no-op) не отличалось ни в плане, ни в
результате исполнения от настоящего: «↪ Перемещено 1 → …» на операции,
которая не изменила ничего. Клиентский слой такие строки даже не отправляет
(batch_move_tasks_raw пропускает fromId == toId — см.
test_client_v2_move_raw), то есть «Перемещено» рапортовалось о запросе,
которого не было.

Сбой чтения списка проектов при этом НЕ превращается в отказ обслуживания
(та же политика, что `_PLAN_UNVERIFIED_NOTE`): план строится, но говорит
вслух, что назначение не сверено.

Стенд — tests/read_stand.py: настоящие клиенты, подменён только HTTP.
"""
import re

import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as stand

GHOST_PROJECT = "6a99ghostghostghost0002"   # такого проекта в аккаунте нет


@pytest.fixture(autouse=True)
def _stand(monkeypatch):
    return stand.wire(monkeypatch)


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


def _mid(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"в превью нет manifest_id:\n{preview}"
    return m.group(1)


def _numbered_rows(preview: str) -> list:
    """Пункты списка плана вместе с их подстроками-пометками (тот же разбор,
    что в test_plan_check_failure_is_visible: пустая строка заканчивает
    пункт, иначе сводки из-под списка достаются последнему пункту)."""
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


# ─────────────────── A1: назначение сверяется на фазе плана ───────────────────

async def test_plan_refuses_nonexistent_destination():
    """Мусорный to_project_id — плана нет ВООБЩЕ, а не план с оговоркой:
    исполнитель отказал бы всей операции, подтверждать нечего."""
    text = await stand.call_direct(
        "move_tasks", summary="Переношу",
        tasks=[{"taskId": stand.TASK_ROOT, "projectId": stand.P_WORK,
                "title": "Собрать отчёт"}],
        to_project_id=GHOST_PROJECT)

    assert "🛑 План НЕ построен" in text, text
    assert "не найден" in text, text
    assert GHOST_PROJECT in text, text          # какой id не нашёлся — сказано
    assert "manifest_id" not in text, text      # подтверждать нечего
    assert not s._MANIFESTS, "манифест на обречённую операцию всё же создан"


async def test_plan_refuses_destination_name_mismatch():
    """to_project_id указывает на «Дом», а вызывающий назвал его «Работа» —
    защита от «не того проекта» обязана сработать ДО подтверждения."""
    text = await stand.call_direct(
        "move_tasks", summary="Переношу",
        tasks=[{"taskId": stand.TASK_ROOT, "projectId": stand.P_WORK,
                "title": "Собрать отчёт"}],
        to_project_id=stand.P_HOME, to_project_name="Работа")

    assert "🛑 План НЕ построен" in text, text
    assert "«Дом»" in text and "«Работа»" in text, text
    assert not s._MANIFESTS


async def test_unreadable_project_list_builds_a_plan_with_a_doubt(monkeypatch):
    """Сбой чтения — не отказ обслуживания: план строится, но сомнение в
    назначении сказано вслух (политика `_PLAN_UNVERIFIED_NOTE`)."""
    v2, v1, transport = stand.wire(monkeypatch)

    def dead(*a, **k):
        raise RuntimeError("state unavailable")

    monkeypatch.setattr(v2, "_request", dead)
    monkeypatch.setattr(v1, "_make_request", dead)
    v2._state_cache = None
    v2._state_ts = 0.0

    text = await stand.call_direct(
        "move_tasks", summary="Переношу",
        tasks=[{"taskId": stand.TASK_ROOT, "title": "Собрать отчёт"}],
        to_project_id=stand.P_HOME)

    assert re.search(r"Манифест `([0-9a-f]+)`", text), (
        f"сбой чтения превратился в отказ строить план:\n{text}")
    low = text.lower()
    assert "⚠️" in text and "назнач" in low and "не удал" in low, (
        f"план молчит о том, что назначение не сверено:\n{text}")


# ────────────── A2: «откуда → куда» именами и пометка no-op ──────────────

async def test_plan_shows_source_and_destination_names():
    """Авто-строка называет ОБА конца перемещения человеческими именами —
    не полагаясь на то, что вызывающий напишет их в summary."""
    text = await stand.call_direct(
        "move_tasks", summary="Переношу",
        tasks=[{"taskId": stand.TASK_ROOT, "projectId": stand.P_WORK,
                "title": "Собрать отчёт"}],
        to_project_id=stand.P_HOME)

    rows = _numbered_rows(text)
    assert len(rows) == 1, text
    assert "«Работа»" in rows[0], f"исходный проект не назван:\n{text}"
    assert "«Дом»" in rows[0], f"проект назначения не назван:\n{text}"


async def test_plan_marks_noop_row():
    """Перемещение в проект, где задача УЖЕ лежит, помечено как факт (ℹ️ —
    канал факта о состоянии, не сомнения ⚠️): подтверждать его — подтверждать
    пустую операцию."""
    text = await stand.call_direct(
        "move_tasks", summary="Переношу",
        tasks=[{"taskId": stand.TASK_ROOT, "projectId": stand.P_WORK,
                "title": "Собрать отчёт"}],
        to_project_id=stand.P_WORK)

    rows = _numbered_rows(text)
    assert len(rows) == 1, text
    assert "ℹ️" in rows[0] and "уже в" in rows[0].lower(), (
        f"no-op строка неотличима от настоящего перемещения:\n{text}")


async def test_plan_mixed_batch_marks_only_the_noop_row():
    """Пометка избирательная: живое перемещение рядом с no-op не пятнается."""
    text = await stand.call_direct(
        "move_tasks", summary="Переношу",
        tasks=[{"taskId": stand.TASK_ROOT, "projectId": stand.P_WORK,
                "title": "Собрать отчёт"},                       # no-op
               {"taskId": stand.TASK_MID, "projectId": stand.P_HOME,
                "title": "Записаться к врачу"}],                 # настоящее
        to_project_id=stand.P_WORK)

    rows = _numbered_rows(text)
    assert len(rows) == 2, text
    assert "ℹ️" in rows[0] and "уже в" in rows[0].lower(), text
    assert "ℹ️" not in rows[1], (
        f"пометка no-op досталась строке с настоящим перемещением:\n{text}")


# ───────────── A2: результат исполнения не врёт про no-op ─────────────

async def test_execution_reports_noop_separately(_stand):
    """Смешанный батч: настоящая строка перемещается и рапортуется, no-op —
    отдельной честной строкой, НЕ внутри «↪ Перемещено»."""
    _v2, _v1, transport = _stand
    preview = await s.move_tasks.direct(
        "Переношу", tasks=[
            {"taskId": stand.TASK_MID, "projectId": stand.P_HOME,
             "title": "Записаться к врачу"},                     # настоящее
            {"taskId": stand.TASK_ROOT, "projectId": stand.P_WORK,
             "title": "Собрать отчёт"}],                         # no-op
        to_project_id=stand.P_WORK)
    result = await s.move_tasks.direct(
        "Переношу", manifest_id=_mid(preview), user_reply="да")

    # настоящее перемещение состоялось и подтверждено живым состоянием
    live = {t["id"]: t for t in transport.state["syncTaskBean"]["update"]}
    assert live[stand.TASK_MID]["projectId"] == stand.P_WORK
    moved_line = next((ln for ln in result.splitlines() if "Перемещено" in ln), "")
    assert "Перемещено 1" in moved_line, result
    assert "«Записаться к врачу»" in moved_line, result
    # no-op — НЕ в списке перемещённых, а в собственной строке-факте
    assert "«Собрать отчёт»" not in moved_line, (
        f"no-op строка выдана за перемещение:\n{result}")
    noop_line = next((ln for ln in result.splitlines()
                      if "ℹ️" in ln and "уже в" in ln.lower()), "")
    assert "«Собрать отчёт»" in noop_line, (
        f"про no-op строку результат молчит:\n{result}")


async def test_execution_all_noop_does_not_claim_a_move(_stand):
    """Все строки уже в целевом списке: «↪ Перемещено» не печатается вовсе —
    ответ обязан отличаться от успешного перемещения."""
    preview = await s.move_tasks.direct(
        "Переношу", tasks=[
            {"taskId": stand.TASK_ROOT, "projectId": stand.P_WORK,
             "title": "Собрать отчёт"}],
        to_project_id=stand.P_WORK)
    result = await s.move_tasks.direct(
        "Переношу", manifest_id=_mid(preview), user_reply="да")

    assert "Перемещено" not in result, (
        f"пустая операция отчиталась как перемещение:\n{result}")
    assert "ℹ️" in result and "уже в" in result.lower(), result
