"""run_filter / get_inbox_tasks: вывод режется честно и дочитывается (def-E1, 2-я волна).

Корень тот же, что у уже починенных get_all_tasks / get_project_tasks, только
проявился он не ошибкой «ответ не влезает», а МОЛЧАЛИВОЙ ПОТЕРЕЙ ХВОСТА.
Оба метода печатали `format_task_tree(tasks)` с потолком по умолчанию (200
строк) и не имели ни `limit`, ни `offset`:

  * run_filter на живом фильтре из 1436 совпадений печатал 200 задач и
    приписывал «... and 1236 more.» — то есть сам сообщал, что 1236 задач
    отброшено, и не давал НИ ОДНОГО способа их достать: остальные 86 %
    результата были недостижимы этим инструментом в принципе;
  * get_inbox_tasks на 344 задачах Входящих терял 144, при этом
    get_project_tasks для Inbox отсылал именно к нему («limit/offset not
    applied: the Inbox is served by get_inbox_tasks, which pages on its own») —
    отсылка к методу, у которого параметров не было вовсе.

Контракт проверяется тот же, что у соседей (get_all_tasks,
get_tasks_by_priority, get_changes, get_completed_tasks):
  * страница по умолчанию ограничена;
  * заголовок называет ОБЩЕЕ число и показанный диапазон;
  * футер называет ТОЧНЫЙ offset, которым дочитывается хвост;
  * offset за концом отвечает явно, а не «задач нет» / «фильтр ничего не нашёл»;
  * подзадача не отрывается от родителя на границе страницы;
  * маленький вывод остаётся ПОБАЙТОВО прежним;
  * предупреждение о невычислимом условии фильтра не теряется на страницах.

Эталоны маленького вывода собираются из того же форматтера
(format_task_tree), а не записаны литералом: тест обязан ловить изменение
СТРУКТУРЫ вывода (заголовок, футер, порядок), но не краснеть от чужих правок
в печати самой строки задачи.

Календарных констант здесь нет намеренно: ни один из двух методов не считает
окно от часов — они читают текущий список задач, поэтому и фикстуры от даты
не зависят (правило репозитория про протухающие фикстуры).
"""
import pytest

import ticktick_mcp.src.server as s

INBOX_ID = "inbox115781412"


class FakeV2:
    """Стенд-ин под TickTickV2Client: пул задач + sync-состояние.

    Методы названы и объявлены как у настоящего клиента (get_state,
    get_inbox_tasks, run_filter_detailed) — двойник не добрее живого: Входящие
    отбираются тем же фильтром projectId == inboxId, а run_filter_detailed
    возвращает ту же пару (задачи, невычислимые условия).
    """

    def __init__(self, tasks, unsupported=None):
        self._tasks = tasks
        self._unsupported = unsupported or []

    def get_state(self, force=False):
        return {"projectProfiles": [{"id": "p1", "name": "Проект"}],
                "inboxId": INBOX_ID}

    def get_inbox_tasks(self):
        inbox = self.get_state()["inboxId"]
        return [t for t in self._tasks if t.get("projectId") == inbox]

    def run_filter_detailed(self, filter_id_or_name):
        return self._tasks, list(self._unsupported)


class FakeOfficial:
    """Официальный v1: про Inbox не знает — ровно как настоящий Open API."""

    def get_project_with_data(self, project_id):
        if project_id == INBOX_ID:
            return {"error": f"project not found: {project_id}"}
        return {"project": {"id": "p1", "name": "Проект"}, "tasks": []}


def _tasks(n, project="p1", first=0):
    return [{"id": f"t{i}", "title": f"Задача {i}", "projectId": project,
             "priority": 0, "status": 0}
            for i in range(first, first + n)]


def _lines(out):
    """Строки задач (format_task_line начинает их с «- », подзадачи — с «↳»)."""
    return [ln for ln in out.splitlines()
            if ln.lstrip().startswith(("- ", "↳"))]


def _forest(project="p1"):
    """3 корня, из них у второго — двое детей."""
    return [
        {"id": "r1", "title": "Корень 1", "projectId": project},
        {"id": "r2", "title": "Корень 2", "projectId": project},
        {"id": "c2a", "title": "Ребёнок 2a", "projectId": project, "parentId": "r2"},
        {"id": "c2b", "title": "Ребёнок 2b", "projectId": project, "parentId": "r2"},
        {"id": "r3", "title": "Корень 3", "projectId": project},
    ]


# ═════════════════════════ run_filter ═════════════════════════

@pytest.fixture
def big_filter(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(_tasks(1000)))


async def test_filter_default_page_is_capped_and_says_how_to_continue(big_filter):
    out = await s.run_filter("For me")

    assert len(_lines(out)) <= 200, (
        f"страница не ограничена: {len(_lines(out))} строк, {len(out)} символов")
    assert "1000" in out, "общее число совпадений не названо"
    assert "offset=200" in out, "не сказано, каким offset дочитать хвост"
    assert "more." not in out, (
        "хвост объявлен потерянным («... and N more.») без способа его достать")


async def test_filter_limit_narrows_the_page(big_filter):
    out = await s.run_filter("For me", limit=10)

    assert len(_lines(out)) == 10
    assert "(id:t9 " in out
    assert "(id:t10 " not in out


async def test_filter_offset_reaches_the_tail(big_filter):
    out = await s.run_filter("For me", limit=10, offset=990)

    assert "(id:t990 " in out and "(id:t999 " in out
    assert "(id:t989 " not in out, "вторая страница повторяет первую"


async def test_filter_offset_past_the_end_is_reported_not_pretended_empty(big_filter):
    out = await s.run_filter("For me", offset=5000)

    assert "matched no open tasks" not in out, (
        "пустая страница выдана за «фильтр ничего не нашёл»")
    assert "1000" in out
    assert "offset" in out.lower()


async def test_filter_page_keeps_subtasks_with_their_parent(monkeypatch):
    """Срез по КОРНЯМ: ребёнок уезжает на страницу вместе с родителем.

    Плоский срез оторвал бы его ровно на границе, а format_task_tree печатает
    такого сироту на ВЕРХНЕМ уровне (без «↳») — подзадача молча превратилась
    бы в самостоятельную задачу фильтра.
    """
    monkeypatch.setattr(s, "ticktick", FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(_forest()))

    out = await s.run_filter("For me", limit=2)

    assert "Ребёнок 2a" in out and "Ребёнок 2b" in out, "дети отстали от корня"
    # «↳ » — маркер вложенности format_task_tree; корень печатается без него.
    assert "↳ - Ребёнок 2a" in out, "ребёнок напечатан как самостоятельная задача"
    assert "Корень 3" not in out, "третий корень попал на первую страницу"
    assert "offset=2" in out, f"хвост недостижим: {out!r}"


async def test_filter_warning_survives_paging(monkeypatch):
    """Предупреждение о невычислимом условии обязано быть на КАЖДОЙ странице,
    включая ответ про offset за концом: иначе вторая страница читается как
    честно отфильтрованная."""
    monkeypatch.setattr(s, "ticktick", FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2",
                        FakeV2(_tasks(1000), unsupported=["someFutureCondition"]))

    tail = await s.run_filter("Странный", limit=10, offset=990)
    past_end = await s.run_filter("Странный", offset=5000)

    assert "⚠" in tail and "someFutureCondition" in tail, (
        "на второй странице пропало предупреждение о непримененном условии")
    assert "⚠" in past_end and "someFutureCondition" in past_end


async def test_filter_small_output_is_unchanged_byte_for_byte(monkeypatch):
    """Защита от «починили большое — сломали обычное»: пока всё влезает, вывод
    обязан совпадать с прежним до последнего байта (ни «showing», ни футера)."""
    tasks = _tasks(3)
    monkeypatch.setattr(s, "ticktick", FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))

    out = await s.run_filter("For me")

    expected = "Filter 'For me' — 3 task(s):\n\n" + s.format_task_tree(tasks, 500)
    assert out == expected, f"маленький вывод изменился:\n{out!r}\n!=\n{expected!r}"


async def test_filter_empty_result_still_answers_plainly(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))

    out = await s.run_filter("For me")

    assert out == "Filter 'For me' matched no open tasks."


# ═════════════════════════ get_inbox_tasks ═════════════════════════

@pytest.fixture
def big_inbox(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(_tasks(344, project=INBOX_ID)))


async def test_inbox_default_page_is_capped_and_says_how_to_continue(big_inbox):
    out = await s.get_inbox_tasks()

    assert len(_lines(out)) <= 200, (
        f"страница не ограничена: {len(_lines(out))} строк, {len(out)} символов")
    assert "344" in out, "общее число задач Входящих не названо"
    assert "offset=200" in out, "не сказано, каким offset дочитать хвост"
    assert "more." not in out, (
        "хвост объявлен потерянным («... and N more.») без способа его достать")


async def test_inbox_limit_narrows_the_page(big_inbox):
    out = await s.get_inbox_tasks(limit=10)

    assert len(_lines(out)) == 10
    assert "(id:t9 " in out
    assert "(id:t10 " not in out


async def test_inbox_offset_reaches_the_tail(big_inbox):
    out = await s.get_inbox_tasks(limit=10, offset=334)

    assert "(id:t334 " in out and "(id:t343 " in out
    assert "(id:t333 " not in out, "вторая страница повторяет первую"


async def test_inbox_offset_past_the_end_is_reported_not_pretended_empty(big_inbox):
    out = await s.get_inbox_tasks(offset=5000)

    assert "No open tasks in the Inbox" not in out, (
        "пустая страница выдана за «Входящие пусты»")
    assert "344" in out
    assert "offset" in out.lower()


async def test_inbox_page_keeps_subtasks_with_their_parent(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(_forest(project=INBOX_ID)))

    out = await s.get_inbox_tasks(limit=2)

    assert "↳ - Ребёнок 2a" in out and "↳ - Ребёнок 2b" in out, (
        "ребёнок отстал от корня или напечатан как самостоятельная задача")
    assert "Корень 3" not in out
    assert "offset=2" in out


async def test_inbox_small_output_is_unchanged_byte_for_byte(monkeypatch):
    tasks = _tasks(3, project=INBOX_ID)
    monkeypatch.setattr(s, "ticktick", FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(tasks))

    out = await s.get_inbox_tasks()

    expected = "Inbox tasks (3):\n\n" + s.format_task_tree(tasks, 500)
    assert out == expected, f"маленький вывод изменился:\n{out!r}\n!=\n{expected!r}"


async def test_inbox_empty_still_answers_plainly(monkeypatch):
    monkeypatch.setattr(s, "ticktick", FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", FakeV2([]))

    out = await s.get_inbox_tasks()

    assert out == "No open tasks in the Inbox."


# ══════════ get_project_tasks по id Входящих: отсылка стала честной ══════════

async def test_project_tasks_applies_limit_and_offset_to_the_inbox(big_inbox):
    """get_project_tasks отсылал к get_inbox_tasks и честно предупреждал, что
    limit/offset «не применены» — потому что применять их было не к чему.
    Теперь они доезжают до Входящих, и предупреждать не о чем."""
    out = await s.get_project_tasks(INBOX_ID, limit=10, offset=334)

    assert "not applied" not in out, "параметры всё ещё объявлены проигнорированными"
    assert "(id:t334 " in out and "(id:t343 " in out
    assert "(id:t333 " not in out, "offset не применён — страница та же самая"
    assert len(_lines(out)) == 10, "limit не применён"
