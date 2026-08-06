"""manual_triage — смоук-набор (полный набор пишет следующий агент).

Что здесь закреплено:
  1. call #1 НИЧЕГО не мутирует и создаёт одноразовый манифест;
  2. call #2 без согласия человека отказывает и ничего не трогает;
  3. в манифест попадают РОВНО переданные операции — ни одной лишней задачи
     из живого состояния (главный урок отключённого plan_declutter: тул не
     имеет права сам добирать кандидатов);
  4. валидация fail-closed: без `said` / с дублем task_id / с merge, чья
     «оставляемая» копия удаляется этим же планом — отказ ЦЕЛИКОМ, без
     манифеста;
  5. полный цикл: смешанный план из 5 разнородных операций исполняется одним
     подтверждением, и итог считается по НЕЗАВИСИМОЙ сверке.

Стиль обвязки — как в tests/test_slice1_real_gates.py: живое состояние это
обычный dict, который фейковые клиенты мутируют, поэтому пост-проверка видит
результат.
"""
import re

import ticktick_mcp.src.server as s


def _mid(preview: str) -> str:
    m = re.search(r"Манифест `([0-9a-f]+)`", preview)
    assert m, f"в превью нет id манифеста:\n{preview}"
    return m.group(1)


class _FakeV2:
    def __init__(self, live):
        self.live = live
        self.calls = []

    def invalidate_cache(self):
        pass

    def batch_update_tasks(self, changes):
        self.calls.append(("update", changes))
        for c in changes:
            t = self.live.setdefault(c["taskId"], {"id": c["taskId"]})
            for k, v in c.items():
                if k != "taskId":
                    t[k] = v
        return {}

    def batch_complete_tasks(self, ids):
        self.calls.append(("complete", list(ids)))
        for tid in ids:
            self.live.pop(tid, None)
        return {}

    def batch_move_tasks(self, ids, to_project_id):
        self.calls.append(("move", list(ids), to_project_id))
        for tid in ids:
            if tid in self.live:
                self.live[tid]["projectId"] = to_project_id
        return {}

    def batch_delete_tasks(self, rows):
        self.calls.append(("delete", [r["taskId"] for r in rows]))
        for r in rows:
            self.live.pop(r["taskId"], None)
        return {}

    def set_task_tags(self, task_id, tags):
        self.calls.append(("tags", task_id, tags))
        if task_id in self.live:
            self.live[task_id]["tags"] = tags


class _FakeOfficial:
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
        if priority is not None:
            t["priority"] = priority
        if due_date is not None:
            t["dueDate"] = due_date
        if start_date is not None:
            t["startDate"] = start_date
        return {"id": task_id}

    def complete_task(self, project_id, task_id):
        self.calls.append(("complete", task_id))
        self.live.pop(task_id, None)
        return {"id": task_id}


_NAMES = {"p_in": "Входящие", "p_work": "Работа"}


def _wire(monkeypatch, live, tmp_path, names=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(names or _NAMES))
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2 = _FakeV2(live)
    official = _FakeOfficial(live)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", official)
    return v2, official


def _live_inbox():
    return {
        "a1": {"id": "a1", "title": "Купить молоко", "projectId": "p_in"},
        "b2": {"id": "b2", "title": "Отчёт", "projectId": "p_work"},
        "c3": {"id": "c3", "title": "Позвонить Ивану", "projectId": "p_in"},
        "d4": {"id": "d4", "title": "Оплатить интернет", "projectId": "p_in"},
        "e5": {"id": "e5", "title": "Позвонить в банк", "projectId": "p_in"},
        "e6": {"id": "e6", "title": "Позвонить в банк", "projectId": "p_work"},
        # НЕ участвует ни в одной операции — не должна попасть в план.
        "zz": {"id": "zz", "title": "Посторонняя задача", "projectId": "p_in"},
    }


def _mixed_ops():
    return [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "это уже неактуально"},
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {"new_title": "Сдать отчёт за июль", "priority": 5},
         "said": "переименуй и поставь высокий приоритет"},
        {"op": "move", "task_id": "c3", "title": "Позвонить Ивану",
         "to_project_id": "p_work", "said": "это рабочее"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "уже сделал"},
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_task_id": "e6", "keep_title": "Позвонить в банк",
         "said": "это одно и то же, оставь одну"},
    ]


# ═══════════════ 1. call #1 — предпросмотр, ничего не тронуто ═══════════════

async def test_call1_previews_and_mutates_nothing(monkeypatch, tmp_path):
    live = _live_inbox()
    before = {k: dict(v) for k, v in live.items()}
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю входящие", _mixed_ops())

    assert v2.calls == [] and official.calls == []
    assert live == before, "call #1 не имеет права ничего менять"
    assert "Манифест" in preview
    # Слова человека видны в предпросмотре — по ним он и узнаёт свою строку.
    assert "по вашим словам: «это уже неактуально»" in preview
    # Реальные названия задач и проектов, а не голые id.
    assert "«Купить молоко»" in preview and "«Входящие»" in preview
    # …и НИ ОДНОГО голого id задачи в самих строках плана (id манифеста —
    # это hex, он живёт в отдельной строке и под эту проверку не попадает).
    plan_lines = [ln for ln in preview.splitlines() if re.match(r"^\d+\. ", ln)]
    assert len(plan_lines) == 5
    for tid in ("a1", "b2", "c3", "d4", "e5", "e6"):
        assert not any(tid in ln for ln in plan_lines), f"{tid} утёк в план"


async def test_preview_orders_least_destructive_first(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю входящие", _mixed_ops())

    order = [preview.index(x) for x in ("✏️ Изменить", "↪ Перенести",
                                        "✅ Закрыть", "🔗 Объединить",
                                        "🗑 Удалить")]
    assert order == sorted(order), f"порядок разрушительности нарушен:\n{preview}"


# ═════════ 2. Манифест содержит РОВНО переданное — ни задачей больше ════════

async def test_manifest_holds_exactly_the_given_operations(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю входящие", _mixed_ops())
    m = s._MANIFESTS[_mid(preview)]

    assert m["kind"] == "manual_triage" and m["tool"] == "manual_triage"
    assert m["_gate"] == "batch"
    assert [o["task_id"] for o in m["tasks"]] == ["b2", "c3", "d4", "e5", "a1"]
    # Ни «zz», ни keep-копия «e6» не превратились в СВОЮ операцию: keep только
    # оставляют, её id живёт внутри merge-операции, а не отдельной строкой.
    assert "zz" not in [o["task_id"] for o in m["tasks"]]
    assert "e6" not in [o["task_id"] for o in m["tasks"]]
    assert len(m["tasks"]) == 5


async def test_summary_gets_the_per_type_counts(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю входящие", _mixed_ops())

    assert ("Разбираю входящие — изменить 1, перенести 1, закрыть 1, "
            "объединить 1, удалить 1") in preview


# ═════════════ 3. call #2 без согласия — отказ, ничего не тронуто ═══════════

async def test_call2_without_reply_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    before = {k: dict(v) for k, v in live.items()}
    v2, official = _wire(monkeypatch, live, tmp_path)
    preview = await s.manual_triage("Разбираю входящие", _mixed_ops())
    mid = _mid(preview)

    refused = await s.manual_triage("Разбираю входящие", manifest_id=mid,
                                    user_reply="")

    assert "🛑" in refused
    assert v2.calls == [] and official.calls == []
    assert live == before
    assert s._MANIFESTS[mid]["consumed"] is False  # пустой ответ не сжигает план


async def test_explicit_no_burns_the_plan(monkeypatch, tmp_path):
    live = _live_inbox()
    v2, official = _wire(monkeypatch, live, tmp_path)
    preview = await s.manual_triage("Разбираю входящие", _mixed_ops())
    mid = _mid(preview)

    assert "🛑" in await s.manual_triage("Разбираю", manifest_id=mid,
                                         user_reply="нет, стоп")
    dead = await s.manual_triage("Разбираю", manifest_id=mid, user_reply="да")

    assert "🛑" in dead
    assert v2.calls == [] and official.calls == []
    assert "a1" in live


# ═══════════════════ 4. Валидация — отказ ЦЕЛИКОМ, без манифеста ════════════

async def test_empty_said_refuses_the_whole_plan(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    ops = _mixed_ops()
    ops[0]["said"] = ""

    out = await s.manual_triage("Разбираю", ops)

    assert "🛑" in out and "said" in out
    assert "Манифест" not in out


async def test_duplicate_task_id_refuses_the_whole_plan(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    ops = [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко", "said": "не нужно"},
        {"op": "complete", "task_id": "a1", "title": "Купить молоко", "said": "сделал"},
    ]

    out = await s.manual_triage("Разбираю", ops)

    assert "🛑" in out and "дважды" in out
    assert "Манифест" not in out


async def test_merge_keeping_a_task_that_is_also_deleted_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    ops = [
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_task_id": "e6", "keep_title": "Позвонить в банк", "said": "дубли"},
        {"op": "delete", "task_id": "e6", "title": "Позвонить в банк",
         "said": "и эту тоже снеси"},
    ]

    out = await s.manual_triage("Разбираю", ops)

    assert "🛑" in out and "обе копии" in out
    assert "Манифест" not in out


async def test_unknown_op_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "archive", "task_id": "a1", "title": "Купить молоко", "said": "в архив"}])

    assert "🛑" in out and "archive" in out


async def test_tool_has_no_filter_or_scope_parameter():
    """Главный инвариант после declutter-инцидента: у тула физически нет
    входа, через который он мог бы «просканировать и предложить»."""
    import inspect
    params = set(inspect.signature(s.manual_triage).parameters)
    assert params == {"summary", "operations", "max_items", "manifest_id",
                      "user_reply"}


# ═══════════════════ 5. Полный цикл одного подтверждения ════════════════════

async def test_full_cycle_applies_every_operation_once(monkeypatch, tmp_path):
    live = _live_inbox()
    v2, official = _wire(monkeypatch, live, tmp_path)
    preview = await s.manual_triage("Разбираю входящие", _mixed_ops())
    mid = _mid(preview)

    out = await s.manual_triage("Разбираю входящие", manifest_id=mid,
                                user_reply="да, давай")

    assert "a1" not in live                       # delete
    assert live["b2"]["title"] == "Сдать отчёт за июль"   # update
    assert live["c3"]["projectId"] == "p_work"    # move
    assert "d4" not in live                       # complete
    assert "e5" not in live and "e6" in live      # merge: дубль ушёл, оригинал жив
    assert live["zz"]["title"] == "Посторонняя задача"    # чужая не тронута
    assert "✅ Выполнено 5 из 5" in out
    assert "🗑 Удалено 1" in out and "🔗 Объединено 1" in out
    assert "❌ Не подтверждено сверкой: 0" in out


async def test_manifest_is_one_shot(monkeypatch, tmp_path):
    live = _live_inbox()
    v2, official = _wire(monkeypatch, live, tmp_path)
    preview = await s.manual_triage("Разбираю", _mixed_ops())
    mid = _mid(preview)
    await s.manual_triage("Разбираю", manifest_id=mid, user_reply="да")
    calls_after = (len(v2.calls), len(official.calls))

    second = await s.manual_triage("Разбираю", manifest_id=mid, user_reply="да")

    assert "🛑" in second
    assert (len(v2.calls), len(official.calls)) == calls_after


async def test_drifted_task_is_skipped_not_applied(monkeypatch, tmp_path):
    """Между планом и «да» человек переименовал задачу руками — операция по
    ней НЕ исполняется, а честно уходит в «пропущено»."""
    live = _live_inbox()
    v2, official = _wire(monkeypatch, live, tmp_path)
    preview = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    live["a1"]["title"] = "Купить молоко и хлеб"

    out = await s.manual_triage("Разбираю", manifest_id=mid, user_reply="да")

    assert "a1" in live, "сдрейфовавшая задача не должна быть удалена"
    assert "d4" not in live
    assert "✅ Выполнено 1 из 2" in out
    assert "Пропущено" in out and "название изменилось" in out


async def test_operation_on_a_vanished_task_is_marked_skipped_at_plan_time(
        monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "ghost", "title": "Старая задача",
         "said": "давно неактуально"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])

    assert "ПРОПУЩЕНО" in preview and "не найдена среди открытых" in preview
    assert "пропущено 1" in preview
    m = s._MANIFESTS[_mid(preview)]
    assert [o["task_id"] for o in m["tasks"]] == ["d4", "ghost"]


async def test_plan_where_everything_is_skipped_is_not_built(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "ghost", "title": "Старая задача",
         "said": "неактуально"}])

    assert "🛑" in out and "Манифест" not in out


async def test_state_unavailable_refuses_fail_closed(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: None)

    out = await s.manual_triage("Разбираю", _mixed_ops())

    assert out == s._STATE_UNAVAILABLE_MSG


async def test_move_to_a_name_that_matches_nothing_is_skipped(monkeypatch, tmp_path):
    """Проект назначения по ИМЕНИ резолвится только точным совпадением —
    подстрочный матчинг был одной из причин declutter-инцидента."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю", [
        {"op": "move", "task_id": "c3", "title": "Позвонить Ивану",
         "to_project": "Раб", "said": "это рабочее"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])

    assert "ПРОПУЩЕНО" in preview
    assert "не найден среди живых проектов" in preview
