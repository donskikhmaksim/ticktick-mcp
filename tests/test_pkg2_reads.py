"""Package 2 of the STANDARD.md retrofit (docs/PLAN_retrofit.md): format_*
helpers + get_projects/get_project/get_project_tasks/get_task/get_all_tasks.

Covers:
- 2.1 honest cap + "показано N из M" in get_project_tasks (no more silent
  unbounded dumps).
- 2.2 cap on get_projects.
- 2.3 format_task/format_project render in Russian; truncation notes in
  format_task_list/format_task_tree are Russian too.
- 2.4 the five tools' success/empty/error strings are Russian.

No real network — the official client is faked."""
import ticktick_mcp.src.server as s


class FakeOfficial:
    def __init__(self, projects=None, project=None, project_data=None, task=None):
        self._projects = projects
        self._project = project
        self._project_data = project_data
        self._task = task

    def get_projects(self):
        return self._projects

    def get_project(self, project_id):
        return self._project

    def get_project_with_data(self, project_id):
        return self._project_data

    def get_task(self, project_id, task_id):
        return self._task


def _wire(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "ticktick_v2", None)


# --- format_task / format_project: Russian labels -------------------------

def test_format_task_is_russian():
    task = {
        "id": "t1", "projectId": "p1", "title": "Позвонить в банк",
        "priority": 5, "status": 0, "dueDate": "2026-08-01T00:00:00+0000",
    }
    out = s.format_task(task)
    assert "Заголовок:" in out
    assert "Срок:" in out
    assert "Приоритет: Высокий" in out
    assert "Статус: Активна" in out
    assert "Title:" not in out
    assert "Priority:" not in out


def test_format_project_is_russian():
    project = {"id": "p1", "name": "Работа", "closed": False}
    out = s.format_project(project)
    assert "Название: Работа" in out
    assert "Закрыт: Нет" in out
    assert "Name:" not in out


def test_format_task_tree_truncation_note_is_russian():
    tasks = [{"id": f"t{i}", "title": f"t{i}", "projectId": "p1"} for i in range(10)]
    out = s.format_task_list(tasks, limit=5)
    assert "и ещё 5" in out
    assert "more" not in out.lower()


# --- get_projects: 2.2 cap + Russian --------------------------------------

async def test_get_projects_empty_is_russian(monkeypatch):
    _wire(monkeypatch, FakeOfficial(projects=[]))
    out = await s.get_projects()
    assert out == "Проекты не найдены."


async def test_get_projects_lists_in_russian(monkeypatch):
    projects = [{"id": "p1", "name": "Работа"}, {"id": "p2", "name": "Дом"}]
    _wire(monkeypatch, FakeOfficial(projects=projects))
    out = await s.get_projects()
    assert "Найдено проектов: 2" in out
    assert "Проект 1:" in out
    assert "Работа" in out and "Дом" in out


async def test_get_projects_honest_cap(monkeypatch):
    projects = [{"id": f"p{i}", "name": f"Проект {i}"} for i in range(s._PROJECTS_LIST_CAP + 10)]
    _wire(monkeypatch, FakeOfficial(projects=projects))
    out = await s.get_projects()
    total = s._PROJECTS_LIST_CAP + 10
    assert f"Найдено проектов: {total}" in out
    assert f"показано {s._PROJECTS_LIST_CAP} из {total}" in out
    assert out.count(":\nНазвание:") == s._PROJECTS_LIST_CAP
    assert f"Проект {s._PROJECTS_LIST_CAP + 1}:" not in out


# --- get_project_tasks: 2.1 cap + Russian ----------------------------------

async def test_get_project_tasks_empty_is_russian(monkeypatch):
    _wire(monkeypatch, FakeOfficial(project_data={"project": {"name": "Работа"}, "tasks": []}))
    out = await s.get_project_tasks("p1")
    assert out == "В проекте «Работа» задач не найдено."


async def test_get_project_tasks_honest_cap(monkeypatch):
    total = s._PROJECT_TASKS_CAP + 25
    tasks = [{"id": f"t{i}", "title": f"t{i}", "projectId": "p1", "priority": 0} for i in range(total)]
    _wire(monkeypatch, FakeOfficial(
        project_data={"project": {"name": "Работа"}, "tasks": tasks}
    ))
    out = await s.get_project_tasks("p1")
    assert f"Найдено задач в проекте «Работа»: {total}" in out
    assert f"показано {s._PROJECT_TASKS_CAP} из {total}" in out
    assert out.count("Задача ") == s._PROJECT_TASKS_CAP


async def test_get_project_tasks_no_cap_note_when_under_limit(monkeypatch):
    tasks = [{"id": "t1", "title": "t1", "projectId": "p1", "priority": 0}]
    _wire(monkeypatch, FakeOfficial(
        project_data={"project": {"name": "Работа"}, "tasks": tasks}
    ))
    out = await s.get_project_tasks("p1")
    assert "показано" not in out


# --- get_project / get_task: Russian error strings -------------------------

async def test_get_project_error_is_russian(monkeypatch):
    _wire(monkeypatch, FakeOfficial(project={"error": "boom"}))
    out = await s.get_project("p1")
    assert out == "Ошибка получения проекта: boom"


async def test_get_task_error_is_russian(monkeypatch):
    _wire(monkeypatch, FakeOfficial(task={"error": "boom"}))
    out = await s.get_task("p1", "t1")
    assert out == "Ошибка получения задачи: boom"


async def test_get_task_success_is_russian(monkeypatch):
    _wire(monkeypatch, FakeOfficial(task={
        "id": "t1", "projectId": "p1", "title": "Купить билеты", "priority": 0, "status": 0,
    }))
    out = await s.get_task("p1", "t1")
    assert "Заголовок: Купить билеты" in out
