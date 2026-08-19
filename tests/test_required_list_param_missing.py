"""Обязательный списочный параметр НЕ ПЕРЕДАН ВОВСЕ ≠ передан пустым
(QA-2 2026-08-19, добор №7).

Живой случай: вызов `delete_tags(names=[...], summary=...)` — опечатка в
имени параметра (правильное — `tags`) — получал «Пустой список — нечего
делать» и уходил уверенным, что теги удалены. Тихий no-op — худший ответ на
опечатку. Теперь `tags is None` (параметр не передан) — явный отказ с именем
параметра; настоящий `[]` остаётся законным «нечего делать». Тот же класс
закрыт у соседнего batch-инструмента с Optional-списком — `delete_tasks`
(`tasks=None` отвечал «Нечего удалять: список пуст»); `apply_task_changes`
на operations=None и раньше отказывал громко («Пустой список операций» с
🛑), а списки plan_task_creation/plan_task_deletion обязательны на уровне
сигнатуры — там опечатка в имени параметра падает валидацией MCP.
"""
import pytest

import ticktick_mcp.src.server as s


@pytest.fixture(autouse=True)
def _ready(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)


async def test_delete_tags_without_tags_param_is_a_loud_refusal():
    out = await s.delete_tags("Удаляю мусорные теги")
    assert "🛑" in out, out
    assert "`tags` обязателен" in out, out
    assert "нечего делать" not in out.lower(), (
        "тихий no-op на опечатку в имени параметра вернулся: " + out)


async def test_delete_tags_genuinely_empty_list_is_still_nothing_to_do(monkeypatch):
    out = await s.delete_tags("Удаляю", tags=[])
    assert out == "Пустой список — нечего делать."


async def test_delete_tasks_without_tasks_param_is_a_loud_refusal():
    out = await s.delete_tasks.direct("⚠️ Удаляю задачи")
    assert "🛑" in out, out
    assert "`tasks` обязателен" in out, out
    assert "список пуст" not in out.lower(), out


async def test_delete_tasks_genuinely_empty_list_keeps_old_answer():
    out = await s.delete_tasks.direct("⚠️ Удаляю задачи", tasks=[])
    assert out == "Нечего удалять: список пуст."
