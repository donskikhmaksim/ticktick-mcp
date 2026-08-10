"""1.3.5, "Готово, когда" п.7 — regression П15: найденная безымянная задача
проходит полный цикл (показ → переименование → удаление), и на КАЖДОМ шаге
согласуется с новым инструментом обзора `find_untitled_tasks` (1.3.5, отдельно
покрыт `tests/test_find_untitled.py`).

До этого файла три соседних куска П15 были покрыты каждый по отдельности:
  - показ и машинный признак `Untitled: true` — `tests/test_untitled_tasks.py`
    и `tests/test_untitled_machine_marker.py` (1.1.7);
  - переименование/удаление безымянной задачи по id, без сверки названия
    (сверять нечего) — `tests/test_untitled_tasks.py` (Н9/1.1.1);
  - обзорный подсчёт по всему аккаунту — `tests/test_find_untitled.py`
    (1.3.5 п.1-6).
Но ни один файл не проверял, что ОДНА И ТА ЖЕ задача, проведённая через все
эти механизмы подряд, остаётся ОДНИМ и тем же объектом с одной и той же
меткой — то есть что `find_untitled_tasks` не расходится с показом и
переименованием на общем сценарии. Ради этого пункт 7 в "Готово, когда"
требует отдельного регрессионного файла.

Второй вопрос, прицельно проверенный здесь (по прямому требованию задания —
"прочитай код, не выдумывай"): Н9 ("каждая правка title обязана нести текущее
название или явный untitled=true, иначе отказ ДО записи") не ломает законное
переименование ДЕЙСТВИТЕЛЬНО безымянной задачи. Чтение `_title_check_armed`
(server.py) показывает, что сверка считается ВЗВЕДЁННОЙ (не отказ) в трёх
случаях: явное видимое title; явный untitled=true, подтверждённый живым
состоянием; ИЛИ — без всякого флага — когда переданное имя пусто И живое имя
тоже пусто (по id, потому что сравнивать нечего). Отказывает Н9 только когда
title не передан, А У ЖИВОЙ ЗАДАЧИ РЕАЛЬНОЕ ИМЯ ЕСТЬ — то есть ровно тот
случай, когда слепая правка стёрла бы что-то настоящее. Тесты 3 и 4 ниже
проверяют оба исхода на контрасте.
"""
import re
import time

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


def _untitled_receipt(tid="t_receipt", pid="p_inbox"):
    """Задача-«пустышка», которая на самом деле документ — тот самый чек
    Home Depot из живого случая, ради которого весь П15 затеян."""
    return {"id": tid, "projectId": pid, "title": "",
            "attachments": [{"fileName": "home-depot-receipt.jpg",
                             "id": "a" * 24}]}


class _FakeV2:
    """Двойник v2-клиента — те же вызовы, что реально делают проверяемые
    пути (переименование и удаление)."""

    def __init__(self, live):
        self.live = live
        self.trash = {}
        self.calls = []

    def invalidate_cache(self):
        pass

    def get_state(self, force=False):
        return {"tags": []}

    def get_tags(self):
        return []

    def get_open_tasks(self):
        return list(self.live.values())

    def batch_update_tasks(self, changes):
        self.calls.append(("update", changes))
        for c in changes:
            t = self.live.setdefault(c["taskId"], {"id": c["taskId"]})
            if "title" in c:
                t["title"] = c["title"]
        return {}

    def batch_delete_tasks(self, items):
        self.calls.append(("delete", [it["taskId"] for it in items]))
        for it in items:
            self.live.pop(it["taskId"], None)
        return {}

    def find_task_any_state(self, task_id):
        if task_id in self.live:
            return self.live[task_id], "open"
        if task_id in self.trash:
            return self.trash[task_id], "trash"
        return None, None

    def get_trash(self, limit=500):
        return list(self.trash.values())[:limit]


class _FakeOfficial:
    """Двойник официального клиента — одиночная правка идёт через него."""

    def __init__(self, live):
        self.live = live
        self.calls = []

    def update_task(self, task_id, project_id, title=None, content=None,
                    start_date=None, due_date=None, priority=None,
                    repeat_flag=None, reminders=None):
        self.calls.append(("update", task_id))
        t = self.live.setdefault(task_id, {"id": task_id})
        if title is not None:
            t["title"] = title
        return {"id": task_id}


def _wire(monkeypatch, live, names=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: names or {"p_inbox": "Inbox"})
    fake = _FakeV2(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    official = _FakeOfficial(live)
    monkeypatch.setattr(s, "ticktick", official)
    return fake, official


# ═══════ 1. Показ: заменитель И машинный признак согласованы (1.1.7) ═══════

def test_untitled_task_card_shows_placeholder_and_machine_marker():
    task = _untitled_receipt()
    out = s.format_task(task)
    assert "Title: (без названия: 📎 1 файл)" in out
    assert "Untitled: true" in out

    normal = {"id": "n1", "projectId": "p_inbox", "title": "Купить молоко"}
    out_normal = s.format_task(normal)
    assert "Untitled:" not in out_normal


# ═ 2. find_untitled_tasks видит ТУ ЖЕ задачу с ТЕМ ЖЕ ярлыком (1.3.5) ═

async def test_find_untitled_tasks_reports_the_same_label_and_category(
        monkeypatch):
    task = _untitled_receipt()
    live = {"t_receipt": task}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_inbox": "Inbox"})

    out = await s.find_untitled_tasks()

    # Ярлык в обзоре — БУКВАЛЬНО тот же вызов, что уже показал карточку
    # (согласованность, а не два независимых текста, которые могут разъехаться).
    expected_label = s._untitled_label(task)
    assert expected_label in out
    assert expected_label in s.format_task(task)
    assert "Безымянных задач найдено: 1" in out
    assert "с вложениями — 1" in out
    assert "действительно пустых — 0" in out


# ═ 3. КОНТРОЛЬ — Н9 отказывает переименовать ИМЕНОВАННУЮ задачу вслепую ═

async def test_renaming_a_named_task_without_old_title_is_refused_before_write(
        monkeypatch):
    live = {"t_named": {"id": "t_named", "projectId": "p_inbox",
                        "title": "Оплатить аренду"}}
    _wire(monkeypatch, live)

    preview = await s.update_tasks("переименовать", [
        {"taskId": "t_named", "projectId": "p_inbox",
         "new_title": "Другое имя"}])

    assert "🛑" in preview
    assert "НЕТ текущего названия" in preview
    # Ничего не записано — отказ случился ДО обращения к TickTick.
    assert live["t_named"]["title"] == "Оплатить аренду"


# ═ 4. Безымянная задача — Н9 её из этой сверки ИСКЛЮЧАЕТ (по id) ═

async def test_renaming_the_untitled_task_without_old_title_is_allowed_by_id(
        monkeypatch):
    """Сверять нечего с обеих сторон (переданное имя пусто, живое тоже пусто)
    — Н9 признаёт объект опознанным ПО id и переименование проходит штатно,
    БЕЗ передачи title и БЕЗ маркера untitled=true (автоматический путь,
    `_title_check_armed`, третья ветка)."""
    live = {"t_receipt": _untitled_receipt()}
    fake, official = _wire(monkeypatch, live)

    preview = await s.update_tasks("назвать чек", [
        {"taskId": "t_receipt", "projectId": "p_inbox",
         "new_title": "Чек Home Depot — возврат $374.92"}])
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)

    out = await s.update_tasks("назвать чек", manifest_id=mid, user_reply="да")

    assert "🛑" not in out
    assert live["t_receipt"]["title"] == "Чек Home Depot — возврат $374.92"

    # Согласованность с 1.3.5: та же самая задача, только что переименованная,
    # больше не безымянная — обзор её не находит.
    out_scan = await s.find_untitled_tasks()
    assert "Безымянных задач среди открытых не найдено (найдено: 0)" in out_scan


# ═ 5. Тот же переход, но ЯВНЫМ untitled=true — второй предусмотренный путь ═

async def test_legit_rename_via_explicit_untitled_marker_also_drops_out(
        monkeypatch):
    """docstring `update_tasks`: "If the task GENUINELY has no name ... pass
    'untitled': true INSTEAD of 'title'". Это ОТДЕЛЬНЫЙ от автоматического
    by-id-пути механизм (структурный маркер, а не молчаливое совпадение
    пустот) — и он обязан приводить к тому же результату для 1.3.5."""
    live = {"t_screenshot": {"id": "t_screenshot", "projectId": "p_inbox",
                             "title": "", "content": "",
                             "attachments": [{"fileName": "defect.png",
                                              "id": "b" * 24}]}}
    _wire(monkeypatch, live)

    before = await s.find_untitled_tasks()
    assert "Безымянных задач найдено: 1" in before

    preview = await s.update_tasks("назвать скриншот", [
        {"taskId": "t_screenshot", "projectId": "p_inbox", "untitled": True,
         "new_title": "Скриншот дефекта — баг #482"}])
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)
    out = await s.update_tasks("назвать скриншот", manifest_id=mid,
                               user_reply="да")

    assert "🛑" not in out
    assert live["t_screenshot"]["title"] == "Скриншот дефекта — баг #482"

    after = await s.find_untitled_tasks()
    assert "Безымянных задач среди открытых не найдено (найдено: 0)" in after


# ═══════ 6. Полный цикл дословно из "Готово, когда" п.7 ═══════

async def test_full_cycle_find_show_rename_delete_stays_consistent(
        monkeypatch):
    """Найденная `find_untitled_tasks` безымянная задача проходит показ,
    переименование и удаление — и на каждом шве обзор 1.3.5 говорит правду
    про её текущее состояние."""
    live = {"t_receipt": _untitled_receipt()}
    fake, official = _wire(monkeypatch, live)

    # 1) НАЙДЕНА обзором (1.3.5)
    scan_before = await s.find_untitled_tasks()
    assert "Безымянных задач найдено: 1" in scan_before
    assert "t_receipt" in scan_before

    # 2) ПОКАЗ — та же задача, тот же заменитель, машинный признак (1.1.7)
    shown = s.format_task_list([live["t_receipt"]])
    assert "(без названия: 📎 1 файл)" in shown
    assert s._untitled_label(live["t_receipt"]) in scan_before

    # 3) ПЕРЕИМЕНОВАНИЕ — маркером untitled=true (Н9/1.1.1), без старого title
    preview = await s.update_tasks("назвать", [
        {"taskId": "t_receipt", "projectId": "p_inbox", "untitled": True,
         "new_title": "Чек Home Depot"}])
    assert "🛑" not in preview
    mid = _extract_manifest_id(preview)
    await s.update_tasks("назвать", manifest_id=mid, user_reply="да")
    assert live["t_receipt"]["title"] == "Чек Home Depot"

    # 4) Обзор 1.3.5 теперь честно не находит её — она больше не безымянная
    scan_after_rename = await s.find_untitled_tasks()
    assert "Безымянных задач среди открытых не найдено (найдено: 0)" in scan_after_rename

    # 5) УДАЛЕНИЕ — уже обычным путём, по новому имени
    plan = await s.plan_task_deletion("убрать", [
        {"taskId": "t_receipt", "title": "Чек Home Depot"}])
    dmid = _extract_manifest_id(plan)
    out = await s.execute_task_deletion(dmid, user_reply="да")

    assert "t_receipt" not in live
    assert "Удалено 1" in out
