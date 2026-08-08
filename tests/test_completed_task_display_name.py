"""Дефект №1 (живая приёмка 2026-08-07): карточка подтверждения отказывалась
называть ЗАВЕРШЁННУЮ задачу по имени.

Две карточки `create_attachment_upload_url`, отличавшиеся ТОЛЬКО статусом
задачи:

    завершённая → «в задачу id 6a7571238f0854e347f51407 —
                   ⚠️ НАЗВАНИЕ ЗАДАЧИ УСТАНОВИТЬ НЕ УДАЛОСЬ»
    открытая    → «в задачу «__AUTOTEST__upd-B1» (id 6a763f848f08e34527bbe2b8)»

Корень: `_live_task_title` при промахе снапшота открытых задач звала
`_official_task_snapshot`, а та кончается `if t.get("status", 0) != 0:
return None` — фильтр «только открытые». Для identity-guard этот фильтр —
осознанная часть политики (в докстринге прямым текстом: fallback не должен
быть ДОБРЕЕ основного пути). Но `_live_task_title` переиспользовала ту же
функцию для ДРУГОЙ цели — нарисовать имя в карточке, — а для отображения
фильтр смысла не имеет: имя завершённой задачи известно, отказ ничего не
защищает.

Правило, ради которого этот файл существует: у guard-функции пустой ответ
значит «не рискую», у отображающей — «не знаю». Разные вещи, одной функцией
не обслуживаются.

Ветка «имя не установить» ОБЯЗАНА сохраниться для настоящих случаев (задачи
нет, API недоступен) — на это здесь два отдельных теста.

Тесты идут через стенд `tests/read_stand.py`: настоящие клиенты, подменён
только транспорт. Ни `_live_task_title`, ни `_official_task_snapshot` —
функции, которые чинит этот фикс, — не подменяются, иначе тест проверял бы
мок, а не фикс.
"""
import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

NO_NAME_MARK = "НАЗВАНИЕ ЗАДАЧИ УСТАНОВИТЬ НЕ УДАЛОСЬ"
GHOST_TASK = "deadbeefdeadbeefdeadbeef"


@pytest.fixture(autouse=True)
def public_base(monkeypatch):
    """Сервер знает свой публичный адрес — иначе тул отказывает ДО гейта."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)


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


def _wire_with_completed(monkeypatch):
    """Стенд, где завершённая задача ЕСТЬ в официальном API (v1 отдаёт её по
    `/project/{pid}/task/{tid}`), но её НЕТ в снапшоте открытых задач v2 —
    ровно положение дел из живой приёмки."""
    return rs.wire(monkeypatch,
                   v1_tasks=list(rs.TASKS) + list(rs.COMPLETED))


async def test_completed_task_is_named_in_the_card(monkeypatch):
    """Завершённая задача: карточка называет её ИМЯ, а не голый id."""
    _wire_with_completed(monkeypatch)

    card = await rs.call("create_attachment_upload_url",
                         task_id=rs.TASK_COMPLETED, project_id=rs.P_WORK,
                         filename="чек.pdf")

    assert "Купить бумагу" in card, card
    assert NO_NAME_MARK not in card, card


async def test_open_task_card_unchanged(monkeypatch):
    """Контроль: открытая задача как называлась по имени, так и называется —
    фикс не должен менять счастливый путь."""
    _wire_with_completed(monkeypatch)

    card = await rs.call("create_attachment_upload_url",
                         task_id=rs.TASK_ROOT, project_id=rs.P_WORK,
                         filename="чек.pdf")

    assert "Собрать отчёт" in card, card
    assert NO_NAME_MARK not in card, card


async def test_nonexistent_task_still_says_name_unknown(monkeypatch):
    """Ветка «имя не установить» СОХРАНЕНА: задачи нет ни среди открытых, ни
    в официальном API — карточка обязана сказать это вслух, а не молча
    показать id."""
    _wire_with_completed(monkeypatch)

    card = await rs.call("create_attachment_upload_url",
                         task_id=GHOST_TASK, project_id=rs.P_WORK,
                         filename="чек.pdf")

    assert NO_NAME_MARK in card, card


async def test_unreadable_api_still_says_name_unknown(monkeypatch):
    """Вторая настоящая причина неизвестности: чтение упало. Тоже вслух."""
    _wire_with_completed(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("v1 недоступен")

    monkeypatch.setattr(s.ticktick, "_make_request", boom)
    monkeypatch.setattr(s.ticktick_v2, "_request", boom)
    s.ticktick_v2._state_cache = None

    card = await rs.call("create_attachment_upload_url",
                         task_id=rs.TASK_COMPLETED, project_id=rs.P_WORK,
                         filename="чек.pdf")

    assert NO_NAME_MARK in card, card
