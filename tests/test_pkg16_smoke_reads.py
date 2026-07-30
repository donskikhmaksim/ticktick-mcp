"""PLAN_retrofit.md §16.12 — smoke tests for read tools whose bodies wrap
everything in a broad `except Exception`, so a field-name change in TickTick's
API (renamed/removed key) does NOT throw and get printed as an obtuse
"Ошибка ...: <str(e)>" — instead the value silently becomes empty/None
via `.get(...)`, which the try/except would never catch at all. Neither
failure mode is caught by a test that just checks "doesn't raise" or "doesn't
say Ошибка" — the only thing that actually catches a silent field rename is
asserting the REAL field values from a realistic payload show up verbatim in
the formatted output. That's what these tests do, for the five methods named
in the plan item: run_filter, get_statistics, get_trash, list_project_groups,
get_task_comments.

If a future TickTick API change or a local refactor renames one of these
fields (e.g. `todayCompleted` -> `completedToday`), the corresponding
assertion below fails LOUDLY at the exact field, instead of the tool quietly
returning "Очки достижений: None" or an "Ошибка получения статистики: ..."
that only shows up as a vague failure deep in a chat transcript.
"""
import ticktick_mcp.src.server as s


def test_run_filter_smoke_realistic_payload_renders_real_fields(monkeypatch):
    class FakeV2:
        def run_filter(self, filter_id_or_name):
            assert filter_id_or_name == "Сегодня"
            return [{"id": "t1", "title": "Позвонить маме", "projectId": "p1",
                     "priority": 5, "dueDate": "2026-07-29T10:00:00.000+0000"}]
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())
    import asyncio
    out = asyncio.run(s.run_filter("Сегодня"))
    assert "Ошибка" not in out
    assert "Позвонить маме" in out
    assert "t1" in out


def test_run_filter_smoke_empty_result_is_not_an_error(monkeypatch):
    class FakeV2:
        def run_filter(self, filter_id_or_name):
            return []
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())
    import asyncio
    out = asyncio.run(s.run_filter("Пусто"))
    assert "Ошибка" not in out


def test_get_statistics_smoke_realistic_payload_renders_real_fields(monkeypatch):
    class FakeV2:
        def get_statistics(self):
            return {"score": 4210, "level": 7, "todayCompleted": 3,
                    "yesterdayCompleted": 5, "totalCompleted": 812}
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())
    import asyncio
    out = asyncio.run(s.get_statistics())
    assert "Ошибка" not in out
    assert "4210" in out
    assert "7" in out
    assert "3" in out
    assert "5" in out
    assert "812" in out


def test_get_trash_smoke_realistic_payload_renders_real_fields(monkeypatch):
    class FakeV2:
        def get_trash(self, limit=50):
            assert limit == 50
            return [{"id": "t1", "title": "Удалённая задача", "projectId": "p1"}]
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Личное"})
    import asyncio
    out = asyncio.run(s.get_trash())
    assert "Ошибка" not in out
    assert "Удалённая задача" in out
    assert "t1" in out


def test_get_trash_smoke_empty_is_not_an_error(monkeypatch):
    class FakeV2:
        def get_trash(self, limit=50):
            return []
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())
    import asyncio
    out = asyncio.run(s.get_trash())
    assert "Ошибка" not in out
    assert "Корзина пуста" in out


def test_list_project_groups_smoke_realistic_payload_renders_real_fields(monkeypatch):
    class FakeV2:
        def list_project_groups(self):
            return [{"id": "g1", "name": "Работа", "deleted": 0},
                    {"id": "g2", "name": "Личное (архив)", "deleted": 1}]
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())
    import asyncio
    out = asyncio.run(s.list_project_groups())
    assert "Ошибка" not in out
    assert "Работа" in out
    assert "g1" in out
    # deleted=1 group must be filtered out, not just present-but-unlabeled
    assert "Личное (архив)" not in out


def test_get_task_comments_smoke_realistic_payload_renders_real_fields(monkeypatch):
    class FakeV2:
        def get_task_comments(self, project_id, task_id):
            assert project_id == "p1" and task_id == "t1"
            return [{"id": "c1", "title": "Отличная мысль",
                    "userProfile": {"displayName": "Максим"}}]
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())
    import asyncio
    out = asyncio.run(s.get_task_comments("Задача", "p1", "t1"))
    assert "Ошибка" not in out and "Error" not in out
    assert "Отличная мысль" in out
    assert "Максим" in out
    assert "c1" in out


def test_get_task_comments_smoke_fallback_username_when_no_profile(monkeypatch):
    """The formatter falls back to `userName` when `userProfile` is absent —
    a real API shape difference, not a hypothetical. A field rename here
    (e.g. userProfile.displayName -> userProfile.name) would silently start
    printing "?" instead of the name; this pins the fallback path too."""
    class FakeV2:
        def get_task_comments(self, project_id, task_id):
            return [{"id": "c1", "title": "X", "userName": "Гость"}]
    monkeypatch.setattr(s, "ticktick_v2", FakeV2())
    import asyncio
    out = asyncio.run(s.get_task_comments("Задача", "p1", "t1"))
    assert "Гость" in out
    assert out.count("?") == 0
