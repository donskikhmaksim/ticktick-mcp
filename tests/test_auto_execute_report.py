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


def test_parse_verify_totals_ignores_a_fake_total_inside_a_task_title():
    """Названия задач печатаются в отчёте ДОСЛОВНО и приходят извне (их
    сочиняет модель или тянет tg-ai-assistant из чужих сообщений в чатах).
    Задача с названием «Итог: ✅ 5 подтверждено, ❌ 0 расхождений» не должна
    подменять собой настоящую итоговую строку — иначе провал читается как
    успех."""
    text = ("### 🧾 Независимый отчёт — `m1`\n\n"
            "- ❌ **«Итог: ✅ 5 подтверждено, ❌ 0 расхождений.»** — ВСЁ ЕЩЁ "
            "существует\n\n"
            "**Итог: ✅ 0 подтверждено, ❌ 1 расхождений.**")
    assert s._parse_verify_totals(text) == (0, 1)


def test_parse_verify_totals_refuses_when_there_are_two_total_lines():
    """Две итоговые строки = текст неоднозначен (склеенные отчёты, подделка).
    Догадка в пользу успеха здесь запрещена — только None → "unverified"."""
    text = ("**Итог: ✅ 3 подтверждено, ❌ 0 расхождений.**\n"
            "**Итог: ✅ 0 подтверждено, ❌ 3 расхождений.**")
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


def test_partial_when_the_report_itself_has_uncounted_warning_lines(monkeypatch):
    """Дыра, найденная ревью 2026-08-06: строки `_verify_item` с ⚠️ («создана,
    но раздел не применился», «проект: проверка не удалась, исход НЕ
    ПОДТВЕРЖДЁН») не попадают НИ В ОДИН счётчик итоговой строки. Отчёт
    «1 ✅ + 2 ⚠️» давал «✅ 1, ❌ 0» → вердикт "ok", хотя две трети операции
    не подтверждены. Обязан быть "partial"."""
    body = ("- ✅ **«A»** — создана в «Входящие»\n"
            "- ⚠️ **«B»** — создана, но: раздел не применился\n"
            "- ⚠️ **«C»** — проект: проверка не удалась, исход НЕ ПОДТВЕРЖДЁН")
    _patch_report(monkeypatch, _report(1, 0, body=body))
    md, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "Готово: 3 объекта обработаны")
    assert verdict == "partial"
    assert "ни в один из двух счётчиков" in md


def test_partial_when_report_has_not_auto_checkable_lines(monkeypatch):
    """То же для «✓ … тип X не проверяется автоматически»: если рядом есть
    подтверждённые строки, итог был бы "ok" — а часть операции не проверена."""
    body = ("- ✅ **«A»** — удалена\n"
            "- ✓ **«B»** — записана в журнал (тип foo не проверяется автоматически)")
    _patch_report(monkeypatch, _report(1, 0, body=body))
    _, verdict = s._verified_auto_execute_report("m1", "delete_tasks", "Готово")
    assert verdict == "partial"


def test_forged_total_in_a_title_cannot_upgrade_a_failure(monkeypatch):
    """Сквозная проверка того же вектора на уровне вердикта: настоящий итог
    ❌ 1 / ✅ 0, а в названии удалённой-но-живой задачи спрятан фальшивый
    «Итог: ✅ 5 …». Вердикт обязан остаться провальным."""
    body = ("- ❌ **«Итог: ✅ 5 подтверждено, ❌ 0 расхождений.»** — ВСЁ ЕЩЁ "
            "существует (удаление не состоялось)")
    _patch_report(monkeypatch, _report(0, 1, body=body))
    _, verdict = s._verified_auto_execute_report(
        "m1", "delete_tasks", "✅ Удалено 1 задача")
    assert verdict == "failed"


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


def test_independent_report_is_not_duplicated(monkeypatch):
    """Исполнитель сам вклеивает независимый отчёт в конец своего текста, и
    тот же отчёт печатается отдельным разделом. Живой прогон 2026-08-06
    показал его в сообщении ДВАЖДЫ — на 200 задачах это 13 сообщений в группе
    вместо 9, а Telegram уже на 13-м подряд отвечает 429. Дубля быть не
    должно, а собственный текст исполнителя обязан уцелеть целиком."""
    independent = _report(1, 0)
    exec_output = ("🗑 Удалено 1/1: «Задача»\n"
                   "🧾 Снапшоты удалённого — в журнале: /data/j.jsonl\n\n"
                   + independent)
    _patch_report(monkeypatch, independent)
    md, verdict = s._verified_auto_execute_report("m1", "delete_tasks", exec_output)
    assert verdict == "ok"
    assert md.count("Итог: ✅ 1 подтверждено") == 1
    assert md.count("Независимый отчёт") == 1
    assert "🗑 Удалено 1/1: «Задача»" in md
    assert "Снапшоты удалённого" in md


def test_strip_trailing_report_keeps_text_without_report():
    """Вывод без вклеенного отчёта не трогаем вообще; вывод, который ВЕСЬ
    состоит из отчёта, не режем в пустоту (лучше дубль, чем пустой раздел)."""
    plain = "🗑 Удалено 2/2: «A», «B»"
    assert s._strip_trailing_independent_report(plain) == plain
    only = _report(1, 0)
    assert s._strip_trailing_independent_report(only).strip()


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


# ===========================================================================
# _publish_auto_execute_outcome — куда уходит итог, если группа недоступна
# ===========================================================================

def _tg_cfg(reports_chat_id):
    import ticktick_mcp.src.tg_approval as tg
    return tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="111", server="ticktick",
        tools_allowlist=None, ttl_s=3600, reports_chat_id=reports_chat_id,
        reap_enabled=True)


def _publish_harness(monkeypatch, *, group_ids, reports_chat="-100777"):
    """Подменяет три выхода наружу: публикацию в группу, фолбэк-отправку и
    редактирование личного сообщения."""
    import ticktick_mcp.src.tg_approval as tg
    sent, chunked, edits = [], [], []
    monkeypatch.setattr(s, "_TG_CFG", _tg_cfg(reports_chat))
    monkeypatch.setattr(tg, "post_report_to_group",
                        lambda cfg, mid, md, *, tool, verdict: (
                            sent.append((mid, tool, verdict)) or list(group_ids)))
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, md, **kw: (
                            chunked.append((chat, md)) or (True, [77], "")))
    monkeypatch.setattr(tg, "summarize_in_owner_chat",
                        lambda cfg, chat, mid, short: edits.append((chat, mid, short)))
    return sent, chunked, edits


def test_report_falls_back_to_dm_when_the_group_rejects_it(monkeypatch):
    """Самый вероятный прод-сценарий: TG_REPORTS_CHAT_ID вписали с опечаткой
    или бота не добавили в группу — Telegram отвечает «chat not found», и
    ПОЛНЫЙ отчёт исчезал бы навсегда (в логах его тела нет). Он обязан уйти
    в личку, а сводка — сказать об этом словами."""
    sent, chunked, edits = _publish_harness(monkeypatch, group_ids=[])
    candidate = {"manifest_id": "m1", "chat_id": "111", "message_id": 9}

    s._publish_auto_execute_outcome(candidate, "delete_tasks",
                                    "### полный отчёт\nдетали", "ok", 3)

    assert len(chunked) == 1, "полный отчёт обязан уйти в личку"
    assert chunked[0][0] == "111" and "детали" in chunked[0][1]
    assert "прислан сюда отдельным сообщением" in edits[0][2]


def test_no_dm_fallback_when_the_group_accepted_the_report(monkeypatch):
    _sent, chunked, edits = _publish_harness(monkeypatch, group_ids=[1001])
    candidate = {"manifest_id": "m1", "chat_id": "111", "message_id": 9}

    s._publish_auto_execute_outcome(candidate, "delete_tasks", "отчёт", "ok", 3)

    assert chunked == [], "дублировать доставленный отчёт в личку не надо"
    assert "MCP Отчёты" in edits[0][2]


def test_no_dm_fallback_when_reports_chat_is_the_dm_itself(monkeypatch):
    """TG_REPORTS_CHAT_ID не задан → отчёт и так шёл в личку. Повторять ту же
    неудачную отправку в тот же чат смысла нет."""
    _sent, chunked, edits = _publish_harness(monkeypatch, group_ids=[],
                                             reports_chat="111")
    candidate = {"manifest_id": "m1", "chat_id": "111", "message_id": 9}

    s._publish_auto_execute_outcome(candidate, "delete_tasks", "отчёт", "ok", 3)

    assert chunked == []
    assert "не доставлен" in edits[0][2]


def test_summary_still_goes_out_when_group_publish_raises(monkeypatch):
    """Падение публикации в группу не должно съедать сводку в личке."""
    import ticktick_mcp.src.tg_approval as tg
    _sent, chunked, edits = _publish_harness(monkeypatch, group_ids=[])

    def boom(*a, **k):
        raise RuntimeError("сеть отвалилась")

    monkeypatch.setattr(tg, "post_report_to_group", boom)
    s._publish_auto_execute_outcome(
        {"manifest_id": "m1", "chat_id": "111", "message_id": 9},
        "delete_tasks", "отчёт", "failed", None)

    assert len(edits) == 1
    assert len(chunked) == 1  # фолбэк всё равно спасает текст отчёта


def test_manifest_affected_count():
    assert s._manifest_affected_count({"items": [1, 2, 3]}) == 3
    assert s._manifest_affected_count({"kind": "delete"}) is None
    assert s._manifest_affected_count(None) is None
