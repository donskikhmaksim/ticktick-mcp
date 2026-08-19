"""QA-2 (2026-08-19), дефекты №8/№9/№10/№11/№12: даты и читающие инструменты.

№8. `plan_task_creation` МОЛЧА отбрасывал due_date неподдерживаемого формата:
«2026-08-19 20:00» (пробел вместо «T») уезжал в API как есть, TickTick его
игнорировал — задача создавалась БЕЗ даты, и ни ответ, ни operation_report
не говорили о потере. Теперь формат с пробелом ПРИНИМАЕТСЯ (как время
владельца), а всё непринимаемое — явный отказ строки.

№9. Пост-проверка сравнивала время СТРОКАМИ ([:10]): исправное обновление
dueDate «2026-08-19T20:00:00-07:00» (хранится как «2026-08-20T03:00:00.000
+0000» — тот же момент, другая сторона полуночи UTC) объявлялось
«❌ не применилось». Сравнение — семантическое, по моменту; время владельца —
America/Los_Angeles, не UTC.

№10. `get_task` с рассинхронной парой project_id/task_id протекал сырой
«500 Server Error: for url: …» — теперь рядом с человеческим текстом стоит
подсказка про причину и живой маршрут (get_task_info).

№11. `get_trash` игнорировал границы limit: 0 молча печатал одну запись,
99999 ронял ответ о лимит размера. Теперь <1 — отказ, >500 — клэмп вслух.

№12. `get_project_tasks` на НЕСУЩЕСТВУЮЩИЙ project_id отвечал «No tasks
found in project '<id>'» — неотличимо от пустого существующего проекта;
`get_task_comments` на несуществующий task_id — «No comments». Оба теперь
отличают «объекта нет» от «объект пуст».
"""
import asyncio
from zoneinfo import ZoneInfo

import pytest

import ticktick_mcp.src.server as s


# ═══════ №9: сравнение моментов, а не строк ════════════════════════════════

def test_same_instant_in_two_zones_agrees():
    """Живой QA-кейс: «2026-08-20T03:00:00.000+0000» и
    «2026-08-19T20:00:00-07:00» — один момент."""
    assert s._verify_dates_equal("2026-08-20T03:00:00.000+0000",
                                 "2026-08-19T20:00:00-07:00")


def test_different_instants_disagree():
    assert not s._verify_dates_equal("2026-08-20T03:00:00.000+0000",
                                     "2026-08-19T21:00:00-07:00")


def test_naive_expectation_reads_in_the_owners_zone(monkeypatch):
    """Ожидание без смещения — время ВЛАДЕЛЬЦА (America/Los_Angeles у
    Максима), не UTC."""
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    assert s._verify_dates_equal("2026-08-20T03:00:00.000+0000",
                                 "2026-08-19T20:00:00")
    assert not s._verify_dates_equal("2026-08-20T03:00:00.000+0000",
                                     "2026-08-20T03:00:00")


def test_all_day_dates_stay_calendar_compare():
    """All-day — календарная дата без зоны (#36): сравнение по [:10]."""
    assert s._verify_dates_equal("2026-08-19", "2026-08-19")
    assert not s._verify_dates_equal("2026-08-19", "2026-08-20")


def test_update_verdict_accepts_the_equivalent_due_date():
    """Интеграция: вердикт update по журналу больше не краснит исправное
    обновление из-за таймзоны."""
    item = {"taskId": "t1", "title": "Задача",
            "expect": {"changes": {"dueDate": "2026-08-19T20:00:00-07:00"}}}
    live_map = {"t1": {"id": "t1", "title": "Задача",
                       "dueDate": "2026-08-20T03:00:00.000+0000"}}
    status, line = s._verify_item("update", item, live_map, {})
    assert status == "ok", line
    assert "не применилось" not in line


# ═══════ №8: due_date с пробелом принимается, мусор — явный отказ ══════════

def test_space_datetime_is_accepted_as_owner_local_time(monkeypatch):
    """«YYYY-MM-DD HH:MM» → полный ISO-момент в зоне владельца (в тестовом
    окружении USER_TIMEZONE=UTC)."""
    out = s._resolve_relative_date("2026-08-19 20:00")
    assert out == "2026-08-19T20:00:00.000+0000", out
    # Идемпотентность: уже нормализованное проходит без изменений.
    assert s._resolve_relative_date(out) == out


def test_space_datetime_respects_the_owner_zone(monkeypatch):
    monkeypatch.setattr(s, "_USER_TZ", ZoneInfo("America/Los_Angeles"))
    out = s._resolve_relative_date("2026-08-19 20:00")
    assert out == "2026-08-19T20:00:00.000-0700", out


async def test_plan_creation_refuses_an_unparseable_date(monkeypatch):
    """Мусорная дата — отказ строки с причиной, а не задача БЕЗ даты."""
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})

    out = await s.plan_task_creation("Создаю", [
        {"title": "Годная", "project_id": "p1"},
        {"title": "Кривая дата", "project_id": "p1",
         "due_date": "31/08/2026 8pm"},
    ])
    assert "Исключены 1" in out, out
    assert "Кривая дата" in out and "неподдерживаемый формат даты" in out
    # Принимаемые формы названы — человеку есть что исправить.
    assert "YYYY-MM-DD" in out


async def test_plan_creation_keeps_the_space_datetime(monkeypatch):
    """QA-кейс: «2026-08-19 20:00» больше не теряется — строка в плане, дата
    нормализована в манифесте."""
    before = dict(s._MANIFESTS)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})

    out = await s.plan_task_creation("Создаю", [
        {"title": "Со сроком", "project_id": "p1",
         "due_date": "2026-08-19 20:00"},
    ])
    try:
        assert "Исключены" not in out, out
        new_mids = [m for m in s._MANIFESTS if m not in before]
        assert len(new_mids) == 1
        raw = s._MANIFESTS[new_mids[0]]["raw"]
        assert raw[0]["due_date"] == "2026-08-19T20:00:00.000+0000"
    finally:
        for m in [m for m in s._MANIFESTS if m not in before]:
            s._MANIFESTS.pop(m, None)


# ═══════ №10: get_task называет причину 500 и живой маршрут ════════════════

async def test_get_task_500_names_the_mismatch_cause(monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    class _Off:
        def get_task(self, project_id, task_id):
            raise RuntimeError(
                "500 Server Error:  for url: https://api.ticktick.com/open/"
                "v1/project/aaa/task/bbb")

    monkeypatch.setattr(s, "ticktick", _Off())
    out = await s.get_task("aaa", "bbb")
    assert "не соответствуют друг другу" in out, out
    assert "get_task_info" in out


async def test_get_task_non_http_error_gets_no_mismatch_hint(monkeypatch):
    """Контроль: обычная ошибка (не HTTP-код) не обрастает чужой подсказкой."""
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    class _Off:
        def get_task(self, project_id, task_id):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(s, "ticktick", _Off())
    out = await s.get_task("aaa", "bbb")
    assert "не соответствуют друг другу" not in out


# ═══════ №11: границы limit у get_trash ════════════════════════════════════

def _trash_stand(monkeypatch, n=5):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)

    class _V2:
        def get_trash(self, limit=500):
            rows = [{"id": f"tr{i}", "title": f"Удалённая {i}",
                     "projectId": "p1"} for i in range(n)]
            return rows[:limit]

    monkeypatch.setattr(s, "ticktick_v2", _V2())


async def test_get_trash_refuses_a_meaningless_limit(monkeypatch):
    _trash_stand(monkeypatch)
    out = await s.get_trash(limit=0)
    assert "🛑" in out and "limit=0" in out, out
    assert "Удалённая" not in out, "записи при отказе не печатаются"


async def test_get_trash_clamps_an_oversized_limit_aloud(monkeypatch):
    _trash_stand(monkeypatch)
    out = await s.get_trash(limit=99999)
    assert "limit=99999" in out and "обрезан до 500" in out, out
    assert "Удалённая 4" in out  # всё, что есть, напечатано


async def test_get_trash_default_still_works(monkeypatch):
    _trash_stand(monkeypatch)
    out = await s.get_trash()
    assert "Trashed tasks (5)" in out
    assert "обрезан" not in out and "🛑" not in out


# ═══════ №12: «объекта нет» ≠ «объект пуст» ════════════════════════════════

async def test_get_project_tasks_unknown_id_is_not_an_empty_project(
        monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_inbox_project", lambda: None)

    class _Off:
        def get_project_with_data(self, project_id):
            return {"project": {}, "tasks": []}  # так канал отвечает на чужой id

    monkeypatch.setattr(s, "ticktick", _Off())
    out = await s.get_project_tasks("6a99ghost")
    assert "не найден" in out and "НЕ пустой проект" in out, out
    assert "No tasks found" not in out


async def test_get_project_tasks_empty_existing_project_still_says_empty(
        monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_inbox_project", lambda: None)

    class _Off:
        def get_project_with_data(self, project_id):
            return {"project": {"id": project_id, "name": "Пустой"},
                    "tasks": []}

    monkeypatch.setattr(s, "ticktick", _Off())
    out = await s.get_project_tasks("p-empty")
    assert "No tasks found in project 'Пустой'" in out


async def test_get_task_comments_unknown_task_is_not_no_comments(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)

    class _V2:
        def get_task_comments(self, project_id, task_id):
            return []

        def find_task_any_state(self, task_id):
            return None, None

    monkeypatch.setattr(s, "ticktick_v2", _V2())
    out = await s.get_task_comments("Призрак", "p1", "ghost")
    assert "не найдена" in out and "get_task_info" in out, out
    assert "No comments" not in out


async def test_get_task_comments_existing_task_without_comments(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)

    class _V2:
        def get_task_comments(self, project_id, task_id):
            return []

        def find_task_any_state(self, task_id):
            return {"id": task_id, "title": "Живая"}, "open"

    monkeypatch.setattr(s, "ticktick_v2", _V2())
    out = await s.get_task_comments("Живая", "p1", "t1")
    assert "No comments on task 'Живая'." in out
