"""Регрессионные тесты на баг подсчёта в operation_report (ночная QA, 2026-08):
`operation_report(record_id)` печатал вердикт «0 расхождений» в ОДНОМ И ТОМ ЖЕ
ответе, где были явно перечислены несколько пунктов с ⚠️ — внутреннее
противоречие. Причина: старый подсчёт учитывал только строки, у которых
ведущий эмодзи совпадал с "✅"/"❌" по заново распознанному напечатанному
markdown-тексту, поэтому вердикты ⚠️ (создано не туда, куда просили;
не удалось перечитать состояние для перепроверки; тип операции без
выделенного проверятеля) печатались в теле отчёта, но нигде не учитывались.

Фикс делает так, что `_verify_item` возвращает явную пару (status, line) —
status ∈ {"ok", "warn", "bad"} — а подсчёт в `_build_operation_report`
считается через len()/count() по ТОМУ ЖЕ списку вердиктов, что и печатается,
а не отдельным счётчиком. Эти тесты проверяют, что это свойство держится:
что напечатано пунктом — то и учтено в «Итог», и для смеси ok/warn/bad,
и для случая «всё чисто».

Чисто-логические тесты: без сети, без реального TickTick — `_open_by_id`/
`_v2_project_names` замоканы на заготовленное живое состояние, тот же
паттерн, что и в tests/test_delete_identity_binding.py.
"""
import re

import ticktick_mcp.src.server as s


def _wire(monkeypatch, live, tmp_path, names=None):
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: names or {"p1": "Проект1", "p2": "Проект2"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))


def _count_body_bullets(report: str) -> dict:
    """Считает пункты-вердикты по ведущему статусному эмодзи прямо из
    напечатанного markdown — это независимая «эталонная правда», с которой
    сверяется подсчёт в строке «Итог»."""
    lines = [l for l in report.splitlines() if l.startswith("- ")]
    return {
        "ok": sum(1 for l in lines if l.startswith("- ✅")),
        "warn": sum(1 for l in lines if l.startswith("- ⚠️")),
        "bad": sum(1 for l in lines if l.startswith("- ❌")),
        "total": len(lines),
    }


def _parse_summary(report: str):
    m = re.search(
        r"Итог: ✅ (\d+) подтверждено, ⚠️ (\d+) не проверено, "
        r"❌ (\d+) расхождений",
        report)
    assert m, f"Итог-строка не найдена или не в ожидаемом формате:\n{report}"
    return {"ok": int(m.group(1)), "warn": int(m.group(2)),
            "bad": int(m.group(3))}


async def test_mixed_ok_warn_bad_tally_matches_printed_bullets(
        monkeypatch, tmp_path):
    """Воспроизводит репортнутый сценарий: батч с несколькими пунктами ⚠️
    (создание попало не в тот проект; тип операции без выделенного
    проверятеля) вместе с настоящим ❌ и настоящим ✅. Числа в «Итог» должны
    точно совпадать с тем, что реально перечислено — ни одно расхождение не
    должно быть невидимым для подсчёта."""
    live = {
        # d1 отсутствует в живом состоянии -> удаление подтверждено (ok)
        "d2": {"id": "d2", "title": "Ещё живая"},              # удаление НЕ применилось -> bad
        "u1": {"id": "u1", "title": "New U1"},                 # обновление совпало -> ok
        "c1": {"id": "c1", "title": "C1", "projectId": "p2"},  # создание не в тот проект -> warn
        "h1": {"id": "h1", "title": "H1"},                     # тип операции без проверятеля -> warn (fallback)
    }
    _wire(monkeypatch, live, tmp_path)

    rid = "mixed-test01"
    s._journal_write({"ts": "2026-08-05T10:00:00+00:00", "record": rid,
                      "op": "delete", "summary": "t",
                      "items": [{"taskId": "d1", "title": "Удалённая"}]})
    s._journal_write({"ts": "2026-08-05T10:00:01+00:00", "record": rid,
                      "op": "delete", "summary": "t",
                      "items": [{"taskId": "d2", "title": "Ещё живая"}]})
    s._journal_write({"ts": "2026-08-05T10:00:02+00:00", "record": rid,
                      "op": "update", "summary": "t",
                      "items": [{"taskId": "u1", "title": "U1",
                                "expect": {"changes": {"title": "New U1"}}}]})
    s._journal_write({"ts": "2026-08-05T10:00:03+00:00", "record": rid,
                      "op": "create", "summary": "t",
                      "items": [{"taskId": "c1", "title": "C1",
                                "expect": {"projectId": "p1"}}]})
    s._journal_write({"ts": "2026-08-05T10:00:04+00:00", "record": rid,
                      "op": "checkin_habit", "summary": "t",
                      "items": [{"taskId": "h1", "title": "H1"}]})

    report = s._build_operation_report(rid)

    bullets = _count_body_bullets(report)
    summary = _parse_summary(report)

    # Суть регрессии: пункты-предупреждения (⚠️) и напечатаны, и учтены.
    assert bullets == {"ok": 2, "warn": 2, "bad": 1, "total": 5}
    assert summary == {"ok": 2, "warn": 2, "bad": 1}
    # Единый источник истины в общем виде: подсчёт == напечатанные пункты,
    # всегда.
    assert summary["ok"] == bullets["ok"]
    assert summary["warn"] == bullets["warn"]
    assert summary["bad"] == bullets["bad"]

    # Машиночитаемый вердикт: при наличии расхождения (здесь bad>0) статус
    # НЕ должен читаться как успех.
    assert "Статус операции: ❌" in report
    assert "Статус операции: ✅" not in report

    # Замороженная эмодзи-легенда запрещает ASCII "✓"/"✗" как статусные
    # маркеры — старый fallback использовал "✓"; проверяем, что он ушёл
    # окончательно.
    assert "✓" not in report
    assert "✗" not in report


async def test_warn_only_batch_is_not_reported_as_full_success(
        monkeypatch, tmp_path):
    """bad == 0, но warn > 0 — всё равно НЕ должно читаться как чистый ✅:
    headless-потребитель, проверяющий только счётчик ❌, иначе пропустит
    проблему."""
    live = {"c1": {"id": "c1", "title": "C1", "projectId": "p2"}}
    _wire(monkeypatch, live, tmp_path)

    rid = "warn-only-01"
    s._journal_write({"ts": "2026-08-05T11:00:00+00:00", "record": rid,
                      "op": "create", "summary": "t",
                      "items": [{"taskId": "c1", "title": "C1",
                                "expect": {"projectId": "p1"}}]})

    report = s._build_operation_report(rid)
    summary = _parse_summary(report)
    assert summary == {"ok": 0, "warn": 1, "bad": 0}
    assert "Статус операции: ⚠️" in report
    assert "Статус операции: ✅" not in report


async def test_zero_discrepancies_is_a_clean_and_honest_report(
        monkeypatch, tmp_path):
    """Обратный случай: реально всё в порядке. «0 расхождений» должно
    появляться только тогда, когда перечислять действительно нечего."""
    live = {}  # удалённая задача реально пропала
    _wire(monkeypatch, live, tmp_path)

    rid = "clean-test01"
    s._journal_write({"ts": "2026-08-05T12:00:00+00:00", "record": rid,
                      "op": "delete", "summary": "t",
                      "items": [{"taskId": "d1", "title": "Удалённая"}]})

    report = s._build_operation_report(rid)

    bullets = _count_body_bullets(report)
    summary = _parse_summary(report)

    assert bullets == {"ok": 1, "warn": 0, "bad": 0, "total": 1}
    assert summary == {"ok": 1, "warn": 0, "bad": 0}
    assert "Статус операции: ✅" in report
    # В теле (построчных пунктах) не должно быть случайных маркеров
    # расхождений (строки «Итог»/«Статус» законно содержат «❌ 0» — грязным
    # не должно быть только тело с пунктами-буллетами).
    body = "\n".join(l for l in report.splitlines() if l.startswith("- "))
    assert "❌" not in body
    assert "⚠️" not in body
