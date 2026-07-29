"""Bug: changing ONE date field (e.g. only due_date) on a task that already
had a date used to leave the OTHER field (startDate) holding its OLD value,
because update_task/batch_update_tasks write startDate/dueDate independently
and only send fields that were actually passed. TickTick then renders
startDate != dueDate as a multi-day RANGE («с 23-го по <новое>») instead of
the intended plain move to a new single date.

Fix: _sync_point_date (ticktick_mcp/src/server.py) — when the caller supplies
exactly ONE of start_date/due_date and the task's live state shows it was
previously a single fixed date (startDate == dueDate), sync the untouched
field to the same new value so the task stays single-date. An intentional
range (caller passes BOTH fields, or the task already had a real range) is
left alone.

These tests cover the pure helper directly, plus a light integration check
through update_tasks' single- and batch-task code paths to confirm the
synced value is what actually gets sent to the TickTick client."""
import ticktick_mcp.src.server as s
from ticktick_mcp.src.ticktick_client import _normalize_date


# ---------------------------------------------------------------------------
# Pure-function tests for _sync_point_date
# ---------------------------------------------------------------------------

class TestSyncPointDate:
    def _point_task(self, date="2026-07-23T00:00:00.000+0000"):
        return {"id": "t1", "title": "x", "startDate": date, "dueDate": date}

    def _range_task(self):
        return {"id": "t1", "title": "x",
                "startDate": "2026-07-20T00:00:00.000+0000",
                "dueDate": "2026-07-25T00:00:00.000+0000"}

    # (a) task was a single fixed date, caller changes only due_date →
    # start_date must be synced to the SAME new value (no range appears).
    def test_only_due_date_on_point_task_syncs_start(self):
        start, due, warn = s._sync_point_date(
            self._point_task(), None, "2026-07-30")
        assert due == "2026-07-30"
        assert start == "2026-07-30"
        assert warn is None

    def test_only_start_date_on_point_task_syncs_due(self):
        start, due, warn = s._sync_point_date(
            self._point_task(), "2026-08-01", None)
        assert start == "2026-08-01"
        assert due == "2026-08-01"
        assert warn is None

    # (b) caller explicitly supplies BOTH fields with different values →
    # deliberate range, must be left exactly as given, no sync, no warning.
    def test_both_fields_given_are_left_untouched(self):
        start, due, warn = s._sync_point_date(
            self._point_task(), "2026-07-28", "2026-08-05")
        assert start == "2026-07-28"
        assert due == "2026-08-05"
        assert warn is None

    def test_both_fields_given_identical_left_untouched(self):
        start, due, warn = s._sync_point_date(
            self._point_task(), "2026-07-28", "2026-07-28")
        assert start == "2026-07-28"
        assert due == "2026-07-28"
        assert warn is None

    # (c) no prior live state at all (brand-new task / state unknown) →
    # nothing to sync against, pass through unchanged — a lone field with no
    # counterpart is just "no date on the other side", not a range.
    def test_no_live_state_passthrough(self):
        start, due, warn = s._sync_point_date(None, None, "2026-07-30")
        assert start is None
        assert due == "2026-07-30"
        assert warn is None

    # Task already had a REAL range before this edit, caller touches only one
    # side → values are left as given (not clobbered) but a warning surfaces.
    def test_existing_range_one_field_changed_warns_but_does_not_sync(self):
        start, due, warn = s._sync_point_date(
            self._range_task(), None, "2026-08-10")
        assert start is None
        assert due == "2026-08-10"
        assert warn is not None
        assert "диапазон" in warn

    def test_existing_range_both_fields_changed_no_warn(self):
        start, due, warn = s._sync_point_date(
            self._range_task(), "2026-07-21", "2026-08-10")
        assert start == "2026-07-21"
        assert due == "2026-08-10"
        assert warn is None

    def test_neither_field_given_noop(self):
        start, due, warn = s._sync_point_date(self._point_task(), None, None)
        assert start is None
        assert due is None
        assert warn is None


# ---------------------------------------------------------------------------
# Integration: update_tasks single-task path actually sends the synced value
# ---------------------------------------------------------------------------

class FakeOfficial:
    """Fakes the bits of TickTickClient.update_task that update_tasks touches,
    and mutates a shared live-task store so post-verify's fresh re-read sees
    the (correctly synced) result — mirroring what the real API would do."""

    def __init__(self, live):
        self.live = live  # {taskId: task dict}, same object _open_by_id reads
        self.calls = []

    def update_task(self, task_id, project_id, title=None, content=None,
                    start_date=None, due_date=None, priority=None,
                    repeat_flag=None, reminders=None):
        self.calls.append(dict(task_id=task_id, project_id=project_id,
                               title=title, content=content,
                               start_date=start_date, due_date=due_date,
                               priority=priority))
        t = self.live[task_id]
        date_only = False
        if start_date:
            val, d = _normalize_date(start_date)
            date_only = date_only or d
            t["startDate"] = val
        if due_date:
            val, d = _normalize_date(due_date)
            date_only = date_only or d
            t["dueDate"] = val
        if date_only:
            t["isAllDay"] = True
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


async def test_single_path_syncs_start_date_when_only_due_given(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Оплатить аренду", "projectId": "p1",
                   "startDate": "2026-07-23T00:00:00.000+0000",
                   "dueDate": "2026-07-23T00:00:00.000+0000"}}
    fake = _wire_single(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "Оплатить аренду",
         "due_date": "2026-07-30"}])
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["due_date"] == "2026-07-30"
    assert call["start_date"] == "2026-07-30"  # synced — no range
    assert "проверено" in result
    assert "диапазон" not in result


async def test_single_path_leaves_explicit_range_alone(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Отпуск", "projectId": "p1",
                   "startDate": "2026-07-23T00:00:00.000+0000",
                   "dueDate": "2026-07-23T00:00:00.000+0000"}}
    fake = _wire_single(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "Отпуск",
         "start_date": "2026-08-01", "due_date": "2026-08-10"}])
    call = fake.calls[0]
    assert call["start_date"] == "2026-08-01"
    assert call["due_date"] == "2026-08-10"
    assert "проверено" in result


async def test_single_path_no_prior_dates_no_sync(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Новая задача", "projectId": "p1"}}
    fake = _wire_single(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "Новая задача",
         "due_date": "2026-07-30"}])
    call = fake.calls[0]
    assert call["due_date"] == "2026-07-30"
    assert call["start_date"] is None
    assert "проверено" in result


# ---------------------------------------------------------------------------
# Integration: update_tasks BATCH path (2+ tasks, no advanced fields) applies
# the same sync rule.
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


async def test_batch_path_syncs_point_task_date(monkeypatch):
    live = {
        "t1": {"id": "t1", "title": "A", "projectId": "p1",
               "startDate": "2026-07-23T00:00:00.000+0000",
               "dueDate": "2026-07-23T00:00:00.000+0000"},
        "t2": {"id": "t2", "title": "B", "projectId": "p1",
               "startDate": "2026-07-24T00:00:00.000+0000",
               "dueDate": "2026-07-24T00:00:00.000+0000"},
    }
    fake_v2 = _wire_batch(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "A", "due_date": "2026-07-30"},
        {"taskId": "t2", "projectId": "p1", "title": "B", "priority": 3},
    ])
    assert len(fake_v2.calls) == 1
    ch_by_id = {c["taskId"]: c for c in fake_v2.calls[0]}
    assert ch_by_id["t1"]["dueDate"][:10] == "2026-07-30"
    assert ch_by_id["t1"]["startDate"][:10] == "2026-07-30"  # synced
    assert "Обновлено 2" in result
    assert "диапазон" not in result


async def test_batch_path_warns_on_existing_range_one_side_changed(monkeypatch):
    live = {
        "t1": {"id": "t1", "title": "Отпуск", "projectId": "p1",
               "startDate": "2026-07-20T00:00:00.000+0000",
               "dueDate": "2026-07-25T00:00:00.000+0000"},
        "t2": {"id": "t2", "title": "B", "projectId": "p1",
               "startDate": "2026-07-24T00:00:00.000+0000",
               "dueDate": "2026-07-24T00:00:00.000+0000"},
    }
    fake_v2 = _wire_batch(monkeypatch, live)
    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "projectId": "p1", "title": "Отпуск", "due_date": "2026-08-10"},
        {"taskId": "t2", "projectId": "p1", "title": "B", "priority": 3},
    ])
    ch_by_id = {c["taskId"]: c for c in fake_v2.calls[0]}
    assert "startDate" not in ch_by_id["t1"]  # left alone, not clobbered
    assert ch_by_id["t1"]["dueDate"][:10] == "2026-08-10"
    assert "диапазон" in result
