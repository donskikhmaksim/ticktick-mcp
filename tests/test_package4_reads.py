"""Package 4 (docs/PLAN_retrofit.md, ФАЗА B) — search/tags/inbox/completed/
filters/trash/statistics reads:
search_tasks, get_recurring_tasks, get_completed_tasks, list_tags,
get_tasks_by_tag, get_inbox_tasks, list_filters, run_filter,
list_project_groups, get_statistics, get_trash.

Covers:
4.1 get_completed_tasks: explicit "hit the 100 cap" note instead of silent
    truncation, plus the docstring change.
4.3 formal caps on list_tags / list_filters / list_project_groups, with
    explicit truncation wording (never silent) when the cap is exceeded.
4.4 Russian text for every response (success/empty/error) of the eleven
    methods this package owns.

search_tasks/get_recurring_tasks's own Russian-canon assertions live in
tests/test_slice3_dates_search.py (they were already covered there before
this package existed); this file focuses on the other nine methods plus the
get_completed_tasks clamp behavior.
"""
import ticktick_mcp.src.server as s
from ticktick_mcp.src.ticktick_v2_client import COMPLETED_MAX_LIMIT


class FakeV2:
    """Minimal v2 stand-in; each method returns whatever the test wired up."""

    def __init__(self, **kwargs):
        self._data = kwargs

    def get_completed_tasks(self, limit=50):
        return self._data.get("completed", [])[:limit]

    def get_tags(self):
        return self._data.get("tags", [])

    def get_tasks_by_tag(self, tag):
        return self._data.get("by_tag", [])

    def get_inbox_tasks(self):
        return self._data.get("inbox", [])

    def get_filters(self):
        return self._data.get("filters", [])

    def run_filter(self, filter_id_or_name):
        return self._data.get("filter_run", [])

    def list_project_groups(self):
        return self._data.get("groups", [])

    def get_statistics(self):
        return self._data.get("stats", {})

    def get_trash(self, limit=50):
        return self._data.get("trash", [])[:limit]


def _install(monkeypatch, **kwargs):
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(**kwargs))


class TestCompletedTasksCap:
    """4.1 — silent truncation is a defect; hitting the 100-cap must say so."""

    async def test_under_cap_no_cap_note(self, monkeypatch):
        _install(monkeypatch, completed=[{"id": "t1", "title": "Done"}])
        out = await s.get_completed_tasks(limit=10)
        assert "Завершённые задачи (1):" in out
        assert "потолок" not in out

    async def test_hits_cap_says_so_explicitly(self, monkeypatch):
        tasks = [{"id": f"t{i}", "title": f"Done {i}"} for i in range(COMPLETED_MAX_LIMIT)]
        _install(monkeypatch, completed=tasks)
        out = await s.get_completed_tasks(limit=500)
        assert f"показано {COMPLETED_MAX_LIMIT}" in out
        assert "потолок API TickTick" in out

    async def test_empty_is_russian(self, monkeypatch):
        _install(monkeypatch, completed=[])
        out = await s.get_completed_tasks()
        assert out == "Завершённых задач не найдено."

    def test_docstring_documents_the_100_cap(self):
        doc = s.get_completed_tasks.__doc__ or ""
        assert "100" in doc


class TestListTagsCap:
    """4.3 — formal cap with an explicit (never silent) truncation note."""

    async def test_under_cap(self, monkeypatch):
        _install(monkeypatch, tags=[{"label": "work"}, {"label": "home"}])
        out = await s.list_tags()
        assert "Теги (2):" in out
        assert "потолок" not in out

    async def test_over_cap_truncates_and_says_so(self, monkeypatch):
        tags = [{"label": f"tag{i}"} for i in range(s._LIST_TAGS_CAP + 5)]
        _install(monkeypatch, tags=tags)
        out = await s.list_tags()
        assert f"показано {s._LIST_TAGS_CAP} из {s._LIST_TAGS_CAP + 5}" in out
        assert "потолок сервера" in out
        assert out.count("- tag") == s._LIST_TAGS_CAP

    async def test_empty_is_russian(self, monkeypatch):
        _install(monkeypatch, tags=[])
        out = await s.list_tags()
        assert out == "Тегов не найдено."


class TestGetTasksByTagRussian:
    async def test_found(self, monkeypatch):
        _install(monkeypatch, by_tag=[{"id": "t1", "title": "Позвонить", "projectId": "p1"}])
        out = await s.get_tasks_by_tag("работа")
        assert "Задачи с тегом «работа» (1):" in out

    async def test_empty_is_russian(self, monkeypatch):
        _install(monkeypatch, by_tag=[])
        out = await s.get_tasks_by_tag("работа")
        assert out == "Открытых задач с тегом «работа» не найдено."


class TestInboxRussian:
    async def test_found(self, monkeypatch):
        _install(monkeypatch, inbox=[{"id": "t1", "title": "Купить молоко", "projectId": "inbox"}])
        out = await s.get_inbox_tasks()
        assert "Задачи во «Входящих» (1):" in out

    async def test_empty_is_russian(self, monkeypatch):
        _install(monkeypatch, inbox=[])
        out = await s.get_inbox_tasks()
        assert out == "Во «Входящих» нет открытых задач."


class TestListFiltersCap:
    async def test_under_cap(self, monkeypatch):
        _install(monkeypatch, filters=[{"name": "Today", "id": "f1", "rule": "due:today"}])
        out = await s.list_filters()
        assert "Фильтры (1):" in out
        assert "правило: due:today" in out
        assert "потолок" not in out

    async def test_over_cap_truncates_and_says_so(self, monkeypatch):
        filters = [{"name": f"F{i}", "id": str(i), "rule": ""} for i in range(s._LIST_FILTERS_CAP + 3)]
        _install(monkeypatch, filters=filters)
        out = await s.list_filters()
        assert f"показано {s._LIST_FILTERS_CAP} из {s._LIST_FILTERS_CAP + 3}" in out
        assert "потолок сервера" in out

    async def test_empty_is_russian(self, monkeypatch):
        _install(monkeypatch, filters=[])
        out = await s.list_filters()
        assert out == "Фильтров не найдено."


class TestRunFilterRussian:
    async def test_matches(self, monkeypatch):
        _install(monkeypatch, filter_run=[{"id": "t1", "title": "X", "projectId": "p1"}])
        out = await s.run_filter("Today")
        assert "Фильтр «Today» — 1 задач(и):" in out

    async def test_no_match_is_russian(self, monkeypatch):
        _install(monkeypatch, filter_run=[])
        out = await s.run_filter("Today")
        assert out == "Фильтр «Today» не нашёл открытых задач."


class TestListProjectGroupsCap:
    async def test_under_cap_skips_deleted(self, monkeypatch):
        groups = [{"name": "Work", "id": "g1"}, {"name": "Gone", "id": "g2", "deleted": 1}]
        _install(monkeypatch, groups=groups)
        out = await s.list_project_groups()
        assert "Группы проектов (1):" in out
        assert "Work" in out and "Gone" not in out

    async def test_over_cap_truncates_and_says_so(self, monkeypatch):
        groups = [{"name": f"G{i}", "id": str(i)} for i in range(s._LIST_PROJECT_GROUPS_CAP + 7)]
        _install(monkeypatch, groups=groups)
        out = await s.list_project_groups()
        assert (f"показано {s._LIST_PROJECT_GROUPS_CAP} из "
                f"{s._LIST_PROJECT_GROUPS_CAP + 7}") in out
        assert "потолок сервера" in out

    async def test_empty_is_russian(self, monkeypatch):
        _install(monkeypatch, groups=[])
        out = await s.list_project_groups()
        assert out == "Групп проектов не найдено."


class TestStatisticsRussian:
    async def test_present(self, monkeypatch):
        _install(monkeypatch, stats={"score": 100, "level": 3, "todayCompleted": 2,
                                      "yesterdayCompleted": 5, "totalCompleted": 999})
        out = await s.get_statistics()
        assert "Очки достижений: 100" in out
        assert "Уровень: 3" in out
        assert "Завершено сегодня: 2" in out
        assert "всего: 999" in out

    async def test_empty_is_russian(self, monkeypatch):
        _install(monkeypatch, stats={})
        out = await s.get_statistics()
        assert out == "Статистика недоступна."


class TestTrashRussian:
    async def test_present(self, monkeypatch):
        _install(monkeypatch, trash=[{"id": "t1", "title": "Old task"}])
        out = await s.get_trash()
        assert "Задачи в корзине (1):" in out

    async def test_empty_is_russian(self, monkeypatch):
        _install(monkeypatch, trash=[])
        out = await s.get_trash()
        assert out == "Корзина пуста."
