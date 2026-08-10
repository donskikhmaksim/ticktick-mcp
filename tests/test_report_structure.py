"""Структура непрошедшего выходит из отчёта РЯДОМ с текстом (1.3.1 захода 1,
2026-08-09).

Болезнь, ради которой пакет писался: отчёт подробно рассказывал про
выполненное и сообщал про невыполненное ЧИСЛОМ — «Объектов в плане: 14.
Подтверждено перепроверкой: 11». Какие три и почему — не сказано, выяснение
занимало четыре запроса. Отчёт, который сообщает о проблеме, но не даёт её
найти, на практике равен отчёту «всё хорошо».

Главное, что стерегут эти тесты, — ОТКУДА берутся данные. Собрать «структуру»
разбором собственного текста отчёта (регуляркой по буллитам) означало бы:
раздел «не вошло» появится, тесты на его присутствие позеленеют, а защита от
подделки вердикта через название задачи окажется разоружена. Поэтому тесты
проверяют не «есть ли раздел», а поля, которых в тексте отчёта НЕТ ВОВСЕ —
прежде всего идентификатор объекта (см. также
tests/test_verify_totals_antiforgery.py).
"""
import pytest

import ticktick_mcp.src.server as s


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Журнал операций в tmp + предсказуемые имена проектов."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Входящие"})
    return tmp_path


def _live(*ids):
    """Живое состояние: перечисленные id — живы, остальных нет."""
    return {i: {"id": i, "projectId": "p1", "title": f"Задача {i}"}
            for i in ids}


def _skip_rec(task_id, title, why, op="delete"):
    """Справочная запись о непошедшем — ровно та форма, которую строит
    `_triage_not_planned_records` и которая уезжает в журнал и в Postgres."""
    return {"task_id": task_id, "op": op, "title": title, "why": why}


def _not_executed(record_id, skipped, not_planned):
    """Записать справку о непошедшем ПОД ТЕМ ЖЕ id, что и мутации.

    В бою это делает сам поллер кнопки: он выставляет метку манифеста
    (`_TG_AUTO_EXECUTE_MANIFEST`) на всё время исполнения, и любая журнальная
    запись получает `tg_manifest`, по которому отчёт её и находит. Тест
    воспроизводит ровно этот путь, а не подкладывает запись руками."""
    token = s._TG_AUTO_EXECUTE_MANIFEST.set(record_id)
    try:
        return s._journal_not_executed(skipped, not_planned, "разбор")
    finally:
        s._TG_AUTO_EXECUTE_MANIFEST.reset(token)


# ===========================================================================
# 1. Вердикт несёт id, имя и причину — данными, а не текстом
# ===========================================================================

def test_verdicts_carry_id_name_and_reason(journal, monkeypatch):
    """Журнал из 3 объектов, 2 подтверждены, 1 нет.

    У непрошедшего обязаны быть заполнены `object_id`, `display_name` и
    `reason`, причём имя — непустое даже у задачи БЕЗ названия (заменитель
    строится из снимка через `_untitled_label`). Идентификатора объекта в
    тексте отчёта нет ни в каком виде — структура, собранная разбором
    собственного текста, это утверждение пройти не может."""
    # t3 жива (удаление не состоялось) и БЕЗ названия — снимок несёт вложение,
    # значит заменитель обязан назвать файл, а не показать голый id.
    monkeypatch.setattr(s, "_open_by_id",
                        lambda fresh=False: {"t3": {"id": "t3",
                                                    "projectId": "p1",
                                                    "title": ""}})
    s._journal_write({"ts": "2026-08-09T12:00:00+00:00", "record": "delete-s01",
                      "op": "delete",
                      "items": [
                          {"taskId": "t1", "title": "Купить молоко"},
                          {"taskId": "t2", "title": "Оплатить счёт"},
                          {"taskId": "t3", "title": "",
                           "snapshot": {"title": "",
                                        "attachments": [{"id": "a1"}]}},
                      ]})
    data = s._build_operation_report_data("delete-s01")

    assert data.totals == (2, 0, 1), data.text
    failed = [v for v in data.verdicts if v.status != "ok"]
    assert len(failed) == 1
    bad = failed[0]
    assert bad.object_id == "t3"
    assert bad.display_name != ""
    assert bad.display_name == "(без названия: 📎 1 файл)"
    assert bad.reason
    assert "всё ещё существует" in bad.reason
    assert bad.op == "delete"
    # Имя и причина едут ОТДЕЛЬНЫМИ полями, а не куском готовой строки: в
    # `line` они уже склеены с эмодзи и разметкой.
    assert "❌" not in bad.reason and "**" not in bad.reason
    # И решающее: идентификатора объекта в тексте отчёта нет вовсе.
    assert "t3" not in data.text
    assert {v.object_id for v in data.verdicts} == {"t1", "t2", "t3"}


# ===========================================================================
# 2. Короткая сводка в личку называет непрошедшее поимённо
# ===========================================================================

def test_short_summary_names_three_failures(journal, monkeypatch):
    """План 14 / подтверждено 11 → сводка называет ТРИ объекта и ТРИ причины.

    Проверяется вхождением подстрок, а не длиной текста: длинная сводка,
    не называющая объектов, — это та же болезнь в более многословной обёртке.
    """
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    s._journal_write({"ts": "2026-08-09T12:00:00+00:00", "record": "delete-s02",
                      "op": "delete",
                      "items": [{"taskId": f"t{i}", "title": f"Задача {i}"}
                                for i in range(11)]})
    _not_executed(
        "delete-s02",
        [_skip_rec("x1", "Позвонить в банк", "название изменилось после плана"),
         _skip_rec("x2", "Купить билеты", "исчезла из открытых задач")],
        [_skip_rec("x3", "Сдать отчёт", "название не совпало с живым")])
    data = s._build_operation_report_data("delete-s02")
    assert data.totals == (11, 0, 0)

    short = s._short_auto_execute_summary(
        "manual_triage", "partial", 14, True, totals=data.totals, data=data)

    assert "Объектов в плане: 14" in short
    assert "Подтверждено перепроверкой: 11" in short
    for name in ("Позвонить в банк", "Купить билеты", "Сдать отчёт"):
        assert name in short, short
    for why in ("название изменилось после плана",
                "исчезла из открытых задач",
                "название не совпало с живым"):
        assert why in short, short


# ===========================================================================
# 3. Две одинаковые операции над разными объектами различимы
# ===========================================================================

def test_two_identical_ops_differ_in_summary(journal, monkeypatch):
    """Семь удалений подряд обязаны отличаться от одного, повторённого семь
    раз. Объект называется даже при ПОЛНОМ успехе — иначе сводки двух разных
    операций совпадают посимвольно."""
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    s._journal_write({"ts": "2026-08-09T12:00:00+00:00", "record": "delete-a",
                      "op": "delete",
                      "items": [{"taskId": "ta", "title": "Купить молоко"}]})
    s._journal_write({"ts": "2026-08-09T12:01:00+00:00", "record": "delete-b",
                      "op": "delete",
                      "items": [{"taskId": "tb", "title": "Оплатить счёт"}]})

    def _summary(rid):
        data = s._build_operation_report_data(rid)
        return s._short_auto_execute_summary("delete_tasks", "ok", 1, True,
                                             totals=data.totals, data=data)

    text_a, text_b = _summary("delete-a"), _summary("delete-b")
    assert text_a != text_b
    assert "Купить молоко" in text_a and "Купить молоко" not in text_b
    assert "Оплатить счёт" in text_b and "Оплатить счёт" not in text_a


# ===========================================================================
# 4. Вердикт «ok» при расхождении плана и факта — жёлтый
# ===========================================================================

def test_ok_verdict_with_drift_is_marked_yellow(journal, monkeypatch):
    """Все журнальные строки подтвердились, но часть плана до мутации не
    доехала — это НЕ полный успех.

    Именно так выглядел живой случай «в плане 14, подтверждено 11»: одиннадцать
    строк «ok», зелёный значок и ни слова про три потерянные операции. Значок
    обязан стать жёлтым И в тексте отчёта, И в кнопочном пути Telegram (там он
    берётся из `_VERDICT_EMOJI` по вердикту)."""
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    s._journal_write({"ts": "2026-08-09T12:00:00+00:00", "record": "tg-drift",
                      "op": "delete",
                      "items": [{"taskId": "t1", "title": "Первая"},
                                {"taskId": "t2", "title": "Вторая"}]})
    _not_executed(
        "tg-drift",
        [_skip_rec("t9", "Третья", "название изменилось после плана")], [])

    data = s._build_operation_report_data("tg-drift")
    # Формальный вердикт по счётчикам — чистый «ok»…
    assert data.totals == (2, 0, 0)
    assert s._verdict_from_totals(data.totals) == "ok"
    # …а общий значок отчёта — жёлтый, и расхождение названо.
    assert data.drift is True
    assert data.overall == "⚠️"
    assert "**Статус операции: ⚠️**" in data.text
    assert "Статус операции: ✅" not in data.text

    # Кнопочный путь Telegram: тот же жёлтый значок, а не «✅ подтверждено».
    md, verdict = s._verified_auto_execute_report(
        "tg-drift", "manual_triage", "### 🧾 Ручной разбор — итог")
    assert verdict == "partial"
    assert s._VERDICT_EMOJI[verdict] == "⚠️"
    assert s._VERDICT_EMOJI["ok"] not in md.split("\n")[0]


# ===========================================================================
# 5. Две рубрики невыполненного не склеены
# ===========================================================================

def test_not_planned_is_separate_rubric(journal, monkeypatch):
    """«⏭ Пропущено» и «❌ Не вошло в план» — разные смыслы и разные
    заголовки: первое было в плане и подтверждено кнопкой, но сдрейфовало;
    второе не подтверждалось ВООБЩЕ. При склейке владелец не отличит
    «согласился и не сделалось» от «не согласовывалось»."""
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    s._journal_write({"ts": "2026-08-09T12:00:00+00:00", "record": "mix-1",
                      "op": "delete",
                      "items": [{"taskId": "t1", "title": "Первая"}]})
    _not_executed(
        "mix-1",
        [_skip_rec("sk1", "Сдрейфовала", "название изменилось после плана")],
        [_skip_rec("np1", "Не согласовывали", "название не совпало с живым")])

    data = s._build_operation_report_data("mix-1")
    assert "#### ⏭ Пропущено" in data.text
    assert "#### ❌ Не вошло в план" in data.text
    assert data.text.index("#### ⏭ Пропущено") \
        < data.text.index("#### ❌ Не вошло в план")

    skipped_ids = {r["task_id"] for r in data.skipped}
    not_planned_ids = {r["task_id"] for r in data.not_planned}
    assert skipped_ids == {"sk1"}
    assert not_planned_ids == {"np1"}
    assert not (skipped_ids & not_planned_ids)

    # Сводка в личку тоже держит их врозь — двумя разными строками.
    short = s._short_auto_execute_summary("manual_triage", "partial", 3, True,
                                          totals=data.totals, data=data)
    assert "⏭ Пропущено" in short and "Не вошло в план" in short
    assert "Сдрейфовала" in short and "Не согласовывали" in short


# ===========================================================================
# 6. Усечение — только вслух
# ===========================================================================

def test_truncated_list_states_real_total(journal, monkeypatch):
    """Список «не вошло» из 200 пунктов печатает «показаны N из 200», где N
    равно числу ФАКТИЧЕСКИ напечатанных строк (тест считает строки, а не верит
    числу). Молчаливое усечение читается как полный результат — остаток
    пропадает бесследно."""
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    s._journal_write({"ts": "2026-08-09T12:00:00+00:00", "record": "big-1",
                      "op": "delete",
                      "items": [{"taskId": "t1", "title": "Первая"}]})
    many = [_skip_rec(f"n{i}", f"Задача {i}", "название не совпало")
            for i in range(200)]
    _not_executed("big-1", [], many)

    data = s._build_operation_report_data("big-1")
    assert len(data.not_planned) == 200

    lines = data.text.split("\n")
    head = lines.index("#### ❌ Не вошло в план — сверка НА ЭТАПЕ ПЛАНА, "
                       "подтверждения по ним не было")
    body = [ln for ln in lines[head + 1:] if ln.startswith("•")]
    tail = [ln for ln in lines[head + 1:] if ln.startswith("… показаны")]
    assert len(tail) == 1, data.text
    assert f"показаны {len(body)} из 200" in tail[0]
    assert len(body) < 200


# ===========================================================================
# 7. Отчёт без структуры (старый формат) по-прежнему разбирается регуляркой
# ===========================================================================

def test_legacy_text_only_report_still_parsed(tmp_path, monkeypatch):
    """Отчёт, собранный СТАРЫМ путём (голый текст, никакой структуры),
    разбирается регуляркой и даёт тот же вердикт, что и раньше.

    Регулярка `_VERIFY_TOTALS_RE` и `_parse_verify_totals` не удалены
    намеренно: они остаются запасным путём и сохраняют правило «не распознал →
    не подтверждено»."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))  # журнала нет вовсе
    legacy = ("### 🧾 Независимый отчёт — `legacy-1`\n_09.08 12:00_\n\n"
              "- ✅ **«Купить молоко»** — удалена\n"
              "- ✅ **«Оплатить счёт»** — удалена\n\n"
              "**Итог: ✅ 2 подтверждено, ⚠️ 0 не проверено, ❌ 0 расхождений.**")
    monkeypatch.setattr(s, "_build_operation_report", lambda rid: legacy)

    md, verdict = s._verified_auto_execute_report(
        "legacy-1", "delete_tasks", "✅ Удалено 2 задачи")
    assert verdict == "ok"
    assert "✅ 2 подтверждено" in md
    assert getattr(md, "data", None) is None   # структуры нет — и это законно
    assert s._parse_verify_totals(legacy) == (2, 0, 0)
    assert s._verdict_from_totals(s._parse_verify_totals(legacy)) == "ok"
