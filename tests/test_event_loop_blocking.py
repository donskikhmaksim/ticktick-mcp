"""Залипание всего сервера (2026-08-19, ночные замеры на проде): тривиальный
`GET /health` отвечал 29с и 12с подряд, а два MCP-вызова отвалились по
таймауту клиента — процесс замирал ЦЕЛИКОМ, а не «один медленный тул».

Причина — синхронные (blocking) вызовы прямо в event loop'е: десятки
async-инструментов звали `_open_by_id(fresh=True)` (синхронный HTTP
`/batch/check/0` к v2 c timeout=20с и ретраями со сном — до ~63с на вызов)
и `_build_operation_report` (внутри — тот же `_open_by_id` плюс retry-петля
`time.sleep` до ~9с) НАПРЯМУЮ, без `_run_blocking`. Пока такой вызов ждёт
сеть, event loop стоит — вместе с /health, вебхуком и всеми параллельными
MCP-сессиями.

Три уровня защиты:
  1. Поведенческие тесты: во время медленного (имитированного) блокирующего
     вызова параллельная корутина обязана продолжать тикать. До фикса они
     падают (loop заморожен на всю длину sleep), после — проходят.
  2. AST-стражник: ни в одной async-функции четырёх модулей не должно быть
     ПРЯМОГО вызова известного блокирующего примитива (не через
     `_run_blocking`/`asyncio.to_thread`). Ловит и будущие регрессии.
  3. Тест heartbeat-детектора (`_event_loop_lag_watchdog`): залипший loop
     оставляет warning в логе — наблюдаемость, которой не хватало ночью.
"""
import ast
import asyncio
import os
import time

import pytest

import ticktick_mcp.src.server as s


# ---------------------------------------------------------------------------
# Общая механика: probe-корутина, меряющая максимальную паузу между тиками.
# Если event loop заблокирован синхронным вызовом — probe не тикает, и
# максимальный разрыв вырастает до длины блокировки.
# ---------------------------------------------------------------------------

_SLOW_S = 0.5      # сколько «сеть» спит в фейке
_MAX_GAP_S = 0.35  # столько probe может пропустить максимум (запас на шум CI)


async def _run_with_probe(coro_fn):
    """Запускает coro_fn() параллельно с 10мс-probe; возвращает (result,
    максимальный разрыв между тиками probe)."""
    stop = asyncio.Event()
    gaps = []

    async def _probe():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    probe_task = asyncio.create_task(_probe())
    await asyncio.sleep(0.05)  # probe успел начать тикать
    try:
        result = await coro_fn()
    finally:
        stop.set()
        await probe_task
    return result, (max(gaps) if gaps else 0.0)


class _SlowV2:
    """Фейковый v2-клиент, у которого выкачивание состояния «висит на сети»
    (time.sleep — ровно то, что делает настоящий requests, ожидая ответ)."""

    def __init__(self, delay=_SLOW_S):
        self.delay = delay

    def get_tags(self):
        return [{"label": "work", "name": "work"}]

    def get_state(self, force=False):
        time.sleep(self.delay)
        return {}

    def get_open_tasks(self):
        return []


def test_list_tags_does_not_block_event_loop(monkeypatch):
    """list_tags зовёт _open_by_id(fresh=True) — подсчёт тегов-сирот. Пока
    этот вызов ждёт сеть, параллельные корутины (читай: /health, соседние
    MCP-сессии) обязаны обслуживаться."""
    monkeypatch.setattr(s, "ticktick_v2", _SlowV2())
    monkeypatch.setattr(s, "ticktick", object())  # _ensure_ready доволен

    result, max_gap = asyncio.run(_run_with_probe(lambda: s.list_tags()))
    assert "Tags (1)" in result
    assert max_gap < _MAX_GAP_S, (
        f"event loop стоял {max_gap:.2f}с во время list_tags — блокирующий "
        "вызов (_open_by_id) исполняется не в _run_blocking")


def test_operation_report_does_not_block_event_loop(monkeypatch):
    """operation_report зовёт _build_operation_report — внутри настоящего:
    свежий v2-снимок + retry-петля time.sleep до ~9с. Здесь он заменён
    фейком, который просто спит, — важно лишь, ГДЕ его исполняют."""
    def _slow_report(record_id):
        time.sleep(_SLOW_S)
        return f"report for {record_id}"

    monkeypatch.setattr(s, "_build_operation_report", _slow_report)
    monkeypatch.setattr(s, "ticktick", object())
    monkeypatch.setattr(s, "ticktick_v2", _SlowV2(delay=0))

    result, max_gap = asyncio.run(
        _run_with_probe(lambda: s.operation_report("rec-1")))
    assert result == "report for rec-1"
    assert max_gap < _MAX_GAP_S, (
        f"event loop стоял {max_gap:.2f}с во время operation_report — "
        "_build_operation_report исполняется не в _run_blocking")


# ---------------------------------------------------------------------------
# AST-стражник: прямые блокирующие вызовы в async def запрещены.
# ---------------------------------------------------------------------------

_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "ticktick_mcp", "src")
_SCANNED_FILES = ("server.py", "tg_approval.py", "tg_auto_execute.py",
                  "consent.py")

# Синхронные функции сервера, которые спят/ходят в сеть или базу — звать из
# async только через _run_blocking.
_BLOCKING_NAMES = {
    "_open_by_id", "_build_operation_report", "_build_operation_report_data",
    "_reread_open_until", "_reread_projects_until", "_official_task_read",
    "_ensure_ready", "_ensure_official", "initialize_client",
    # automation_key: канал "window" добывается find_window — синхронным
    # psycopg2-запросом к Postgres (connect_timeout до 10с). Из async — только
    # через _run_blocking (см. consent._automation_channel_off_loop).
    "_automation_key_channel", "_automation_key_matches",
    "find_window", "check_window",
}
# Объекты, ЛЮБОЙ метод которых (кроме белого списка чистых) — синхронная
# сеть/БД.
_BLOCKING_OBJS = {"ticktick", "ticktick_v2", "requests", "tg_approval",
                  "manifest_store", "automation_key", "psycopg2"}
# Их методы, которые НЕ блокируют (чистая память/арифметика) — разрешены.
_PURE_ATTRS = {
    "invalidate_cache", "enabled_for", "secret_token_matches",
    "approval_status_of", "store_ready", "resolve_tool_alias",
    "strip_agent_instructions", "split_for_telegram", "md_to_telegram_html",
    "CLAIM_WON", "CLAIM_TAKEN", "CLAIM_ABSENT",
}
_WRAPPERS = {"_run_blocking", "to_thread", "run_in_executor"}


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        base = f.value
        parts = [f.attr]
        while isinstance(base, ast.Attribute):
            parts.append(base.attr)
            base = base.value
        if isinstance(base, ast.Name):
            parts.append(base.id)
        return ".".join(reversed(parts))
    return None


def _scan_async_body(func, path, hits):
    """Обходит тело async-функции, НЕ спускаясь в lambda и вложенные sync
    def (их тела в loop'е напрямую не исполняются — они передаются в
    _run_blocking) и НЕ считая аргументы самих обёрток."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Lambda, ast.FunctionDef)):
            continue
        if isinstance(node, ast.AsyncFunctionDef):
            _scan_async_body(node, path, hits)
            continue
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name:
                root = name.split(".")[0]
                leaf = name.split(".")[-1]
                if leaf in _WRAPPERS:
                    continue  # func-аргумент обёртки исполнится в потоке
                bad = (
                    name == "time.sleep"
                    or leaf in _BLOCKING_NAMES
                    or (root in _BLOCKING_OBJS and leaf not in _PURE_ATTRS)
                )
                if bad:
                    hits.append(f"{path}:{node.lineno}: async "
                                f"{func.name}() -> {name}")
        stack.extend(ast.iter_child_nodes(node))


def test_no_direct_blocking_calls_in_async_functions():
    hits = []
    for fname in _SCANNED_FILES:
        path = os.path.join(_SRC_DIR, fname)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                _scan_async_body(node, fname, hits)
    assert not hits, (
        "Прямые блокирующие вызовы в async-функциях (заворачивать в "
        "_run_blocking):\n" + "\n".join(sorted(set(hits))))


# ---------------------------------------------------------------------------
# Heartbeat-детектор: залипание loop'а оставляет след в логе.
# ---------------------------------------------------------------------------

def test_loop_lag_watchdog_logs_blockage(monkeypatch, caplog):
    monkeypatch.setattr(s, "_LOOP_LAG_CHECK_INTERVAL_S", 0.02)
    monkeypatch.setattr(s, "_LOOP_LAG_WARN_S", 0.1)

    async def _main():
        task = asyncio.create_task(s._event_loop_lag_watchdog())
        await asyncio.sleep(0.05)   # watchdog начал мерить
        time.sleep(0.3)             # имитация блокирующего вызова в loop'е
        await asyncio.sleep(0.05)   # watchdog проснулся и увидел лаг
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level("WARNING"):
        asyncio.run(_main())
    assert any("event loop" in r.message.lower() for r in caplog.records), (
        "watchdog не заметил заблокированный event loop")


def test_loop_lag_watchdog_quiet_when_healthy(monkeypatch, caplog):
    monkeypatch.setattr(s, "_LOOP_LAG_CHECK_INTERVAL_S", 0.02)
    monkeypatch.setattr(s, "_LOOP_LAG_WARN_S", 0.1)

    async def _main():
        task = asyncio.create_task(s._event_loop_lag_watchdog())
        await asyncio.sleep(0.15)   # несколько здоровых тиков
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level("WARNING"):
        asyncio.run(_main())
    assert not any("event loop" in r.message.lower() for r in caplog.records), (
        "watchdog шумит на здоровом loop'е")
