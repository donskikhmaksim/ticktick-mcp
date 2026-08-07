"""Живой прогон 2026-08-07 00:03 PT: identity-guard отказал в move_tasks для
задачи «__AUTOTEST__dup-src» (id 6a7571238f0854e347f51407, обычная задача,
НЕ дубликат) с формулировкой «не найдена среди открытых» — через 25 МИНУТ
после того, как та же задача была штатно перемещена (перемещение реально
состоялось). Сразу после отказа та же задача читалась МГНОВЕННО и напрямую
через get_project_tasks И через get_task (оба — официальный Open API,
`ticktick_client.py`, НЕ `ticktick_v2_client.py`).

Диагноз предыдущего захода («guard читает состояние одним чтением без
ретрая, дубликат может не успеть проявиться») был верным на живых данных
2026-08-06, но неполным: 25 минут — не гонка, которую можно пережидать
ретраем ТОГО ЖЕ источника (_POSTVERIFY_RETRY_DELAYS_S покрывает секунды, не
десятки минут). Корень — `_guard_task`/`_split_tasks_by_state` (server.py)
ищут задачу ИСКЛЮЧИТЕЛЬНО через `_open_by_id()`, который строится из
`ticktick_v2.get_open_tasks()` (неофициальный v2 web-API, /batch/check/0).
Задача может надолго выпасть из ЭТОЙ ОДНОЙ выборки «открытых задач» —
сколько её ни перечитывай, результат тот же. Официальный Open API
(`ticktick.get_task(project_id, task_id)`, `ticktick_client.py`) — СОВСЕМ
ДРУГОЙ backend, этой проблеме не подвержен.

Фикс: `_guard_task` (server.py, рядом с `_open_by_id`) — прежде чем
объявить задачу отсутствующей, если её нет в v2-снимке, пробует один
точечный запрос к официальному API: `_official_task_snapshot(project_id,
task_id)`, когда project_id известен (передаётся ПОЧТИ во всех вызовах —
move_tasks, update_tasks, complete_tasks, delete_tasks, add/update/delete_
task_comment, attach_file_to_task, create_subtask, set/unset_task_parent,
set_task_tags), иначе `_official_task_scan(task_id)` — перебор всех
официальных проектов (используется, когда у вызывающего инструмента вообще
нет параметра project_id — abandon_task, duplicate_task). Сама сверка
id/название/контейнер (`_guard_task`'s остальная логика) НЕ изменилась —
изменился только способ добыть объект перед этой сверкой.

Тесты ниже:
  * позитив — ровно живой сценарий (в v2-снимке пусто, официальный API
    видит задачу мгновенно) — guard ОБЯЗАН разрешить операцию, и сквозной
    (tool-level) тест через _move_tasks_impl, доказывающий, что вся цепочка
    (не только _guard_task в изоляции) действительно работает;
  * негатив — задачи ДЕЙСТВИТЕЛЬНО нет нигде (ни в v2, ни в официальном
    API) → guard обязан по-прежнему отказать («missing»);
  * негатив — задача находится через официальный API, но название НЕ то,
    что ожидал вызывающий → guard обязан отказать («mismatch») — защита
    не ослабла, просто добывание объекта стало надёжнее;
  * project_id неизвестен вовсе (abandon_task/duplicate_task) — тот же
    сценарий, но через полный скан официальных проектов;
  * happy path остаётся БЕСПЛАТНЫМ: когда задача и так есть в v2-снимке,
    официальный API не вызывается вообще (ноль лишних запросов)."""
import ticktick_mcp.src.server as s


class _FakeOfficial:
    """Заглушка официального (OAuth) TickTick-клиента: словарь
    {(project_id, task_id): task_dict} + список проектов. .calls считает
    точечные get_task-запросы — используется, чтобы доказать «ноль лишних
    запросов на счастливом пути»."""

    def __init__(self, tasks=None, projects=None):
        self._tasks = dict(tasks or {})  # {(pid, tid): task}
        self._projects = projects if projects is not None else []
        self.calls = []          # [(project_id, task_id), …]
        self.project_list_calls = 0

    def get_task(self, project_id, task_id):
        self.calls.append((project_id, task_id))
        t = self._tasks.get((project_id, task_id))
        if t is None:
            return {"error": "404 Not Found"}
        return t

    def get_projects(self):
        self.project_list_calls += 1
        return self._projects


def _official_task(task_id, project_id, title, status=0):
    return {"id": task_id, "title": title, "projectId": project_id, "status": status}


# ---------------------------------------------------------------------------
# _guard_task напрямую — unit-уровень, без остальной цепочки move_tasks.
# ---------------------------------------------------------------------------

def test_guard_falls_back_to_official_api_when_v2_snapshot_is_missing_the_task(
        monkeypatch):
    """Ровно живой сценарий: v2-снимок «открытых задач» пуст (задача из
    него выпала), но официальный Open API находит её мгновенно по
    project_id/task_id. Guard ОБЯЗАН разрешить (status == ok), взяв
    название/проект из официального ответа."""
    fake = _FakeOfficial(tasks={
        ("p_new", "t1"): _official_task("t1", "p_new", "__AUTOTEST__dup-src"),
    })
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_new": "Новый"})

    g = s._guard_task("t1", "__AUTOTEST__dup-src", "p_new", by_id={})

    assert g.status == "ok"
    assert g.project_id == "p_new"
    assert g.title == "__AUTOTEST__dup-src"
    assert fake.calls == [("p_new", "t1")]  # ровно один точечный запрос


def test_guard_ok_fallback_carries_through_split_tasks_by_state(monkeypatch):
    """_split_tasks_by_state (используется move_tasks/update_tasks/…
    батчами) делегирует в _guard_task для каждого элемента — доказываем,
    что fallback реально долетает через этот слой, а не только при прямом
    вызове _guard_task."""
    fake = _FakeOfficial(tasks={
        ("p_new", "t1"): _official_task("t1", "p_new", "__AUTOTEST__dup-src"),
    })
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_new": "Новый"})

    found, mismatch, missing = s._split_tasks_by_state(
        [{"taskId": "t1", "projectId": "p_new", "title": "__AUTOTEST__dup-src"}],
        by_id={})

    assert missing == []
    assert mismatch == []
    assert found == [{"taskId": "t1", "title": "__AUTOTEST__dup-src",
                      "projectId": "p_new", "armed": True}]


def test_guard_stays_missing_when_task_genuinely_does_not_exist(monkeypatch):
    """Негатив: ни v2, ни официальный API задачу не находят (реально
    удалена/неверный id) — guard ОБЯЗАН по-прежнему отказать."""
    fake = _FakeOfficial(tasks={})  # официальный API тоже её не знает
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_new": "Новый"})

    g = s._guard_task("ghost", "Призрачная задача", "p_new", by_id={})

    assert g.status == "missing"
    assert fake.calls == [("p_new", "ghost")]  # fallback реально пробовался


def test_guard_stays_mismatch_when_official_fallback_finds_a_different_title(
        monkeypatch):
    """Негатив: id реально существует (найден через официальный API), но
    его название НЕ то, что ожидал вызывающий — guard обязан отказать как
    mismatch, а не молча принять чужой объект. Защита не ослабла."""
    fake = _FakeOfficial(tasks={
        ("p_new", "t1"): _official_task("t1", "p_new", "Совсем другая задача"),
    })
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_new": "Новый"})

    g = s._guard_task("t1", "__AUTOTEST__dup-src", "p_new", by_id={})

    assert g.status == "mismatch"
    assert "Совсем другая задача" in g.message


def test_guard_treats_completed_task_found_via_fallback_as_still_missing(
        monkeypatch):
    """Официальный API может отдать и завершённую задачу — она НЕ входит в
    пул «открытых», ровно как _open_by_id уже фильтрует только открытые.
    Fallback не должен стать лазейкой, расширяющей guard на закрытые задачи."""
    fake = _FakeOfficial(tasks={
        ("p_new", "t1"): _official_task("t1", "p_new", "Готово", status=2),
    })
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_new": "Новый"})

    g = s._guard_task("t1", "Готово", "p_new", by_id={})

    assert g.status == "missing"


def test_guard_does_not_call_official_api_when_v2_snapshot_already_has_it(
        monkeypatch):
    """Счастливый путь остаётся БЕСПЛАТНЫМ: когда v2-снимок уже содержит
    задачу, официальный API не вызывается вообще — ноль лишних запросов."""
    fake = _FakeOfficial(tasks={})
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_new": "Новый"})

    g = s._guard_task("t1", "Задача", "p_new",
                      by_id={"t1": {"id": "t1", "title": "Задача", "projectId": "p_new"}})

    assert g.status == "ok"
    assert fake.calls == []  # никакого fallback-запроса


# ---------------------------------------------------------------------------
# Без project_id вообще (abandon_task/duplicate_task) — полный скан проектов.
# ---------------------------------------------------------------------------

def test_guard_falls_back_to_full_project_scan_when_project_id_is_unknown(
        monkeypatch):
    """abandon_task/duplicate_task не принимают project_id как параметр —
    единственный запасной путь без него: перебрать официальные проекты."""
    fake = _FakeOfficial(
        tasks={("p2", "t1"): _official_task("t1", "p2", "__AUTOTEST__dup-src")},
        projects=[{"id": "p1"}, {"id": "p2"}, {"id": "p3"}])
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p2": "Новый"})

    g = s._guard_task("t1", "__AUTOTEST__dup-src", by_id={})  # project_id="" (default)

    assert g.status == "ok"
    assert g.project_id == "p2"
    assert fake.project_list_calls == 1


def test_guard_full_scan_still_refuses_when_task_is_nowhere(monkeypatch):
    fake = _FakeOfficial(tasks={}, projects=[{"id": "p1"}, {"id": "p2"}])
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {})

    g = s._guard_task("ghost", "Призрак", by_id={})

    assert g.status == "missing"


def test_guard_never_falls_back_when_official_client_is_unconfigured(monkeypatch):
    """Если официальный клиент вообще не настроен (ticktick is None) —
    fallback не пытается ничего вызвать, поведение остаётся прежним
    (честный missing, без AttributeError на None)."""
    monkeypatch.setattr(s, "ticktick", None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_new": "Новый"})

    g = s._guard_task("t1", "Задача", "p_new", by_id={})

    assert g.status == "missing"


# ---------------------------------------------------------------------------
# Сквозной (tool-level) тест — ровно сценарий move_tasks из живого прогона:
# доказывает, что фикс реально долетает до конца инструмента, а не только
# до _guard_task в изоляции.
# ---------------------------------------------------------------------------

class _FakeV2MoveOnly:
    def __init__(self):
        self.calls = []

    def batch_move_tasks_raw(self, rows):
        self.calls.append(("move_raw", list(rows)))
        return {}


def _open_by_id_sequence(states):
    """Возвращает states[0] на первый вызов (identity-guard, ДО мутации),
    затем states[1] на все последующие (post-verify, ПОСЛЕ мутации) — как
    и было бы у реального клиента: v2-снимок один и тот же на протяжении
    короткого post-verify окна, просто отличается от снимка ДО мутации."""
    calls = {"n": 0}

    def _fake(fresh=False):
        i = min(calls["n"], len(states) - 1)
        calls["n"] += 1
        return dict(states[i])
    _fake.calls = calls
    return _fake


async def test_move_tasks_second_move_succeeds_when_v2_snapshot_lost_the_task(
        monkeypatch):
    """__AUTOTEST__dup-src была перемещена в p_new 25 минут назад. Второй
    move_tasks (обратно, в p_old) видит ПУСТОЙ v2-снимок открытых задач (в
    точности симптом из живого прогона — «не найдена среди открытых»), но
    официальный API находит её мгновенно в p_new. move_tasks ОБЯЗАН
    выполнить перемещение, а не отказать с «Не найдены среди открытых» —
    ни в v2-снимке identity-guard'а, ни (после самой мутации) в возврате
    инструмента.

    move_tasks' own docstring only asks the caller for {"title", "taskId"}
    per item (no projectId) — so in the REAL incident the guard had no
    project_id to point-read with either, and had to fall back to the full
    project scan (_official_task_scan), exactly as wired here."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: {"p_new": "Новый", "p_old": "Старый"})
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "move-test0002")
    monkeypatch.setattr(s.time, "sleep", lambda *a, **k: None)  # тест не должен реально ждать

    fake_official = _FakeOfficial(
        tasks={("p_new", "t1"): _official_task("t1", "p_new", "__AUTOTEST__dup-src")},
        projects=[{"id": "p_old"}, {"id": "p_new"}])
    monkeypatch.setattr(s, "ticktick", fake_official)

    empty_before_mutation = {}  # «не найдена среди открытых» — точный живой симптом
    after_mutation = {"t1": {"id": "t1", "title": "__AUTOTEST__dup-src",
                             "projectId": "p_old"}}
    reads = _open_by_id_sequence([empty_before_mutation, after_mutation])
    monkeypatch.setattr(s, "_open_by_id", reads)

    fake_v2 = _FakeV2MoveOnly()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)

    result = await s._move_tasks_impl(
        "Перемещаю", [{"taskId": "t1", "title": "__AUTOTEST__dup-src"}],
        "p_old", "Старый")

    assert "↷ Не найдены" not in result
    assert "Перемещено 1" in result
    assert "❌" not in result
    # Raw move (not batch_move_tasks): fromProjectId came from the identity
    # guard's OWN confirmed live projectId (p_new — found via the fallback),
    # not re-derived by v2 from a snapshot that doesn't have the task.
    assert fake_v2.calls == [
        ("move_raw", [{"taskId": "t1", "fromProjectId": "p_new",
                       "toProjectId": "p_old"}])]
    # Полный скан официальных проектов реально нашёл задачу — доказательство,
    # что fallback без project_id тоже долетает до конца инструмента.
    assert ("p_new", "t1") in fake_official.calls
