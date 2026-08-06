"""Регрессионные тесты на баг ДОСТАВКИ при создании задач:
execute_task_creation с ЯВНО указанным project_id иногда клал задачу в
Inbox вместо запрошенного проекта. Официальный API создания отвечает HTTP
200 с id задачи, даже когда переданный project_id не резолвится в живой
проект (или когда бэкенд TickTick сам перекладывает задачу в другое место)
— поэтому старый код выглядел как чистый «✓ создано» без единого
предупреждения.

Настоящая причина (см. server.py, _create_tasks_impl / plan_task_creation):
  1. Guard назначения на стороне создания (_guard_project) вызывался только
     С project_name для сверки — когда вызывающий (обычный случай) передавал
     только project_id, guard'у было нечего сверять, и ЛЮБОЙ project_id
     проходил прямиком к мутирующему API-вызову, резолвится он или нет
     (дыра fail-OPEN).
  2. Собственная проверка «проект не найден» в plan_task_creation была
     закрыта условием `if names and pname is None` — когда сама карта
     имён проектов приходила пустой (v2 недоступен И v1-фолбэк тоже не
     сработал), проверка целиком пропускалась, и непроверяемый project_id
     проходил план без единого возражения.

Правка делает обе точки FAIL-CLOSED: _guard_project теперь вызывается с
require_known=True в _create_tasks_impl (отказ всегда, когда project_id не
резолвится в живой проект, вне зависимости от project_name), а
plan_task_creation отказывает всегда, когда id не резолвится — включая
случай, когда сама сверка недоступна.

Этот файл покрывает, по пунктам из задания:
  (а) явный project_id доживает от plan_task_creation до execute_task_creation
      и доходит до реального API-вызова без изменений;
  (б) нерезолвящийся project_id приводит к отказу — и на уровне
      _create_tasks_impl, и на уровне плана, включая случай, когда сам
      список проектов прочитать не удалось — никогда не тихий Inbox;
  (в) post-verify (пере-проверка после мутации) ловит расхождение, когда
      внешний сервис положил задачу не туда, куда просили (имитация
      реального поведения TickTick — молчаливого переноса в Inbox).
"""
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(text: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', text)
    assert m, f"no manifest_id found: {text!r}"
    return m.group(1)


class _FakeV2:
    """Минимальный клиент v2: список проектов (для _v2_project_names) +
    состояние открытых задач (для fresh-чтений _open_by_id в post-verify),
    оба поверх ОДНОГО общего изменяемого словаря, в который пишет фейковый
    официальный клиент при создании."""

    def __init__(self, projects, open_tasks, inbox_id="inbox1"):
        self.projects = projects
        self.open_tasks = open_tasks
        self.inbox_id = inbox_id
        self.invalidate_calls = 0

    def invalidate_cache(self):
        self.invalidate_calls += 1

    def get_state(self, force=False):
        return {"projectProfiles": self.projects, "inboxId": self.inbox_id}

    def get_open_tasks(self):
        return list(self.open_tasks.values())

    def batch_create_tasks(self, tasks):
        return {}

    def _request(self, method, path, json=None):
        return {}

    def set_task_column(self, task_id, column_id):
        pass


class _FakeOfficial:
    """Фейк create_task официального API. `misfile_to`, если задан,
    имитирует поведение самого бэкенда TickTick — молчаливую перекладку
    задачи в ДРУГОЙ projectId (например Inbox), не совпадающий с тем, что
    был отправлен, при этом с обычным ответом 200/id — именно то поведение,
    из-за которого исходный баг выглядел успехом."""

    def __init__(self, open_tasks, misfile_to=None):
        self.open_tasks = open_tasks
        self.misfile_to = misfile_to
        self.create_calls = []
        self._n = 0

    def create_task(self, title, project_id, content=None, start_date=None,
                    due_date=None, priority=0, is_all_day=False,
                    repeat_flag=None, reminders=None):
        self._n += 1
        tid = f"newtask{self._n}"
        self.create_calls.append({"title": title, "project_id": project_id})
        real_pid = self.misfile_to or project_id
        self.open_tasks[tid] = {"id": tid, "title": title, "projectId": real_pid}
        return {"id": tid}


def _wire(monkeypatch, projects, open_tasks=None, misfile_to=None):
    open_tasks = {} if open_tasks is None else open_tasks
    fake_v2 = _FakeV2(projects, open_tasks)
    fake_official = _FakeOfficial(open_tasks, misfile_to=misfile_to)
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    monkeypatch.setattr(s, "ticktick", fake_official)
    return fake_v2, fake_official


# ===========================================================================
# (а) явный project_id доживает от плана до execute и до реального вызова
# ===========================================================================

async def test_explicit_project_id_survives_plan_to_execute(monkeypatch):
    projects = [{"id": "pA", "name": "Работа"}, {"id": "pB", "name": "Личное"}]
    _wire(monkeypatch, projects)

    preview = await s.plan_task_creation(
        "тест", [{"title": "Позвонить маме", "project_id": "pB"}])
    mid = _extract_manifest_id(preview)
    assert "**Личное**" in preview

    result = await s.execute_task_creation(mid, user_reply="да, создавай")

    fake_official = s.ticktick
    assert fake_official.create_calls == [
        {"title": "Позвонить маме", "project_id": "pB"}]
    assert "🛑" not in result
    assert "Создано 1" in result


async def test_explicit_project_id_survives_direct_create_impl_call(monkeypatch):
    """Та же гарантия для движка нижнего уровня напрямую (покрывает и путь
    create_tasks с automation_key для headless-вызовов — он вызывает этот же
    движок без изменений)."""
    projects = [{"id": "pA", "name": "Работа"}]
    _wire(monkeypatch, projects)

    result = await s._create_tasks_impl(
        "тест", [{"title": "Оплатить счёт", "project_id": "pA"}])

    fake_official = s.ticktick
    assert fake_official.create_calls == [
        {"title": "Оплатить счёт", "project_id": "pA"}]
    assert "🛑" not in result


# ===========================================================================
# (б) нерезолвящийся project_id — отказ, никогда тихий Inbox
# ===========================================================================

async def test_unresolvable_project_id_refuses_before_any_api_call(monkeypatch):
    """Движок создания должен отказать ДО вызова мутирующего API — это и
    есть тот самый недостающий fail-closed guard (require_known=True)."""
    projects = [{"id": "pA", "name": "Работа"}]
    _wire(monkeypatch, projects)

    result = await s._create_tasks_impl(
        "тест", [{"title": "Задача-призрак", "project_id": "ghost-id-999"}])

    fake_official = s.ticktick
    assert fake_official.create_calls == []  # до API дело не дошло
    assert "🛑" in result
    assert "Задача-призрак" in result
    assert "Создано" not in result  # никакой фантомной строки успеха


async def test_unresolvable_project_id_refused_at_plan_stage(monkeypatch):
    """Fail-closed на слой раньше: сам plan_task_creation не должен молча
    класть в манифест id, который не может резолвить."""
    projects = [{"id": "pA", "name": "Работа"}]
    _wire(monkeypatch, projects)

    preview = await s.plan_task_creation(
        "тест", [{"title": "Задача-призрак", "project_id": "ghost-id-999"}])

    assert "План создания — 0" in preview
    assert "Исключены 1" in preview
    assert "Задача-призрак" in preview


async def test_names_lookup_unavailable_fails_closed_not_silent(monkeypatch):
    """Конкретно та регрессия: когда сама сверка имён проектов недоступна
    (v2 не отвечает И v1-фолбэк тоже падает), СТАРОЕ условие
    `if names and pname is None` пропускало проверку целиком на пустой
    карте, пропуская непроверяемый project_id прямиком через план. Теперь
    должен быть отказ."""
    class _BrokenV2:
        def invalidate_cache(self):
            pass

        def get_state(self, force=False):
            raise RuntimeError("v2 unreachable")

        def get_open_tasks(self):
            raise RuntimeError("v2 unreachable")

    class _BrokenOfficial:
        def get_projects(self):
            raise RuntimeError("v1 fallback also failed")

    monkeypatch.setattr(s, "ticktick_v2", _BrokenV2())
    monkeypatch.setattr(s, "ticktick", _BrokenOfficial())

    preview = await s.plan_task_creation(
        "тест", [{"title": "Слепая задача", "project_id": "any-id"}])

    assert "План создания — 0" in preview
    assert "🛑" in preview
    assert "недоступен" in preview


# ===========================================================================
# (в) post-verify ловит расхождение доставки (задача попала не туда)
# ===========================================================================

async def test_post_verify_flags_delivery_mismatch_misfiled_to_inbox(monkeypatch):
    """project_id резолвится нормально (guard'у не к чему придраться — id
    реально указывает на живой проект), но внешний сервис всё равно кладёт
    задачу в ДРУГОЙ проект (Inbox) — именно тот сценарий «выглядит как
    успех» из отчёта QA-кампании. Post-verify обязан это показать, а не
    спрятать за чистым «✓ создано»."""
    projects = [{"id": "pWork", "name": "Работа"}]
    _wire(monkeypatch, projects, misfile_to="inbox1")

    result = await s._create_tasks_impl("тест", [
        {"title": "Купить билеты", "project_id": "pWork",
         "project_name": "Работа"}])

    fake_official = s.ticktick
    assert fake_official.create_calls  # guard пропустил, вызов API прошёл
    assert "Проверка назначения" in result
    assert "попала в" in result
    assert "Работа" in result
    assert "Inbox" in result


async def test_post_verify_is_silent_when_delivery_matches(monkeypatch):
    """Контроль: без подмены — без предупреждения о расхождении, создание
    остаётся чистым успехом (страхует от перекошенной проверки, которая бы
    ругалась на всё подряд)."""
    projects = [{"id": "pWork", "name": "Работа"}]
    _wire(monkeypatch, projects)  # misfile_to=None -> попадает куда просили

    result = await s._create_tasks_impl("тест", [
        {"title": "Купить билеты", "project_id": "pWork",
         "project_name": "Работа"}])

    assert "Проверка назначения" not in result
    assert "Создано 1" in result
