"""get_all_tasks / get_project_tasks: вывод режется честно и дочитывается (def-E1).

Корень: у обоих методов не было ни limit, ни offset — они печатали ВСЁ.
На боевом аккаунте владельца (1481 задача, 35 проектов) обычный вызов без
параметров возвращал 164 871 символ у get_all_tasks и 153 436 символов у
get_project_tasks на ОДИН проект из 310 задач: оба ответа не влезали в лимит
и возвращали ошибку вместо данных, то есть инструментами не являлись.

Проверяется ровно контракт постраничной выдачи, принятый у соседей
(get_tasks_by_priority, get_changes, get_completed_tasks):
  * страница по умолчанию влезает;
  * заголовок называет ОБЩЕЕ число и показанный диапазон;
  * футер говорит, каким offset дочитать хвост;
  * offset за концом отвечает явно, а не «задач нет»;
  * подзадача не отрывается от родителя на границе страницы;
  * маленький вывод остаётся ПОБАЙТОВО прежним.

Эталоны маленького вывода собираются из тех же форматтеров (format_task /
format_task_tree), а не записаны литералом: тест обязан ловить изменение
СТРУКТУРЫ вывода (заголовок, нумерация, порядок, лишний футер), но не
краснеть от чужих правок в печати самой строки задачи.

Календарных констант здесь нет намеренно: ни один из двух методов не считает
окно от часов — они читают текущий список задач, поэтому и фикстуры от даты
не зависят (правило репозитория про протухающие фикстуры).
"""
import pytest

import ticktick_mcp.src.server as s


class FakeV2:
    """Пул открытых задач v2 + состояние с именами проектов."""

    def __init__(self, tasks):
        self._tasks = tasks

    def get_open_tasks(self):
        return self._tasks

    def get_state(self, force=False):
        return {"projectProfiles": [{"id": "p1", "name": "Проект"}],
                "inboxId": "inbox1"}


class FakeOfficial:
    """Официальный клиент v1 — единственное, что трогает get_project_tasks."""

    def __init__(self, tasks):
        self._tasks = tasks

    def get_project_with_data(self, project_id):
        return {"project": {"id": "p1", "name": "Проект"}, "tasks": self._tasks}


def _tasks(n, first=0):
    return [{"id": f"t{i}", "title": f"Задача {i}", "projectId": "p1",
             "priority": 0, "status": 0}
            for i in range(first, first + n)]


def _lines(out):
    """Строки задач компактного вывода (format_task_line начинает их с «- »)."""
    return [ln for ln in out.splitlines() if ln.lstrip().startswith(("- ", "↳ "))
            or ln.lstrip().startswith("↳")]


# ═════════════════════════ get_all_tasks ═════════════════════════

@pytest.fixture
def big_v2(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficial([]))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(_tasks(1000)))


async def test_all_tasks_default_page_is_capped_and_says_how_to_continue(big_v2):
    out = await s.get_all_tasks()

    assert len(_lines(out)) <= 200, (
        f"страница не ограничена: {len(_lines(out))} строк, {len(out)} символов")
    assert "1000" in out, "общее число задач не названо"
    assert "offset=200" in out, "не сказано, каким offset дочитать хвост"


async def test_all_tasks_limit_narrows_the_page(big_v2):
    out = await s.get_all_tasks(limit=10)

    assert len(_lines(out)) == 10
    assert "(id:t9 " in out
    assert "(id:t10 " not in out


async def test_all_tasks_offset_reaches_the_tail(big_v2):
    out = await s.get_all_tasks(limit=10, offset=990)

    assert "(id:t990 " in out and "(id:t999 " in out
    assert "(id:t989 " not in out, "вторая страница повторяет первую"


async def test_all_tasks_offset_past_the_end_is_reported_not_pretended_empty(big_v2):
    out = await s.get_all_tasks(offset=5000)

    assert "No tasks found." not in out, "пустая страница выдана за «задач нет»"
    assert "1000" in out
    assert "offset" in out.lower()


async def test_page_slice_keeps_subtasks_with_their_parent():
    """Срез считается по КОРНЯМ, и поддерево уезжает на страницу целиком.

    Плоский срез оторвал бы ребёнка от родителя ровно на границе страницы, а
    format_task_tree печатает такого сироту на ВЕРХНЕМ уровне (без «↳») — то
    есть подзадача молча превращалась бы в самостоятельную задачу.
    """
    forest = [
        {"id": "r1", "title": "Корень 1", "projectId": "p1"},
        {"id": "r2", "title": "Корень 2", "projectId": "p1"},
        {"id": "c2a", "title": "Ребёнок 2a", "projectId": "p1", "parentId": "r2"},
        {"id": "c2b", "title": "Ребёнок 2b", "projectId": "p1", "parentId": "r2"},
        {"id": "r3", "title": "Корень 3", "projectId": "p1"},
    ]

    page, total_roots, off, shown_to = s._page_task_forest(forest, limit=2, offset=0)

    assert total_roots == 3, "лимит обязан считаться в корнях, а не в строках"
    assert [t["id"] for t in page] == ["r1", "r2", "c2a", "c2b"], (
        "дети не уехали вместе со своим корнем")
    assert (off, shown_to) == (0, 2)

    tail, _, _, tail_to = s._page_task_forest(forest, limit=2, offset=2)
    assert [t["id"] for t in tail] == ["r3"], "хвост не совпадает с остатком корней"
    assert tail_to == 3


async def test_page_size_is_bounded_by_tasks_not_by_root_count():
    """Куст подзадач не имеет права раздуть страницу.

    Если бы limit считался в КОРНЯХ, то 20 корней по 10 задач в каждом дали бы
    200 строк вместо 20 — то есть объём вывода (ровно то, из-за чего метод и
    падал) снова стал бы неограниченным. Потолок мягкий: поддерево, которое
    его пересекло, дописывается целиком, а не рвётся пополам.
    """
    forest = []
    for r in range(50):
        forest.append({"id": f"r{r}", "title": f"Корень {r}", "projectId": "p1"})
        forest += [{"id": f"r{r}c{k}", "title": f"Ребёнок {r}.{k}",
                    "projectId": "p1", "parentId": f"r{r}"} for k in range(9)]

    page, total_roots, _, shown_to = s._page_task_forest(forest, limit=20, offset=0)

    assert total_roots == 50
    assert len(page) <= 20 + 9, f"страница разбухла до {len(page)} задач вместо ~20"
    assert shown_to == 2, "взято не по задачам, а по корням"
    # Ни одно поддерево не разрезано: у каждого корня страницы все 9 детей.
    ids = {t["id"] for t in page}
    for r in range(shown_to):
        assert all(f"r{r}c{k}" in ids for k in range(9)), f"поддерево r{r} разрезано"


async def test_all_tasks_counts_the_page_in_top_level_tasks(monkeypatch):
    """Ребёнок не съедает место корня: 3 корня + 2 ребёнка при limit=2 —
    это «показаны 1-2 из 3», а не «показаны 1-2 из 5»."""
    forest = [
        {"id": "r1", "title": "Корень 1", "projectId": "p1"},
        {"id": "r2", "title": "Корень 2", "projectId": "p1"},
        {"id": "c2a", "title": "Ребёнок 2a", "projectId": "p1", "parentId": "r2"},
        {"id": "c2b", "title": "Ребёнок 2b", "projectId": "p1", "parentId": "r2"},
        {"id": "r3", "title": "Корень 3", "projectId": "p1"},
    ]
    monkeypatch.setattr(s, "ticktick", FakeOfficial([]))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(forest))

    out = await s.get_all_tasks(limit=2)

    assert "offset=2" in out, f"хвост недостижим: {out!r}"
    assert "(id:r3 " not in out, "третий корень попал на первую страницу"


async def test_all_tasks_small_output_is_unchanged_byte_for_byte(monkeypatch):
    """Защита от «починили большое — сломали обычное»: пока всё влезает,
    вывод обязан совпадать с прежним до последнего байта (ни «showing», ни
    футера, ни изменившейся нумерации)."""
    tasks = _tasks(3)
    monkeypatch.setattr(s, "ticktick", FakeOfficial([]))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))

    out = await s.get_all_tasks()

    expected = ("All open tasks (3):\n\n"
                "── Проект (3 tasks) ──\n"
                + s.format_task_tree(tasks, 500) + "\n")
    assert out == expected, f"маленький вывод изменился:\n{out!r}\n!=\n{expected!r}"


# ═════════════════════════ get_project_tasks ═════════════════════════

@pytest.fixture
def big_project(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficial(_tasks(300)))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))


async def test_project_tasks_default_page_is_capped_and_says_how_to_continue(big_project):
    out = await s.get_project_tasks("p1")

    cards = [ln for ln in out.splitlines() if ln.startswith("Task ")]
    assert len(cards) <= 50, (
        f"страница не ограничена: {len(cards)} карточек, {len(out)} символов")
    assert "300" in out, "общее число задач не названо"
    assert "offset=50" in out, "не сказано, каким offset дочитать хвост"


async def test_project_tasks_limit_narrows_the_page(big_project):
    out = await s.get_project_tasks("p1", limit=5)

    cards = [ln for ln in out.splitlines() if ln.startswith("Task ")]
    assert len(cards) == 5
    assert "(id: t4 |" in out
    assert "(id: t5 |" not in out


async def test_project_tasks_offset_reaches_the_tail_with_running_numbers(big_project):
    out = await s.get_project_tasks("p1", limit=5, offset=295)

    assert "(id: t295 |" in out and "(id: t299 |" in out
    assert "(id: t294 |" not in out, "вторая страница повторяет первую"
    # Сквозная нумерация: без неё хвост выглядит как первая страница.
    assert "Task 296:" in out, f"нумерация не продолжается: {out[:200]!r}"


async def test_project_tasks_offset_past_the_end_is_reported_not_pretended_empty(big_project):
    out = await s.get_project_tasks("p1", offset=5000)

    assert "No tasks found" not in out, "пустая страница выдана за «задач нет»"
    assert "300" in out
    assert "offset" in out.lower()


async def test_project_tasks_small_project_output_is_unchanged_byte_for_byte(monkeypatch):
    tasks = _tasks(3)
    monkeypatch.setattr(s, "ticktick", FakeOfficial(tasks))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))

    out = await s.get_project_tasks("p1")

    expected = "Found 3 tasks in project 'Проект':\n\n"
    for i, task in enumerate(tasks, 1):
        expected += f"Task {i}:\n" + s.format_task(task) + "\n"
    assert out == expected, f"маленький вывод изменился:\n{out!r}\n!=\n{expected!r}"


async def test_project_tasks_empty_project_still_answers_plainly(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficial([]))
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))

    out = await s.get_project_tasks("p1")

    assert out == "No tasks found in project 'Проект'."
