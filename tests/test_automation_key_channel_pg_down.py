"""`_automation_key_channel` переживает лежащий Postgres — приёмка разбора
QA-2 (2026-08-19, дефект №6).

ЧТО БЫЛО СЛОМАНО. Проверка каналов ключа идёт на КАЖДЫЙ вызов инструмента с
непустым `automation_key`, и оконный канал (`automation_key.find_window`) —
поход в Postgres — стоял в цепочке БЕЗ try, причём ДО legacy-канала
(`MCP_SECRET`), которому база вообще не нужна. При недоступной базе
`psycopg2.OperationalError` вылетал наружу голым трейсбеком: у клиента со
старым `MCP_SECRET` (легальный путь) вместо прохода — исключение; у клиента
с неверным ключом вместо обычного интерактивного пути — тоже исключение.

Правильное поведение: недоступное хранилище окон = «оконный канал сейчас не
отвечает» (WARNING в лог), остальные каналы проверяются дальше, вызов
инструмента продолжается штатно.
"""
import asyncio

import pytest

import ticktick_mcp.src.automation_key as ak
import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s


class _PgDown(Exception):
    """Двойник psycopg2.OperationalError — сам psycopg2 в тестовом окружении
    может быть не установлен, а классу проверки всё равно: ловится любой сбой
    похода в хранилище."""


@pytest.fixture
def _window_store_down(monkeypatch):
    def _boom(provided):
        raise _PgDown("could not connect to server: Connection refused")

    monkeypatch.setattr(ak, "find_window", _boom)


def test_legacy_secret_still_passes_when_the_window_store_is_down(
        monkeypatch, _window_store_down):
    """Клиенту со старым MCP_SECRET база не нужна — лежащий Postgres не имеет
    права его отшибать."""
    monkeypatch.setattr(s, "SECRET", "старый-секрет")
    assert s._automation_key_channel("старый-секрет") == "legacy"


def test_wrong_key_degrades_to_no_channel_instead_of_a_traceback(
        monkeypatch, _window_store_down):
    monkeypatch.setattr(s, "SECRET", "старый-секрет")
    assert s._automation_key_channel("неверный-ключ") == ""


def test_the_outage_is_logged_as_a_warning(monkeypatch, _window_store_down,
                                           caplog):
    monkeypatch.setattr(s, "SECRET", None)
    with caplog.at_level("WARNING"):
        s._automation_key_channel("какой-то-ключ")
    assert any("find_window" in r.message and "не отвечает" in r.message
               for r in caplog.records), \
        "пропуск оконного канала обязан быть виден в логе"


def test_static_key_short_circuits_before_the_store(monkeypatch,
                                                    _window_store_down):
    """Статический ключ сверяется ДО похода в базу — при совпадении лежащий
    Postgres вообще не участвует."""
    monkeypatch.setattr(ak, "matches_static", lambda k: k == "статический")
    assert s._automation_key_channel("статический") == "static"


def test_a_tool_call_with_a_key_survives_the_outage(monkeypatch,
                                                    _window_store_down):
    """Сквозная приёмка: вызов инструмента с непустым (и не совпавшим) ключом
    при лежащей базе НЕ рвётся исключением — уходит обычным интерактивным
    путём (превью плана), по-русски и без трейсбека."""
    monkeypatch.setattr(s, "SECRET", None)
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(
        s, "_open_by_id",
        lambda fresh=False: {"t1": {"id": "t1", "title": "Мусор",
                                    "projectId": "p1"}})
    before = dict(consent._MANIFESTS)
    try:
        out = asyncio.run(s.delete_tasks.direct(
            "⚠️ Удаляю «Мусор»",
            [{"taskId": "t1", "title": "Мусор", "projectId": "p1"}],
            automation_key="ключ-которому-нужна-база"))
    finally:
        consent._MANIFESTS.clear()
        consent._MANIFESTS.update(before)
    assert "Манифест `" in out, \
        "ожидался обычный интерактивный путь (план), а не ошибка"
    assert "Traceback" not in out and "OperationalError" not in out
