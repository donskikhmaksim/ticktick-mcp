"""Package 5 (retrofit): the wide/heavy read methods — search_all_tasks,
get_task_info, get_task_activity, get_changes, list_task_attachments.

Covers:
  5.1 get_changes: honest cap on the final events list (~200, "shown N of M"),
      and a cap on the open-tasks pool scanned to build it.
  5.2 search_all_tasks: closed_matches/comment_matches get the same honest
      "...and N more" truncation open_matches already had via format_task_tree,
      instead of a silent [:100].
  5.4 Russian output for search_all_tasks, get_task_info, list_task_attachments
      (get_task_activity's Russian output is covered by test_task_activity.py).
"""
import ticktick_mcp.src.server as s


class FakeV2Changes:
    """Stand-in for TickTickV2Client covering only what get_changes touches."""

    def __init__(self, open_tasks=None, completed=None, trash=None):
        self._open = open_tasks or []
        self._completed = completed or []
        self._trash = trash or []

    def get_open_tasks(self):
        return self._open

    def get_completed_tasks(self, limit=50, from_str="", to_str=None):
        return self._completed

    def get_trash(self, limit=50):
        return self._trash

    def get_state(self):
        return {"projectProfiles": [], "inboxId": "inbox1"}


def _open_task(tid, day, title=None):
    return {
        "id": tid,
        "title": title or tid,
        "projectId": "p1",
        "createdTime": f"2026-06-{day:02d}T10:00:00+0000",
        "modifiedTime": f"2026-06-{day:02d}T10:00:00+0000",
    }


class TestGetChangesEventsCap:
    async def test_under_cap_shows_plain_count(self, monkeypatch):
        tasks = [_open_task(f"t{i}", 1) for i in range(5)]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2Changes(open_tasks=tasks))

        out = await s.get_changes(since="2026-06-01", until="2026-06-30")

        assert "(5)" in out
        assert "показано" not in out

    async def test_over_cap_is_honestly_truncated(self, monkeypatch):
        # 250 distinct create events on 250 distinct days within [2026-01-01, 2026-12-31]
        tasks = [
            {
                "id": f"t{i}",
                "title": f"task{i}",
                "projectId": "p1",
                "createdTime": f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T10:00:00+0000",
                "modifiedTime": f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T10:00:00+0000",
            }
            for i in range(250)
        ]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2Changes(open_tasks=tasks))

        out = await s.get_changes(since="2026-01-01", until="2026-12-31")

        assert "показано 200 из 250" in out

    async def test_open_scan_cap_warns_when_pool_exceeds_it(self, monkeypatch):
        tasks = [_open_task(f"t{i}", (i % 28) + 1) for i in range(s._GET_CHANGES_OPEN_SCAN_CAP + 50)]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2Changes(open_tasks=tasks))

        out = await s.get_changes(since="2026-06-01", until="2026-06-30")

        assert "лимита сканирования" in out
        assert str(s._GET_CHANGES_OPEN_SCAN_CAP) in out

    async def test_open_scan_cap_not_mentioned_when_under_it(self, monkeypatch):
        tasks = [_open_task(f"t{i}", 1) for i in range(3)]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2Changes(open_tasks=tasks))

        out = await s.get_changes(since="2026-06-01", until="2026-06-30")

        assert "лимита сканирования" not in out


class TestGetChangesIsRussian:
    async def test_no_changes_message_is_russian(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", FakeV2Changes())
        out = await s.get_changes(since="2026-06-01", until="2026-06-30")
        assert "изменений не найдено" in out


class FakeV2Search:
    """Stand-in covering what search_all_tasks' open-pool branch touches."""

    def __init__(self, open_tasks=None):
        self._open = open_tasks or []

    def get_open_tasks(self):
        return self._open

    def get_completed_tasks(self, limit=100):
        return []


class FakeV1Search:
    """Stand-in for the v1 client's closed-project branch."""

    def __init__(self, closed_projects_tasks=None):
        # {"projX": [task, ...]}
        self._closed = closed_projects_tasks or {}

    def get_projects(self):
        return [{"id": pid, "closed": True} for pid in self._closed]

    def get_project_with_data(self, pid):
        return {"tasks": self._closed.get(pid, [])}


class TestSearchAllTasksHonestTruncation:
    async def test_closed_matches_over_cap_says_and_n_more(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", FakeV2Search())
        closed_tasks = [
            {"id": f"c{i}", "title": "budget review", "projectId": "closedproj"}
            for i in range(120)
        ]
        monkeypatch.setattr(s, "ticktick", FakeV1Search({"closedproj": closed_tasks}))

        out = await s.search_all_tasks("budget", scope="closed")

        assert "… и ещё 20." in out

    async def test_closed_matches_under_cap_has_no_truncation_note(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", FakeV2Search())
        closed_tasks = [
            {"id": f"c{i}", "title": "budget review", "projectId": "closedproj"}
            for i in range(3)
        ]
        monkeypatch.setattr(s, "ticktick", FakeV1Search({"closedproj": closed_tasks}))

        out = await s.search_all_tasks("budget", scope="closed")

        assert "и ещё" not in out


class TestSearchAllTasksIsRussian:
    async def test_no_matches_is_russian(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", FakeV2Search())
        monkeypatch.setattr(s, "ticktick", FakeV1Search())

        out = await s.search_all_tasks("nope-nothing-here", scope="open")

        assert "Ничего не найдено" in out

    async def test_matches_header_is_russian(self, monkeypatch):
        tasks = [{"id": "t1", "title": "поговорить с банком", "projectId": "p1"}]
        monkeypatch.setattr(s, "ticktick_v2", FakeV2Search(open_tasks=tasks))
        monkeypatch.setattr(s, "ticktick", FakeV1Search())

        out = await s.search_all_tasks("банк", scope="open")

        assert "Совпадения по 'банк'" in out
        assert "Открытые проекты" in out


class FakeV2TaskInfo:
    def __init__(self, tasks, inbox_id="inbox1"):
        self._tasks = tasks
        self._inbox_id = inbox_id

    def get_state(self):
        return {"inboxId": self._inbox_id, "syncTaskBean": {"update": self._tasks}}


class TestGetTaskInfoIsRussian:
    async def test_found_task_is_russian(self, monkeypatch):
        task = {"id": "t1", "title": "Позвонить маме", "projectId": "p1",
                "priority": 0, "status": 0, "creator": "me"}
        monkeypatch.setattr(s, "ticktick_v2", FakeV2TaskInfo([task], inbox_id="me"))

        out = await s.get_task_info("t1")

        assert "Задача: Позвонить маме" in out
        assert "статус: Активна" in out

    async def test_missing_task_is_russian(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", FakeV2TaskInfo([]))

        out = await s.get_task_info("ghost")

        assert "не найдена среди открытых" in out


class FakeV2Attachments:
    def __init__(self, atts):
        self._atts = atts

    def get_state(self):
        return {"inboxId": "inbox1", "syncTaskBean": {"update": []}}


class TestListTaskAttachmentsIsRussian:
    async def test_no_attachments_is_russian(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", object())
        monkeypatch.setattr(s, "_merged_task_attachments", lambda task_id: [])

        out = await s.list_task_attachments("t1")

        assert "нет вложений" in out

    async def test_with_attachments_is_russian(self, monkeypatch):
        monkeypatch.setattr(s, "ticktick_v2", object())
        atts = [{"id": "a1", "fileName": "report.pdf", "fileSize": 2048}]
        monkeypatch.setattr(s, "_merged_task_attachments", lambda task_id: atts)

        out = await s.list_task_attachments("t1")

        assert "Вложения задачи t1" in out
        assert "report.pdf" in out
