"""Реестр исполнителей кнопки переживает разнос главного файла (2026-08-09).

Пункт 1.2.4 захода 1 увёл исполнение по кнопке из `server.py` в отдельный
модуль. У этого переноса две молчаливые поломки — обе без единой строки в
журнале, обе с одинаковым симптомом «нажал кнопку, ничего не произошло»:

1. Поиск исполнителя шёл через `globals().get(f"_{tool}_impl")`. Пока код
   лежал в одном файле с тридцатью двумя функциями `_<тул>_impl`, это
   работало. После переезда `globals()` — словарь ВЫНЕСЕННОГО модуля, где
   ни одной такой функции нет, и generic-путь вернул бы None для каждой из
   30 команд, проходящих через `_gate_single`/`_gate_batch`.
2. Реестр `_AUTO_EXECUTORS` заполняется четырьмя вызовами
   `_register_auto_executor` при загрузке модуля. Если вынесенный модуль
   загрузится позже, чем кто-то первый раз спросит реестр, реестр окажется
   пустым — и кандидат просто не найдётся.

Обе проверяются здесь, на настоящем коде, а не на его пересказе.
"""
import asyncio

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_auto_execute as t


def test_registry_is_full_after_import():
    """Сразу после импорта `server` реестр исполнителей полон: четыре записи,
    столько же, сколько было до разноса. Пустой реестр — это «кнопка нажата,
    ничего не произошло», причём совершенно молча."""
    assert len(t._AUTO_EXECUTORS) == 4, (
        f"в реестре {len(t._AUTO_EXECUTORS)} записей вместо четырёх: "
        f"{sorted(t._AUTO_EXECUTORS)} — модуль кнопки загружен не полностью "
        "или регистрация потерялась при переносе")
    assert set(t._AUTO_EXECUTORS) == {"delete_tasks", "create_tasks",
                                      "delete_project", "rename_tag"}
    # Реестр — ОДИН объект на оба имени, а не копия: иначе регистрация с одной
    # стороны не видна с другой.
    assert s._AUTO_EXECUTORS is t._AUTO_EXECUTORS


def test_generic_executor_finds_impl_after_split():
    """Generic-исполнитель находит `_<тул>_impl` ПОСЛЕ того, как код кнопки
    уехал из `server.py`, — то есть ищет в пространстве имён главного файла,
    а не в своём собственном.

    Тест написан так, чтобы падать именно на возврате к `globals()`: он
    подменяет `_update_tasks_impl` на модуле `server` (там, где эта функция
    живёт) и требует, чтобы позвали именно подменённое."""
    called = {}

    async def fake_impl(summary, tasks, **extra):
        called["summary"] = summary
        called["tasks"] = tasks
        called["extra"] = extra
        return "✅ подменённый исполнитель отработал"

    real = s._update_tasks_impl
    s._update_tasks_impl = fake_impl
    try:
        out = asyncio.run(t._generic_gate_auto_execute("delete 1", {
            "tool": "update_tasks",
            "_gate": "batch",
            "summary": "сводка",
            "tasks": [{"task_id": "t1", "project_id": "p1"}],
            "extra": {},
        }))
    finally:
        s._update_tasks_impl = real

    assert out == "✅ подменённый исполнитель отработал"
    assert called["summary"] == "сводка"
    assert called["tasks"] == [{"task_id": "t1", "project_id": "p1"}]


def test_generic_executor_finds_single_gate_impl_after_split():
    """То же для одиночного гейта (`_gate_single`): у него другая форма
    вызова — параметры разворачиваются из `params`."""
    seen = {}

    async def fake_impl(**params):
        seen.update(params)
        return "✅ одиночный исполнитель отработал"

    real = s._rename_tag_impl
    s._rename_tag_impl = fake_impl
    try:
        out = asyncio.run(t._generic_gate_auto_execute("rename 1", {
            "tool": "rename_tag",
            "_gate": "single",
            "params": {"old_name": "старый", "new_name": "новый"},
        }))
    finally:
        s._rename_tag_impl = real

    assert out == "✅ одиночный исполнитель отработал"
    assert seen == {"old_name": "старый", "new_name": "новый"}


def test_resolver_sees_generic_impl_for_a_gated_tool():
    """`_resolve_auto_executor` подхватывает generic-путь для гейтованной
    команды, у которой нет своей записи в реестре, — именно эта развилка и
    молчала бы после переноса, если бы поиск остался на `globals()`."""
    entry = t._resolve_auto_executor("update_tasks", {"_gate": "batch"})
    assert entry is not None, (
        "generic-исполнитель не найден для update_tasks — поиск "
        "`_<тул>_impl` смотрит не в тот модуль")
    assert entry is t._GENERIC_GATE_ENTRY


def test_missing_impl_still_refuses_loudly():
    """Обратная сторона: если исполнителя действительно нет, путь не
    притворяется успешным, а падает с внятным текстом."""
    with pytest.raises(RuntimeError, match="нет исполнителя _нетакого_impl"):
        asyncio.run(t._generic_gate_auto_execute("delete 1", {
            "tool": "нетакого", "_gate": "batch",
            "summary": "", "tasks": [],
        }))
