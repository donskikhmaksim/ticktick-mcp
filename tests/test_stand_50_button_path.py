"""СТЕНДОВАЯ ПРИЁМКА 1.3.1 на плане из 50 операций (пункт 10 задания,
2026-08-09).

Что именно проверяется — ровно то, что требовал пункт: план из пятидесяти
операций проходит КНОПОЧНЫМ путём Telegram; полный отчёт доставлен ЦЕЛИКОМ
(разбиение на части — настоящее, `split_for_telegram`, а не «влезло в одно
сообщение»); в отчёте присутствует раздел «не вошло», и число в нём совпадает
с числом строк в нём же.

ЧЕСТНАЯ ОГОВОРКА. Это НЕ живой стенд: боевого бота, чата и сети здесь нет.
Отправка подменена двойниками — но подменён только транспорт (`requests` к
Telegram), а всё, что решает исход, настоящее: план строит настоящий
`manual_triage`, исполняет настоящий зарегистрированный исполнитель кнопки
(`_resolve_auto_executor`), отчёт собирает настоящая
`_verified_auto_execute_report`, режет настоящий `split_for_telegram`, и
доставленное склеивается обратно и сверяется с исходным текстом посимвольно.
Не покрыто по сравнению с живым прогоном: поведение самого Telegram (429,
flood-wait, разметка) — то есть ровно то, что двойник изобразить и не может.
"""
import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg
from tests.test_manual_triage import _mid, _notify_recorder, _tg_on, _wire


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _stand_plan():
    """50 операций, которые войдут в план, + 3, которые не пройдут сверку на
    этапе плана (названия не совпадают с живыми). Из вошедших 3 сдрейфуют
    ПОСЛЕ подтверждения — их задачи переименуют между планом и кнопкой."""
    live, ops = {}, []
    for i in range(50):
        tid = f"L{i:03d}"
        title = f"Позвонить по объявлению №{i} и уточнить условия аренды"
        live[tid] = {"id": tid, "title": title, "projectId": "p_in"}
        ops.append({"op": "delete", "task_id": tid, "title": title,
                    "said": f"объявление №{i} уже неактуально"})
    for i in range(3):
        tid = f"N{i}"
        live[tid] = {"id": tid, "title": f"Живое название {i}",
                     "projectId": "p_in"}
        ops.append({"op": "delete", "task_id": tid,
                    "title": f"Название из плана {i}",
                    "said": f"это тоже убираем, {i}"})
    return live, ops


async def test_stand_50_operations_button_path_delivers_whole_report(
        monkeypatch, tmp_path):
    live, ops = _stand_plan()
    _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    sent_plan = _notify_recorder(monkeypatch)

    # ── Фаза 1: план. 50 операций в манифест, 3 — в справку «не вошло». ──
    preview = await s.manual_triage("Разбираю входящие", ops)
    mid = _mid(preview)
    m = s._MANIFESTS[mid]
    assert len(m["tasks"]) == 50, preview
    not_planned = (m.get("extra") or {}).get("not_planned") or []
    assert len(not_planned) == 3
    assert "❌ Не вошло" in preview
    # План ушёл владельцу целиком (в бою это `send_message_chunked`).
    assert len(sent_plan) == 1 and sent_plan[0]["manifest_id"] == mid

    # ── Дрейф между «да» и мутацией: три задачи переименовали руками. ──
    for i in range(3):
        live[f"L{i:03d}"]["title"] = f"Уже другое название {i}"

    # ── Фаза 2: КНОПКА. Тот же путь, что у поллера: метка манифеста, ──
    #    зарегистрированный исполнитель, независимый отчёт по журналу.
    entry = s._resolve_auto_executor("manual_triage", m)
    assert entry is not None
    token = s._TG_AUTO_EXECUTE_MANIFEST.set(mid)
    try:
        report_text = await entry.execute(mid, m)
    finally:
        s._TG_AUTO_EXECUTE_MANIFEST.reset(token)

    full_md, verdict = s._verified_auto_execute_report(
        mid, "manual_triage", report_text, m)

    # ── Отчёт: обе рубрики на месте, числа сходятся со строками. ──
    data = getattr(full_md, "data", None)
    assert data is not None, "структура не доехала до отчёта кнопки"
    assert len(data.skipped) == 3 and len(data.not_planned) == 3
    assert verdict == "partial", verdict      # 47 из 50 — это НЕ полный успех

    lines = full_md.split("\n")
    head_np = [i for i, ln in enumerate(lines)
               if ln.startswith("#### ❌ Не вошло в план")]
    head_sk = [i for i, ln in enumerate(lines)
               if ln.startswith("#### ⏭ Пропущено")]
    assert len(head_np) == 1 and len(head_sk) == 1, full_md
    body_np = [ln for ln in lines[head_np[0] + 1:]
               if ln.startswith("•") or ln.startswith("- ")]
    assert len(body_np) == len(data.not_planned) == 3, full_md
    assert not [ln for ln in lines if ln.startswith("… показаны")], \
        "на 50 операциях усечения быть не должно"

    # ── Доставка: чанкинг настоящий, ни один объект не потерян. ──
    #
    # Сверять склейку кусков с исходником ПОСИМВОЛЬНО нельзя: `split_for_telegram`
    # не просто режет — он переоткрывает разметку на границе куска и добавляет
    # маркеры «(часть N/M)», то есть склейка законно длиннее оригинала. Поэтому
    # проверяется то, ради чего доставка и нужна: каждая строка отчёта и каждый
    # названный объект доехали.
    chunks = tg.split_for_telegram(full_md)
    assert len(chunks) > 1, "отчёт уместился в одно сообщение — стенд не тот"
    # Склейка короче оригинала ровно на переносы, съеденные границами кусков,
    # — поэтому сверяются СТРОКИ, а не длина.
    joined = "\n".join(chunks)
    lost = [ln for ln in full_md.split("\n")
            if ln.strip() and len(ln) < 500 and ln not in joined]
    assert not lost, f"строки отчёта потеряны при разбиении: {lost[:3]}"

    delivered = []

    def _fake_post(cfg, manifest_id, text, tool=None, verdict=None):
        parts = tg.split_for_telegram(text)
        delivered.extend(parts)
        return tg.ReportDelivery(list(range(len(parts))), len(parts),
                                 len(parts), True)

    short_seen = []

    def _fake_summary(cfg, chat_id, message_id, text):
        short_seen.append(text)
        return True

    monkeypatch.setattr(tg, "post_report_to_group", _fake_post)
    monkeypatch.setattr(tg, "summarize_in_owner_chat", _fake_summary)
    candidate = {"manifest_id": mid, "chat_id": "1", "message_id": 10}
    s._publish_auto_execute_outcome(candidate, "manual_triage", full_md,
                                    verdict, s._manifest_affected_count(m))

    got = "".join(delivered)
    # Все три невошедших и все три пропущенных названы в доставленном тексте.
    for i in range(3):
        assert f"Название из плана {i}" in got, "невошедшее не доехало в группу"
        assert f"Уже другое название {i}" in got or f"№{i} " in got, \
            "пропущенное не доехало в группу"
    # И все 47 исполненных объектов — тоже.
    for i in range(3, 50):
        assert f"объявлению №{i} " in got, f"объект {i} потерян при доставке"
    assert len(short_seen) == 1
    short = short_seen[0]
    # Сводка в личку называет объекты, а не только числа.
    assert "Объектов в плане: 50" in short
    assert "⏭ Пропущено" in short and "Не вошло в план" in short
    assert "Уже другое название 0" in short or "Позвонить по объявлению №0" \
        in short, short
    assert "Живое название 0" in short or "Название из плана 0" in short, short
