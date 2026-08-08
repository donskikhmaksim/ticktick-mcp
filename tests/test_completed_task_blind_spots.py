"""Пункт 4 разбора 2026-08-07: остатки ТОГО ЖЕ класса, что дефекты №1-№3 —
отображающий путь смотрит в источник, ограниченный ОТКРЫТЫМИ задачами, и
поэтому слепнет на завершённой (или просто выпавшей из v2-снапшота) задаче.

Правило, по которому эти места отобраны: у guard-функции пустой ответ значит
«не рискую», у отображающей — «не знаю»; ограничение «только открытые» —
guard-политика, и в отображении оно не к месту.

Здесь закрыты четыре места, найденные аудитом и являющиеся ПРЯМЫМ
продолжением уже сделанных фиксов:

  1. `_live_task_title` без project_id — фикс №1 закрывал только случай, когда
     project_id передан; без него завершённая задача снова давала «имя
     установить не удалось», хотя ленты завершённых/корзины её знают;
  2. `_create_attachment_upload_url_impl` резолвил project_id через
     `_resolve_project_id` (только открытые) — и отказывал «Could not resolve
     project_id» УЖЕ ПОСЛЕ того, как человек одобрил выдачу ссылки на запись
     в аккаунт; эталон (`_attachment_project_id`) лежал в том же файле и уже
     применён на download-пути;
  3. `_attach_file_to_task_impl` и 4. `_abandon_task_impl` печатали
     «[task 6a757123…]» вместо имени, хотя рядом стоящий guard живое имя УЖЕ
     установил (в `_duplicate_task_impl` этот фолбэк есть — а у них не было).

Через стенд tests/read_stand.py: настоящие клиенты, подменён только
транспорт.
"""
import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

NO_NAME_MARK = "НАЗВАНИЕ ЗАДАЧИ УСТАНОВИТЬ НЕ УДАЛОСЬ"
PLACEHOLDER = f"[task {rs.TASK_COMPLETED[:8]}…]"


@pytest.fixture(autouse=True)
def public_base(monkeypatch):
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


def _wire(monkeypatch):
    """Завершённая задача есть и в ленте завершённых v2, и в официальном API."""
    return rs.wire(monkeypatch, v1_tasks=list(rs.TASKS) + list(rs.COMPLETED))


# ── 1. имя завершённой задачи БЕЗ project_id ────────────────────────────

async def test_live_title_resolves_completed_task_without_project_id(monkeypatch):
    _wire(monkeypatch)

    assert s._live_task_title(rs.TASK_COMPLETED) == "Купить бумагу"


async def test_upload_card_names_completed_task_without_project_id(monkeypatch):
    """Тот же факт через настоящую карточку: project_id не передан."""
    _wire(monkeypatch)

    card = await rs.call("create_attachment_upload_url",
                         task_id=rs.TASK_COMPLETED, filename="чек.pdf")

    assert "Купить бумагу" in card, card
    assert NO_NAME_MARK not in card, card


async def test_live_title_still_says_unknown_for_a_ghost(monkeypatch):
    """Ветка «не знаю» сохранена: задачи нет ни в одном источнике."""
    _wire(monkeypatch)

    assert s._live_task_title("deadbeefdeadbeefdeadbeef") is None


# ── 2. выдача ссылки на завершённую задачу доходит до конца ─────────────

async def test_upload_link_is_minted_for_a_completed_task(monkeypatch):
    """Раньше отказ «Could not resolve project_id» прилетал ПОСЛЕ одобрения:
    человек согласился выдать право записи, а ссылки не получил."""
    _wire(monkeypatch)

    out = await s._create_attachment_upload_url_impl(
        task_id=rs.TASK_COMPLETED, project_id=None, filename="чек.pdf")

    assert "Could not resolve project_id" not in out, out
    assert "/ul/" in out, out


# ── 3-4. имя в результате мутации берётся у guard'а, а не из открытых ───

async def test_attach_result_names_completed_task(monkeypatch):
    """task_title пуст — имя обязано прийти от guard'а, а не placeholder'ом."""
    v2, _v1, _t = _wire(monkeypatch)
    monkeypatch.setattr(v2, "upload_attachment",
                        lambda *a, **k: {"fileName": "чек.pdf", "size": 10})

    out = await s._attach_file_to_task_impl(
        task_title="", task_id=rs.TASK_COMPLETED, project_id=rs.P_WORK,
        url="https://example.com/чек.pdf")

    assert PLACEHOLDER not in out, out
    assert "Купить бумагу" in out, out


async def test_abandon_result_names_task_found_outside_the_snapshot(monkeypatch):
    """abandon_task работает только по ОТКРЫТЫМ задачам (это его политика, и
    она не меняется) — но задача, выпавшая из v2-снапшота и найденная
    guard'ом через официальный API, обязана быть названа по имени: именно
    такой случай (снапшот отстал на 25 минут) описан в комментарии к
    _official_task_snapshot как реально наблюдавшийся."""
    v2, _v1, _t = _wire(monkeypatch)
    lagging = [t for t in rs.TASKS if t["id"] != rs.TASK_ROOT]
    v2._state_cache = rs.build_state(tasks=lagging)
    monkeypatch.setattr(v2, "abandon_task", lambda *a, **k: {})

    out = await s._abandon_task_impl(summary="", task_id=rs.TASK_ROOT,
                                     task_title=None)

    assert f"[task {rs.TASK_ROOT[:8]}…]" not in out, out
    assert "Собрать отчёт" in out, out
