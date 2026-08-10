"""П6 (2026-08-09): исполняющая часть обязана отказывать, если у манифеста
записан тул, который в MCP-сервере СЕЙЧАС не зарегистрирован.

Баг: `_resolve_auto_executor` отдавал исполнителя любому манифесту, у
которого нашлась подходящая метка — explicit-запись в `_AUTO_EXECUTORS`, либо
функция `_<tool>_impl` в globals() для generic `_gate_batch`/`_gate_single`
пути. Ни то, ни другое не проверяет, жив ли сам `@mcp.tool()` декоратор.
Отключение тула в этом проекте делают комментированием РОВНО ОДНОГО
декоратора (см. plan_declutter/execute_declutter/resume_declutter/
set_declutter_decision в server.py) — сама функция и её `_impl` остаются в
модуле нетронутыми, поэтому старая проверка их по-прежнему находила.
Практический эффект (описан в самом server.py у `_resolve_auto_executor` и
`_register_auto_executor`, 2026-08-04/05): непогашенный в базе план +
нажатие кнопки ✅ в Telegram исполнили бы операцию, которую владелец считает
выключенной.

Тесты ниже симулируют ровно это состояние («декоратор снят, всё остальное
живо») через `monkeypatch.delitem` над реальным реестром FastMCP
(`mcp._tool_manager._tools`) — тем же реестром, которым пользуется
tests/test_tool_registry.py, — а не выдуманным двойником.
"""
import asyncio
import time

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


def _undeclare_tool(monkeypatch, name: str) -> None:
    """Снимает регистрацию `@mcp.tool()` для `name`, оставляя саму функцию и
    её `_impl`/запись в `_AUTO_EXECUTORS` нетронутыми — точная симуляция
    «закомментировали декоратор, забыли про остальное». `monkeypatch.delitem`
    сам восстановит запись по завершении теста."""
    tools = s.mcp._tool_manager._tools
    assert name in tools, f"{name} должен быть живым @mcp.tool() на старте теста"
    monkeypatch.delitem(tools, name)


def _registered_tool_names() -> set:
    return {t.name for t in asyncio.run(s.mcp.list_tools())}


# ───────────────────── explicit-регистрация (_AUTO_EXECUTORS) ─────────────

def test_explicit_registry_tool_refuses_when_decorator_disabled(monkeypatch):
    """create_tasks сегодня зарегистрирован явно через
    `_register_auto_executor`. Убираем ТОЛЬКО регистрацию `@mcp.tool()` (как
    будто декоратор закомментировали, а про `_register_auto_executor` в
    16053-й строке забыли) — исполнение обязано отказать, а не пройти."""
    real_entry = s._AUTO_EXECUTORS["create_tasks"]
    _undeclare_tool(monkeypatch, "create_tasks")

    entry = s._resolve_auto_executor("create_tasks", {})

    assert entry is not None, (
        "манифест не должен молча выпасть из очереди без объяснения — "
        "раньше это был бы None и тихий continue")
    assert entry is not real_entry, "не должен исполнять настоящую мутацию"
    assert entry.rehash is real_entry.rehash, (
        "привязка к показанному плану (хэш) не должна ломаться отказом — "
        "иначе try_auto_execute решит, что план 'уплыл', и снова смолчит")

    out = asyncio.run(entry.execute("mid-explicit", {}))
    assert out.startswith("🛑")
    assert "create_tasks" in out
    assert "отсутств" in out.lower()


# ───────────────────── generic _gate_batch/_gate_single путь ──────────────

def test_generic_gate_tool_refuses_when_decorator_disabled(monkeypatch):
    """apply_task_changes не в `_AUTO_EXECUTORS` — резолвится generic-путём по
    наличию `_apply_task_changes_impl` в globals(). Тот же сценарий: тул снят
    из реестра FastMCP, `_impl` остался — отказ, а не тихий пропуск.

    2026-08-10: раньше здесь стояло старое имя `manual_triage`. После §1.3.4
    оно псевдоним, и снятие ОДНОГО его декоратора уже не делает инструмент
    недоступным — реестр честно находит каноническое имя (см.
    `_tool_registration_status`). Проверять «декоратор сняли» надо на том
    имени, за которым стоит сама функция, иначе тест доказывал бы обратное
    тому, что называется в его заголовке."""
    _undeclare_tool(monkeypatch, "apply_task_changes")
    m = {"_gate": "batch", "tool": "apply_task_changes"}

    entry = s._resolve_auto_executor("apply_task_changes", m)

    assert entry is not None
    assert entry is not s._GENERIC_GATE_ENTRY
    assert entry.rehash is s._GENERIC_GATE_ENTRY.rehash

    out = asyncio.run(entry.execute("mid-generic", m))
    assert out.startswith("🛑")
    assert "apply_task_changes" in out
    assert "отсутств" in out.lower()


# ───────────────────── прямая регрессия по исходному инциденту ────────────

def test_declutter_stays_refused_even_if_registration_forgotten(monkeypatch):
    """Исходный инцидент, слово в слово (см. комментарий у `server.py`
    ~16064-16070, рядом с `_register_auto_executor`): «закомментировали
    только декоратор (первый проход), регистрацию в `_AUTO_EXECUTORS`
    забыли снять» — нажатие кнопки в Telegram исполнило бы declutter
    взаправду. Симулируем ровно эту забывчивость: временно (через
    monkeypatch.setitem — не raising=False, запись снимается автоматически)
    возвращаем execute_declutter в `_AUTO_EXECUTORS` и проверяем, что
    решает регистрация в FastMCP, а не наличие записи в реестре."""
    assert "execute_declutter" not in _registered_tool_names()
    assert "execute_declutter" not in s._AUTO_EXECUTORS

    async def _would_really_mutate(manifest_id, m):
        return "✅ настоящая мутация declutter выполнена"  # не должно случиться

    fake_entry = s._AutoExecutorEntry(lambda m: "fake-hash", _would_really_mutate)
    monkeypatch.setitem(s._AUTO_EXECUTORS, "execute_declutter", fake_entry)

    entry = s._resolve_auto_executor("execute_declutter", {})

    assert entry is not None
    assert entry is not fake_entry, "не должен исполнять забытую регистрацию"
    out = asyncio.run(entry.execute("mid-declutter", {}))
    assert out.startswith("🛑")
    assert "execute_declutter" in out
    assert "отсутств" in out.lower()


# ───────────────────── регресс: живые тулы работают как раньше ────────────

def test_registered_tools_resolve_exactly_as_before():
    """Обязательная страховка от собственной поломки: НИЧЕГО не должно
    измениться для тулов, которые реально зарегистрированы — тот же объект
    исполнителя (identity), не обёртка-отказ."""
    for tool in ("create_tasks", "delete_tasks", "delete_project", "rename_tag"):
        assert tool in s._AUTO_EXECUTORS, f"{tool} должен быть в _AUTO_EXECUTORS"
        assert s._resolve_auto_executor(tool, {}) is s._AUTO_EXECUTORS[tool]

    m = {"_gate": "batch", "tool": "manual_triage"}
    assert s._resolve_auto_executor("manual_triage", m) is s._GENERIC_GATE_ENTRY


def test_unregistered_and_unrecognized_tool_still_returns_none():
    """Имя, которого нет НИ в `_AUTO_EXECUTORS`, ни как `_<tool>_impl` —
    поведение не меняется: как и раньше, просто None (это не «отключённый
    тул», а «этот вид манифеста никогда не исполнялся кнопкой»)."""
    assert s._resolve_auto_executor("this_tool_never_existed", {}) is None
    assert s._resolve_auto_executor("", {}) is None


# ─────────── независимый аудит (2026-08-09): сама проверка регистрации ────
# может упасть — fail-closed, а не пробросить исключение наружу ────────────

class _BrokenToolManager:
    """Двойник `mcp._tool_manager`, чей `get_tool` падает — ровно то, что
    случилось бы при несовместимом обновлении FastMCP: `_tool_manager` —
    приватный, ничем не гарантированный атрибут библиотеки. Первая версия
    этого фикса (376b3aa) звала `mcp._tool_manager.get_tool(...)` БЕЗ
    try/except внутри `_resolve_auto_executor`, а тот в свою очередь вызывался
    из `_auto_executable_tool` → `_tg_auto_execute_pending`, которая внутри
    `_tg_auto_execute_tick` идёт БЕЗ обёртки — одно упавшее исключение на
    ОДНОМ манифесте останавливало разбор ВСЕХ кандидатов в этом тике,
    включая полностью рабочие, зарегистрированные тулы. Внешний
    `_tg_auto_execute_poller_loop` процесс не ронял, но следующий тик через
    10 секунд повторял тот же провал — автоисполнение вставало целиком,
    хотя до этой правки такого дефекта не было вовсе (`_resolve_auto_executor`
    состоял только из `dict.get`/`globals().get`, бросить не мог)."""

    def get_tool(self, name):
        raise RuntimeError("реестр инструментов недоступен (симуляция сбоя)")


def test_registry_check_failure_refuses_instead_of_raising(monkeypatch):
    """Главная регрессия из аудита: падение самой проверки регистрации не
    должно вылетать из `_resolve_auto_executor` наружу — иначе именно тот
    необёрнутый вызов из `_tg_auto_execute_pending` стопорит цикл поиска
    кандидатов целиком. Вместо этого — исполнитель-отказник, как и для
    честно отключённого тула."""
    real_entry = s._AUTO_EXECUTORS["create_tasks"]
    monkeypatch.setattr(s.mcp, "_tool_manager", _BrokenToolManager())

    entry = s._resolve_auto_executor("create_tasks", {})  # не должно бросить

    assert entry is not None
    assert entry is not real_entry
    assert entry.rehash is real_entry.rehash

    out = asyncio.run(entry.execute("mid-broken-registry", {}))
    assert out.startswith("🛑")
    assert "create_tasks" in out


def test_registry_check_failure_message_differs_from_plain_disabled(monkeypatch):
    """Текст отказа обязан различать «тул отключён» (решение владельца) и
    «проверка упала» (поломка сервера) — иначе владелец, увидев отказ,
    решит, что сам когда-то выключил этот тул, хотя на деле сломался
    внутренний вызов к приватному API FastMCP."""
    monkeypatch.setattr(s.mcp, "_tool_manager", _BrokenToolManager())

    entry = s._resolve_auto_executor("create_tasks", {})
    out = asyncio.run(entry.execute("mid-broken-registry-2", {}))

    assert "провер" in out.lower(), "текст должен называть причину — сбой проверки"
    assert "отключён" not in out and "удалён" not in out, (
        "формулировка «отключён/удалён» подразумевает решение владельца — "
        "для сбоя самой проверки регистрации это вводящий в заблуждение текст")


def test_registry_check_failure_is_logged_with_traceback(monkeypatch, caplog):
    """`logger.exception` обязателен: без записи с трассировкой причина
    («реестр внезапно недоступен») не видна нигде, кроме гадания по
    симптомам («кнопка перестала работать»)."""
    monkeypatch.setattr(s.mcp, "_tool_manager", _BrokenToolManager())

    with caplog.at_level("ERROR"):
        s._resolve_auto_executor("create_tasks", {})

    assert "create_tasks" in caplog.text
    assert any(rec.exc_info for rec in caplog.records), (
        "ожидал запись с трассировкой (logger.exception), а не голый logger.error")


def test_registered_tools_survive_a_broken_registry_check_for_other_tools():
    """Регресс-страховка на будущее: пока реестр НЕ сломан, поведение теста
    выше вообще не должно быть нужно — обычный живой тул как резолвился
    напрямую (identity), так и резолвится."""
    assert s._resolve_auto_executor("create_tasks", {}) is s._AUTO_EXECUTORS["create_tasks"]


# ───── СТРУКТУРНАЯ ЛОВУШКА (независимый аудит, 2026-08-09): call site, не функция ─────

class _FakeV2DeleteForTick:
    """Двойник тех же методов ticktick_v2, которыми пользуется исполнитель
    delete_tasks и candidate-discovery поллера (get_open_tasks/invalidate_cache
    зовутся по пути, даже когда до реального удаления не доходит)."""

    def __init__(self, live):
        self.live = live

    def get_state(self, force=False):
        pass

    def get_open_tasks(self):
        return list(self.live.values())

    def batch_delete_tasks(self, items):
        for it in items:
            self.live.pop(it["taskId"], None)
        return {}

    def invalidate_cache(self):
        pass


def test_disabled_tool_refuses_through_the_real_poller_tick_not_only_the_resolver(
        monkeypatch):
    """Все восемь тестов выше дёргают `_resolve_auto_executor` НАПРЯМУЮ — они
    убедительно доказывают, что сама функция честная, но ничего не говорят о
    месте, где её реально вызывают в бою: `_tg_auto_execute_tick` (нажатие
    кнопки ✅ в Telegram). Замена ТОЛЬКО этого call site на прямой поиск в
    `_AUTO_EXECUTORS`/generic-словаре в обход `_resolve_auto_executor` не
    трогает саму функцию — весь набор из восьми тестов выше остался бы
    зелёным (проверено: с такой подменой на call site'е они все проходят), а
    кнопка в проде исполнила бы операцию, которую владелец считает
    выключенной — ровно тот сценарий, который вся эта проверка существует
    предотвратить.

    Гоняем НАСТОЯЩИЙ `_tg_auto_execute_tick()` целиком (как в
    tests/test_tg_auto_execute.py), не переигрываем его шаги вручную —
    Postgres и Telegram подменены, реестр FastMCP настоящий."""
    _undeclare_tool(monkeypatch, "delete_tasks")

    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))

    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    monkeypatch.setattr(s, "ticktick_v2", _FakeV2DeleteForTick(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})

    mid = "mid-undeclared-real-tick"
    s._MANIFESTS[mid] = {
        "kind": "delete", "consumed": False, "created": time.monotonic(),
        "object_hash": s._manifest_object_hash("delete", ["t1"]),
        "summary": "test", "items": [{
            "taskId": "t1", "projectId": "p1", "title": "Купить молоко",
            "project": "Покупки", "snapshot": {},
        }],
    }

    def _fake_approvals(mids, lost_scan_since_ms=None):
        return {m: {"status": "APPROVED", "expires_at": tg._now_ms() + 3_600_000,
                    "chat_id": "c1", "message_id": 7, "extra_message_ids": [],
                    "created_at": tg._now_ms(), "decided_at": None,
                    "report_message_ids": [], "lost_notified_at": None}
                for m in mids if m == mid}
    monkeypatch.setattr(tg, "get_tg_approvals", _fake_approvals)

    group = []
    monkeypatch.setattr(
        tg, "post_report_to_group",
        lambda cfg, manifest_id, report_md, *, tool, verdict:
            group.append((manifest_id, tool, verdict, report_md))
            or tg.ReportDelivery([1001], 1, 1, True))
    monkeypatch.setattr(tg, "summarize_in_owner_chat",
                        lambda cfg, chat_id, message_id, short_md: True)

    asyncio.run(s._tg_auto_execute_tick())

    assert "t1" in live, (
        "боевой путь исполнил удаление тулом, снятым из реестра FastMCP — "
        "_resolve_auto_executor обойдён на call site в _tg_auto_execute_tick")
    assert group, "об исходе не опубликовано ни одного отчёта"
    _pub_mid, _tool, verdict, report_md = group[0]
    assert verdict != "ok"
    assert "🛑" in report_md and "отсутств" in report_md.lower()
