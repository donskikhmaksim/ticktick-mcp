"""Аудируемость трёх каналов automation_key (docs/TZ/TZ_temp_automation_key.md
§4, тестовый план §6, пункты 5, 6, 8, 9):

  5. Операции под временным токеном / AUTOMATION_KEY / устаревшим MCP_SECRET
     помечены в отчёте РАЗДЕЛЬНО.
  6. Активное окно не мешает и не подменяет AUTOMATION_KEY/MCP_SECRET — все
     три канала работают независимо и одновременно.
  8. AUTOMATION_KEY работает независимо от MCP_SECRET.
  9. MCP_SECRET по-прежнему работает (обратная совместимость).

Пункты 8/9 сначала проверяются напрямую на `_automation_key_matches`/
`_automation_key_channel` (без сети/базы — самый прямой и быстрый признак),
пункты 5/6 — end-to-end через РЕАЛЬНЫЙ гейтованный тул (complete_tasks) и
настоящий журнал/operation_report, тем же приёмом мокинга TickTick, что и
tests/test_automation_key_first_call.py."""
import re

import pytest

import ticktick_mcp.src.automation_key as ak
import ticktick_mcp.src.server as s
from tests.pg_helper import fresh_automation_key_store, fresh_stores, requires_pg

pytestmark = requires_pg


# ═══════ 8/9. AUTOMATION_KEY независим от MCP_SECRET; MCP_SECRET работает ═══

def test_automation_key_works_independently_of_mcp_secret(monkeypatch):
    monkeypatch.setattr(s, "SECRET", "")            # старый канал ВЫКЛЮЧЕН
    monkeypatch.setattr(ak, "AUTOMATION_KEY", "the-static-key")

    assert s._automation_key_matches("the-static-key") is True
    assert s._automation_key_channel("the-static-key") == "static"
    # Пустой MCP_SECRET не должен совпадать ни с чем — в частности не должен
    # "случайно" совпасть с пустым automation_key.
    assert s._automation_key_matches("") is False


def test_mcp_secret_still_works_as_automation_key(monkeypatch):
    """Обратная совместимость (TZ §2): пока AUTOMATION_KEY не роздан внешним
    автоматизациям, старый MCP_SECRET продолжает открывать дверь."""
    monkeypatch.setattr(s, "SECRET", "legacy-mcp-secret")
    monkeypatch.setattr(ak, "AUTOMATION_KEY", "")   # новый канал не настроен

    assert s._automation_key_matches("legacy-mcp-secret") is True
    assert s._automation_key_channel("legacy-mcp-secret") == "legacy"


def test_wrong_key_matches_no_channel_even_with_both_configured(monkeypatch):
    monkeypatch.setattr(s, "SECRET", "legacy-mcp-secret")
    monkeypatch.setattr(ak, "AUTOMATION_KEY", "the-static-key")

    assert s._automation_key_matches("что-то-третье") is False
    assert s._automation_key_channel("что-то-третье") == ""


def test_static_and_legacy_do_not_cross_match(monkeypatch):
    """AUTOMATION_KEY и MCP_SECRET — РАЗНЫЕ секреты: значение одного не
    должно открывать дверь как другой (иначе они не были бы независимыми
    каналами, а одним под двумя именами)."""
    monkeypatch.setattr(s, "SECRET", "legacy-mcp-secret")
    monkeypatch.setattr(ak, "AUTOMATION_KEY", "the-static-key")

    assert s._automation_key_channel("legacy-mcp-secret") == "legacy"
    assert s._automation_key_channel("the-static-key") == "static"


# ═══════════════════ 5/6. Разметка канала в отчёте, end-to-end ═════════════

class _FakeV2Complete:
    def __init__(self, live):
        self._live = live
        self.completed_ids = []

    def batch_complete_tasks(self, ids):
        for i in ids:
            self._live.pop(i, None)
            self.completed_ids.append(i)
        return {}


_RID_RE = re.compile(r'operation_report\(record_id="([^"]+)"\)')


def _record_id_of(text: str) -> str:
    m = _RID_RE.search(text)
    assert m, f"в ответе нет record_id — журнал не был записан:\n{text}"
    return m.group(1)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Живой Postgres (обе таблицы approval-слоя + окна automation_key),
    заглушенный TickTick — тот же набор, что у test_automation_key_first_call
    .py's `wired`, плюс `fresh_automation_key_store()`."""
    fresh_stores()
    fresh_automation_key_store()
    monkeypatch.setattr(s, "_ensure_ready", lambda: "")
    monkeypatch.setattr(s, "_ensure_official", lambda: "")
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    from ticktick_mcp.src import manifest_store, tg_approval
    manifest_store.close_store()
    tg_approval.close_store()
    ak.close_store()


@pytest.fixture
def completion(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Задача 1", "projectId": "p1"},
            "t2": {"id": "t2", "title": "Задача 2", "projectId": "p1"}}
    fake = _FakeV2Complete(live)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return live, fake


async def _complete_under(monkeypatch, live, fake, key: str, title: str, task_id: str):
    """Один вызов complete_tasks с указанным automation_key, возвращает
    record_id, чтобы вызывающий тест мог тут же прочитать отчёт по нему.

    ДВЕ задачи за вызов (не одна) НАМЕРЕННО: `_complete_tasks_impl` при
    `len(tasks) > 1` идёт batch-веткой через `ticktick_v2.batch_complete_
    tasks` (замокан фикстурой `completion`), а при ровно одной — веткой
    через официальный `ticktick.complete_task`, которого в этих тестах нет
    вовсе (мокается только `ticktick_v2`). Обе ветки одинаково зовут
    `_op_journal`/`_journal_write` — какой из двух путь, для этого файла не
    важно, важна только сама разметка канала."""
    task_id_2 = task_id + "-b"
    live[task_id] = {"id": task_id, "title": title, "projectId": "p1"}
    live[task_id_2] = {"id": task_id_2, "title": title + " (2)", "projectId": "p1"}
    out = await s.complete_tasks(f"Закрываю {title}", [
        {"title": title, "taskId": task_id, "projectId": "p1"},
        {"title": title + " (2)", "taskId": task_id_2, "projectId": "p1"},
    ], automation_key=key)
    return _record_id_of(out)


async def test_static_key_operation_is_marked_automation_key_in_the_report(
        wired, completion, monkeypatch):
    monkeypatch.setattr(s, "SECRET", "legacy-mcp-secret")
    monkeypatch.setattr(ak, "AUTOMATION_KEY", "the-static-key")
    live, fake = completion

    rid = await _complete_under(monkeypatch, live, fake, "the-static-key",
                                "Статика", "t-static")

    report = s._build_operation_report_data(rid).text
    assert "AUTOMATION_KEY" in report and "MCP_SECRET" not in report
    assert "окне" not in report  # не спутан с временным окном


async def test_window_token_operation_is_marked_open_window_in_the_report(
        wired, completion, monkeypatch):
    monkeypatch.setattr(s, "SECRET", "legacy-mcp-secret")
    monkeypatch.setattr(ak, "AUTOMATION_KEY", "the-static-key")
    live, fake = completion
    token = ak.generate_window("4242")

    rid = await _complete_under(monkeypatch, live, fake, token,
                                "Окно", "t-window")

    report = s._build_operation_report_data(rid).text
    assert "открыт" in report and "временный ключ" in report
    assert "AUTOMATION_KEY)" not in report
    assert "MCP_SECRET" not in report


async def test_legacy_secret_operation_is_marked_mcp_secret_in_the_report(
        wired, completion, monkeypatch):
    monkeypatch.setattr(s, "SECRET", "legacy-mcp-secret")
    monkeypatch.setattr(ak, "AUTOMATION_KEY", "the-static-key")
    live, fake = completion

    rid = await _complete_under(monkeypatch, live, fake, "legacy-mcp-secret",
                                "Легаси", "t-legacy")

    report = s._build_operation_report_data(rid).text
    assert "MCP_SECRET, устаревший путь" in report
    assert "AUTOMATION_KEY)" not in report


async def test_interactive_operation_carries_no_automation_note(wired, monkeypatch, tmp_path):
    """Обычная (не-automation_key) операция НЕ должна печатать строку аудита
    канала — она есть ТОЛЬКО у автоматики. `complete_tasks`/остальные
    прямые task-мутаторы сами теперь automation-only (§1.3.4 захода 1) —
    интерактивный путь к ним идёт ТОЛЬКО через apply_task_changes, что для
    ЭТОГО теста избыточная обвязка; поэтому здесь бьём ровно в то место,
    которое реально решает — `consent._journal_write`/`_build_operation_
    report_data` — с ContextVar каналом, гарантированно НЕ выставленным
    (как оно и есть на настоящем интерактивном пути: ни `_require_consent`,
    ни `_gate_batch`/`_gate_single` не зовут `_AUTOMATION_CHANNEL.set(...)`
    нигде, кроме СВОИХ трёх automation_key-веток)."""
    from ticktick_mcp.src import consent as c

    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    monkeypatch.setattr(s, "_v2_project_names", lambda: {})
    assert c._AUTOMATION_CHANNEL.get() == "", (
        "ContextVar не пуст ДО теста — соседний тест оставил протёкшее значение")
    rid = c._op_journal("complete", [{"taskId": "t-chat", "title": "Через чат"}],
                        "Закрываю через чат")
    assert rid

    report = s._build_operation_report_data(rid).text
    assert "AUTOMATION_KEY" not in report
    assert "MCP_SECRET" not in report
    assert "временный ключ" not in report


async def test_three_channels_do_not_interfere_with_each_other_back_to_back(
        wired, completion, monkeypatch):
    """Пункт 6: три канала подряд (окно → статика → легаси → окно) — КАЖДЫЙ
    отчёт помечен ПРАВИЛЬНЫМ каналом, ни один не унаследовал метку соседа.
    Это же — прямая проверка, что ContextVar-метка «съедается» при первом
    чтении журнала (consent.py's _AUTOMATION_CHANNEL) и не протекает в
    СЛЕДУЮЩУЮ, не связанную операцию."""
    monkeypatch.setattr(s, "SECRET", "legacy-mcp-secret")
    monkeypatch.setattr(ak, "AUTOMATION_KEY", "the-static-key")
    live, fake = completion
    token = ak.generate_window("4242")

    rid_window1 = await _complete_under(monkeypatch, live, fake, token, "О1", "t-w1")
    rid_static = await _complete_under(monkeypatch, live, fake, "the-static-key", "С1", "t-s1")
    rid_legacy = await _complete_under(monkeypatch, live, fake, "legacy-mcp-secret", "Л1", "t-l1")
    rid_window2 = await _complete_under(monkeypatch, live, fake, token, "О2", "t-w2")

    r_window1 = s._build_operation_report_data(rid_window1).text
    r_static = s._build_operation_report_data(rid_static).text
    r_legacy = s._build_operation_report_data(rid_legacy).text
    r_window2 = s._build_operation_report_data(rid_window2).text

    assert "временный ключ" in r_window1 and "AUTOMATION_KEY)" not in r_window1
    assert "MCP_SECRET" not in r_window1

    assert "AUTOMATION_KEY)" in r_static
    assert "временный ключ" not in r_static and "MCP_SECRET" not in r_static

    assert "MCP_SECRET, устаревший путь" in r_legacy
    assert "временный ключ" not in r_legacy and "AUTOMATION_KEY)" not in r_legacy

    assert "временный ключ" in r_window2 and "AUTOMATION_KEY)" not in r_window2
    assert "MCP_SECRET" not in r_window2
