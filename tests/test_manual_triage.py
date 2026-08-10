"""manual_triage — полный набор.

Что здесь закреплено:
  1. call #1 НИЧЕГО не мутирует и создаёт одноразовый манифест;
  2. call #2 без согласия человека отказывает и ничего не трогает;
  3. в манифест попадают РОВНО переданные операции — ни одной лишней задачи
     из живого состояния (главный урок отключённого plan_declutter: тул не
     имеет права сам добирать кандидатов), и в под-исполнителей на фазе 2
     уходят РОВНО те же id;
  4. валидация fail-closed: без `said` / с дублем task_id / с merge, чья
     «оставляемая» копия удаляется этим же планом / с запрещёнными ключами в
     `changes` — отказ ЦЕЛИКОМ, без манифеста;
  5. полный цикл: смешанный план из 5 разнородных операций исполняется одним
     подтверждением, и итог считается по НЕЗАВИСИМОЙ сверке;
  6. частичный успех НИКОГДА не выдаётся за полный: дрейф, неподтверждённая
     сверка и недоступное живое состояние видны в шапке отчёта.

Стиль обвязки — как в tests/test_slice1_real_gates.py: живое состояние это
обычный dict, который фейковые клиенты мутируют, поэтому пост-проверка видит
результат. Сети нет; там, где проверяется КОМУ и С ЧЕМ ушла работа, вместо
фейковых клиентов подменяются сами под-исполнители (_stub_sub_impls).
"""
import re

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


@pytest.fixture(autouse=True)
def _isolate_manifests():
    """`_MANIFESTS` — модульный глобал на всю сессию. Снимаем копию, работаем
    на чистом, возвращаем как было: иначе тест «отказ не создал манифеста» мог
    бы увидеть чужой манифест, а соседние файлы — наши."""
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


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

    def batch_move_tasks_raw(self, rows):
        ids = [r["taskId"] for r in rows]
        to_project_id = rows[0]["toProjectId"] if rows else None
        self.calls.append(("move", ids, to_project_id))
        for r in rows:
            tid = r["taskId"]
            if tid in self.live:
                self.live[tid]["projectId"] = r["toProjectId"]
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


def _noisy(live, n=120, prefix="bulk"):
    """Добавляет в живое состояние n посторонних задач из разных проектов,
    включая «тестовый» — ровно та обстановка, в которой отключённый
    plan_declutter однажды смешал реальные задачи с тестовыми в один готовый
    к исполнению план. Ни одна из них не названа человеком, поэтому ни одна
    не имеет права оказаться в плане."""
    projects = ("p_in", "p_work", "p_test")
    for i in range(n):
        tid = f"{prefix}{i:03d}"
        live[tid] = {"id": tid, "title": f"Посторонняя задача №{i}",
                     "projectId": projects[i % 3]}
    return live


_NOISY_NAMES = {"p_in": "Входящие", "p_work": "Работа",
                "p_test": "Тестовый проект"}


def _stub_sub_impls(monkeypatch, live=None):
    """Подменяет ВСЕ четыре под-исполнителя фазы 2 (`_update_tasks_impl`,
    `_move_tasks_impl`, `_complete_tasks_impl`, `_execute_task_deletion_impl`)
    записывающими заглушками и возвращает общий список вызовов в порядке их
    совершения: [(вид, [id…], полезная нагрузка), …].

    Проверяемый факт — не «что-то исполнилось», а РОВНО какие id ушли в
    каждого исполнителя, в каком порядке и сколькими вызовами. Когда передан
    `live`, заглушка ещё и применяет эффект к живому состоянию, чтобы
    независимая финальная сверка судила по факту, а не по пустоте."""
    calls = []

    async def _upd(summary, items):
        calls.append(("update", [i.get("taskId") for i in items], list(items)))
        for i in items:
            t = (live or {}).get(i.get("taskId"))
            if t is not None and i.get("new_title"):
                t["title"] = i["new_title"]
        return "### заглушка _update_tasks_impl"

    async def _mov(summary, items, to_project_id, to_project_name=None):
        calls.append(("move", [i.get("taskId") for i in items], to_project_id))
        for i in items:
            t = (live or {}).get(i.get("taskId"))
            if t is not None:
                t["projectId"] = to_project_id
        return "### заглушка _move_tasks_impl"

    async def _cmp(summary, items):
        calls.append(("complete", [i.get("taskId") for i in items], list(items)))
        for i in items:
            (live if live is not None else {}).pop(i.get("taskId"), None)
        return "### заглушка _complete_tasks_impl"

    async def _del(manifest_id, m=None):
        items = (m or {}).get("items") or []
        calls.append(("delete", [i.get("taskId") for i in items], list(items)))
        for i in items:
            (live if live is not None else {}).pop(i.get("taskId"), None)
        return "### заглушка _execute_task_deletion_impl"

    monkeypatch.setattr(s, "_update_tasks_impl", _upd)
    monkeypatch.setattr(s, "_move_tasks_impl", _mov)
    monkeypatch.setattr(s, "_complete_tasks_impl", _cmp)
    monkeypatch.setattr(s, "_execute_task_deletion_impl", _del)
    return calls


async def _assert_refused_outright(monkeypatch, live, ops, needle, max_items=50):
    """Общий контракт ЛЮБОГО отказа валидации: план отвергнут ЦЕЛИКОМ —
    манифеста нет вовсе (нечего «дожать» вторым вызовом), ни один
    под-исполнитель не позван, живое состояние побайтово прежнее."""
    calls = _stub_sub_impls(monkeypatch, live)
    before = {k: dict(v) for k, v in live.items()}

    out = await s.apply_task_changes("Разбираю", ops, max_items=max_items)

    assert "🛑" in out and needle in out, out
    assert "Манифест" not in out, "отказ не имеет права строить план"
    assert s._MANIFESTS == {}, "отказ оставил манифест — его можно было бы дожать"
    assert calls == [], f"отказ дошёл до исполнителей: {calls}"
    assert live == before
    return out


# ═══════════════ 1. call #1 — предпросмотр, ничего не тронуто ═══════════════

async def test_call1_previews_and_mutates_nothing(monkeypatch, tmp_path):
    live = _live_inbox()
    before = {k: dict(v) for k, v in live.items()}
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())

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

    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())

    order = [preview.index(x) for x in ("✏️ Изменить", "↪ Перенести",
                                        "✅ Закрыть", "🔗 Объединить",
                                        "🗑 Удалить")]
    assert order == sorted(order), f"порядок разрушительности нарушен:\n{preview}"


# ═════════ 2. Манифест содержит РОВНО переданное — ни задачей больше ════════

async def test_manifest_holds_exactly_the_given_operations(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())
    m = s._MANIFESTS[_mid(preview)]

    assert m["kind"] == "manual_triage" and m["tool"] == "apply_task_changes"
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

    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())

    assert ("Разбираю входящие — изменить 1, перенести 1, закрыть 1, "
            "объединить 1, удалить 1") in preview


# ═════════════ 3. call #2 без согласия — отказ, ничего не тронуто ═══════════

async def test_call2_without_reply_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    before = {k: dict(v) for k, v in live.items()}
    v2, official = _wire(monkeypatch, live, tmp_path)
    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())
    mid = _mid(preview)

    refused = await s.apply_task_changes("Разбираю входящие", manifest_id=mid,
                                    user_reply="")

    assert "🛑" in refused
    assert v2.calls == [] and official.calls == []
    assert live == before
    assert s._MANIFESTS[mid]["consumed"] is False  # пустой ответ не сжигает план


async def test_explicit_no_burns_the_plan(monkeypatch, tmp_path):
    live = _live_inbox()
    v2, official = _wire(monkeypatch, live, tmp_path)
    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())
    mid = _mid(preview)

    assert "🛑" in await s.apply_task_changes("Разбираю", manifest_id=mid,
                                         user_reply="нет, стоп")
    dead = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "🛑" in dead
    assert v2.calls == [] and official.calls == []
    assert "a1" in live


# ═══════════════════ 4. Валидация — отказ ЦЕЛИКОМ, без манифеста ════════════

async def test_empty_said_refuses_the_whole_plan(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    ops = _mixed_ops()
    ops[0]["said"] = ""

    await _assert_refused_outright(monkeypatch, live, ops, "said")


async def test_missing_said_key_refuses_the_whole_plan(monkeypatch, tmp_path):
    """Не «пустая строка», а вовсе отсутствующий ключ — та же ветка, но по ней
    легко проскочить, если проверка написана как `if "said" in op`."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    ops = _mixed_ops()
    ops[2].pop("said")

    await _assert_refused_outright(monkeypatch, live, ops, "said")


async def test_duplicate_task_id_refuses_the_whole_plan(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    ops = [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко", "said": "не нужно"},
        {"op": "complete", "task_id": "a1", "title": "Купить молоко", "said": "сделал"},
    ]

    await _assert_refused_outright(monkeypatch, live, ops, "дважды")


async def test_merge_keeping_a_task_that_is_also_deleted_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    ops = [
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_task_id": "e6", "keep_title": "Позвонить в банк", "said": "дубли"},
        {"op": "delete", "task_id": "e6", "title": "Позвонить в банк",
         "said": "и эту тоже снеси"},
    ]

    await _assert_refused_outright(monkeypatch, live, ops, "обе копии")


async def test_merge_of_a_task_with_itself_is_refused(monkeypatch, tmp_path):
    """keep_task_id == task_id: «объединить задачу саму с собой» — это просто
    удаление единственной копии под видом дедупликации."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_task_id": "e5", "keep_title": "Позвонить в банк",
         "said": "оставь одну"}], "сама с собой")


async def test_unknown_op_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "archive", "task_id": "a1", "title": "Купить молоко",
         "said": "в архив"}], "archive")


async def test_empty_operations_list_is_refused(monkeypatch, tmp_path):
    """Ни пустой список, ни вовсе не переданный `operations` не должны
    приводить к «а давай я сам посмотрю, что у тебя там есть»."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [],
                                   "Пустой список операций")
    calls = _stub_sub_impls(monkeypatch, live)
    out = await s.apply_task_changes("Разбираю")           # operations вообще не передан
    assert "🛑" in out and "Пустой список операций" in out
    assert s._MANIFESTS == {} and calls == []


async def test_more_operations_than_max_items_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    ops = [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко", "said": "не надо"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет", "said": "сделал"},
        {"op": "delete", "task_id": "e5", "title": "Позвонить в банк", "said": "дубль"},
    ]

    await _assert_refused_outright(monkeypatch, live, ops, "больше капа",
                                   max_items=2)


async def test_empty_task_id_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "delete", "task_id": "", "title": "Купить молоко",
         "said": "не нужно"}], "пустой task_id")
    await _assert_refused_outright(monkeypatch, live, [
        {"op": "delete", "title": "Купить молоко", "said": "не нужно"}],
        "пустой task_id")


async def test_empty_title_is_refused(monkeypatch, tmp_path):
    """title — не украшение превью, а сам identity guard: без него сервер не
    может проверить, что id указывает на ТУ задачу."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "delete", "task_id": "a1", "title": "", "said": "не нужно"}],
        "пустой title")


async def test_update_without_changes_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    for changes in ({}, None, "new_title=Отчёт"):
        op = {"op": "update", "task_id": "b2", "title": "Отчёт",
              "said": "поправь"}
        if changes is not None:
            op["changes"] = changes
        await _assert_refused_outright(monkeypatch, live, [op],
                                       "update без")


async def test_move_without_a_destination_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "move", "task_id": "c3", "title": "Позвонить Ивану",
         "said": "это рабочее"}], "move без")
    await _assert_refused_outright(monkeypatch, live, [
        {"op": "move", "task_id": "c3", "title": "Позвонить Ивану",
         "to_project_id": "  ", "to_project": "", "said": "это рабочее"}],
        "move без")


async def test_merge_without_keep_fields_is_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_title": "Позвонить в банк", "said": "дубли"}],
        "merge без keep_task_id")
    await _assert_refused_outright(monkeypatch, live, [
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_task_id": "e6", "said": "дубли"}], "merge без keep_title")


@pytest.mark.parametrize("key", ["title", "taskId", "task_id", "projectId",
                                 "project_id"])
async def test_forbidden_change_keys_are_refused(key, monkeypatch, tmp_path):
    """Попытка разоружить identity guard изнутри: `changes={"title": …}`
    подменил бы «текущее название» на желаемое, и сверка id↔задача сравнила бы
    значение сама с собой. Переименование — это `new_title`, перенос —
    отдельная операция `move`."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {key: "Совсем другое", "priority": 5},
         "said": "поправь приоритет"}], "запрещённые ключи")


async def test_tool_has_no_filter_or_scope_parameter():
    """Главный инвариант после declutter-инцидента: у тула физически нет
    входа, через который он мог бы «просканировать и предложить»."""
    import inspect
    params = set(inspect.signature(s.apply_task_changes).parameters)
    # `automation_key` добавлен 2026-08-06 (#118): headless-клиент с верным
    # ключом исполняет батч сразу, без плана и без кнопки владельцу. Ко входу
    # «просканируй и предложи» это отношения не имеет — набор всё равно
    # закреплён ЦЕЛИКОМ, чтобы новый параметр нельзя было добавить молча.
    assert params == {"summary", "operations", "max_items", "manifest_id",
                      "user_reply", "automation_key"}


# ═══════════════════ 5. Полный цикл одного подтверждения ════════════════════

@pytest.mark.triage_e2e("update")
@pytest.mark.triage_e2e("move")
@pytest.mark.triage_e2e("complete")
@pytest.mark.triage_e2e("merge")
@pytest.mark.triage_e2e("delete")
async def test_full_cycle_applies_every_operation_once(monkeypatch, tmp_path):
    live = _live_inbox()
    v2, official = _wire(monkeypatch, live, tmp_path)
    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())
    mid = _mid(preview)

    out = await s.apply_task_changes("Разбираю входящие", manifest_id=mid,
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
    preview = await s.apply_task_changes("Разбираю", _mixed_ops())
    mid = _mid(preview)
    await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")
    calls_after = (len(v2.calls), len(official.calls))

    second = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "🛑" in second
    assert (len(v2.calls), len(official.calls)) == calls_after


async def test_drifted_task_is_skipped_not_applied(monkeypatch, tmp_path):
    """Между планом и «да» человек переименовал задачу руками — операция по
    ней НЕ исполняется, а честно уходит в «пропущено»."""
    live = _live_inbox()
    v2, official = _wire(monkeypatch, live, tmp_path)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    live["a1"]["title"] = "Купить молоко и хлеб"

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "a1" in live, "сдрейфовавшая задача не должна быть удалена"
    assert "d4" not in live
    assert "✅ Выполнено 1 из 2" in out
    assert "Пропущено" in out and "название изменилось" in out


async def test_operation_on_a_vanished_task_is_dropped_from_the_plan(
        monkeypatch, tmp_path):
    """2026-08-09 (П19): не прошедшая сверку операция В ПЛАН НЕ ПОПАДАЕТ.
    Раньше она оставалась строкой того же плана с пометкой ⚠️ ПРОПУЩЕНО и
    лежала в манифесте — здесь закреплено обратное: в манифесте её нет, а
    человек читает про неё в справочном блоке ПОД планом, к которому кнопка
    подтверждения не относится."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "ghost", "title": "Старая задача",
         "said": "давно неактуально"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])

    assert "ПРОПУЩЕНО" not in preview, "строки-пометки в плане больше нет"
    assert "❌ Не вошло: 1" in preview
    assert "не найдена среди открытых" in preview
    assert "id ghost" in preview, "справка обязана называть идентификатор"
    assert "не вошло в план 1" in preview
    m = s._MANIFESTS[_mid(preview)]
    assert [o["task_id"] for o in m["tasks"]] == ["d4"]


async def test_plan_where_everything_is_skipped_is_not_built(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    out = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "ghost", "title": "Старая задача",
         "said": "неактуально"}])

    assert "🛑" in out and "Манифест" not in out


async def test_state_unavailable_refuses_fail_closed(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: None)

    out = await s.apply_task_changes("Разбираю", _mixed_ops())

    assert out == s._STATE_UNAVAILABLE_MSG


async def test_move_to_a_name_that_matches_nothing_is_skipped(monkeypatch, tmp_path):
    """Проект назначения по ИМЕНИ резолвится только точным совпадением —
    подстрочный матчинг был одной из причин declutter-инцидента."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "move", "task_id": "c3", "title": "Позвонить Ивану",
         "to_project": "Раб", "said": "это рабочее"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])

    assert "не найден среди живых проектов" in preview
    # 2026-08-09 (П19): нерезолвнутый перенос не строка плана, а справка под ним
    assert "ПРОПУЩЕНО" not in preview
    assert "❌ Не вошло: 1" in preview
    assert [o["task_id"] for o in s._MANIFESTS[_mid(preview)]["tasks"]] == ["d4"]


# ═══════ 6. Анти-declutter: в план не попадает ничего, чего не назвали ══════
# Самый важный класс тестов в этом файле. Предшественник — автоматический
# plan_declutter — был отключён владельцем НАВСЕГДА после того, как сам
# просканировал весь аккаунт и молча смешал реальные задачи с тестовыми в
# один готовый к исполнению план. Здесь закреплено, что manual_triage так не
# может ни на фазе плана, ни на фазе исполнения, ни по устройству сигнатуры.

async def test_huge_live_state_yields_exactly_the_two_named_operations(
        monkeypatch, tmp_path):
    """122 живые задачи из трёх проектов (включая «Тестовый проект»), человек
    назвал ДВЕ — в манифесте обязано быть ровно две, те самые."""
    live = _noisy(_live_inbox(), n=120)
    assert len(live) > 100
    _wire(monkeypatch, live, tmp_path, names=_NOISY_NAMES)

    preview = await s.apply_task_changes("Разбираю входящие", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "это уже неактуально"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "уже оплатил"}])
    m = s._MANIFESTS[_mid(preview)]

    assert [o["task_id"] for o in m["tasks"]] == ["d4", "a1"]
    assert len(m["tasks"]) == 2
    # Ни одной посторонней задачи и ни одного постороннего проекта в превью:
    # если тул хоть раз «дозаполнит» план из живого состояния, это упадёт.
    assert "Посторонняя задача" not in preview
    assert "Тестовый проект" not in preview
    assert "закрыть 1, удалить 1" in preview


async def test_sub_executors_receive_exactly_the_planned_ids(monkeypatch, tmp_path):
    """Фаза 2 под микроскопом: под-исполнители получают РОВНО те id, что были
    в плане, и ни одного больше — при 127 живых задачах вокруг."""
    live = _noisy(_live_inbox(), n=120)
    _wire(monkeypatch, live, tmp_path, names=_NOISY_NAMES)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())
    mid = _mid(preview)

    await s.apply_task_changes("Разбираю входящие", manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["update", "move", "complete", "delete"]
    got = [tid for c in calls for tid in c[1]]
    assert sorted(got) == ["a1", "b2", "c3", "d4", "e5"]
    assert len(got) == len(set(got)), f"один id ушёл в исполнение дважды: {got}"
    # «e6» — оставляемая копия merge: её id живёт ВНУТРИ операции, но сама она
    # не имеет права попасть ни в один исполнитель.
    assert "e6" not in got
    assert not any(tid.startswith("bulk") for tid in got), got


async def test_published_tool_schema_exposes_no_filter_or_scope_parameter():
    """Структурный инвариант «фича не может отрастить скан»: у ОПУБЛИКОВАННОЙ
    схемы тула (то, что видит модель) нет ни одного входа, через который можно
    было бы попросить «сам найди, что почистить» — ни query, ни filter, ни
    project_id, ни limit-по-возрасту."""
    tools = await s.mcp.list_tools()
    tool = next(t for t in tools if t.name == "apply_task_changes")
    props = set((tool.inputSchema.get("properties") or {}))

    assert props == {"summary", "operations", "max_items", "manifest_id",
                     "user_reply", "automation_key"}, props
    assert tool.inputSchema.get("required") == ["summary"]
    # `operations` — именно СПИСОК объектов, а не строка-запрос.
    assert tool.inputSchema["properties"]["operations"]["type"] == "array"


# ═══════════════════════ 7. Гейт: подмена, TTL, Telegram ════════════════════

async def test_call1_calls_no_sub_executor_at_all(monkeypatch, tmp_path):
    """Дополняет проверку по фейковым клиентам: на фазе плана не позван ни
    один из четырёх под-исполнителей — то есть мутировать физически нечем."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)

    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())

    assert calls == []
    assert s._MANIFESTS[_mid(preview)]["consumed"] is False


@pytest.mark.parametrize("reply", ["нет", "не надо", "стоп", "отмена"])
async def test_negative_reply_refuses_and_burns_the_plan(reply, monkeypatch, tmp_path):
    live = _live_inbox()
    before = {k: dict(v) for k, v in live.items()}
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", _mixed_ops())
    mid = _mid(preview)

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply=reply)

    assert "🛑" in out and "НЕ подтвердил" in out
    assert calls == [] and live == before
    assert s._MANIFESTS[mid]["consumed"] is True, "план обязан быть аннулирован"


async def test_call2_ignores_a_swapped_operations_list(monkeypatch, tmp_path):
    """no-swap: на call #2 подсунут ДРУГОЙ список операций (по другой задаче)
    — исполняются СОХРАНЁННЫЕ в манифесте, а показанный человеку план и
    сделанное совпадают."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"}])
    mid = _mid(preview)

    out = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "zz", "title": "Посторонняя задача",
         "said": "подменённая строка"}], manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["delete"]
    assert calls[0][1] == ["a1"], "исполнился подменённый список, а не план"
    assert "zz" in live and "a1" not in live
    assert "Посторонняя задача" not in out


async def test_expired_manifest_is_refused(monkeypatch, tmp_path):
    """Манифест живёт час: протухший план исполнять нельзя — «да» по нему
    относится к тексту, которого человек уже не помнит."""
    live = _live_inbox()
    before = {k: dict(v) for k, v in live.items()}
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", _mixed_ops())
    mid = _mid(preview)

    s._MANIFESTS[mid]["created"] -= s._MANIFEST_TTL + 60

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "🛑" in out
    assert calls == [] and live == before


async def test_telegram_button_path_replays_the_same_plan(monkeypatch, tmp_path):
    """Кнопка ✅ в Telegram исполняет план САМ СЕРВЕР, минуя слой тулов: для
    этого манифест должен опознаваться generic-исполнителем и его хэш —
    пересчитываться той же формулой. Иначе кнопка либо не работает, либо
    работает без привязки к показанному плану."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    preview = await s.apply_task_changes("Разбираю", _mixed_ops())
    mid = _mid(preview)
    m = s._MANIFESTS[mid]

    assert m["tool"] == "apply_task_changes" and m["_gate"] == "batch"
    assert s._auto_execute_tool_of(m) == "apply_task_changes"
    assert s._resolve_auto_executor("manual_triage", m) is s._GENERIC_GATE_ENTRY
    assert s._generic_gate_rehash(m) == m["object_hash"]

    rec = []

    async def _impl(summary, tasks):
        rec.append((summary, tasks))
        return "### заглушка _apply_task_changes_impl"

    monkeypatch.setattr(s, "_apply_task_changes_impl", _impl)

    await s._generic_gate_auto_execute(mid, m)

    assert len(rec) == 1
    assert rec[0][0] == m["summary"]
    assert [o["task_id"] for o in rec[0][1]] == [o["task_id"] for o in m["tasks"]]


async def test_rehash_changes_when_the_stored_operations_are_swapped(
        monkeypatch, tmp_path):
    """Зеркало предыдущего: если содержимое манифеста подменить между показом
    и нажатием, пересчитанный хэш обязан разойтись с сохранённым."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    preview = await s.apply_task_changes("Разбираю", _mixed_ops())
    m = s._MANIFESTS[_mid(preview)]

    m["tasks"] = [dict(m["tasks"][0], task_id="ДРУГАЯ-ЗАДАЧА")]

    assert s._generic_gate_rehash(m) != m["object_hash"]


# ═══════ 8. Identity guard и ЧЕСТНЫЙ частичный итог ═══════
# Требование владельца: никогда не выдавать частичный успех за полный.

async def test_title_mismatch_is_skipped_and_never_executed(monkeypatch, tmp_path):
    """id указывает на живую задачу, но названо не то — операция В ПЛАН НЕ
    ВОШЛА (2026-08-09, П19: не строка с пометкой, а справка под планом) и до
    исполнителя не доходит. Поэтому итог и считается «1 из 1»: план состоял
    из ОДНОЙ операции — подтверждали ровно её."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить хлеб",
         "said": "молоко больше не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    assert "ПРОПУЩЕНО" not in preview
    assert "❌ Не вошло: 1" in preview and "название не совпало" in preview
    assert "«Купить молоко»" in preview   # видно, как задача называется НА САМОМ ДЕЛЕ
    assert [o["task_id"] for o in s._MANIFESTS[mid]["tasks"]] == ["d4"]

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["complete"]
    assert calls[0][1] == ["d4"]
    assert "a1" in live
    assert "✅ Выполнено 1 из 1" in out


async def test_vanished_task_never_reaches_an_executor(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "ghost", "title": "Старая задача",
         "said": "давно неактуально"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["complete"]
    assert "ghost" not in [tid for c in calls for tid in c[1]]
    # «из 1», а не «из 2»: исчезнувшая задача в план не вошла (2026-08-09, П19)
    assert "✅ Выполнено 1 из 1" in out


async def test_merge_is_refused_when_the_kept_copy_is_missing_at_plan_time(
        monkeypatch, tmp_path):
    """Критично: если «оставляемой» копии нет среди открытых, удалять дубль
    НЕЛЬЗЯ — иначе от пары не останется ни одной задачи."""
    live = _live_inbox()
    live.pop("e6")
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_task_id": "e6", "keep_title": "Позвонить в банк",
         "said": "это дубли, оставь одну"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    assert "ПРОПУЩЕНО" not in preview
    assert "❌ Не вошло: 1" in preview and "дубль НЕ удаляю" in preview
    assert [o["task_id"] for o in s._MANIFESTS[mid]["tasks"]] == ["d4"]

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "e5" in live, "снесли единственную оставшуюся копию"
    assert "e5" not in [tid for c in calls for tid in c[1]]
    assert "✅ Выполнено 1 из 1" in out


async def test_merge_is_refused_when_the_kept_copy_vanishes_after_the_plan(
        monkeypatch, tmp_path):
    """Тот же случай, но копия исчезает МЕЖДУ планом и «да» — ловит уже
    повторная сверка перед мутацией, а не сверка на фазе плана."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_task_id": "e6", "keep_title": "Позвонить в банк",
         "said": "это дубли, оставь одну"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    live.pop("e6")

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "e5" in live
    assert "e5" not in [tid for c in calls for tid in c[1]]
    assert "основная задача исчезла" in out
    assert "✅ Выполнено 1 из 2" in out


async def test_kept_copy_renamed_after_the_plan_blocks_the_merge(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_task_id": "e6", "keep_title": "Позвонить в банк",
         "said": "это дубли, оставь одну"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    live["e6"]["title"] = "Позвонить в банк по ипотеке"

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "e5" in live and "e6" in live
    assert calls == [] or "e5" not in [tid for c in calls for tid in c[1]]
    assert "основную задачу переименовали" in out


async def test_drift_between_plan_and_execution_is_reported_honestly(
        monkeypatch, tmp_path):
    """call #1 и call #2 видят РАЗНОЕ живое состояние: сдрейфовавшая операция
    пропущена, остальные исполнены, а шапка показывает «выполнено 2 из 3» —
    не полный успех."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {"new_title": "Сдать отчёт за июль"},
         "said": "переименуй"},
        {"op": "move", "task_id": "c3", "title": "Позвонить Ивану",
         "to_project_id": "p_work", "said": "это рабочее"},
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"}])
    mid = _mid(preview)

    live["a1"]["title"] = "Купить молоко и хлеб"   # человек поправил руками

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["update", "move"]
    assert "a1" not in [tid for c in calls for tid in c[1]]
    assert "a1" in live
    assert "✅ Выполнено 2 из 3" in out
    assert "⏭ пропущено 1" in out
    assert "название изменилось после плана" in out
    assert "Выполнено 3 из 3" not in out


async def test_everything_drifted_calls_no_executor_at_all(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    live["a1"]["title"] = "Купить молоко и хлеб"
    live.pop("d4")

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert calls == []
    assert "НИЧЕГО НЕ ВЫПОЛНЕНО" in out
    assert "Ни одна задача не тронута" in out
    assert "✅ Выполнено" not in out
    assert "a1" in live


async def test_nothing_done_report_is_classified_as_failure(monkeypatch, tmp_path):
    """Стык двух веток, который НЕ ловил ни один тест по отдельности.

    `fix/silent-failures` научил кнопочный путь ставить разные надгробия:
    «выполнено» против «нажато, но НЕ выполнено». Решает это
    `_auto_execute_report_is_failure`, и судит он по НАЧАЛУ отчёта (внутри
    успешного отчёта ❌ может стоять у отдельного элемента пачки — это
    частичный результат, а не провал). manual_triage пришёл другой веткой и
    начинал свой отчёт нейтральным «### 🧾 Ручной разбор», поэтому случай
    «не выполнено ВООБЩЕ ничего» получал надгробие «✅ выполнено» — ровно тот
    тихий отказ, ради которого вторая ветка и писалась.

    Отчёт здесь не копируется в тест строкой (копия разъедется с кодом), а
    строится настоящим вызовом."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"}])
    mid = _mid(preview)
    live.pop("a1")                      # исчезла между планом и подтверждением

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert calls == []
    assert "НИЧЕГО НЕ ВЫПОЛНЕНО" in out
    assert s._auto_execute_report_is_failure(out), (
        "отчёт «ничего не выполнено» классифицирован как УСПЕХ — по кнопке "
        "он получил бы надгробие «✅ исполнено», и следующий вызов по этому "
        f"id услышал бы, что всё сделано. Отчёт:\n{out}")


async def test_successful_triage_report_is_not_classified_as_failure(
        monkeypatch, tmp_path):
    """Обратная сторона того же стыка: нормальный отчёт НЕ должен читаться
    как провал, иначе выполненная операция получала бы надгробие «НЕ
    выполнено» и человека звали бы перепроверять сделанное."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "уже сделал"}])
    mid = _mid(preview)

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "✅ Выполнено 1 из 1" in out
    assert not s._auto_execute_report_is_failure(out), out


async def test_executor_ran_but_verification_did_not_confirm(monkeypatch, tmp_path):
    """Под-исполнитель отчитался успехом, а задача осталась на месте (частая
    форма молчаливого отказа TickTick). Итог судится по ЖИВОМУ состоянию, а
    не по тексту исполнителя: это НЕ успех."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live=None)   # заглушки НИЧЕГО не меняют
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"},
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"}])
    mid = _mid(preview)

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["complete", "delete"]
    assert "❌ Выполнено 0 из 2" in out, "галочка рядом с нулём (побочный пункт Д7)"
    assert "❌ Не подтверждено сверкой: 2" in out
    assert "ВСЁ ЕЩЁ существует" in out and "всё ещё среди открытых" in out


async def test_unreadable_state_at_final_verification_says_unverified(
        monkeypatch, tmp_path):
    """Живое состояние отвалилось ПОСЛЕ мутаций: отчёт обязан сказать «исход
    НЕ ПОДТВЕРЖДЁН», а не отрапортовать успех по числу отправленных операций."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    seen = {"n": 0}

    def _flaky_state(fresh=False):
        seen["n"] += 1
        return dict(live) if seen["n"] == 1 else None   # финальная сверка падает

    monkeypatch.setattr(s, "_open_by_id", _flaky_state)

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["complete"]
    assert "Исход НЕ ПОДТВЕРЖДЁН" in out
    assert "Считать выполненным НЕЛЬЗЯ" in out
    assert "✅ Выполнено" not in out


async def test_moves_to_several_projects_are_one_call_per_destination(
        monkeypatch, tmp_path):
    """`_move_tasks_impl` переносит весь переданный список В ОДИН проект —
    значит на каждый проект назначения нужен свой вызов, и задачи не имеют
    права перепутаться между группами."""
    live = _live_inbox()
    live["f7"] = {"id": "f7", "title": "Полить цветы", "projectId": "p_in"}
    names = {"p_in": "Входящие", "p_work": "Работа", "p_home": "Дом"}
    _wire(monkeypatch, live, tmp_path, names=names)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "move", "task_id": "c3", "title": "Позвонить Ивану",
         "to_project_id": "p_work", "said": "это рабочее"},
        {"op": "move", "task_id": "d4", "title": "Оплатить интернет",
         "to_project_id": "p_work", "said": "тоже рабочее"},
        {"op": "move", "task_id": "f7", "title": "Полить цветы",
         "to_project": "Дом", "said": "а это домашнее"}])
    mid = _mid(preview)

    await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    moves = [(c[2], c[1]) for c in calls if c[0] == "move"]
    assert len(calls) == len(moves) == 2, calls
    assert dict(moves) == {"p_work": ["c3", "d4"], "p_home": ["f7"]}
    assert live["c3"]["projectId"] == "p_work"
    assert live["d4"]["projectId"] == "p_work"
    assert live["f7"]["projectId"] == "p_home"


# ═══════════════════ 9. Порядок исполнения ═══════════════════

async def test_execution_order_is_least_destructive_first(monkeypatch, tmp_path):
    """Порядок вызовов под-исполнителей — возрастание разрушительности:
    update → move → complete → (merge + delete). Сбой на середине не должен
    оставить задачу удалённой раньше, чем её успели поправить."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", _mixed_ops())
    mid = _mid(preview)

    await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["update", "move", "complete", "delete"]
    # merge и delete идут ОДНИМ вызовом того же проверенного движка удаления,
    # и внутри него дубль (merge) стоит раньше обычного удаления.
    assert calls[-1][1] == ["e5", "a1"]


# ═══════ 10. no-swap: два независимых слоя, каждый со своим тестом ══════════
# «Подменить набор между планом и исполнением» защищено дважды: (а) сам тул на
# call #2 вообще не смотрит на переданный `operations`, (б) `_gate_batch`
# отдаёт СОХРАНЁННЫЙ список, чем бы его ни звали. Тест на итоговое поведение
# (см. test_call2_ignores_a_swapped_operations_list) проходит, даже если один
# из слоёв сломать, — поэтому у каждого слоя есть ещё и свой прямой тест.

async def test_gate_hands_back_the_stored_operations_not_call2_arguments():
    """Слой (б), напрямую через `_gate_batch`: даже если тул однажды начнёт
    передавать на call #2 свой список, исполнить обязано сохранённое."""
    planned = [{"op": "delete", "task_id": "a1", "title": "Купить молоко",
                "said": "не нужно"}]
    plan = await s._gate_batch("manual_triage", "apply_task_changes", planned, "Разбираю",
                               "", "", s._describe_triage_op, items_arg="operations")
    mid = _mid(plan.message)

    swapped = [{"op": "delete", "task_id": "zz", "title": "Посторонняя задача",
                "said": "подмена"}]
    out = await s._gate_batch("manual_triage", "apply_task_changes", swapped, "Разбираю",
                              mid, "да", s._describe_triage_op, items_arg="operations")

    assert out.proceed is True
    assert [o["task_id"] for o in out.tasks] == ["a1"]


async def test_call2_does_not_even_look_at_the_operations_argument(
        monkeypatch, tmp_path):
    """Слой (а): на call #2 переданный список не валидируется и не резолвится
    вовсе. Подсовываем заведомо НЕВАЛИДНЫЙ (пустой `said`) — если бы тул его
    смотрел, был бы отказ валидации; правильное поведение — исполнить план."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"}])
    mid = _mid(preview)

    out = await s.apply_task_changes("Разбираю", [{"op": "чушь", "task_id": "",
                                              "title": "", "said": ""}],
                                manifest_id=mid, user_reply="да")

    assert "🛑" not in out.splitlines()[0]
    assert [c[0] for c in calls] == ["delete"] and calls[0][1] == ["a1"]


# ═══════ 11. Временный манифест удаления не переживает вызов ════════════════
# Удаление/объединение исполняется тем же движком, что и обычное
# plan_task_deletion → execute_task_deletion, для чего внутри собирается
# синтетический манифест. Он обязан исчезнуть — иначе фоновый TG-поллер увидел
# бы «живой план удаления», которого человеку никто не показывал.

async def test_synthetic_deletion_manifest_does_not_survive_the_call(
        monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", _mixed_ops())
    mid = _mid(preview)

    await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert not [k for k in s._MANIFESTS if k.startswith("triage-")]


async def test_synthetic_manifest_is_cleaned_up_even_if_deletion_raises(
        monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    _stub_sub_impls(monkeypatch, live)

    async def _boom(manifest_id, m=None):
        raise RuntimeError("TickTick упал на удалении")

    monkeypatch.setattr(s, "_execute_task_deletion_impl", _boom)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"}])
    mid = _mid(preview)

    with pytest.raises(RuntimeError):
        await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert not [k for k in s._MANIFESTS if k.startswith("triage-")]


async def test_changes_invisible_in_the_open_list_are_reported_as_unchecked(
        monkeypatch, tmp_path):
    """Напоминание/повтор/колонка/исполнитель в списке открытых задач не
    видны — сверить их нечем. Отчёт обязан сказать «не проверяется
    автоматически», а не зачесть операцию в успех."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {"reminders": ["09:00"]},
         "said": "напомни утром"}])
    mid = _mid(preview)

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["update"]
    assert "⚠️ не проверяется автоматически: 1" in out
    assert "❌ Выполнено 0 из 1" in out, "галочка рядом с нулём (побочный пункт Д7)"
    assert "не проверить" in out


# ═══════ 12. Кнопка подтверждает РОВНО ТО, что владелец увидел ═════════════
# Требование прежнее и неизменное: кнопка ✅ — единственный внеполосный фактор
# согласия, и подтверждать ею строки, которых не было в сообщении, нельзя.
# Изменился СПОСОБ его выполнения. Раньше общий слой резал превью по
# искусственному PREVIEW_CAP и слал обрезок, поэтому здесь стоял fail-closed
# отказ строить длинный план (мера, помеченная автором как временная: «общий
# слой параллельно переделывает другая ветка»). Та ветка приехала и обрезку
# убрала — план доставляется ЦЕЛИКОМ, разбитый на несколько сообщений
# (split_for_telegram / send_message_chunked). Отказ вместе с его причиной
# снят, а тесты ниже держат новое: длинный план строится и доезжает полностью.
# Предел разового ущерба по-прежнему на _TRIAGE_PLAN_DAMAGE_CAP (50 операций).

def _tg_on(monkeypatch, allowlist=None):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=allowlist, ttl_s=3600))


def _tg_off(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


def _notify_recorder(monkeypatch):
    """Подменяет отправку в Telegram записывающей заглушкой: сети нет, а факт
    «что именно ушло бы владельцу» становится проверяемым."""
    seen = []

    def _notify(cfg, manifest_id, preview, tool):
        seen.append({"manifest_id": manifest_id, "preview": preview, "tool": tool})
        return True, ""

    monkeypatch.setattr(tg, "notify_plan", _notify)
    return seen


def _long_plan(n=50):
    """n удалений с реалистично длинными названиями и обоснованиями — ровно
    та форма разбора, ради которой тул и написан («я сам себе деклатер»)."""
    live, ops = {}, []
    for i in range(n):
        tid = f"L{i:03d}"
        live[tid] = {"id": tid, "title": f"Позвонить по объявлению №{i} "
                                          "и уточнить условия аренды",
                     "projectId": "p_in"}
        ops.append({"op": "delete", "task_id": tid,
                    "title": f"Позвонить по объявлению №{i} и уточнить условия аренды",
                    "said": f"объявление №{i} уже неактуально, снимаю с контроля"})
    return live, ops


async def test_long_plan_reaches_telegram_whole_instead_of_being_refused(
        monkeypatch, tmp_path):
    """ПЕРЕПИСАН ПРИ СЛИЯНИИ 2026-08-06. Здесь стояло обратное утверждение:
    план на 50 операций ОТВЕРГАЛСЯ, потому что общий слой резал превью по
    искусственному `PREVIEW_CAP` и слал обрезок, а кнопка ✅ исполняла весь
    манифест — человек подтверждал строки, которых не видел.

    Ветка отчётов в группу убрала обрезку по прямому требованию Максима
    («нельзя молча резать, надо доставить целиком, разбив на несколько
    сообщений»). Причина отказа исчезла, а сам отказ стал вредным — он
    запрещал бы законный длинный разбор. Теперь проверяется НОВОЕ поведение:
    план строится, уходит целиком и его текст НЕ обрезан."""
    live, ops = _long_plan(50)
    _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    seen = _notify_recorder(monkeypatch)

    preview = await s.apply_task_changes("Разбираю входящие", ops)

    assert "🛑" not in preview
    assert "не помещается" not in preview
    m = s._MANIFESTS[_mid(preview)]
    assert len(m["tasks"]) == 50
    assert len(seen) == 1 and seen[0]["tool"] == "apply_task_changes"
    # Превью длиннее ОДНОГО телеграмного сообщения — и всё равно уходит
    # целиком: разбиение на части живёт в слое отправки, а не в обрезке.
    assert len(preview) > tg.TELEGRAM_TEXT_LIMIT, len(preview)
    # В Telegram уходит САМ план; возвращаемый моделью текст — это он же плюс
    # приписка «ждём нажатия кнопки», которую дописывает `_maybe_tg_notify_plan`
    # уже ПОСЛЕ отправки. Поэтому сверяем вхождением, а не равенством: важно
    # ровно одно — план ушёл целиком, без «…» на конце.
    sent = seen[0]["preview"]
    assert sent in preview, "в Telegram ушёл не тот текст, что показан модели"
    assert len(sent) > tg.TELEGRAM_TEXT_LIMIT, "план ушёл в Telegram усечённым"
    assert not sent.rstrip().endswith("…"), "план обрезали многоточием"


async def test_the_same_long_plan_is_fine_when_telegram_layer_is_off(
        monkeypatch, tmp_path):
    """Зеркало предыдущего со стороны выключенного слоя: длина плана вообще ни
    на что не влияет — ни с Telegram, ни без него."""
    live, ops = _long_plan(50)
    _wire(monkeypatch, live, tmp_path)
    _tg_off(monkeypatch)

    preview = await s.apply_task_changes("Разбираю входящие", ops)

    assert "🛑" not in preview
    m = s._MANIFESTS[_mid(preview)]
    assert len(m["tasks"]) == 50
    assert len(preview) > tg.TELEGRAM_TEXT_LIMIT, len(preview)


async def test_short_plan_still_goes_to_telegram_untouched(monkeypatch, tmp_path):
    """Проверка на переусердствование: обычный разбор из пяти операций уходит
    в Telegram как есть, одним сообщением."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    seen = _notify_recorder(monkeypatch)

    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())

    assert len(seen) == 1 and seen[0]["tool"] == "apply_task_changes"
    assert len(seen[0]["preview"]) <= tg.TELEGRAM_TEXT_LIMIT
    assert s._MANIFESTS[_mid(preview)]["tg_notified"] is True


# ═══════ 13. Типы внутри `changes` — отказ ДО единой мутации ════════════════
# Схема `operations` типизации не несёт: JSON позволяет положить в due_date
# число. Раньше это проезжало превью, отправляло update, доводило план до
# необратимого удаления — и падало УЖЕ ПОСЛЕ него, на разборе даты в сверке.
# Человек получал traceback вместо отчёта и делал вывод «упало, значит ничего
# не сделано», хотя задачи были удалены.

async def test_numeric_due_date_is_refused_at_plan_time(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    out = await _assert_refused_outright(monkeypatch, live, [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {"due_date": 20260810}, "said": "поставь на 10 августа"}],
        "due_date")
    assert "строкой" in out and "int" in out


async def test_non_string_tags_are_refused_at_plan_time(monkeypatch, tmp_path):
    """Раньше этот случай ронял ФАЗУ ПЛАНА traceback'ом (`', '.join([1, 2])`)
    — то есть тул падал ещё до всякой мутации, но с невнятной ошибкой.

    2026-08-09 (1.3.3/изм-4, дизайн раздел 9): теги через `changes` у
    `update` запрещены ВОВСЕ — тег, записанный обновлением задачи, ложится на
    неё без регистрации в аккаунте (тег-сирота: не виден в list_tags, не
    удаляется delete_tag). Отказ по запрещённому ключу приходит РАНЬШЕ
    типизации, поэтому нетипизированный список теперь проверяется на
    собственном типе `op="tags"` (tests/test_triage_new_types.py), а здесь
    закреплено, что отказ у `update` называет ЗАМЕНУ, а не просто «нельзя»:
    иначе вызывающий либо бросит законное намерение, либо пойдёт в обход."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    out = await _assert_refused_outright(monkeypatch, live, [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {"tags": [1, 2]}, "said": "перетегируй"}], "tags")
    assert 'op="tags"' in out and "тег-сирота" in out


@pytest.mark.parametrize("bad", [2, 4, "5", 5.0, True])
async def test_out_of_range_priority_is_refused(bad, monkeypatch, tmp_path):
    """`_update_tasks_impl` проверяет приоритет только на одиночном пути; в
    батче он уходит в API как есть, поэтому ловим на фазе плана."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {"priority": bad}, "said": "подними приоритет"}], "priority")


@pytest.mark.parametrize("key,value", [("new_title", 42), ("content", ["a"]),
                                        ("start_date", 20260810),
                                        ("reminders", "09:00")])
async def test_wrongly_typed_change_values_are_refused(key, value, monkeypatch,
                                                        tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {key: value}, "said": "поправь"}], key)


# ═══════ 14. `changes` с ключами, которых сервер не применяет ═══════════════
# `changes={"foo":"bar"}` раньше проходил валидацию: превью печатало «(поля
# изменений не распознаны)» — человек одобрял неизвестно что, — исполнялся
# пустой update, а отчёт противоречил сам себе («✅ Выполнено 0 из 1» в шапке
# и «✏️ «Отчёт» обновлено (проверено)» в секции исполнителя).

async def test_unknown_change_keys_are_refused(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    out = await _assert_refused_outright(monkeypatch, live, [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {"foo": "bar"}, "said": "поправь"}], "foo")
    # Отказ обязан быть действенным: перечисляет, что МОЖНО.
    assert "new_title" in out and "due_date" in out


async def test_a_typo_next_to_a_valid_key_is_refused_too(monkeypatch, tmp_path):
    """Опечатка рядом с рабочим ключом опаснее одиночной: человек видит в
    превью корректную часть, одобряет её — и молча не получает вторую."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {"new_title": "Сдать отчёт", "duedate": "2026-08-10"},
         "said": "переименуй и поставь срок"}], "duedate")


async def test_reminders_and_repeat_are_named_in_the_preview(monkeypatch, tmp_path):
    """Оборотная сторона того же: ключи, которые исполнитель РЕАЛЬНО применяет,
    обязаны быть названы в превью, а не сворачиваться в «поля изменений не
    распознаны»."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "update", "task_id": "b2", "title": "Отчёт",
         "changes": {"reminders": ["09:00"], "repeat_flag": "RRULE:FREQ=WEEKLY"},
         "said": "напоминай раз в неделю утром"}])

    assert "напоминания меняются" in preview and "повтор меняется" in preview
    assert "поля изменений не распознаны" not in preview


# ═══════ 15. Исключение в финальной сверке не имеет права скрыть мутации ════

async def test_verification_crash_still_reports_that_mutations_were_sent(
        monkeypatch, tmp_path):
    """Мутации уже отправлены (часть необратима). Если независимая сверка
    падает, тул обязан вернуть ЧЕСТНЫЙ отчёт «отправлено, сверка не удалась»,
    а не traceback: traceback читается как «упало, значит ничего не сделано»."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"},
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"}])
    mid = _mid(preview)

    def _boom(op, live_map, names):
        raise AttributeError("'int' object has no attribute 'strip'")

    monkeypatch.setattr(s, "_verify_triage_op", _boom)

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert [c[0] for c in calls] == ["complete", "delete"]
    assert "МУТАЦИИ УЖЕ ОТПРАВЛЕНЫ" in out
    assert "сверка НЕ УДАЛАСЬ" in out and "strip" in out
    assert "вручную" in out
    # Ни намёка на успех и ни намёка на «ничего не сделано».
    assert "✅ Выполнено" not in out
    assert "НИЧЕГО НЕ ВЫПОЛНЕНО" not in out
    # Сырые ответы исполнителей всё равно на месте — они и есть то, что можно
    # прочитать глазами, раз машинная сверка отказала.
    assert "заглушка _execute_task_deletion_impl" in out


# ═══════ 16. Инструкция «начни заново» называет СВОЙ аргумент ═══════════════

async def test_manifest_gone_message_names_the_operations_argument(
        monkeypatch, tmp_path):
    """`{tool_name}(summary, tasks, ...)` было захардкожено: у manual_triage
    аргумента `tasks` нет, и модель, послушавшись, получила бы ошибку
    валидации MCP вместо повторного плана."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    out = await s.apply_task_changes("Разбираю", _mixed_ops(),
                                manifest_id="deadbeef1234", user_reply="да")

    assert "apply_task_changes(summary, operations, ...)" in out
    assert "tasks" not in out


# ═══════ 17. Синтетический манифест удаления вообще не попадает в реестр ════

async def test_synthetic_deletion_manifest_is_never_put_into_the_registry(
        monkeypatch, tmp_path):
    """Сильнее прежнего «его удаляют в finally»: манифеста НЕТ в `_MANIFESTS`
    ни в один момент, даже пока идёт удаление. Иначе публичный
    `execute_task_deletion("triage-…")` мог бы исполнить удаление, которого
    человеку не показывали, по одному чат-«да»."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    seen_ids, dele = [], []

    async def _del(manifest_id, m=None):
        # Снимок реестра ИЗНУТРИ исполнения — то самое окно, где раньше жил
        # синтетический манифест.
        seen_ids.append((manifest_id, sorted(s._MANIFESTS)))
        dele.append([i["taskId"] for i in (m or {}).get("items") or []])
        for i in (m or {}).get("items") or []:
            live.pop(i["taskId"], None)
        return "### заглушка удаления"

    monkeypatch.setattr(s, "_execute_task_deletion_impl", _del)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"}])
    mid = _mid(preview)

    await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert dele == [["a1"]], "удаление всё-таки должно было произойти"
    synth_id, registry_during = seen_ids[0]
    assert synth_id.startswith("triage-")
    assert not [k for k in registry_during if k.startswith("triage-")], \
        f"синтетический манифест был виден в реестре: {registry_during}"
    assert synth_id not in s._MANIFESTS

    # …и публичный тул его не подхватит ни во время, ни после.
    out = await s.execute_task_deletion(manifest_id=synth_id, user_reply="да")
    assert "🛑" in out and "не найден" in out
    assert dele == [["a1"]], "публичный execute повторил удаление"


# ═══════ 18. Удаление/закрытие родителя честно говорит про подзадачи ════════
# Подзадачи НЕ удаляются вместе с родителем (план не имеет права разрастаться
# сверх названного человеком) — но они останутся сиротами, и человек обязан
# узнать об этом ДО «да», а не после.

def _live_with_subtasks():
    return {
        "P1": {"id": "P1", "title": "Ремонт квартиры", "projectId": "p_in"},
        "S1": {"id": "S1", "title": "Выбрать плитку", "projectId": "p_in",
               "parentId": "P1"},
        "S2": {"id": "S2", "title": "Вызвать замерщика", "projectId": "p_in",
               "parentId": "P1"},
        "P2": {"id": "P2", "title": "Отпуск", "projectId": "p_in"},
        "S3": {"id": "S3", "title": "Купить билеты", "projectId": "p_in",
               "parentId": "P2"},
        "d4": {"id": "d4", "title": "Оплатить интернет", "projectId": "p_in"},
    }


async def test_deleting_a_parent_warns_about_its_open_subtasks(monkeypatch, tmp_path):
    live = _live_with_subtasks()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "P1", "title": "Ремонт квартиры",
         "said": "ремонт отменился"}])

    assert "у неё 2 открытые подзадачи — они останутся без родителя" in preview
    # …и при этом дети НЕ добрались до плана: тул не разрастается сам.
    m = s._MANIFESTS[_mid(preview)]
    assert [o["task_id"] for o in m["tasks"]] == ["P1"]


async def test_completing_a_parent_warns_about_one_subtask(monkeypatch, tmp_path):
    live = _live_with_subtasks()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "complete", "task_id": "P2", "title": "Отпуск",
         "said": "съездил уже"}])

    assert "у неё 1 открытая подзадача — она останется без родителя" in preview


async def test_a_childless_task_gets_no_subtask_note(monkeypatch, tmp_path):
    live = _live_with_subtasks()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "не нужно"}])

    assert "подзадач" not in preview


async def test_merge_warns_about_the_duplicates_subtasks_too(monkeypatch, tmp_path):
    """merge — это то же необратимое удаление, только под другим словом:
    у дубля дети осиротеют ровно так же."""
    live = _live_with_subtasks()
    live["P1b"] = {"id": "P1b", "title": "Ремонт квартиры", "projectId": "p_work"}
    live["S9"] = {"id": "S9", "title": "Смета", "projectId": "p_work",
                  "parentId": "P1b"}
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "merge", "task_id": "P1b", "title": "Ремонт квартиры",
         "keep_task_id": "P1", "keep_title": "Ремонт квартиры",
         "said": "это одно и то же"}])

    assert "у неё 1 открытая подзадача — она останется без родителя" in preview


# ═══════ 19. Кап, который нельзя поднять снаружи ═══════════════════════════

async def test_max_items_cannot_be_raised_above_the_hard_cap(monkeypatch, tmp_path):
    """Кап, который волен поднять сам вызывающий, — не кап. `max_items=10000`
    строил план на 200 удалений; для фичи, родившейся из «слишком большой
    готовый к исполнению план», это существенно."""
    live, ops = _long_plan(s._TRIAGE_PLAN_DAMAGE_CAP + 1)
    _wire(monkeypatch, live, tmp_path)

    out = await _assert_refused_outright(monkeypatch, live, ops, "больше капа",
                                         max_items=10000)

    assert f"больше капа {s._TRIAGE_PLAN_DAMAGE_CAP}" in out
    assert "max_items=10000" in out and "НЕ поднимает" in out


async def test_max_items_can_still_be_lowered(monkeypatch, tmp_path):
    """Опустить кап вызывающий по-прежнему может — это осторожность, а не
    обход."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    out = await _assert_refused_outright(monkeypatch, live, _mixed_ops(),
                                         "больше капа", max_items=2)
    assert "больше капа 2" in out


async def test_the_hard_cap_is_fifty():
    assert s._TRIAGE_PLAN_DAMAGE_CAP == 50


# ═══════ 20. Повторяющееся обоснование — заметное предупреждение ════════════
# Докстринг требует, чтобы в `said` были слова человека про ЭТУ задачу, но
# машинно это непроверяемо. Жёсткий отказ был бы ложноположительным на честном
# «эти пять удали» (одна фраза действительно про пять задач), поэтому —
# предупреждение, зато видное в ОБОИХ каналах согласия.

async def test_repeated_said_adds_a_visible_warning_to_the_preview(
        monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "разобрать инбокс"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "разобрать инбокс"},
        {"op": "delete", "task_id": "e5", "title": "Позвонить в банк",
         "said": "Разобрать   инбокс"}])   # регистр/пробелы не спасают

    assert "⚠️ Одно и то же обоснование у 3 строк" in preview
    assert "разобрать инбокс" in preview
    # Это ПРЕДУПРЕЖДЕНИЕ, а не отказ: план строится, человек решает сам.
    assert "🛑" not in preview
    assert len(s._MANIFESTS[_mid(preview)]["tasks"]) == 3


async def test_the_repeated_said_warning_also_reaches_telegram(monkeypatch, tmp_path):
    """Главное требование к этому предупреждению: оно должно быть видно и в
    чате, и на кнопке — иначе внеполосный фактор согласия его не покажет."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    seen = _notify_recorder(monkeypatch)

    await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "эти две мне не нужны"},
        {"op": "delete", "task_id": "e5", "title": "Позвонить в банк",
         "said": "эти две мне не нужны"}])

    assert len(seen) == 1
    assert "⚠️ Одно и то же обоснование у 2 строк" in seen[0]["preview"]


async def test_distinct_said_produces_no_warning(monkeypatch, tmp_path):
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю входящие", _mixed_ops())

    assert "Одно и то же обоснование" not in preview


async def test_the_warning_sits_before_the_call2_instruction(monkeypatch, tmp_path):
    """Предупреждение печатается после списка операций и ДО инструкции «когда
    он согласится — позови снова», чтобы не разрывать инструкцию и не потерять
    хвост, по которому модель понимает протокол."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "обе не нужны"},
        {"op": "delete", "task_id": "e5", "title": "Позвонить в банк",
         "said": "обе не нужны"}])

    assert preview.index("⚠️ Одно и то же обоснование") < preview.index("Покажи это")
    assert preview.rstrip().endswith("Манифест одноразовый, действует 1 час.")


# ═══════ 21. Мелочи, которые видит человек ═════════════════════════════════

async def test_nothing_executed_says_the_plan_is_already_burned(monkeypatch, tmp_path):
    """«НИЧЕГО НЕ ВЫПОЛНЕНО» без слова о судьбе манифеста подталкивает
    повторить «да» по тому же id — а он уже погашен."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    _stub_sub_impls(monkeypatch, live)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить молоко",
         "said": "не нужно"}])
    mid = _mid(preview)

    live["a1"]["title"] = "Купить молоко и хлеб"

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "НИЧЕГО НЕ ВЫПОЛНЕНО" in out
    assert "уже погашен" in out and "заново" in out
    assert s._MANIFESTS[mid]["consumed"] is True


async def test_move_from_an_unknown_project_says_so(monkeypatch, tmp_path):
    """`«» → «Работа»` не говорит человеку ничего о том, откуда едет задача."""
    live = {"c3": {"id": "c3", "title": "Позвонить Ивану", "projectId": "p_ghost"}}
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "move", "task_id": "c3", "title": "Позвонить Ивану",
         "to_project_id": "p_work", "said": "это рабочее"}])

    assert "«неизвестный проект» → «Работа»" in preview
    assert "«» →" not in preview


async def test_merge_preview_admits_the_duplicates_fields_are_lost(
        monkeypatch, tmp_path):
    """«Объединить» обещает слияние, а код удаляет дубль: его заметки, срок и
    теги исчезают вместе с ним. Пока настоящего слияния нет, это обязано быть
    написано в плане, а не подразумеваться."""
    live = _live_inbox()
    live["e5"]["content"] = "спросить про рефинансирование, номер 8-800-…"
    live["e5"]["dueDate"] = "2026-08-10T12:00:00.000+0000"
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "merge", "task_id": "e5", "title": "Позвонить в банк",
         "keep_task_id": "e6", "keep_title": "Позвонить в банк",
         "said": "это одно и то же, оставь одну"}])

    assert "🔗 Объединить дубли: удалить «Позвонить в банк»" in preview
    assert "его заметки, срок и теги НЕ переносятся" in preview
    assert "оставить «Позвонить в банк» (проект «Работа»)" in preview


def test_docstring_matches_the_runtime_instruction_about_call2():
    """Докстринг говорил «Do NOT re-send operations», а рантайм-текст гейта —
    «список можно повторить как есть». Модель не должна выбирать, кому верить."""
    doc = s.apply_task_changes.__doc__
    assert "IGNORED on call #2" in doc
    assert "Do NOT re-send" not in doc
    assert "may be repeated verbatim" in doc


# ═══════ 20. П19: непрошедшее сверку В ПЛАН НЕ ПОПАДАЕТ ВОВСЕ ═══════════════
# (2026-08-09) Раньше такая операция оставалась строкой ТОГО ЖЕ плана с
# пометкой ⚠️ ПРОПУЩЕНО. Человеку показывали двадцать строк, три из них
# помеченные, он жал ОДНУ кнопку — и пометки проходили мимо внимания:
# решение принималось по большинству. Кнопка обязана подтверждать только то,
# что реально выполнимо; всё остальное — справка, к которой она не относится.


def _twenty_ops(live):
    """Двадцать удалений по живым задачам — размер разбора из приёмки ТЗ."""
    ops = []
    for i in range(20):
        tid = f"P{i:03d}"
        live[tid] = {"id": tid, "title": f"Позвонить по объявлению №{i}",
                     "projectId": "p_in"}
        ops.append({"op": "delete", "task_id": tid,
                    "title": f"Позвонить по объявлению №{i}",
                    "said": f"объявление №{i} уже неактуально"})
    return ops


async def test_one_renamed_task_out_of_twenty_leaves_the_plan_but_is_reported(
        monkeypatch, tmp_path):
    """Приёмка ТЗ: девятнадцать в манифест, двадцатая — только в справку.

    Проверяется ровно то, что было сломано: состав МАНИФЕСТА (кнопка ✅
    исполняет его, а не текст), а не только текст превью."""
    live = {}
    ops = _twenty_ops(live)
    _wire(monkeypatch, live, tmp_path)
    live["P007"]["title"] = "Позвонить по объявлению №7 (уже созвонились)"

    preview = await s.apply_task_changes("Разбираю входящие", ops)

    m = s._MANIFESTS[_mid(preview)]
    assert len(m["tasks"]) == 19
    assert "P007" not in [o["task_id"] for o in m["tasks"]]
    assert "✅ В план вошло: 19 операций" in preview
    assert "❌ Не вошло: 1" in preview
    assert "id P007" in preview
    # Что ожидалось и что на самом деле — обе стороны названы, а не «пропущено».
    assert "Позвонить по объявлению №7 (уже созвонились)" in preview
    assert "ПРОПУЩЕНО" not in preview


async def test_the_only_dead_task_creates_no_manifest_and_no_telegram_message(
        monkeypatch, tmp_path):
    """Приёмка ТЗ: план из одной удалённой задачи — манифеста нет вовсе, в
    Telegram не уходит ничего. Просить «да» на план, где исполнять нечего, —
    это выпрашивать согласие на пустоту."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    seen = _notify_recorder(monkeypatch)
    before = dict(s._MANIFESTS)

    out = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "ghost", "title": "Старая задача",
         "said": "неактуально"}])

    assert seen == [], f"в Telegram что-то ушло: {seen}"
    assert s._MANIFESTS == before, "манифест создан на пустом плане"
    assert "🛑" in out and "план НЕ построен" in out
    assert "id ghost" in out and "не найдена среди открытых" in out


async def test_a_plan_where_everything_matches_shows_no_reference_block(
        monkeypatch, tmp_path):
    """Приёмка ТЗ: когда расхождений нет, справочного блока нет тоже —
    обычный план не должен обрастать пустой рубрикой «не вошло: 0»."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", _mixed_ops())

    assert "Не вошло" not in preview
    assert "В план вошло" not in preview
    assert "не вошло в план" not in preview
    assert len(s._MANIFESTS[_mid(preview)]["tasks"]) == 5


async def test_the_reference_block_reaches_telegram_below_the_plan(
        monkeypatch, tmp_path):
    """Справка обязана дойти и до владельца — ниже черты, тем же сообщением,
    но БЕЗ собственных кнопок: кнопка у сообщения одна и относится к плану."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    seen = _notify_recorder(monkeypatch)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "ghost", "title": "Старая задача",
         "said": "неактуально"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])

    assert len(seen) == 1
    sent = seen[0]["preview"]
    assert "❌ Не вошло: 1" in sent
    assert "id ghost" in sent
    # Справка стоит ПОСЛЕ списка операций, а не вместо/выше него.
    assert sent.index("1. ✅ Закрыть") < sent.index("❌ Не вошло")
    assert sent in preview


async def test_no_argument_of_the_tool_can_switch_the_precheck_off():
    """Тест на отсутствие обходного пути (требование ТЗ).

    Сверка — первый шаг внутри инструмента, и другого способа создать манифест
    не существует. Тест сторожит СИГНАТУРУ: если однажды появится параметр
    вроде `skip_precheck`/`force`/`trust_titles`, он упадёт и потребует
    объяснить, почему обязательное правило снова стало необязательным.
    `automation_key` в списке разрешённых не потому, что он что-то пропускает,
    — он обходит ПОДТВЕРЖДЕНИЕ, а не сверку (см. тест ниже)."""
    import inspect

    got = set(inspect.signature(s.apply_task_changes).parameters)

    assert got == {"summary", "operations", "max_items", "manifest_id",
                   "user_reply", "automation_key"}, got


async def test_automation_key_executes_without_a_button_but_never_without_the_check(
        monkeypatch, tmp_path):
    """Единственный аргумент, который вообще меняет маршрут, — ключ headless-
    автоматики. Он снимает подтверждение, но НЕ сверку: непрошедшая операция
    не исполняется и здесь, и автоматика узнаёт о ней из ответа."""
    monkeypatch.setattr(s, "SECRET", "test-secret")
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    _tg_on(monkeypatch)
    seen = _notify_recorder(monkeypatch)

    out = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить хлеб",
         "said": "не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}], automation_key="test-secret")

    assert seen == [], "автоматика не должна слать кнопки"
    assert [c[0] for c in calls] == ["complete"]
    assert "a1" in live, "операция, не прошедшая сверку, всё-таки выполнилась"
    assert "❌ не вошло в план 1" in out, "шапка отчёта молчит о невошедшем"
    assert "#### ❌ Не вошло в план" in out and "название не совпало" in out
    assert "«Купить хлеб»" in out


# ═══════ 21. Справка про невошедшее ПЕРЕЖИВАЕТ нажатие кнопки ══════════════
# (2026-08-09, найдено независимым аудитом.) Превью живёт до нажатия:
# `summarize_in_owner_chat` перезаписывает сообщение с планом короткой сводкой
# («Объектов в плане: 19 · Подтверждено перепроверкой: 19»), а
# `_cleanup_plan_leftovers` стирает остальные куски. Единственный текст,
# который остаётся навсегда, — отчёт об исполнении, уходящий в группу-архив.
# До П19 невошедшее попадало туда САМО: оно лежало в манифесте помеченными
# строками, и отчёт печатал блок «⏭ Пропущено». Выбросив эти строки из плана,
# справку надо донести до отчёта явно — иначе улучшение превью оплачено
# потерей архива, а компенсирует потерю только внимательность модели.


def _plan_with_one_rename(monkeypatch, tmp_path):
    """План из двух операций, одна из которых не проходит сверку (задачу
    переименовали руками). Возвращает (live, calls, preview)."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    return live, calls


async def test_the_execution_report_names_what_did_not_make_the_plan(
        monkeypatch, tmp_path):
    """Чат-«да»: отчёт (он же уходит в постоянный архив) обязан назвать
    невошедшее — id, задачу и причину, а не только посчитать его в шапке."""
    live, calls = _plan_with_one_rename(monkeypatch, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить хлеб",
         "said": "не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    mid = _mid(preview)

    out = await s.apply_task_changes("Разбираю", manifest_id=mid, user_reply="да")

    assert "❌ не вошло в план 1" in out, "шапка отчёта молчит о невошедшем"
    assert "#### ❌ Не вошло в план" in out
    assert "id a1" in out
    assert "«Купить молоко»" in out, "отчёт не назвал задачу"
    assert "название не совпало" in out, "отчёт не назвал причину"
    # Рубрика отдельная от «⏭ Пропущено»: то был дрейф ПОСЛЕ подтверждения,
    # а это — то, что не подтверждалось вообще.
    assert "подтверждения по ним не было" in out
    assert [c[0] for c in calls] == ["complete"]


async def test_the_reference_survives_the_button_path_through_the_manifest(
        monkeypatch, tmp_path):
    """Кнопочный путь: сервер исполняет план САМ, через
    `_generic_gate_auto_execute(mid, m)` — тул вторым вызовом не зовётся. Всё,
    что доедет до отчёта, обязано лежать В МАНИФЕСТЕ, а не в памяти вызова."""
    live, calls = _plan_with_one_rename(monkeypatch, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить хлеб",
         "said": "не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    m = s._MANIFESTS[_mid(preview)]

    # `title` — название ИЗ ПЛАНА (живого у исчезнувшей задачи может не быть
    # вовсе), а как объект называется НА САМОМ ДЕЛЕ, говорит причина.
    assert m["extra"]["not_planned"] == [
        {"task_id": "a1", "op": "delete", "title": "Купить хлеб",
         "why": "название не совпало — по этому id сейчас «Купить молоко», "
                "а в плане «Купить хлеб»"}]
    # Справка — НЕ исполняемые строки: в ней нет ни одного ключа, по которому
    # её можно было бы принять за операцию (вернуть их в `tasks` значило бы
    # вернуть ровно ту проблему, ради которой П19 и делался).
    assert set(m["extra"]["not_planned"][0]) == {"task_id", "op", "title", "why"}

    out = await s._generic_gate_auto_execute(_mid(preview), m)

    assert "#### ❌ Не вошло в план" in out and "«Купить молоко»" in out
    assert [c[0] for c in calls] == ["complete"]


async def test_the_reference_survives_a_server_restart(monkeypatch, tmp_path):
    """Манифест переживает перезапуск через Postgres (`_durable_payload` →
    json → `_manifest_from_payload`). Справка обязана пережить его вместе с
    планом: сериализуемость здесь не деталь, а условие того, что после
    рестарта отчёт всё ещё знает, о чём умолчать нельзя."""
    import json

    live, _calls = _plan_with_one_rename(monkeypatch, tmp_path)
    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить хлеб",
         "said": "не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    m = s._MANIFESTS[_mid(preview)]

    # Ровно то, что уезжает в базу и приезжает обратно в другом процессе.
    revived = s._manifest_from_payload(
        json.loads(json.dumps(s._durable_payload(m), ensure_ascii=False)))

    assert revived["extra"]["not_planned"] == m["extra"]["not_planned"]


async def test_a_clean_plan_stores_no_reference_field_at_all(monkeypatch, tmp_path):
    """Зеркало: когда расхождений нет, в манифесте не заводится и поле —
    отчёт не должен обрастать пустой рубрикой."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", _mixed_ops())

    assert s._MANIFESTS[_mid(preview)]["extra"] == {}


# ═══════ 22. Порчи, которые проходили мимо тестов ══════════════════════════
# Все четыре внесены независимым аудитом 2026-08-09 и дали полный зелёный
# прогон. Каждая — отдельный способ «человек не узнал про часть».


def _three_bad_ops():
    """Три операции, каждая непрошедшая по СВОЕЙ причине и своего типа —
    иначе фильтр по одному типу (порча №2) остался бы невидимым."""
    return [
        {"op": "delete", "task_id": "a1", "title": "Купить хлеб",
         "said": "не нужно"},                       # название не совпало
        {"op": "update", "task_id": "ghost1", "title": "Призрак",
         "changes": {"new_title": "Призрак-2"}, "said": "переименуй"},
        {"op": "move", "task_id": "c3", "title": "Позвонить Ивану",
         "to_project": "Такого-нет", "said": "это рабочее"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"},                         # единственная исполнимая
    ]


async def test_every_single_mismatch_is_printed_not_just_the_first(
        monkeypatch, tmp_path):
    """Порча №1: `records[:1]` — счётчик «❌ Не вошло: 3» остаётся верным, а
    печатается одна строка из трёх. Число и количество строк обязаны сходиться,
    поэтому тест считает СТРОКИ, а не верит заголовку."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю", _three_bad_ops())

    assert "❌ Не вошло: 3" in preview
    printed = [ln for ln in preview.splitlines() if ln.startswith("• id ")]
    assert len(printed) == 3, f"напечатано {len(printed)} строк из 3:\n{preview}"
    # И каждая из трёх названа поимённо, а не «и ещё две».
    for needle in ("id a1", "id ghost1", "id c3"):
        assert needle in preview, needle


async def test_a_failed_update_is_reported_like_any_other_kind(
        monkeypatch, tmp_path):
    """Порча №2: `... and o.get("op") != "update"` — непрошедший update
    исчезает молча, а в заголовке остаётся «не вошло в план 1» без единого
    объяснения. Тест проверяет КАЖДЫЙ из пяти типов по отдельности."""
    for kind, op in (
            ("update", {"op": "update", "task_id": "ghost1", "title": "Призрак",
                        "changes": {"new_title": "Иначе"}, "said": "переименуй"}),
            ("delete", {"op": "delete", "task_id": "ghost1", "title": "Призрак",
                        "said": "не нужно"}),
            ("complete", {"op": "complete", "task_id": "ghost1",
                          "title": "Призрак", "said": "сделал"}),
            ("move", {"op": "move", "task_id": "ghost1", "title": "Призрак",
                      "to_project_id": "p_work", "said": "рабочее"}),
            ("merge", {"op": "merge", "task_id": "ghost1", "title": "Призрак",
                       "keep_task_id": "e6", "keep_title": "Позвонить в банк",
                       "said": "дубль"})):
        live = _live_inbox()
        _wire(monkeypatch, live, tmp_path)
        s._MANIFESTS.clear()

        preview = await s.apply_task_changes("Разбираю", [
            op, {"op": "complete", "task_id": "d4",
                 "title": "Оплатить интернет", "said": "сделал"}])

        assert "❌ Не вошло: 1" in preview, f"{kind}: нет блока\n{preview}"
        assert "id ghost1" in preview, f"{kind}: расхождение не названо"
        assert "«Призрак»" in preview, f"{kind}: задача не названа"


async def test_a_skip_mark_vetoes_execution_even_without_a_drift_reason(
        monkeypatch, tmp_path):
    """Порча №3: `if False and op.get("_skip")` в `_apply_task_changes_impl`.

    Сейчас её прячет то, что `_triage_drift_reason` дублирует все пять причин
    `_skip`, — но это совпадение, а не гарантия: появится причина без
    зеркальной drift-проверки, и старый манифест (их поднимает из базы
    `_rehydrate_manifest` после перезапуска) исполнит помеченное.

    Поэтому здесь операция ЖИВАЯ и по всем drift-проверкам чистая, а помечена
    причиной, которой в `_triage_drift_reason` нет вовсе. Единственное, что
    может её остановить, — сама пометка."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    calls = _stub_sub_impls(monkeypatch, live)
    old_manifest_task = {
        "op": "delete", "task_id": "a1", "title": "Купить молоко",
        "said": "не нужно", "_project_id": "p_in", "_live_title": "Купить молоко",
        "_skip": "причина из будущей версии сверки, зеркала в drift у неё нет"}

    assert s._triage_drift_reason(old_manifest_task, live, _NAMES) == "", \
        "фикстура сломана: drift сам её ловит, порча снова невидима"

    out = await s._apply_task_changes_impl("Старый план", [
        old_manifest_task,
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал", "_project_id": "p_in",
         "_live_title": "Оплатить интернет"}])

    assert [c[0] for c in calls] == ["complete"], "помеченная операция исполнена"
    assert "a1" in live
    assert "Пропущено" in out and "причина из будущей версии" in out


async def test_the_report_quotes_the_summary_with_its_counts(
        monkeypatch, tmp_path):
    """Порча №4: подзаголовок отчёта `_{summary}_` заменён константой — и
    отчёт после исполнения перестал повторять, ЧТО именно разбирали и сколько
    в план не вошло. Ничем не сторожилось: прежняя проверка «"пропущено 1" in
    out» исчезла вместе со старым поведением, а замены не появилось."""
    live, _calls = _plan_with_one_rename(monkeypatch, tmp_path)

    preview = await s.apply_task_changes("Разбираю входящие после созвона", [
        {"op": "delete", "task_id": "a1", "title": "Купить хлеб",
         "said": "не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])

    out = await s.apply_task_changes("Разбираю", manifest_id=_mid(preview),
                                user_reply="да")

    assert "_Разбираю входящие после созвона — закрыть 1; " \
           "не вошло в план 1_" in out, out


# ═══════ 23. Кап считается по тому, что реально уходит в манифест ══════════

async def test_the_cap_counts_the_plan_not_the_rejected_input(
        monkeypatch, tmp_path):
    """50 валидных + 1 непрошедшая = 50 исполнимых, то есть ровно кап.
    Отказывать здесь — значит отказывать из-за мусора, который и так выброшен
    (кап ограничивает разовый УЩЕРБ, а ущерб наносит только исполнимое)."""
    live, ops = _long_plan(s._TRIAGE_PLAN_DAMAGE_CAP)
    live["gone"] = {"id": "gone", "title": "Уже удалена", "projectId": "p_in"}
    ops.append({"op": "delete", "task_id": "gone", "title": "Уже удалена",
                "said": "неактуально"})
    _wire(monkeypatch, live, tmp_path)
    live.pop("gone")                       # исчезла к моменту построения плана

    preview = await s.apply_task_changes("Разбираю входящие", ops)

    assert "больше капа" not in preview
    assert len(s._MANIFESTS[_mid(preview)]["tasks"]) == s._TRIAGE_PLAN_DAMAGE_CAP
    assert "❌ Не вошло: 1" in preview


async def test_the_cap_still_refuses_when_the_plan_itself_is_too_big(
        monkeypatch, tmp_path):
    """Зеркало: считать по исполнимым — не значит ослабить. 51 исполнимая по
    прежнему отвергается целиком."""
    live, ops = _long_plan(s._TRIAGE_PLAN_DAMAGE_CAP + 1)
    _wire(monkeypatch, live, tmp_path)

    await _assert_refused_outright(monkeypatch, live, ops, "больше капа")


def test_a_short_id_is_printed_without_a_promise_of_more():
    """`a1…` обещало продолжение, которого у короткого id нет."""
    assert s._short_task_id("a1") == "a1"
    assert s._short_task_id("6a73adfc1234567890") == "6a73adfc…"


# ═══════ 24. Справка о невошедшем не врёт про НАЙДЕННУЮ задачу (Д10) ═══════


def _live_with_untitled_attachment():
    """Живое состояние, где есть безымянная задача С ВЛОЖЕНИЕМ.

    Название — один zero-width space: `_looks_untitled` (показ) считает такую
    задачу безымянной, а `_names_agree` (предохранитель) сверяет её с планом
    посимвольно, поэтому операция доходит до сверки и падает на СВОЕЙ
    причине, а не на несовпадении имени."""
    live = _live_inbox()
    live["u1"] = {"id": "u1", "title": "​", "projectId": "p_in",
                  "attachments": [{"id": "f1", "fileName": "чек.jpg"}]}
    return live


async def test_the_reference_names_an_untitled_task_it_actually_read(
        monkeypatch, tmp_path):
    """Д10 (2026-08-09). Задача НАЙДЕНА в живом состоянии и прочитана (у неё
    посчитано вложение), а из плана выброшена по ДРУГОЙ причине — проект
    назначения не существует. Справка обязана назвать её заменителем по
    содержимому, а не общим фолбэком «её нет в живом состоянии»: это
    дословно тот текст, который П15 объявил ложью, и он уезжает в Postgres
    вместе с манифестом и в архивный отчёт навсегда."""
    live = _live_with_untitled_attachment()
    _wire(monkeypatch, live, tmp_path)
    _stub_sub_impls(monkeypatch, live)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "move", "task_id": "u1", "title": "​",
         "to_project": "Проект, которого нет", "said": "это в работу"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])

    assert "❌ Не вошло: 1" in preview, preview
    assert "(без названия: 📎 1 файл)" in preview, \
        f"справка не назвала задачу её заменителем:\n{preview}"
    assert s._NO_NAME_TASK not in preview, \
        f"справка соврала про НАЙДЕННУЮ задачу:\n{preview}"
    assert s._BY_ID_NOTE in preview, "не сказано, что личность сверена по id"


async def test_the_label_reaches_the_archived_report_through_the_manifest(
        monkeypatch, tmp_path):
    """Та же метка обязана пережить кнопку: отчёт после исполнения — это
    единственный текст, который уходит в группу-архив навсегда, а справка в
    нём собирается из записи манифеста, а не из памяти вызова."""
    live = _live_with_untitled_attachment()
    _wire(monkeypatch, live, tmp_path)
    _stub_sub_impls(monkeypatch, live)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "move", "task_id": "u1", "title": "​",
         "to_project": "Проект, которого нет", "said": "это в работу"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    m = s._MANIFESTS[_mid(preview)]
    rec = m["extra"]["not_planned"][0]

    assert rec["label"] == "(без названия: 📎 1 файл)", rec
    assert rec["untitled"] is True, rec

    out = await s._generic_gate_auto_execute(_mid(preview), m)

    assert "(без названия: 📎 1 файл)" in out, out
    assert s._NO_NAME_TASK not in out, out


def test_the_reference_record_still_carries_nothing_executable():
    """Зеркало к Д10: метка добавлена, но запись по-прежнему НЕ операция —
    ни одного ключа, по которому её можно было бы исполнить."""
    rec = s._triage_not_planned_records([
        {"op": "move", "task_id": "u1", "title": "​", "said": "в работу",
         "changes": {"new_title": "подмена"}, "keep_task_id": "e6",
         "to_project_id": "p_work", "_label": "(без названия: 📎 1 файл)",
         "_untitled": True, "_live_title": "​",
         "_skip": "проект назначения не найден"}])[0]

    assert set(rec) == {"task_id", "op", "title", "label", "untitled", "why"}
    assert not ({"changes", "keep_task_id", "to_project_id", "to_project",
                 "said", "_skip", "_to_project_id"} & set(rec))

    # Задача, которую даже не нашли, метки не имеет — и полей под неё в записи
    # не заводится: пустое «имя» в базе хуже отсутствия ключа.
    ghost = s._triage_not_planned_records([
        {"op": "delete", "task_id": "ghost", "title": "Призрак",
         "said": "убери", "_skip": "не найдена среди открытых задач"}])[0]
    assert set(ghost) == {"task_id", "op", "title", "why"}


# ═══════ 25. Отказ по капу не молчит о непрошедших (Д9) ════════════════════


async def test_the_cap_refusal_still_names_what_did_not_pass_the_check(
        monkeypatch, tmp_path):
    """Д9 (2026-08-09). План больше капа отвергается целиком — но справка о
    непрошедших сверку уже посчитана, и жить ей больше негде: манифеста нет,
    превью нет, второго шанса рассказать про эти операции не будет. Без неё
    модель делит список пополам и строит новый план на те же непрошедшие."""
    live, ops = _long_plan(s._TRIAGE_PLAN_DAMAGE_CAP + 1)
    live["ghost"] = {"id": "ghost", "title": "Уже удалена", "projectId": "p_in"}
    ops.append({"op": "delete", "task_id": "ghost", "title": "Уже удалена",
                "said": "неактуально"})
    _wire(monkeypatch, live, tmp_path)
    live.pop("ghost")                      # исчезла к моменту построения плана

    out = await _assert_refused_outright(monkeypatch, live, ops, "больше капа")

    assert "❌ Не вошло: 1" in out, f"отказ по капу молчит о непрошедшей:\n{out}"
    assert "id ghost" in out
    assert "«Уже удалена»" in out
    assert "не найдена среди открытых задач" in out, "причина не названа"


async def test_a_clean_overflow_refusal_has_no_empty_reference_block(
        monkeypatch, tmp_path):
    """Зеркало: когда непрошедших нет, отказ по капу не обрастает пустой
    рубрикой «❌ Не вошло: 0»."""
    live, ops = _long_plan(s._TRIAGE_PLAN_DAMAGE_CAP + 1)
    _wire(monkeypatch, live, tmp_path)

    out = await _assert_refused_outright(monkeypatch, live, ops, "больше капа")

    assert "Не вошло" not in out, out


# ═══════ 26. Ранний выход исполнителя не теряет справку (Д8) ═══════════════


_NOT_PLANNED_ONE = [{"task_id": "a1", "op": "delete", "title": "Купить хлеб",
                     "why": "название не совпало — по этому id сейчас "
                            "«Купить молоко», а в плане «Купить хлеб»"}]


async def test_the_state_unavailable_exit_still_names_what_did_not_make_it(
        monkeypatch, tmp_path):
    """Д8 (2026-08-09). Манифест погашен, исполнитель стартовал, живое
    состояние недоступно. Про операции, не прошедшие сверку, сказать больше
    негде и некогда: план одноразовый, справка жила только в нём, превью в
    личке уже затёрто сводкой."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: None)

    out = await s._apply_task_changes_impl(
        "Разбираю",
        [{"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
          "said": "сделал", "_project_id": "p_in",
          "_live_title": "Оплатить интернет"}],
        not_planned=list(_NOT_PLANNED_ONE))

    assert "состояние TickTick недоступно" in out, out
    assert "#### ❌ Не вошло в план" in out, f"справка потеряна:\n{out}"
    assert "id a1" in out and "название не совпало" in out


async def test_the_not_ready_exit_still_names_what_did_not_make_it(
        monkeypatch, tmp_path):
    """Тот же ранний выход этажом выше: сервер вообще не готов. Справка
    одноразовая ровно так же."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    monkeypatch.setattr(s, "_ensure_ready", lambda: "🛑 Сервер не настроен.")

    out = await s._apply_task_changes_impl(
        "Разбираю",
        [{"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
          "said": "сделал", "_project_id": "p_in",
          "_live_title": "Оплатить интернет"}],
        not_planned=list(_NOT_PLANNED_ONE))

    assert "Сервер не настроен" in out
    assert "#### ❌ Не вошло в план" in out, f"справка потеряна:\n{out}"


async def test_an_early_exit_without_a_reference_is_byte_for_byte_the_old_text(
        monkeypatch, tmp_path):
    """Зеркало: когда справки нет, ранний выход не обрастает ни пустой
    рубрикой, ни лишним переводом строки."""
    live = _live_inbox()
    _wire(monkeypatch, live, tmp_path)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: None)

    out = await s._apply_task_changes_impl(
        "Разбираю",
        [{"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
          "said": "сделал", "_project_id": "p_in",
          "_live_title": "Оплатить интернет"}])

    assert out == s._STATE_UNAVAILABLE_MSG


async def test_the_reference_survives_the_button_path_into_a_dead_state(
        monkeypatch, tmp_path):
    """Полный кнопочный путь: план построен, справка уехала в манифест, а к
    моменту исполнения живое состояние отвалилось. Отчёт — единственный текст,
    который увидит человек, и он обязан назвать невошедшее."""
    live, _calls = _plan_with_one_rename(monkeypatch, tmp_path)

    preview = await s.apply_task_changes("Разбираю", [
        {"op": "delete", "task_id": "a1", "title": "Купить хлеб",
         "said": "не нужно"},
        {"op": "complete", "task_id": "d4", "title": "Оплатить интернет",
         "said": "сделал"}])
    m = s._MANIFESTS[_mid(preview)]
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: None)

    out = await s._generic_gate_auto_execute(_mid(preview), m)

    assert "состояние TickTick недоступно" in out, out
    assert "#### ❌ Не вошло в план" in out, f"справка потеряна:\n{out}"
    assert "«Купить молоко»" in out


# ═══════ 27. Два предела: объём входа и ущерб плана (Д12) ══════════════════


async def test_a_huge_input_is_refused_before_live_state_is_even_read(
        monkeypatch, tmp_path):
    """Д12 (2026-08-09). Предел ущерба считается по прошедшим сверку — и это
    правильно, но верхней границы на ДЛИНУ ВХОДА после той правки не осталось
    ни одной. Вызов, где сверку переживает горстка строк, а остальные тысячи
    едут в превью, в манифест и в архивный отчёт, обязан отвергаться ДО
    чтения живого состояния."""
    live, ops = _long_plan(s._TRIAGE_INPUT_VOLUME_CAP + 1)
    _wire(monkeypatch, live, tmp_path)
    reads = []
    monkeypatch.setattr(s, "_open_by_id",
                        lambda fresh=False: reads.append(1) or dict(live))

    out = await _assert_refused_outright(
        monkeypatch, live, ops, "больше предела на объём одного вызова")

    assert reads == [], "живое состояние читалось, хотя вход отвергнут"
    assert str(s._TRIAGE_INPUT_VOLUME_CAP) in out


async def test_the_volume_limit_does_not_care_how_many_survive_the_check(
        monkeypatch, tmp_path):
    """Тот самый сценарий: почти всё не проходит сверку, план получается
    крошечный. Предел УЩЕРБА такой вызов честно пропускает — держать его
    обязан отдельный предел объёма."""
    live, ops = _long_plan(s._TRIAGE_INPUT_VOLUME_CAP + 40)
    _wire(monkeypatch, live, tmp_path)
    # Всё, кроме десяти, исчезло из живого состояния — в план ушло бы 10.
    for tid in list(live)[10:]:
        live.pop(tid)

    out = await _assert_refused_outright(
        monkeypatch, live, ops, "больше предела на объём одного вызова")

    assert "больше капа" not in out, "перепутаны два разных предела"


async def test_the_two_limits_are_different_numbers_and_different_checks(
        monkeypatch, tmp_path):
    """Зеркало к обоим: 60 операций — больше предела УЩЕРБА (50), но меньше
    предела ОБЪЁМА (200). Отказ должен быть по ущербу, а не по объёму, иначе
    пределы снова сведены в один."""
    assert s._TRIAGE_PLAN_DAMAGE_CAP == 50
    assert s._TRIAGE_INPUT_VOLUME_CAP == 200
    assert s._TRIAGE_INPUT_VOLUME_CAP > s._TRIAGE_PLAN_DAMAGE_CAP

    live, ops = _long_plan(s._TRIAGE_PLAN_DAMAGE_CAP + 10)
    _wire(monkeypatch, live, tmp_path)

    out = await _assert_refused_outright(monkeypatch, live, ops, "больше капа")

    assert "объём одного вызова" not in out


async def test_the_volume_limit_is_not_a_lever_the_caller_can_pull(
        monkeypatch, tmp_path):
    """`max_items` — рычаг вызывающего на УЩЕРБ («сделай план поменьше»).
    Объём собственного ввода вызывающий регулировать не вправе: и щедрый, и
    скупой max_items одинаково не спасают вызов на 201 операцию."""
    live, ops = _long_plan(s._TRIAGE_INPUT_VOLUME_CAP + 1)
    _wire(monkeypatch, live, tmp_path)

    for lever in (10_000, 5):
        await _assert_refused_outright(
            monkeypatch, live, ops, "больше предела на объём одного вызова",
            max_items=lever)


async def test_a_plan_at_the_damage_cap_still_builds(monkeypatch, tmp_path):
    """И главное: ни один из двух пределов не съел законный разбор — ровно
    кап ущерба по-прежнему проходит."""
    live, ops = _long_plan(s._TRIAGE_PLAN_DAMAGE_CAP)
    _wire(monkeypatch, live, tmp_path)

    preview = await s.apply_task_changes("Разбираю входящие", ops)

    assert "🛑" not in preview
    assert len(s._MANIFESTS[_mid(preview)]["tasks"]) == s._TRIAGE_PLAN_DAMAGE_CAP
