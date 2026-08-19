"""QA 2026-08-19, бага №2 (живая приёмка): `rename_tag(..., allow_merge=True)`
падал сырой 500-й, когда `new_name` уже существовал.

Живой прогон против TickTick: `rename_tag(old_name="X", new_name="Y",
allow_merge=true)`, когда «Y» уже есть, отвечал буквально
"Error renaming tag: 500 Server Error:  for url: https://api.ticktick.com/
api/v2/tag/rename". Данные целы (TickTick ничего не поменял), но: (а) фича
слияния не работает вовсе — `/tag/rename` не умеет сливать в существующий
тег, а не просто «иногда падает»; (б) даже когда падает, человек получал
сырой текст requests вместо объяснения.

Без живых вызовов к проду (запрещено правилами этой задачи) нельзя ни
подобрать другой payload для настоящего слияния, ни надёжно обойти его
переносом тегов вручную (слепая зона: завершённые задачи не видны через
get_tasks_by_tag/get_open_tasks) — поэтому фикс НЕ пытается притвориться, что
слияние сработало: он ловит отказ API и превращает его в честное объяснение
с конкретными следующими шагами, ничего не подменяя в самом API-вызове (та же
`ticktick_v2.rename_tag(old_name, new_name)`, что и раньше — тест ниже это
явно проверяет по количеству/аргументам вызова)."""
import pytest

import ticktick_mcp.src.server as s


class _FakeV2MergeFails:
    """Тот же двойник, что у test_consent_gate.py::_FakeV2Tags, но
    `rename_tag` честно воспроизводит живое поведение TickTick: 500, когда
    цель уже существует (слияние), обычное успешное переименование иначе."""

    def __init__(self, names):
        self._names = list(names)
        self.rename_calls = []

    def get_state(self, force=True):
        return {}

    def get_tags(self):
        return [{"name": n} for n in self._names]

    def rename_tag(self, old_name, new_name):
        self.rename_calls.append((old_name, new_name))
        if new_name.lower() in self._names:
            raise RuntimeError(
                "500 Server Error:  for url: "
                "https://api.ticktick.com/api/v2/tag/rename")
        self._names = [n for n in self._names if n != old_name.lower()]
        self._names.append(new_name.lower())
        return {}


async def test_merge_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2MergeFails(["a", "b"])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")

    assert "✅" not in result, result
    assert "переименован" not in result.lower() or "🛑" in result, result
    assert "🛑" in result, result


async def test_merge_failure_does_not_leak_the_raw_500_as_the_whole_message(
        monkeypatch):
    """Сырая '500 Server Error' не должна доезжать до человека БЕЗ
    объяснения — она допустима как диагностическая деталь В СКОБКАХ (так уже
    делает `_humanize_api_error`), но не как весь ответ целиком."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2MergeFails(["a", "b"])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")

    assert result.strip() != (
        "Error renaming tag: 500 Server Error:  for url: "
        "https://api.ticktick.com/api/v2/tag/rename"), result
    assert "не выполнено" in result.lower() or "не поддерживает" in result.lower(), result


async def test_merge_failure_explains_the_manual_workaround(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2MergeFails(["a", "b"])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")

    assert "get_tasks_by_tag" in result, result
    assert "set_task_tags" in result, result
    assert "delete_tag" in result, result


async def test_merge_failure_touches_nothing(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2MergeFails(["a", "b"])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    await s.rename_tag("a", "b", allow_merge=True, user_reply="да, сливай")

    assert fake._names == ["a", "b"], (
        f"тег изменился, хотя слияние обязано было отказать: {fake._names}")
    assert fake.rename_calls == [("a", "b")], (
        "API вызывается ровно так же, как и раньше (ничего не подменяли в "
        f"самом вызове): {fake.rename_calls}")


async def test_plain_rename_unaffected_by_the_merge_fix(monkeypatch):
    """Контроль: обычное переименование (цель не существует) по-прежнему
    работает как раньше — фикс не тронул success-путь."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2MergeFails(["старый"])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = await s.rename_tag("старый", "новый")

    assert "переименован" in result, result
    assert "новый" in fake._names, fake._names
