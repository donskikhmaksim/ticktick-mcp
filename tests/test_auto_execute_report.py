"""Тесты честной пост-верификации автоисполнения (2026-08-06, пункт 3 ТЗ
Максима: «слово "успешно" не должно принадлежать тому же коду, который делал
мутацию»).

Проверяется чистая логика `server._verified_auto_execute_report` и её
вспомогательные функции — БЕЗ сети, БЕЗ Postgres, БЕЗ TickTick: независимая
перепроверка (`_build_operation_report`) везде монкейпатчится, потому что
тестируется именно ПРАВИЛО вынесения вердикта, а не сам движок отчёта (он
покрыт отдельно).

Главный инвариант, ради которого всё это писалось: "ok" выдаётся ТОЛЬКО когда
независимое живое чтение реально подтвердило всё (расхождений 0 И
подтверждённых > 0). Любая неопределённость — "unverified", никогда не "ok".
"""
import pytest

import ticktick_mcp.src.server as s


def _report(ok_n, bad_n, body="- ✅ **«X»** — удалена"):
    """Независимый отчёт ровно в том формате, который печатает
    _build_operation_report (важна последняя строка «Итог: …»)."""
    return (f"### 🧾 Независимый отчёт — `m1`\n_06.08 12:00_\n\n{body}\n\n"
            f"**Итог: ✅ {ok_n} подтверждено, ❌ {bad_n} расхождений.**")


def _patch_report(monkeypatch, value):
    """value: строка → отчёт; Exception → падение перепроверки."""
    def _fake(record_id):
        if isinstance(value, BaseException):
            raise value
        return value
    monkeypatch.setattr(s, "_build_operation_report", _fake)


# ===========================================================================
# _parse_verify_totals — маленькая чистая функция разбора итоговой строки
# ===========================================================================

def test_parse_verify_totals_reads_both_numbers():
    assert s._parse_verify_totals(_report(3, 1)) == (3, 1)


def test_parse_verify_totals_handles_zeroes():
    assert s._parse_verify_totals(_report(0, 0)) == (0, 0)


@pytest.mark.parametrize("text", [
    "",
    "какой-то текст без итога",
    "**Итог: всё хорошо**",
    "Итог: ✅ несколько подтверждено, ❌ пара расхождений.",
])
def test_parse_verify_totals_returns_none_on_unknown_format(text):
    assert s._parse_verify_totals(text) is None


# ===========================================================================
# _verdict_from_totals — правило вердикта по фактам перепроверки
# ===========================================================================

@pytest.mark.parametrize("totals,expected", [
    ((3, 0), "ok"),
    ((1, 0), "ok"),
    ((2, 1), "partial"),
    ((0, 2), "failed"),
    ((0, 0), "unverified"),   # подтверждать было нечего — это НЕ успех
    (None, "unverified"),     # формат не распознан
])
def test_verdict_from_totals(totals, expected):
    assert s._verdict_from_totals(totals) == expected


# ===========================================================================
# _verified_auto_execute_report — вердикты
# ===========================================================================

def test_ok_only_when_independent_check_confirms_everything(monkeypatch):
    _patch_report(monkeypatch, _report(2, 0))
    md, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 2 задачи (перечитано после удаления)")
    assert verdict == "ok"
    assert "✅ 2 подтверждено" in md


def test_partial_when_independent_check_is_mixed(monkeypatch):
    _patch_report(monkeypatch, _report(1, 1))
    _, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 2 задачи")
    assert verdict == "partial"


def test_partial_when_executor_itself_hedges(monkeypatch):
    """Перепроверка чистая, но сам исполнитель написал ⚠️ / «не подтверждён»
    — это понижение до partial, а не «ok»."""
    _patch_report(monkeypatch, _report(1, 0))
    _, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks",
        "✅ Удалена 1 задача\n⚠️ read-back по одной задаче не подтверждён")
    assert verdict == "partial"


def test_failed_when_executor_reports_explicit_failure(monkeypatch):
    _patch_report(monkeypatch, _report(0, 0))
    _, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "🛑 Ошибка: TickTick вернул 500, НЕ удалено")
    assert verdict == "failed"


def test_failed_when_nothing_confirmed_but_something_expected(monkeypatch):
    _patch_report(monkeypatch, _report(0, 3))
    _, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 3 задачи")
    assert verdict == "failed"


def test_explicit_failure_marker_ignored_when_executor_also_has_success(monkeypatch):
    """«Ошибка» рядом с ✅ — это не «явный провал» исполнителя; вердикт тогда
    берётся из независимой перепроверки (здесь — partial из-за ⚠️-ветки)."""
    _patch_report(monkeypatch, _report(2, 0))
    _, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 2 задачи (одна Ошибка сети, повтор помог)")
    assert verdict == "ok"


def test_unverified_when_independent_check_raises(monkeypatch):
    _patch_report(monkeypatch, RuntimeError("live read down"))
    md, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 2 задачи")
    assert verdict == "unverified"
    assert "live read down" in md


def test_unverified_when_journal_has_no_records(monkeypatch):
    _patch_report(monkeypatch,
                  "🧾 В журнале нет записей по m1 — операция не исполнялась.")
    _, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 2 задачи")
    assert verdict == "unverified"


def test_unverified_when_live_state_unavailable(monkeypatch):
    _patch_report(monkeypatch,
                  "### 🧾 Отчёт по `m1` невозможен\n⚠️ Живое состояние "
                  "TickTick недоступно — исход НЕ ПОДТВЕРЖДЁН.")
    _, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 2 задачи")
    assert verdict == "unverified"


def test_unverified_when_report_format_unrecognised(monkeypatch):
    _patch_report(monkeypatch, "какой-то новый формат отчёта без итоговой строки")
    _, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 2 задачи")
    assert verdict == "unverified"


def test_unverified_when_report_is_empty(monkeypatch):
    _patch_report(monkeypatch, "")
    _, verdict = s._verified_auto_execute_report("m1", "delete_tasks", "✅ ок")
    assert verdict == "unverified"


def test_unverified_for_operation_type_not_auto_checkable(monkeypatch):
    """_verify_item для незнакомого op пишет «тип … не проверяется
    автоматически» и ставит «✓», которая не считается ни в ✅, ни в ❌ →
    итог 0/0. Это обязано быть "unverified", а не "ok"."""
    _patch_report(monkeypatch, _report(
        0, 0, body="- ✓ **«X»** — записана в журнал (тип foo не проверяется автоматически)"))
    _, verdict = s._verified_auto_execute_report("m1", "foo_tool", "готово")
    assert verdict == "unverified"


# ===========================================================================
# _verified_auto_execute_report — состав markdown
# ===========================================================================

def test_full_markdown_contains_both_sections(monkeypatch):
    _patch_report(monkeypatch, _report(2, 0))
    md, _ = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 2 задачи (read-back исполнителя)")
    assert "#### Что сделал исполнитель" in md
    assert "#### Независимая перепроверка (живое чтение)" in md
    assert "read-back исполнителя" in md          # текст исполнителя целиком
    assert "Независимый отчёт" in md              # текст перепроверки целиком
    assert "Основание вердикта" in md
    assert "m1" in md


def test_full_markdown_keeps_long_report_uncut(monkeypatch):
    """Ничего не обрезаем — длину держит чанкинг в tg_approval."""
    long_body = "\n".join(f"- ✅ **«Задача {i}»** — удалена" for i in range(400))
    _patch_report(monkeypatch, _report(400, 0, body=long_body))
    md, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 400 задач")
    assert verdict == "ok"
    assert len(md) > 4096
    assert "Задача 399" in md


def test_failure_markdown_explains_basis(monkeypatch):
    _patch_report(monkeypatch, _report(0, 0))
    md, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "🛑 Ошибка исполнения")
    assert verdict == "failed"
    assert "исполнитель сам отрапортовал провал" in md


# ===========================================================================
# Короткая сводка в личку
# ===========================================================================

def test_short_summary_points_to_group_when_delivered():
    short = s._short_auto_execute_summary("delete_tasks", "ok", 3, True)
    assert "MCP Отчёты" in short
    assert "Затронуто объектов: 3" in short
    assert short.count("\n") <= 3  # 2-4 строки, личный чат не захламляется


def test_short_summary_admits_group_delivery_failure():
    short = s._short_auto_execute_summary("delete_tasks", "ok", 3, False)
    assert "не доставлен" in short
    assert "MCP Отчёты" not in short


def test_short_summary_without_known_object_count():
    short = s._short_auto_execute_summary("delete_tasks", "unverified", None, True)
    assert "Затронуто объектов" not in short
    assert "НЕ подтверждено" in short


def test_manifest_affected_count():
    assert s._manifest_affected_count({"items": [1, 2, 3]}) == 3
    assert s._manifest_affected_count({"kind": "delete"}) is None
    assert s._manifest_affected_count(None) is None
