"""Живой QA-баг (2026-08-19): update_tasks call #2 рапортовал «✏️ обновлено
(проверено)» на элементе, где не задано НИ ОДНОГО поля изменения (только
taskId+title+projectId) — хотя get_task до/после был идентичен.

Причина (server.py):
  * `_update_tasks_impl` (single-task path) всё равно звал
    `ticktick.update_task(..., title=None, content=None, ...)` — no-op 200 —
    и собирало `expect.changes = {}` для пост-верификации.
  * `_update_tasks_impl` (batch path, 2+ задач без advanced-полей) собирало
    `ch = {"taskId": tid}` без единого реального поля и всё равно клало его
    в `changes`, отправляя no-op в `batch_update_tasks`.
  * `_verify_item_core` (ветка `op == "update"`) сравнивает живое состояние
    только с полями из `expect.changes` — пустой словарь даёт пустой список
    расхождений (`diffs == []`), а пустой список расхождений читается как
    «все изменения на месте» (`_ItemVerdict("ok", ...)`). Это вакуумная
    истина: не «сверили и совпало», а «сверять было нечего».

План (call #1) при этом уже вёл себя честно — `_update_change_bits` печатает
«(поля изменений не распознаны)» — но это была ТОЛЬКО подсказка в превью,
исполнение (call #2) её не читало и всё равно шло в TickTick.

Починка: `_update_item_has_changes` — тот же список полей, что и в
`_update_change_bits` (один источник правды), — отказывает такой строке ДО
identity guard'а и ДО обращения к TickTick, в обоих путях."""
import ticktick_mcp.src.server as s


# ---------------------------------------------------------------------------
# Pure predicate
# ---------------------------------------------------------------------------

class TestUpdateItemHasChanges:
    def test_no_recognized_fields_is_false(self):
        t = {"taskId": "t1", "title": "X", "projectId": "p1"}
        assert s._update_item_has_changes(t) is False
        assert s._update_change_bits(t) == "(поля изменений не распознаны)"

    def test_only_priority_is_true(self):
        t = {"taskId": "t1", "title": "X", "priority": 5}
        assert s._update_item_has_changes(t) is True

    def test_only_reminders_is_true(self):
        # advanced-поле, не входит в v2-батч, но реально применяется —
        # _update_change_bit_list обязана его видеть, как и превью плана.
        t = {"taskId": "t1", "title": "X", "reminders": ["TRIGGER:PT0S"]}
        assert s._update_item_has_changes(t) is True

    def test_empty_content_string_counts_as_a_change(self):
        # content не проверяется на truthiness — стирание содержимого
        # ("") ЯВЛЯЕТСЯ изменением, `is not None` — то же правило, что и в
        # _update_change_bits/_update_tasks_impl.
        t = {"taskId": "t1", "title": "X", "content": ""}
        assert s._update_item_has_changes(t) is True


# ---------------------------------------------------------------------------
# Integration: single-task path (len(tasks) == 1) must refuse, not call the
# API, not journal, and must NOT print «обновлено»/«проверено».
# ---------------------------------------------------------------------------

class FakeOfficial:
    def __init__(self, live):
        self.live = live
        self.calls = []

    def update_task(self, task_id, project_id, title=None, content=None,
                    start_date=None, due_date=None, priority=None,
                    repeat_flag=None, reminders=None):
        self.calls.append(dict(task_id=task_id))
        t = self.live[task_id]
        if title is not None:
            t["title"] = title
        if content is not None:
            t["content"] = content
        if priority is not None:
            t["priority"] = priority
        return {"id": task_id}


def _wire_single(monkeypatch, live):
    fake = FakeOfficial(live)
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {
        k: dict(v) for k, v in fake.live.items()})
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    return fake


async def test_single_task_empty_update_is_refused_not_reported_success(monkeypatch):
    live = {"t1": {"id": "t1", "title": "__QATEST_CORE__ happy create",
                   "projectId": "p1"}}
    fake = _wire_single(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1",
         "title": "__QATEST_CORE__ happy create"}])
    # Раньше здесь было "✏️ «...» обновлено (проверено)" — ложный успех.
    assert "обновлено" not in result
    assert "проверено" not in result
    assert "нечего менять" in result
    assert "🛑" in result
    # Не должно было дойти ни до какого сетевого вызова.
    assert fake.calls == []


async def test_single_task_empty_update_writes_no_journal_record(monkeypatch):
    live = {"t1": {"id": "t1", "title": "X", "projectId": "p1"}}
    _wire_single(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "X"}])
    # Пустое обновление — не мутация, журналировать нечего: без ссылки
    # "operation_report(record_id=..." в ответе.
    assert "operation_report" not in result


async def test_single_task_real_change_still_reports_success(monkeypatch):
    # Смежная строка того же вызова с реальным полем ДОЛЖНА пройти как
    # раньше — правка не должна задевать законные обновления.
    live = {"t1": {"id": "t1", "title": "X", "projectId": "p1", "priority": 0}}
    fake = _wire_single(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "X", "priority": 5}])
    assert "проверено" in result
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Integration: batch path (2+ tasks, no advanced fields).
# ---------------------------------------------------------------------------

class FakeV2:
    def __init__(self, live):
        self.live = live
        self.calls = []

    def batch_update_tasks(self, changes):
        self.calls.append(changes)
        for ch in changes:
            t = self.live.get(ch["taskId"])
            if not t:
                continue
            for field in ("startDate", "dueDate", "priority", "title",
                         "isAllDay", "content", "tags"):
                if field in ch:
                    t[field] = ch[field]
        return {"id2error": {}}


def _wire_batch(monkeypatch, live):
    fake_v2 = FakeV2(live)
    monkeypatch.setattr(s, "ticktick", object())
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {
        k: dict(v) for k, v in fake_v2.live.items()})
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    return fake_v2


async def test_batch_mixed_empty_and_real_change(monkeypatch):
    live = {
        "t1": {"id": "t1", "title": "Пустая правка", "projectId": "p1",
              "priority": 0},
        "t2": {"id": "t2", "title": "Настоящая правка", "projectId": "p1",
              "priority": 0},
    }
    fake_v2 = _wire_batch(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "Пустая правка"},
        {"taskId": "t2", "projectId": "p1", "title": "Настоящая правка",
         "priority": 5},
    ])
    # Только реальная правка ушла в batch_update_tasks.
    assert len(fake_v2.calls) == 1
    sent_ids = [c["taskId"] for c in fake_v2.calls[0]]
    assert sent_ids == ["t2"]
    assert "Настоящая правка" in result
    assert "обновлено (проверено)" not in result  # старый (одиночный) фразинг
    assert "Обновлено 1" in result
    assert "«Пустая правка»: нечего менять" in result


async def test_batch_all_empty_calls_no_api(monkeypatch):
    live = {
        "t1": {"id": "t1", "title": "A", "projectId": "p1"},
        "t2": {"id": "t2", "title": "B", "projectId": "p1"},
    }
    fake_v2 = _wire_batch(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "A"},
        {"taskId": "t2", "projectId": "p1", "title": "B"},
    ])
    assert fake_v2.calls == []
    assert "Обновлено" not in result
    assert result.count("нечего менять") == 2
    assert "operation_report" not in result


# ---------------------------------------------------------------------------
# Смежный случай, решённый СОЗНАТЕЛЬНО (не бага): значения в запросе РАВНЫ
# текущим живым значениям. Запрос НЕ пуст — поле реально распознано и
# передано, просто состояние уже совпадает. Пост-верификация проверяет
# "совпадает ли живое состояние с запрошенным" (факт), а не "изменилось ли
# что-то на сервере" (дельта) — это тот же принцип, на котором построены
# все остальные ветки _verify_item_core (например create/restore проверяют
# итоговое состояние, а не сам факт сетевого вызова). Запрошенное значение
# ДЕЙСТВИТЕЛЬНО верно в живом состоянии — «обновлено (проверено)» здесь не
# ложь. В отличие от полностью пустого элемента (тесты выше), где не было
# NIL заявленного намерения вообще. Оставлено БЕЗ ИЗМЕНЕНИЙ — тест
# фиксирует это решение, чтобы будущая правка не считала его багом молча.
async def test_value_equal_to_current_still_reports_verified_by_design(monkeypatch):
    live = {"t1": {"id": "t1", "title": "X", "projectId": "p1", "priority": 5}}
    fake = _wire_single(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "X", "priority": 5}])
    assert "проверено" in result
    assert len(fake.calls) == 1
