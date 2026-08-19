"""`_build_operation_report` не исполняется В event loop — приёмка разбора
QA-2 (2026-08-19, дефект №5).

ЧТО БЫЛО СЛОМАНО. Внутри `_build_operation_report` — сетевые чтения живого
состояния и цикл ретраев post-verify (`_POSTVERIFY_RETRY_DELAYS_S`) с
`time.sleep` суммарно до ~9 секунд. Три площадки звали его ПРЯМО в event
loop: `operation_report`, `execute_task_creation` (приклейка независимого
отчёта) и `_execute_task_deletion_impl`. На время построения отчёта замирал
ВЕСЬ сервер — /health, фоновый поллер кнопок, все параллельные MCP-сессии.
Кнопочный путь (tg_auto_execute.py) делал это правильно с самого начала —
через поток.

Способ проверки: двойник отчёта смотрит, есть ли в ЕГО потоке бегущий event
loop (`asyncio.get_running_loop()`), — в правильном мире отчёт строится в
рабочем потоке `asyncio.to_thread`, где loop'а нет.
"""
import asyncio
import time

import pytest

import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(consent._MANIFESTS)
    yield
    consent._MANIFESTS.clear()
    consent._MANIFESTS.update(before)


@pytest.fixture
def _report_probe(monkeypatch):
    """Двойник `_build_operation_report`: возвращаемый текст говорит, из
    какого мира его позвали."""
    seen = {}

    def _probe(record_id):
        try:
            asyncio.get_running_loop()
            seen["where"] = "in-loop"
            return f"ОТЧЁТ ({record_id}) построен В event loop — блокировка"
        except RuntimeError:
            seen["where"] = "off-loop"
            return f"ОТЧЁТ ({record_id}) построен вне event loop"

    monkeypatch.setattr(s, "_build_operation_report", _probe)
    return seen


def test_operation_report_builds_off_the_event_loop(monkeypatch,
                                                    _report_probe):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    out = asyncio.run(s.operation_report("create-abc123"))
    assert _report_probe.get("where") == "off-loop", (
        "operation_report обязан уводить построение отчёта в поток — внутри "
        "сетевые чтения и time.sleep-ретраи до ~9 секунд")
    assert "вне event loop" in out


def test_execute_task_creation_appends_the_report_off_the_event_loop(
        monkeypatch, _report_probe):
    monkeypatch.setenv(consent._GATE_DISABLED_ENV, "1")
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    async def _fake_create(summary, raw):
        return ('✅ Создано 1 из 1. Проверка: '
                'operation_report(record_id="create-abc123")')

    monkeypatch.setattr(s, "_create_tasks_impl", _fake_create)
    now = time.monotonic()
    consent._MANIFESTS["off-loop-cr"] = {
        "kind": "create", "raw": [{"title": "Задача", "project_id": "p1"}],
        "created": now, "plan_shown_at": now, "summary": "Создаю",
        "consumed": False, "tool": "create_tasks", "_gate": "create"}

    out = asyncio.run(s.execute_task_creation("off-loop-cr", user_reply=""))

    assert _report_probe.get("where") == "off-loop", (
        "приклейка независимого отчёта в execute_task_creation обязана идти "
        "через _run_blocking")
    assert "вне event loop" in out


def test_deletion_impl_appends_the_report_off_the_event_loop(
        monkeypatch, _report_probe):
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    state = {"reads": 0}

    def _open(fresh=False):
        state["reads"] += 1
        # Первое чтение (сверка перед удалением) — задача жива; второе
        # (post-verify) — задачи больше нет, то есть «удалена, проверено».
        if state["reads"] == 1:
            return {"t1": {"id": "t1", "title": "Мусор", "projectId": "p1"}}
        return {}

    monkeypatch.setattr(s, "_open_by_id", _open)
    monkeypatch.setattr(s, "_journal_write", lambda payload: "журнал.jsonl")
    monkeypatch.setattr(
        s, "ticktick_v2",
        type("_FakeV2", (), {
            "batch_delete_tasks": staticmethod(lambda items: {}),
        })())
    now = time.monotonic()
    consent._MANIFESTS["off-loop-del"] = {
        "kind": "delete", "created": now, "plan_shown_at": now,
        "summary": "Удаляю", "consumed": False,
        "items": [{"taskId": "t1", "projectId": "p1", "title": "Мусор",
                   "project": "Проект", "snapshot": {"title": "Мусор"}}]}

    out = asyncio.run(s._execute_task_deletion_impl(
        "off-loop-del", consent._MANIFESTS["off-loop-del"]))

    assert "Удалено 1" in out
    assert _report_probe.get("where") == "off-loop", (
        "приклейка отчёта в _execute_task_deletion_impl обязана идти через "
        "_run_blocking")
    assert "вне event loop" in out
