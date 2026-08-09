"""Ревью ветки, дефекты Д3 и Д2 (2026-08-09) — заменитель безымянной задачи
(`_untitled_label`) конфликтовал с разделителем компактной строки, и его же
собственный вывод, поданный обратно сервером, читался как подмена объекта.

Д3 — КОЛЛИЗИЯ РАЗДЕЛИТЕЛЕЙ. `format_task_line` делит «название» и «мета»
символом «·» (U+00B7, с пробелами вокруг — `" · "`), и это ЗАМОРОЖЕННЫЙ
внешний контракт: его разбирает регулярками бот `tg-ai-assistant`. До правки
`_untitled_label` печатал ТОТ ЖЕ символ ВНУТРИ заменителя
(«(без названия · 📎 1 файл)»), и у безымянной задачи с вложением И любым
мета-полем строка несла «·» ДВАЖДЫ — разбор по первому разделителю рвал
название и мету пополам. Правка меняет только ВНУТРЕННИЙ разделитель на
двоеточие; внешний — трогать нельзя, он общий контракт.

Д2 — ЗАМЕНИТЕЛЬ, ВОЗВРАЩЁННЫЙ ОБРАТНО, ЧИТАЕТСЯ КАК ПОДЛОГ. Модель читает
список, видит у безымянной задачи заменитель и — как требуют описания
инструментов («имя сверяется с живым списком») — подставляет УВИДЕННОЕ
обратно как `title` в вызов мутатора (`complete_tasks` и любой другой,
идущий через `_guard_task`). Без распознавания сервер сравнивает непустой
заменитель с пустым живым названием и отказывает «id указывает на «», а НЕ
«(без названия: …)»» — штрафует модель за точное повторение собственного
вывода. `_is_untitled_placeholder` закрывает это ЯВНЫМ сравнением (не
подстрокой): заменитель признаётся только когда живое название пусто И
переданная строка совпадает символ в символ с тем, что `_untitled_label`
вычисляет для ЭТОГО ЖЕ живого объекта прямо сейчас.
"""
from datetime import datetime, timedelta, timezone

import ticktick_mcp.src.server as s
from tests.test_untitled_tasks import _receipt_task


# ═════════════ Д3 — один разделитель, а не два ═════════════

def test_untitled_task_with_attachment_and_due_date_has_one_separator():
    """Приёмка Д3: строка безымянной задачи с вложением И сроком несёт РОВНО
    ОДНО вхождение внешнего разделителя `" · "` — иначе внешний парсер делит
    название и мету не там."""
    due = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
    task = dict(_receipt_task(), dueDate=due)

    line = s.format_task_line(task, "Inbox")

    assert line.count(" · ") == 1, (
        f"два вхождения разошли бы регулярку внешнего парсера: {line!r}")
    assert "(без названия: 📎 1 файл)" in line


def test_untitled_task_with_text_and_priority_has_one_separator():
    """Тот же класс коллизии, другая пара заменитель+мета: «есть текст» +
    приоритет."""
    due = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    task = {"id": "t1", "title": "", "attachments": [],
            "content": "проверить возврат", "priority": 5, "dueDate": due}

    line = s.format_task_line(task, "Inbox")

    assert line.count(" · ") == 1, f"{line!r}"
    assert "(без названия: есть текст)" in line


def test_placeholder_colon_never_collides_with_the_frozen_separator():
    """Внутренний символ заменителя («:») и внешний разделитель компактной
    строки («·», U+00B7) обязаны остаться РАЗНЫМИ codepoint'ами — иначе
    коллизия просто переехала бы на другой символ."""
    label = s._untitled_label({"attachments": [{"fileName": "x.jpg", "id": "a" * 24}]})
    assert "·" not in label
    assert ":" in label


# ═════════════ Д2 — заменитель на входе узнаётся, не подлог ═════════════

def test_is_untitled_placeholder_recognises_the_exact_echo():
    """Юнит на саму функцию распознавания: живое имя пусто, переданная
    строка совпадает символ в символ с тем, что сервер печатает для ЭТОГО
    объекта прямо сейчас — заменитель узнан."""
    live = _receipt_task()
    label = s._untitled_label(live)

    assert s._is_untitled_placeholder(label, live) is True


def test_is_untitled_placeholder_is_exact_not_substring():
    """ЯВНОЕ сравнение, не «на глазок по подстроке» — строка, всего лишь
    СОДЕРЖАЩАЯ «без названия», заменителем не признаётся, если не совпадает
    полностью с вычисленным ПРЯМО СЕЙЧАС значением (здесь — устаревшее число
    вложений: было 2, стало 1)."""
    live = _receipt_task()  # 1 вложение
    stale_label = "(без названия: 📎 2 файла) вот это подозрительно похоже"

    assert s._is_untitled_placeholder(stale_label, live) is False


def test_is_untitled_placeholder_does_not_disarm_a_real_title():
    """Если у живой задачи ЕСТЬ название, заменителем ничего не признаётся —
    иначе можно было бы подсунуть текст заменителя вместо настоящей сверки
    имени именованной задачи."""
    live = {"id": "t1", "title": "Купить молоко", "projectId": "p1"}
    label = s._untitled_label(live)  # "(без названия)" — не про эту задачу

    assert s._is_untitled_placeholder(label, live) is False


class _FakeOfficialComplete:
    """Двойник v1-клиента: одиночный complete_task, как у настоящего."""

    def __init__(self, live):
        self.live = live
        self.calls = []

    def complete_task(self, project_id, task_id):
        self.calls.append((project_id, task_id))
        self.live.pop(task_id, None)          # завершённая покидает открытый пул
        return {"id": task_id}


class _FakeV2BatchComplete:
    """Двойник v2-клиента для батч-ветки `_complete_tasks_impl`."""

    def __init__(self, live):
        self.live = live
        self.calls = []

    def batch_complete_tasks(self, task_ids):
        ids = list(task_ids)
        self.calls.append(ids)
        for tid in ids:
            self.live.pop(tid, None)
        return {}


def _wire_complete(monkeypatch, live, tmp_path):
    """Как `_wire` из test_untitled_tasks, плюс клиент, умеющий ЗАВЕРШАТЬ
    (тамошний `_FakeOfficial` знает только `update_task`)."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: {"p_inbox": "Inbox", "p2": "Работа"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2 = _FakeV2BatchComplete(live)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    official = _FakeOfficialComplete(live)
    monkeypatch.setattr(s, "ticktick", official)
    return v2, official


async def test_placeholder_roundtrip_completes_a_single_task(monkeypatch, tmp_path):
    """Приёмка Д2, одиночный путь `_complete_tasks_impl` (len(tasks)==1,
    идёт через `_guard_task` напрямую, server.py:~1780). Строка, которую
    сервер САМ печатает для этой безымянной задачи, подана обратно как
    `title` — операция обязана пройти, а не отказать «id указывает на «»,
    а НЕ …»."""
    live = {"t_receipt": _receipt_task()}
    _wire_complete(monkeypatch, live, tmp_path)
    label = s._untitled_label(live["t_receipt"])

    out = await s._complete_tasks_impl(
        "завершаю", [{"taskId": "t_receipt", "projectId": "p_inbox",
                      "title": label}])

    assert "🛑" not in out, out
    assert "id указывает на" not in out, out
    assert "t_receipt" not in live, "задача не завершилась"
    assert "✓" in out


async def test_placeholder_roundtrip_completes_in_a_batch(monkeypatch, tmp_path):
    """Тот же приём, батч-путь (len(tasks)>1 → `_split_tasks_by_state` →
    `_guard_task`, server.py:~1932/1780) — вторая безымянная задача рядом,
    чтобы попасть именно в батч-ветку."""
    live = {"t_receipt": _receipt_task(),
            "t_named": {"id": "t_named", "projectId": "p_inbox",
                        "title": "Купить молоко"}}
    _wire_complete(monkeypatch, live, tmp_path)
    label = s._untitled_label(live["t_receipt"])

    out = await s._complete_tasks_impl("завершаю", [
        {"taskId": "t_receipt", "projectId": "p_inbox", "title": label},
        {"taskId": "t_named", "projectId": "p_inbox", "title": "Купить молоко"}])

    assert "🛑" not in out, out
    assert "id указывает на" not in out, out
    assert "t_receipt" not in live and "t_named" not in live
    assert "Завершено 2" in out, out


async def test_a_genuinely_wrong_title_on_the_same_task_is_still_refused(
        monkeypatch, tmp_path):
    """ГРАНИЦА: распознавание заменителя не отключает сверку имени вообще —
    случайное чужое название на том же id по-прежнему ловится как подмена
    объекта."""
    live = {"t_receipt": _receipt_task()}
    _wire_complete(monkeypatch, live, tmp_path)

    out = await s._complete_tasks_impl(
        "завершаю", [{"taskId": "t_receipt", "projectId": "p_inbox",
                      "title": "Совсем другое название"}])

    assert "t_receipt" in live, "задача не должна была завершиться"
    assert "🛑" in out
    assert "Совсем другое название" in out
