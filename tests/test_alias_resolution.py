"""ПСЕВДОНИМ СТАРОГО ИМЕНИ разрешается во ВСЕХ ПЯТИ местах (2026-08-10,
docs/TZ/ZAHOD1.md §1.3.4 В).

`manual_triage` переименован в `apply_task_changes`. Старое имя осталось
рабочим: его знает внешний вызывающий (n8n, телеграм-бот), его несёт манифест,
построенный ДО выката и лежащий в Postgres, и его помнит модель, у которой
список инструментов закэширован на сессию.

Почему на каждое место отдельный тест, а не один сквозной. Каждое непокрытое
место отваливается ТИХО — не с ошибкой, а исчезновением поведения:

  1. `TG_APPROVAL_TOOLS` — список ИМЁН инструментов. Разъехавшееся имя не даёт
     ни ошибки, ни предупреждения: план просто перестаёт уходить в Telegram,
     текстовый путь остаётся живым, второй фактор подтверждения исчезает.
  2. `_AUTO_EXECUTORS` / `_AUTO_EXECUTE_TOOL_FOR_KIND` — ключ везде имя
     инструмента; не найдя записи, поллер молча пропускает кандидата.
  3. Поиск `_<tool>_impl` в `_generic_gate_auto_execute` — падает уже ПОСЛЕ
     нажатия кнопки владельцем.
  4. `_tool_registration_status` — манифест со старым именем вернул бы
     `disabled`, и владелец, нажав кнопку на плане, который только что видел,
     получил бы «инструмент отсутствует».
  5. Манифест из базы (`_rehydrate_manifest`) — план, показанный до
     перезапуска и подтверждённый после, обязан найти исполнителя.

Ни сети, ни Telegram: место 5 работает на настоящем Postgres (иначе оно
проверяло бы двойник, а не подъём плана из базы), остальные — чистые вызовы.
"""
import asyncio

import pytest

import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg
import ticktick_mcp.src.tg_auto_execute as tg_exec

from tests.pg_helper import fresh_store, requires_pg


OLD, NEW = "manual_triage", "apply_task_changes"


# ───────────────── место 1: TG_APPROVAL_TOOLS ─────────────────

def test_tg_approval_tools_accepts_old_name_and_refuses_unknown(monkeypatch):
    """Старое имя в переменной окружения канонизируется (кнопка продолжает
    работать), а НЕИЗВЕСТНОЕ имя роняет старт с внятным сообщением — вместо
    молчаливой деградации до работы без кнопки."""
    monkeypatch.setenv("TG_APPROVAL_TOOLS", f"{OLD}, delete_tasks")
    cfg = tg.load_tg_approval_config()
    assert cfg.tools_allowlist == {OLD, "delete_tasks"}, (
        "разбор переменной окружения сам по себе ничего не канонизирует — "
        "это делает сверка с реестром на старте")

    s._canonicalize_tg_approval_tools(cfg)

    assert cfg.tools_allowlist == {NEW, "delete_tasks"}, (
        "старое имя обязано превратиться в каноническое: иначе план "
        "агрегатора молча перестанет уходить в Telegram")

    monkeypatch.setenv("TG_APPROVAL_TOOLS", "manual_triage_typo")
    broken = tg.load_tg_approval_config()
    with pytest.raises(RuntimeError) as e:
        s._canonicalize_tg_approval_tools(broken)
    assert "manual_triage_typo" in str(e.value), (
        "сообщение обязано НАЗВАТЬ неизвестное имя — иначе оператор ищет "
        "опечатку глазами по всему списку")


# ───────────────── место 2: _AUTO_EXECUTORS / _AUTO_EXECUTE_TOOL_FOR_KIND ──

def test_auto_executor_registry_is_reached_through_the_old_name(monkeypatch):
    """Запись в `_AUTO_EXECUTORS`, заведённая под НОВЫМ именем, обязана
    находиться по старому — ключ этого словаря и есть имя инструмента.

    Заодно проверяется вторая половина того же места: `_auto_execute_tool_of`
    отдаёт КАНОНИЧЕСКОЕ имя. Именно оно сверяется с `TG_APPROVAL_TOOLS` и
    печатается владельцу, поэтому манифест со старым именем обязан пройти
    allowlist, где стоит уже новое."""
    entry = s._AutoExecutorEntry(lambda m: "hash", lambda mid, m: None)
    monkeypatch.setitem(s._AUTO_EXECUTORS, NEW, entry)

    assert s._resolve_auto_executor(OLD, {}) is entry, (
        "старое имя не нашло исполнителя, заведённого под новым — кандидат "
        "молча выпадет из очереди поллера")

    assert s._auto_execute_tool_of({"tool": OLD, "_gate": "batch"}) == NEW
    assert s._auto_execute_tool_of({"_tg_tool": OLD}) == NEW
    # Запасная ветка того же соответствия (kind → имя инструмента) резолв тоже
    # проходит: она отдаёт имя из `_AUTO_EXECUTE_TOOL_FOR_KIND`, и если туда
    # когда-нибудь попадёт устаревшее имя, канонизация его подхватит.
    assert s._auto_execute_tool_of({"kind": "delete"}) == "delete_tasks"


# ───────────────── место 3: поиск `_<tool>_impl` ─────────────────

def test_old_manifest_finds_impl(monkeypatch):
    """Манифест со старым именем `tool` обязан найти `_apply_task_changes_impl`.

    Разрешение псевдонима стоит ДО поиска в пространстве имён — уберите его, и
    `_generic_gate_auto_execute` бросит «нет исполнителя _manual_triage_impl»
    уже ПОСЛЕ того, как владелец нажал кнопку."""
    seen = {}

    async def _impl(summary, tasks, **extra):
        seen["args"] = (summary, tasks, extra)
        return "### отчёт-заглушка"

    monkeypatch.setattr(s, "_apply_task_changes_impl", _impl)
    m = {"_gate": "batch", "tool": OLD, "summary": "Разбираю",
         "tasks": [{"op": "complete", "task_id": "t1"}]}

    out = asyncio.run(s._generic_gate_auto_execute("mid-old-name", m))

    assert out == "### отчёт-заглушка"
    assert seen["args"][0] == "Разбираю"
    assert seen["args"][1] == m["tasks"]


# ───────────────── место 4: _tool_registration_status ─────────────────

def test_registration_status_of_the_old_name_is_not_disabled(monkeypatch):
    """Манифест со старым именем не должен читаться как «инструмент отключён».

    Проверяется ОБА состояния: пока обёртка `manual_triage` зарегистрирована,
    ответ приходит по имени как есть; когда её однажды снимут (условие снятия
    псевдонима — ноль вызовов в логах), ответ обязан прийти через каноническое
    имя, а не превратиться в отказ на кнопке, которую владелец только что
    видел."""
    assert tg_exec._tool_registration_status(OLD) == "registered"

    monkeypatch.delitem(s.mcp._tool_manager._tools, OLD)
    assert tg_exec._tool_registration_status(OLD) == "registered", (
        "после снятия обёртки старое имя обязано резолвиться в каноническое — "
        "иначе висящий план получит «инструмент отсутствует»")

    assert tg_exec._tool_registration_status("this_tool_never_existed") == "disabled", (
        "резолв псевдонима не должен превращать любое незнакомое имя в живое")


# ───────────────── место 5: манифест из базы после перезапуска ─────────────

@requires_pg
def test_manifest_from_db_with_the_old_tool_name_executes(monkeypatch):
    """План построен ДО выката (в базе лежит `tool` = "manual_triage"), процесс
    перезапущен, владелец подтверждает — обязано исполниться.

    «Перезапуск» здесь — пустая `_MANIFESTS` при живой строке в Postgres: это
    ровно то состояние, в котором свежий процесс встречает вчерашний план
    (настоящий fork процесса проверяет tests/test_manifest_restart.py, здесь
    важна не перезагрузка кода, а подъём плана из базы под старым именем)."""
    store = fresh_store()
    before = dict(s._MANIFESTS)
    s._MANIFESTS.clear()
    try:
        mid = "aliasmid00001"
        payload = consent._durable_payload({
            "kind": "manual_triage", "tool": OLD, "_gate": "batch",
            "tasks": [{"op": "complete", "task_id": "t1", "title": "A"}],
            "summary": "Разбираю вчерашнее", "consumed": False,
            "object_hash": "irrelevant-here", "extra": {},
        })
        assert store.save(mid, OLD, payload,
                          created_at_ms=payload["_created_ms"],
                          expires_at_ms=payload["_created_ms"] + 3_600_000)

        asyncio.run(consent._rehydrate_manifest(mid))
        m = s._MANIFESTS.get(mid)
        assert m is not None, "план не поднялся из базы"
        assert m.get("tool") == OLD, (
            "подъём из базы обязан отдавать поле `tool` КАК ЕСТЬ — "
            "переписывать чужую запись задним числом мы не имеем права")

        called = {}

        async def _impl(summary, tasks, **extra):
            called["ok"] = (summary, tasks)
            return "### отчёт-заглушка"

        monkeypatch.setattr(s, "_apply_task_changes_impl", _impl)
        out = asyncio.run(s._generic_gate_auto_execute(mid, m))

        assert out == "### отчёт-заглушка"
        assert called["ok"][0] == "Разбираю вчерашнее"
    finally:
        s._MANIFESTS.clear()
        s._MANIFESTS.update(before)
