"""ЗАКРЫТИЕ ПРЯМЫХ ПУТЕЙ (2026-08-10, docs/TZ/ZAHOD1.md §1.3.4).

Два слоя, оба обязательны и оба проверяются здесь:

  Слой 1 — ЗАПРЕТ. Каждый закрытый инструмент без ключа автоматики возвращает
  обучающий отказ и НИЧЕГО не делает. Проверяется чтением живого состояния,
  а не текста ответа: текст можно сочинить, живое состояние — нет.

  Слой 2 — СОКРЫТИЕ. Закрытые имена исчезают из `tools/list`, но остаются в
  реестре сервера и вызываемыми по имени. Модель видит один правильный путь —
  агрегатор `apply_task_changes`; автоматика и фоновое исполнение по кнопке
  работают как работали.

Порядок между слоями не косметический: между «спрятали» и «поставили отказ»
метод невидим, но полностью открыт любому, кто знает имя. Поэтому отказ
ставился РАНЬШЕ сокрытия, а этот файл проверяет оба состояния сразу.
"""
import asyncio

import pytest

import ticktick_mcp.src.server as s


# Тринадцать закрываемых имён. Порядок — как в ТЗ (порядок появления в
# server.py). Тип операции агрегатора, заменяющий каждое, объявлен в коде
# (`s._HIDDEN_TOOL_REPLACEMENT`) и проверяется отдельным файлом
# tests/test_hidden_tools_coverage.py — здесь список нужен только как предмет
# проверок листинга и реестра.
HIDDEN_THIRTEEN = (
    "create_tasks", "update_tasks", "complete_tasks", "delete_tasks",
    "delete_task_with_subtasks", "create_subtask", "move_tasks",
    "set_task_parent", "unset_task_parent", "set_task_tags", "restore_tasks",
    "abandon_task", "duplicate_task",
)

# Открытые инструменты, которые НИКТО не должен спрятать заодно: удаление
# поддерева (агрегатор его намеренно не разворачивает), честный отчёт по
# идентификатору, чтение тегов, теговые мутации и создание проектов.
STAY_LISTED = ("plan_task_deletion", "execute_task_deletion", "operation_report",
               "list_tags", "delete_tag", "delete_tags", "create_project")


def _listed() -> set:
    return {t.name for t in asyncio.run(s.mcp.list_tools())}


def _registry() -> dict:
    return s.mcp._tool_manager._tools


# ───────────────────────── слой 2: сокрытие ─────────────────────────

def test_listing_excludes_hidden():
    """Ни одного из тринадцати закрытых имён и ни `manual_triage` в листинге
    нет; `apply_task_changes` — есть."""
    names = _listed()
    still_visible = sorted(n for n in HIDDEN_THIRTEEN + ("manual_triage",)
                           if n in names)
    assert not still_visible, (
        f"в tools/list всё ещё видны закрытые имена: {still_visible} — "
        "модель выберет между «как надо» и «как быстрее» сама")
    assert "apply_task_changes" in names, (
        "единственный правильный путь изменения задач обязан быть виден")


def test_registry_still_has_hidden():
    """Сокрытие — это фильтр листинга, а НЕ удаление из реестра.

    Если кто-то заменит фильтрацию на удаление или на комментирование
    `@mcp.tool()`, `get_tool` перестанет находить инструмент, и фоновое
    исполнение по кнопке начнёт отказывать для всех закрытых операций
    (`_tool_registration_status` спрашивает именно реестр) — дыра П6."""
    manager = s.mcp._tool_manager
    for name in HIDDEN_THIRTEEN + ("manual_triage",):
        assert manager.get_tool(name) is not None, (
            f"{name} пропал из реестра FastMCP — кнопка Telegram для его "
            "планов начнёт отвечать «инструмент отсутствует»")


def test_open_tools_stay_listed():
    """Никто не спрятал лишнего заодно."""
    names = _listed()
    missing = sorted(n for n in STAY_LISTED if n not in names)
    assert not missing, (
        f"из листинга пропали инструменты, которые обязаны остаться: {missing}")


def test_hidden_set_is_an_env_var_rollback_point(monkeypatch):
    """Точка отката одна и она — переменная окружения, а не правка кода.

    Пустая (именно заданная пустой, не отсутствующая) `MCP_HIDDEN_TOOLS`
    возвращает модели прежний набор команд целиком."""
    monkeypatch.setenv(s._HIDDEN_TOOLS_ENV, "")
    names = _listed()
    for n in HIDDEN_THIRTEEN + ("manual_triage",):
        assert n in names, f"{n} не вернулся в листинг при пустом откате"

    monkeypatch.setenv(s._HIDDEN_TOOLS_ENV, "update_tasks")
    names = _listed()
    assert "update_tasks" not in names
    assert "delete_tasks" in names, (
        "переменная окружения обязана задавать множество ЦЕЛИКОМ, а не "
        "дополнять зашитый в код список — иначе прочитать её и понять, что "
        "скрыто, невозможно")


def test_patching_fastmcp_list_tools_would_not_work(monkeypatch):
    """Почему фильтр стоит на `mcp._tool_manager.list_tools`, а не на
    `mcp.list_tools`.

    Обработчик запроса `tools/list` привязан к СВЯЗАННОМУ методу в момент
    создания сервера (`FastMCP._setup_handlers`), поэтому переприсваивание
    атрибута позже ни на что не влияет. Это утверждение из ТЗ проверяется
    здесь, а не принимается на веру: если однажды SDK начнёт читать атрибут
    заново, тест упадёт — и тогда точку вмешательства можно будет упростить."""
    from mcp.types import ListToolsRequest

    async def _liar():
        return []

    monkeypatch.setattr(s.mcp, "list_tools", _liar)
    handler = s.mcp._mcp_server.request_handlers[ListToolsRequest]
    result = asyncio.run(handler(ListToolsRequest(method="tools/list")))

    names = {t.name for t in result.root.tools}
    assert names, (
        "подмена mcp.list_tools подействовала — значит обработчик читает "
        "атрибут заново, и фильтр можно (и нужно) ставить туда")
    assert "apply_task_changes" in names


# ───────────────────── слой 1: запрет по ключу ─────────────────────

_CALL_ARGS = {
    "create_tasks": dict(summary="Создаю", tasks=[{"title": "Новая", "project_id": "p1"}]),
    "update_tasks": dict(summary="Меняю", tasks=[{"taskId": "t1", "projectId": "p1",
                                                  "new_title": "Другое"}]),
    "complete_tasks": dict(summary="Закрываю", tasks=[{"taskId": "t1", "title": "A",
                                                       "projectId": "p1"}]),
    "delete_tasks": dict(summary="Удаляю", tasks=[{"taskId": "t1", "title": "A",
                                                   "projectId": "p1"}]),
    "delete_task_with_subtasks": dict(summary="Удаляю дерево", task_id="t1",
                                      project_id="p1"),
    "create_subtask": dict(parent_task_title="A", subtask_title="Под", parent_task_id="t1",
                           project_id="p1"),
    "move_tasks": dict(summary="Переношу", tasks=[{"taskId": "t1", "title": "A"}],
                       to_project_id="p2", to_project_name="Проект-2"),
    "set_task_parent": dict(summary="Вкладываю", tasks=[{"taskId": "t2", "title": "B"}],
                            parent_task_id="t1", project_id="p1", parent_task_title="A"),
    "unset_task_parent": dict(task_title="B", parent_task_title="A", task_id="t2",
                              parent_task_id="t1", project_id="p1"),
    "set_task_tags": dict(summary="Перетегиваю",
                          tasks=[{"taskId": "t1", "title": "A", "tags": ["дом"]}]),
    "restore_tasks": dict(summary="Восстанавливаю", tasks=[{"taskId": "t1", "title": "A"}],
                          to_project_id="p1"),
    "abandon_task": dict(summary="Не буду", task_id="t1", task_title="A"),
    "duplicate_task": dict(summary="Дублирую", task_id="t1", task_title="A"),
}


class _TripWire:
    """Клиент TickTick, у которого любой пишущий метод — провал теста.

    Чтения отвечают одним и тем же живым состоянием, поэтому «живое состояние
    не изменилось» проверяется чтением ЖИВОГО СОСТОЯНИЯ, а не текста ответа:
    отказ можно написать в ответе и всё равно всё сделать."""

    def __init__(self):
        self.live = {"t1": {"id": "t1", "title": "A", "projectId": "p1"},
                     "t2": {"id": "t2", "title": "B", "projectId": "p1"}}

    # --- чтения ---
    def get_state(self, force=False):
        return {"syncTaskBean": {"update": list(self.live.values())}}

    def get_open_tasks(self):
        return list(self.live.values())

    def get_projects(self):
        return [{"id": "p1", "name": "Проект"}, {"id": "p2", "name": "Проект-2"}]

    def get_trash(self, limit=50):
        self.asked_trash_limit = limit
        return []

    def get_tags(self):
        return []

    def invalidate_cache(self):
        pass

    # --- любая запись = провал ---
    def __getattr__(self, name):
        def _boom(*a, **k):
            raise AssertionError(
                f"закрытый инструмент дошёл до записи в TickTick: {name}()")
        return _boom


@pytest.fixture
def tripwire(monkeypatch):
    wire = _TripWire()
    monkeypatch.setattr(s, "ticktick", wire)
    monkeypatch.setattr(s, "ticktick_v2", wire)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект", "p2": "Проект-2"})
    return wire


@pytest.mark.parametrize("tool", HIDDEN_THIRTEEN)
def test_call_without_key_refuses_and_changes_nothing(tool, tripwire):
    """Прогоняется ПО КАЖДОМУ из тринадцати имён, а не по одному образцу.

    Способ подделки, от которого это защищает: «спрятать, не закрыв» —
    множество скрытых заполнено, листинг чист, а проверка ключа забыта в одной
    функции (например в `restore_tasks`). Модель метод не видит и потому не
    зовёт, так что в чате всё выглядит правильно; дыра откроется, когда имя
    всплывёт из старого контекста или из документации."""
    before = {k: dict(v) for k, v in tripwire.live.items()}

    out = asyncio.run(s.mcp.call_tool(tool, dict(_CALL_ARGS[tool])))
    text = out[0][0].text if isinstance(out, tuple) else str(out)

    assert "apply_task_changes" in text, (
        f"{tool}: отказ обязан НАЗВАТЬ единственный правильный путь — иначе "
        "модель не узнает, куда идти, и попробует следующий прямой метод")
    assert tripwire.live == before, (
        f"{tool}: живое состояние изменилось, несмотря на отказ")


@pytest.mark.parametrize("tool", HIDDEN_THIRTEEN)
def test_hidden_tools_take_an_automation_key_argument(tool):
    """Ключ — единственный способ пройти запрет, поэтому у каждого закрытого
    инструмента он обязан быть в сигнатуре.

    Исключение ровно одно и оно осознанное: `delete_task_with_subtasks`
    отказывает ВСЕГДА и никого не пускает — ни человека, ни автоматику, — а
    перенаправляет на `plan_task_deletion`, который единственный умеет
    разворачивать поддерево."""
    import inspect

    params = set(inspect.signature(getattr(s, tool)).parameters)
    if tool == "delete_task_with_subtasks":
        assert "automation_key" not in params
        return
    assert "automation_key" in params


def test_call_with_key_executes(monkeypatch, tripwire):
    """Тот же вызов с ВАЛИДНЫМ ключом выполняется как раньше.

    Ключ снимает ровно один вопрос — «человек согласен?». Он не снимает ни
    сверку личности объекта, ни потолки, ни журнал, ни перепроверку: ниже
    подменён только сам мутатор, весь путь до него настоящий."""
    seen = {}

    async def _impl(summary, tasks, **extra):
        seen["called"] = (summary, tasks)
        return "✅ выполнено (заглушка мутатора)"

    monkeypatch.setattr(s, "_complete_tasks_impl", _impl)

    out = asyncio.run(s.complete_tasks(
        summary="Закрываю", tasks=[{"taskId": "t1", "title": "A", "projectId": "p1"}],
        automation_key="test-secret"))

    assert "выполнено" in out, out
    assert seen["called"][0] == "Закрываю"
    assert seen["called"][1] == [{"taskId": "t1", "title": "A", "projectId": "p1"}]


def test_wrong_key_is_refused_like_no_key(tripwire):
    """Неверный ключ обязан вести себя как отсутствующий — иначе «почти
    угаданный» ключ давал бы другой ответ, то есть подсказку."""
    before = {k: dict(v) for k, v in tripwire.live.items()}

    out = asyncio.run(s.update_tasks(
        summary="Меняю", tasks=[{"taskId": "t1", "projectId": "p1", "new_title": "X"}],
        automation_key="не-тот-ключ-🙂"))

    assert "apply_task_changes" in out
    assert tripwire.live == before
