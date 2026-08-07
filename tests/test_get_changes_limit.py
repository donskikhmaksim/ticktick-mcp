"""get_changes: лента изменений режется честно и дочитывается (def-D6).

Корень: у аудит-фида не было ни limit, ни пагинации — соседние
get_completed_tasks/get_trash их имеют, а этот печатал ВСЁ. Реальный вызов на
три дня вернул 461 событие (~56 КБ) и упёрся в лимит токенов при чтении: фид,
который невозможно прочитать, аудитом не является. Тестов на метод не было ни
одного.

Все временные метки фикстур считаются от datetime.now(timezone.utc) — метод
берёт «сегодня» с реальных часов, и календарная константа тут молча протухла
бы через N дней (правило репозитория).
"""
from datetime import datetime, timedelta, timezone

import pytest

import ticktick_mcp.src.server as s


def _stamp(hours_ago: int) -> str:
    """ISO-метка «столько-то часов назад» — от реальных часов, не от даты."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.000+0000")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class FakeV2:
    def __init__(self, open_tasks=None, completed=None, trash=None):
        self._open = open_tasks or []
        self._completed = completed or []
        self._trash = trash or []

    def get_open_tasks(self):
        return self._open

    def get_completed_tasks(self, limit=100, from_str=None, to_str=None):
        return self._completed[:limit]

    def get_trash(self, limit=300):
        return self._trash[:limit]

    def get_state(self, force=False):
        return {"projectProfiles": [{"id": "p1", "name": "Проект"}], "inboxId": "inbox1"}


def _made(n: int, hours_ago: int = 1):
    return [{"id": f"t{i}", "title": f"Событие {i}", "projectId": "p1",
             "createdTime": _stamp(hours_ago), "modifiedTime": _stamp(hours_ago)}
            for i in range(n)]


@pytest.fixture
def _many(monkeypatch):
    monkeypatch.setattr(s, "ticktick", object())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(open_tasks=_made(150)))


async def test_default_call_is_capped_and_says_how_to_continue(_many):
    out = await s.get_changes(since=_today())

    body = [ln for ln in out.splitlines() if "Событие" in ln]
    assert len(body) == 100, f"лента не обрезана: {len(body)} строк"
    assert "150" in out            # общее число событий названо
    assert "offset=100" in out     # и сказано, чем дочитать


async def test_limit_is_respected(_many):
    out = await s.get_changes(since=_today(), limit=5)

    body = [ln for ln in out.splitlines() if "Событие" in ln]
    assert len(body) == 5


async def test_offset_reaches_the_tail(_many):
    first = await s.get_changes(since=_today(), limit=10)
    second = await s.get_changes(since=_today(), limit=10, offset=10)

    firsts = {ln for ln in first.splitlines() if "Событие" in ln}
    seconds = {ln for ln in second.splitlines() if "Событие" in ln}
    assert len(seconds) == 10
    assert not (firsts & seconds), "вторая страница повторяет первую"


async def test_source_cap_is_disclosed(monkeypatch):
    # Завершённые приходят из API пачкой не больше 100 — когда их ровно
    # столько, лента заведомо неполна, и это должно быть сказано вслух.
    completed = [{"id": f"c{i}", "title": f"Готово {i}", "projectId": "p1",
                  "completedTime": _stamp(2)} for i in range(100)]
    monkeypatch.setattr(s, "ticktick", object())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(completed=completed))

    out = await s.get_changes(since=_today(), limit=200)

    # Все 100 событий влезли в страницу — значит про неполноту может сказать
    # только пометка о потолке ИСТОЧНИКА, и она обязана быть.
    assert "потолок" in out.lower(), f"молчание о капе источника: {out[-400:]!r}"
    cap_line = next(ln for ln in out.splitlines() if "потолок" in ln.lower())
    assert "аверш" in cap_line  # названо, какой именно источник упёрся
    assert "100" in cap_line


async def test_empty_range_still_answers_plainly(monkeypatch):
    monkeypatch.setattr(s, "ticktick", object())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())

    out = await s.get_changes(since=_today())

    assert "не найдено" in out
