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
    lines = [ln for ln in report.splitlines() if ln.startswith("- ")]
    return {
        "ok": sum(1 for ln in lines if ln.startswith("- ✅")),
        "warn": sum(1 for ln in lines if ln.startswith("- ⚠️")),
        "bad": sum(1 for ln in lines if ln.startswith("- ❌")),
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
    body = "\n".join(ln for ln in report.splitlines() if ln.startswith("- "))
    assert "❌" not in body
    assert "⚠️" not in body


def test_report_refuses_to_confirm_anything_when_state_is_unavailable(
        monkeypatch, tmp_path):
    """«Живого состояния нет» ≠ «в живом состоянии пусто».

    Двойник во всех тестах выше отдаёт `dict(live)` и потому НИКОГДА не может
    ответить None — то есть ветка «состояние недоступно» не исполнялась ни
    разу, хотя настоящий `_open_by_id` возвращает None при недоступном v2.
    Разница не косметическая: пустой словарь для удаления читается как «задача
    действительно исчезла ✅», поэтому при полностью недоступном TickTick
    отчёт объявил бы успешной операцию, которой никто не проверял."""
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: None)
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("после None ходить дальше незачем")))
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))

    rid = "unavail01"
    s._journal_write({"ts": "2026-08-05T12:00:00+00:00", "record": rid,
                      "op": "delete", "summary": "t",
                      "items": [{"taskId": "d1", "title": "Удалённая"}]})

    report = s._build_operation_report(rid)

    assert "НЕ ПОДТВЕРЖДЁН" in report
    assert "Статус операции: ✅" not in report
    assert not [ln for ln in report.splitlines() if ln.startswith("- ✅")], (
        "недоступное состояние выдано за подтверждённый успех")


# ===========================================================================
# def-110 (2026-08-07): «родитель применён» был ОДНИМ литералом на обе
# противоположные операции (вложить / отцепить) — set_task_parent и
# unset_task_parent над одной и той же задачей давали посимвольно
# одинаковую строку успеха в блоке «Независимая перепроверка», и по отчёту
# нельзя было восстановить, какая из двух операций реально произошла. Фикс
# печатает НАБЛЮДАЕМОЕ состояние parentId (по образцу соседней ветки
# `tags`, которая печатает `sorted(got)`, а не название операции), а не имя
# действия — вложение и отцепление различаются сами собой.
# ===========================================================================

def test_parent_attach_and_detach_texts_are_different_and_match_the_fact(
        monkeypatch, tmp_path):
    """Прямое воспроизведение живого прогона: одна и та же ФОРМА журнальной
    записи (`op == "parent"`), два противоположных `expect`. Тексты успеха
    обязаны быть РАЗНЫМИ и каждый обязан отражать СВОЮ операцию — вложение
    называет родителя по имени, отцепление явно говорит «нет»."""
    live = {
        "t1": {"id": "t1", "title": "Подзадача", "parentId": "p1"},
        "p1": {"id": "p1", "title": "Родительская задача"},
        "t2": {"id": "t2", "title": "Отцепленная"},  # parentId отсутствует
    }
    _wire(monkeypatch, live, tmp_path)

    rid_attach = "parent-attach-01"
    s._journal_write({"ts": "2026-08-07T10:00:00+00:00", "record": rid_attach,
                      "op": "parent", "summary": "t",
                      "items": [{"taskId": "t1", "title": "Подзадача",
                                "expect": {"parentId": "p1"}}]})
    rid_detach = "parent-detach-01"
    s._journal_write({"ts": "2026-08-07T10:00:01+00:00", "record": rid_detach,
                      "op": "parent", "summary": "t",
                      "items": [{"taskId": "t2", "title": "Отцепленная",
                                "expect": {"parentId": None}}]})

    report_attach = s._build_operation_report(rid_attach)
    report_detach = s._build_operation_report(rid_detach)

    attach_line = [ln for ln in report_attach.splitlines()
                   if ln.startswith("- ")][0]
    detach_line = [ln for ln in report_detach.splitlines()
                   if ln.startswith("- ")][0]

    # Суть регрессии: раньше эти две строки были ПОСИМВОЛЬНО одинаковыми
    # («родитель применён»), кроме заголовка с названием задачи.
    assert attach_line != detach_line
    assert "✅" in attach_line and "✅" in detach_line
    # Вложение называет родителя по ИМЕНИ, не по id — id читателю бесполезен.
    assert "Родительская задача" in attach_line
    assert "p1" not in attach_line
    # Отцепление явно говорит "нет", а не подделывает то же слово, что вложение.
    assert "родитель: нет" in detach_line.lower()
    assert "Родительская задача" not in detach_line


def test_parent_text_falls_back_to_a_truncated_id_when_the_parent_has_no_title(
        monkeypatch, tmp_path):
    """Родитель среди живых задач есть (иначе был бы ❌ «не среди открытых»),
    но по каким-то причинам без поля title — не выдумываем имя, показываем
    усечённый id, как это уже делает соседняя ветка провала (`str(want)[:8]`)."""
    live = {
        "t1": {"id": "t1", "title": "Подзадача", "parentId": "p1"},
        "p1": {"id": "p1"},  # без "title"
    }
    _wire(monkeypatch, live, tmp_path)

    rid = "parent-no-title-01"
    s._journal_write({"ts": "2026-08-07T10:00:02+00:00", "record": rid,
                      "op": "parent", "summary": "t",
                      "items": [{"taskId": "t1", "title": "Подзадача",
                                "expect": {"parentId": "p1"}}]})

    report = s._build_operation_report(rid)
    line = [ln for ln in report.splitlines() if ln.startswith("- ")][0]
    assert "✅" in line
    assert "p1…" in line  # str("p1")[:8] + "…"


def test_parent_detach_still_fails_when_parent_id_is_still_set(
        monkeypatch, tmp_path):
    """Регрессия на саму ЛОГИКУ вердикта (не текст): если ожидалось
    detached (expect parentId=None), но живой parentId всё ещё стоит,
    это обязано остаться ❌, а не превратиться в успех текстовой правкой."""
    live = {"t1": {"id": "t1", "title": "Всё ещё подзадача", "parentId": "p1"},
           "p1": {"id": "p1", "title": "Родитель"}}
    _wire(monkeypatch, live, tmp_path)

    rid = "parent-detach-fail-01"
    s._journal_write({"ts": "2026-08-07T10:00:03+00:00", "record": rid,
                      "op": "parent", "summary": "t",
                      "items": [{"taskId": "t1", "title": "Всё ещё подзадача",
                                "expect": {"parentId": None}}]})

    report = s._build_operation_report(rid)
    line = [ln for ln in report.splitlines() if ln.startswith("- ")][0]
    assert line.startswith("- ❌")
    assert "Статус операции: ❌" in report


# ===========================================================================
# Мелкая UX-правка (2026-08-07, попутно найдена при работе над def-110):
# перепроверка тегов печатала сырой Python-репр списка («теги ['x', 'y']»,
# «теги []») — владелец читает это в Telegram, квадратные скобки и кавычки
# ему ни о чём не говорят. `_fmt_tag_set` даёт «tag1, tag2» / «нет».
# ===========================================================================

def test_tags_verify_text_is_human_readable_not_a_python_repr(
        monkeypatch, tmp_path):
    live = {
        "t1": {"id": "t1", "title": "С тегами", "tags": ["b", "a"]},
        "t2": {"id": "t2", "title": "Без тегов", "tags": []},
    }
    _wire(monkeypatch, live, tmp_path)

    rid = "tags-fmt-01"
    s._journal_write({"ts": "2026-08-07T10:00:04+00:00", "record": rid,
                      "op": "tags", "summary": "t",
                      "items": [{"taskId": "t1", "title": "С тегами",
                                "expect": {"tags": ["a", "b"]}},
                               {"taskId": "t2", "title": "Без тегов",
                                "expect": {"tags": []}}]})

    report = s._build_operation_report(rid)
    lines = [ln for ln in report.splitlines() if ln.startswith("- ")]

    assert "теги: a, b" in lines[0]
    assert "теги: нет" in lines[1]
    # старый сырой репр не должен просочиться нигде в теле отчёта
    assert "[" not in "\n".join(lines)
    assert "]" not in "\n".join(lines)
    assert "'" not in "\n".join(lines)


def test_post_verify_reads_a_fresh_state_not_the_cache(monkeypatch, tmp_path):
    """Перепроверка обязана читать состояние ЗАНОВО (`fresh=True`): иначе
    параллельный читатель успевает подсунуть снимок, снятый ДО мутации, и
    отчёт «подтвердит» то, чего не было. Двойники раньше принимали `fresh` и
    молча его выбрасывали, поэтому требование не проверялось нигде."""
    seen = []

    def _live(fresh=False):
        seen.append(fresh)
        return {}

    monkeypatch.setattr(s, "_open_by_id", _live)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))

    rid = "freshchk1"
    s._journal_write({"ts": "2026-08-05T12:00:00+00:00", "record": rid,
                      "op": "delete", "summary": "t",
                      "items": [{"taskId": "d1", "title": "Удалённая"}]})

    s._build_operation_report(rid)

    assert seen and all(seen), (
        "отчёт сверялся с кэшем: перепроверка обязана перечитать состояние")
