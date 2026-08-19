"""docs/DESIGN_approval_gate.md §9 п.7 (аудит 2026-08-19): происхождение
согласия («кнопка в Telegram/веб-подтверждение» vs «текстовое да» vs
«аварийный выключатель гейта») обязано быть видно в журнале мутаций и в
человекочитаемом operation_report — не только в сыром JSON-поле.

ДО этой правки `TG_AUTO_REPLY_MARKER` в tg_approval.py была объявлена и НИ
РАЗУ не читалась (try_auto_execute идёт мимо _require_consent целиком — см.
её собственный докстринг), а поле `"tg_manifest"`, которое реально кладёт
`_journal_write` на записи, сделанные во время авто-исполнения по кнопке,
нигде не превращалось в текст отчёта — в отличие от `automation_channel`,
у которого уже был свой `_automation_channel_note`. `s._tg_button_note` —
новая функция; на коде без неё эти тесты падают AttributeError'ом, что и
доказывает дефект.

Ни сети, ни Postgres — journal читается с диска (tmp_path), live-состояние
подставное, как в tests/test_delete_project.py."""
import json

import ticktick_mcp.src.server as s


# ===========================================================================
# _tg_button_note — чистая функция, без журнала/диска
# ===========================================================================

def test_no_note_when_no_record_carries_tg_manifest():
    records = [{"op": "delete", "items": []}, {"op": "update", "items": []}]
    assert s._tg_button_note(records) == ""


def test_note_when_any_record_carries_tg_manifest():
    records = [{"op": "delete", "items": []},
              {"op": "update", "items": [], "tg_manifest": "m-abc123"}]
    note = s._tg_button_note(records)
    assert note != ""
    assert "кнопк" in note.lower()


def test_note_ignores_falsy_tg_manifest_values():
    """Пустая строка/None в поле — не значит «подтверждено кнопкой»: поле
    просто не должно было попасть в запись вовсе (см. _journal_write —
    `if mid and not record.get("tg_manifest")`, mid никогда не пуст, когда
    ставится). Проверка на всякий случай, чтобы будущий рефактор не завёл
    ложный "" в это поле."""
    assert s._tg_button_note([{"tg_manifest": ""}, {"tg_manifest": None}]) == ""


# ===========================================================================
# _build_operation_report_data — та же метка, но через настоящий журнал на
# диске и настоящий текст отчёта, который видит владелец
# ===========================================================================

def _write_journal(tmp_path, record: dict):
    path = tmp_path / "deletion_journal.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_button_confirmed_operation_is_noted_in_the_human_readable_report(
        monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    monkeypatch.setattr(s, "_v2_project_names", lambda: {})
    _write_journal(tmp_path, {
        "ts": "2026-08-19T12:00:00+00:00", "record": "delete-aaaa1111",
        "op": "delete", "items": [{"taskId": "t1", "title": "Задача"}],
        "tg_manifest": "m-aaaa1111",
    })

    report = s._build_operation_report("delete-aaaa1111")

    assert "кнопк" in report.lower()
    assert "delete-aaaa1111" in report


def test_plain_text_confirmed_operation_has_no_button_note(monkeypatch, tmp_path):
    """Контроль: обычное текстовое «да» (без TG-слоя вовсе) ничего не
    помечает — журнал побайтово прежний, отчёт не врёт про кнопку, которой
    не было."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    monkeypatch.setattr(s, "_v2_project_names", lambda: {})
    _write_journal(tmp_path, {
        "ts": "2026-08-19T12:00:00+00:00", "record": "delete-bbbb2222",
        "op": "delete", "items": [{"taskId": "t2", "title": "Задача 2"}],
    })

    report = s._build_operation_report("delete-bbbb2222")

    assert "кнопк" not in report.lower()


def test_gate_disabled_operation_is_noted_but_not_as_a_button(monkeypatch, tmp_path):
    """Третий исход — аварийный выключатель гейта: помечен `automation_channel`
    (существующий канал), а НЕ `tg_manifest` — печатается СВОЯ строка
    (`_automation_channel_note`), не строка про кнопку. Три происхождения
    согласия (кнопка / текст / выключенный гейт) должны различаться в
    отчёте, а не сливаться в одно "подтверждено как-то"."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    monkeypatch.setattr(s, "_v2_project_names", lambda: {})
    _write_journal(tmp_path, {
        "ts": "2026-08-19T12:00:00+00:00", "record": "delete-cccc3333",
        "op": "delete", "items": [{"taskId": "t3", "title": "Задача 3"}],
        "automation_channel": "gate_disabled",
    })

    report = s._build_operation_report("delete-cccc3333")

    assert "кнопк" not in report.lower()
    assert "Исполнено автоматикой" in report


# ===========================================================================
# TG_AUTO_REPLY_MARKER — убрана, а не просто переименована
# ===========================================================================

def test_tg_auto_reply_marker_constant_no_longer_exists():
    """Мёртвая константа удалена целиком (§9 п.7) — рабочий механизм теперь
    `tg_manifest`-поле журнала + `_tg_button_note`, проверенные выше."""
    import ticktick_mcp.src.tg_approval as tg
    assert not hasattr(tg, "TG_AUTO_REPLY_MARKER")
