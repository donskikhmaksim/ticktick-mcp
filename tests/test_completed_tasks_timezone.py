"""2026-08-19 (QA-2, воспроизведено вживую на проде): get_completed_tasks()
не показывал только что завершённую задачу — владелец закрыл задачу, тут же
позвал get_completed_tasks(limit=10) и получил десять ДАВНИХ задач без неё
(и спустя 40 минут её всё ещё не было).

Корень: сервер TickTick сравнивает границы from/to ленты
/project/all/completed с completedTime **в UTC**, а верхняя граница по
умолчанию собиралась как datetime.now(_USER_TZ) — «фикс» от 2026-08-09
(П9 пакет ТЗ, пункт 7), который сам оказался багом: строка «сейчас по LA»
читается сервером как «UTC семь часов назад», и всё, завершённое за
последние ~7 часов, вырезается из «recently completed». (Живое
доказательство UTC-трактовки: get_changes с to="…23:59:59" ту же задачу
видел, get_completed_tasks с to="сейчас по LA" — нет.) До 2026-08-09 голый
datetime.now() был верен лишь случайно — процесс на Railway живёт в UTC.

Прежняя версия этого файла закрепляла тестом именно баг («граница должна
следовать _USER_TZ»); теперь закрепляется правильный инвариант: граница по
умолчанию НЕ зависит от _USER_TZ и никогда не лежит в прошлом относительно
UTC-«сейчас» — задача, завершённая секунду назад, обязана проходить фильтр
при ЛЮБОМ часовом поясе владельца.

Второй зафиксированный дефект того же вызова: порядок ленты у API не
гарантирован (вживую наблюдался вперемешку по dueDate) — клиент обязан сам
отсортировать по фактическому времени завершения, свежие первыми.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import ticktick_mcp.src.ticktick_v2_client as c

EAST = "Pacific/Kiritimati"  # UTC+14
WEST = "Etc/GMT+12"          # UTC-12


def _make_client(monkeypatch, captured, tasks=None):
    client = c.TickTickV2Client(token="fake-token-for-test")

    def _fake_request(method, path, params=None, **kwargs):
        captured["params"] = params
        return {"tasks": list(tasks or [])}

    monkeypatch.setattr(client, "_request", _fake_request)
    return client


@pytest.mark.parametrize("owner_tz", [EAST, WEST])
def test_default_to_bound_never_cuts_off_a_just_completed_task(monkeypatch, owner_tz):
    """Сервер сравнивает `to` с completedTime в UTC. Значит граница по
    умолчанию обязана быть >= UTC-«сейчас» при любом _USER_TZ — иначе
    задача, завершённая минуту назад (completedTime = UTC-сейчас),
    отфильтровывается, и «recently completed» врёт. Для WEST (UTC-12)
    реализация через _USER_TZ давала границу на 12 часов в прошлом —
    этот параметр падал до фикса."""
    captured = {}
    client = _make_client(monkeypatch, captured)
    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(owner_tz))

    just_completed = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    client.get_completed_tasks()
    got_to = captured["params"]["to"]

    # Формат прежний — сервер его уже принимает.
    datetime.strptime(got_to, "%Y-%m-%d %H:%M:%S")
    # Лексикографическое сравнение строк этого формата = сравнение времён.
    assert got_to >= just_completed, (
        f"верхняя граница {got_to!r} лежит в прошлом относительно UTC-сейчас "
        f"{just_completed!r} (owner_tz={owner_tz}) — свежезавершённая задача "
        f"будет вырезана из ленты")


def test_default_to_bound_does_not_depend_on_user_timezone(monkeypatch):
    """Граница — свойство UTC-часов сервера TickTick, не владельца: смена
    _USER_TZ между вызовами не должна двигать её на часы (допуск — секунды
    реального времени между двумя вызовами)."""
    captured = {}
    client = _make_client(monkeypatch, captured)

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(EAST))
    client.get_completed_tasks()
    got_east = captured["params"]["to"]

    monkeypatch.setattr(c, "_USER_TZ", ZoneInfo(WEST))
    client.get_completed_tasks()
    got_west = captured["params"]["to"]

    delta = abs(datetime.strptime(got_east, "%Y-%m-%d %H:%M:%S")
                - datetime.strptime(got_west, "%Y-%m-%d %H:%M:%S"))
    assert delta < timedelta(minutes=5), (
        f"граница сдвинулась на {delta} от одной лишь смены _USER_TZ — "
        f"значит, она всё ещё собирается в поясе владельца")


def test_explicit_to_str_is_passed_through_untouched(monkeypatch):
    """Явно переданная вызывающим граница не перекрывается дефолтом."""
    captured = {}
    client = _make_client(monkeypatch, captured)
    fixed = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    client.get_completed_tasks(to_str=fixed)

    assert captured["params"]["to"] == fixed


def test_feed_is_sorted_by_completed_time_newest_first(monkeypatch):
    """API отдаёт ленту в негарантированном порядке (вживую — вперемешку по
    dueDate); клиент обязан вернуть её по completedTime, свежие первыми,
    а задачи вовсе без completedTime — в хвосте, не вперемешку."""
    scrambled = [
        {"id": "old", "title": "давняя",
         "completedTime": "2026-08-15T10:00:00.000+0000",
         "dueDate": "2026-08-17T00:00:00.000+0000"},
        {"id": "undated", "title": "без времени завершения"},
        {"id": "fresh", "title": "закрыта минуту назад",
         "completedTime": "2026-08-19T21:00:00.000+0000"},
        {"id": "mid", "title": "вчерашняя",
         "completedTime": "2026-08-18T03:00:00.000+0000",
         "dueDate": "2026-08-15T00:00:00.000+0000"},
    ]
    captured = {}
    client = _make_client(monkeypatch, captured, tasks=scrambled)

    got = [t["id"] for t in client.get_completed_tasks()]

    assert got == ["fresh", "mid", "old", "undated"]
