"""Дефект №4 (2026-08-06, живой прогон move_tasks, манифест 58afe2beeea7):
задача «__AUTOTEST__dup-src (copy)» РЕАЛЬНО переместилась в целевой проект —
подтверждено независимым чтением через get_project_tasks (ОФИЦИАЛЬНЫЙ Open
API) сразу после нажатия ✅, projectId на самой задаче обновился. Но Telegram
получил «🔴 не выполнено... ❌ НЕ перемещено... не найдена среди открытых
(ожидалась живой)» — и от собственного post-verify _move_tasks_impl, и от
независимой перепроверки operation_report.

Гипотеза Максима («пост-проверка ищет задачу в ИСХОДНОМ проекте — логика
инвертирована») ПРОВЕРЕНА ПО КОДУ И НЕ ПОДТВЕРДИЛАСЬ: сравнение и в
_move_tasks_impl, и в _verify_item(op="move", ...) уже сверяет
live.projectId именно с НОВЫМ (to_project_id), не со старым — доказано
и чтением кода, и прогоном обеих функций на управляемых данных (см.
test_verify_item_confirms_move_when_task_landed_in_new_project и
test_move_tasks_impl_reports_success_when_move_is_already_visible ниже,
happy path без единой гонки).

Настоящая причина другая: ОБА поствери-чтения идут через ОДИН И ТОТ ЖЕ
неофициальный v2 web-API (`ticktick_v2`, /batch/check/0) — ТЕМ ЖЕ, которым
сама мутация и была сделана — и сразу после записи это чтение может на
секунду-две отставать от уже состоявшейся мутации (задача читалась как «не
среди открытых вовсе», а не «нашлась, но в старом проекте» — то есть даже не
inverted-логика, а стейл снимок). Официальный Open API (get_project_tasks)
использует СОВСЕМ ДРУГОЙ backend и этой гонки не подвержен — этим и
объясняется, почему ручная независимая проверка Максима увидела успех
мгновенно, а серверный post-verify — нет.

Фикс (первая версия, c1de039): _reread_open_until/_reread_projects_until
(server.py, рядом с _open_by_id) — ограниченный ретрай (2 попытки, пауза
0.7с) поверх ТОГО ЖЕ строгого сравнения «там, куда перемещали» — цена ноль,
когда состояние уже свежее, и ограниченная пауза только когда снимок реально
отстаёт. Применено во ВСЕХ identity-changing мутациях: move_tasks,
set_task_parent, unset_task_parent, move_project_to_group, restore_tasks
(обе точки), плюс независимая перепроверка _build_operation_report.

ПОВТОРНЫЙ живой прогон (2026-08-06 23:38 PT, уже НА этом фиксе, манифест
b87428f1b426): того же дефекта — «__AUTOTEST__dup-src» реально
переместилась (независимо подтверждено немедленным чтением через
get_project_tasks, официальный Open API), но ОБЕ v2-перепроверки
(_move_tasks_impl И operation_report) снова отчитались ❌ «не найдена среди
открытых». Окно в 2×0.7с=1.4с максимум — оказалось НЕДОСТАТОЧНЫМ: реальное
отставание v2-синка TickTick иногда превышает эту величину. Переход
пост-проверки на официальный Open API целиком РАССМОТРЕН и ОТКЛОНЁН —
official-клиент физически не знает о project groups/папках (move_project_to_
group) и о корзине (restore_tasks), так что не покрывает 2 из 6 точек ретрая
вообще (см. блок-комментарий над _POSTVERIFY_RETRY_DELAYS_S в server.py).

Фикс v2 (этот файл): _reread_open_until/_reread_projects_until и отдельный
цикл в _build_operation_report переведены с (attempts × delay) на РАСПИСАНИЕ
растущих пауз _POSTVERIFY_RETRY_DELAYS_S = (0.5, 1.0, 1.5, 2.5, 3.5) —
суммарно ~9с в худшем случае (5 повторов вместо 2), но всё так же с нулевой
ценой на счастливом пути (первое же чтение проходит проверку без единой
паузы). Настоящий провал (мутация правда не применилась) по-прежнему остаётся
❌ после исчерпания расписания — расширение окна не ослабляет сравнение.

Каждый тест ниже — пара (позитив: гонка не должна превращать реальный успех
в ложный ❌; негатив: настоящий провал ОБЯЗАН остаться ❌, ретрай никогда не
превращает провал в успех), плюс отдельные юнит-тесты на сами
_reread_open_until/_reread_projects_until (поведение ретрая закреплено
независимо от того, какой именно метод его вызывает), плюс
«*_deeper_than_old_window» тесты — доказательство, что окно РЕАЛЬНО
расширено: лаг глубже старого предела (2 повтора) падал бы на старом коде и
проходит на новом, на ДЕФОЛТНЫХ (не инжектированных) константах продакшена."""
import json

import pytest

import ticktick_mcp.src.server as s


def _stale_then_fresh(states):
    """monkeypatch-заглушка для s._open_by_id: возвращает states[0] на первый
    вызов, states[1] на второй, и т.д.; после исчерпания списка повторяет
    ПОСЛЕДНЕЕ состояние (как и было бы у реального клиента, если TickTick
    так и не догнал реальность за отведённые попытки — негативный сценарий).
    Прикрепляет счётчик вызовов как .calls для ассертов на число чтений."""
    calls = {"n": 0}

    def _fake(fresh=False):
        i = min(calls["n"], len(states) - 1)
        calls["n"] += 1
        return states[i]

    _fake.calls = calls
    return _fake


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # Тесты не должны реально ждать секунды из _POSTVERIFY_RETRY_DELAYS_S —
    # ретраится логика, а не секундомер.
    monkeypatch.setattr(s.time, "sleep", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# _reread_open_until напрямую — поведение ретрая закреплено отдельно от
# того, какой именно исполнитель его вызывает.
# ---------------------------------------------------------------------------

def test_reread_open_until_succeeds_immediately_when_already_settled(monkeypatch):
    reads = _stale_then_fresh([{"t1": {"id": "t1", "projectId": "p_new"}}])
    monkeypatch.setattr(s, "_open_by_id", reads)

    result = s._reread_open_until(lambda m: m.get("t1", {}).get("projectId") == "p_new")

    assert result == {"t1": {"id": "t1", "projectId": "p_new"}}
    assert reads.calls["n"] == 1  # ни одной лишней попытки, когда всё уже видно


def test_reread_open_until_retries_and_recovers_from_transient_staleness(monkeypatch):
    reads = _stale_then_fresh([
        {"t1": {"id": "t1", "projectId": "p_old"}},   # 1-е чтение — ещё стейл
        {"t1": {"id": "t1", "projectId": "p_new"}},   # 2-е — уже видно
    ])
    monkeypatch.setattr(s, "_open_by_id", reads)

    result = s._reread_open_until(lambda m: m.get("t1", {}).get("projectId") == "p_new")

    assert result["t1"]["projectId"] == "p_new"
    assert reads.calls["n"] == 2


def test_reread_open_until_gives_up_after_exhausting_attempts(monkeypatch):
    reads = _stale_then_fresh([{"t1": {"id": "t1", "projectId": "p_old"}}])  # никогда не меняется
    monkeypatch.setattr(s, "_open_by_id", reads)

    result = s._reread_open_until(
        lambda m: m.get("t1", {}).get("projectId") == "p_new",
        delays=(0, 0))

    assert result["t1"]["projectId"] == "p_old"  # честный провал, не подделан
    assert reads.calls["n"] == 3  # 1 исходное + 2 повтора, не бесконечно


def test_reread_open_until_gives_up_after_exhausting_default_schedule(monkeypatch):
    """То же самое, но на РЕАЛЬНОМ дефолтном расписании продакшена
    (_POSTVERIFY_RETRY_DELAYS_S), а не на инжектированном — доказывает, что
    честный провал остаётся честным и на текущем расширенном окне, не только
    в теории с delays=(0, 0)."""
    reads = _stale_then_fresh([{"t1": {"id": "t1", "projectId": "p_old"}}])
    monkeypatch.setattr(s, "_open_by_id", reads)

    result = s._reread_open_until(lambda m: m.get("t1", {}).get("projectId") == "p_new")

    assert result["t1"]["projectId"] == "p_old"
    assert reads.calls["n"] == 1 + len(s._POSTVERIFY_RETRY_DELAYS_S)


def test_reread_open_until_survives_lag_deeper_than_old_window(monkeypatch):
    """Ключевое доказательство расширения окна: 4 стейл-чтения подряд после
    мутации (задача «не видна» дольше, чем позволяло СТАРОЕ окно — 2 попытки
    по 0.7с, то есть максимум 2 повторных чтения), и лишь 5-е чтение видит
    свежее состояние. Старое окно (2 повтора) сдалось бы на 3-м чтении и
    вернуло бы стейл — новое (5 повторов по умолчанию) обязано дотянуть до
    5-го и вернуть success. Использует ДЕФОЛТНОЕ расписание продакшена, не
    инжектированное — это тест реальных констант, а не абстрактного
    механизма."""
    stale = {"t1": {"id": "t1", "projectId": "p_old"}}
    fresh_state = {"t1": {"id": "t1", "projectId": "p_new"}}
    reads = _stale_then_fresh([stale, stale, stale, stale, fresh_state])
    monkeypatch.setattr(s, "_open_by_id", reads)

    result = s._reread_open_until(lambda m: m.get("t1", {}).get("projectId") == "p_new")

    assert result["t1"]["projectId"] == "p_new"  # догнало на 5-м чтении
    assert reads.calls["n"] == 5
    # Явная проверка, что это ДЕЙСТВИТЕЛЬНО глубже старого окна (иначе тест
    # ничего не доказывает про РАСШИРЕНИЕ, а не про факт наличия ретрая).
    OLD_WINDOW_READS = 3  # 1 исходное + 2 старых повтора (0.7с каждый)
    assert reads.calls["n"] > OLD_WINDOW_READS


def test_reread_open_until_returns_none_immediately_when_state_unavailable(monkeypatch):
    reads = _stale_then_fresh([None])
    monkeypatch.setattr(s, "_open_by_id", reads)

    result = s._reread_open_until(lambda m: True)

    assert result is None
    assert reads.calls["n"] == 1  # None не путается с «check вернул False» — не ретраится


# ---------------------------------------------------------------------------
# _reread_projects_until напрямую — тот же контракт, что у _reread_open_until,
# но для move_project_to_group (список проектов, не карта задач по id).
# ---------------------------------------------------------------------------

class _FakeV2ProjectsOnly:
    """Заглушка ticktick_v2 только для _reread_projects_until: список
    состояний по очереди, как _stale_then_fresh, но под интерфейс
    get_state/list_projects."""

    def __init__(self, states):
        self.states = states
        self.calls = 0

    def get_state(self, force=False):
        return {}

    def list_projects(self):
        i = min(self.calls, len(self.states) - 1)
        self.calls += 1
        return self.states[i]


def test_reread_projects_until_succeeds_immediately_when_already_settled(monkeypatch):
    fake = _FakeV2ProjectsOnly([[{"id": "p1", "groupId": "g_new"}]])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = s._reread_projects_until(lambda ps: ps[0].get("groupId") == "g_new")

    assert result == [{"id": "p1", "groupId": "g_new"}]
    assert fake.calls == 1  # ни одной лишней попытки


def test_reread_projects_until_survives_lag_deeper_than_old_window(monkeypatch):
    """Проектный аналог test_reread_open_until_survives_lag_deeper_than_old_window
    — 4 стейл-чтения подряд (глубже старого окна в 2 повтора), 5-е видит
    свежий groupId. Дефолтное расписание продакшена, не инжектированное."""
    stale = [{"id": "p1", "groupId": None}]
    fresh_state = [{"id": "p1", "groupId": "g_new"}]
    fake = _FakeV2ProjectsOnly([stale, stale, stale, stale, fresh_state])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = s._reread_projects_until(lambda ps: ps[0].get("groupId") == "g_new")

    assert result[0]["groupId"] == "g_new"
    assert fake.calls == 5
    OLD_WINDOW_READS = 3
    assert fake.calls > OLD_WINDOW_READS


def test_reread_projects_until_gives_up_after_exhausting_default_schedule(monkeypatch):
    stale = [{"id": "p1", "groupId": None}]  # никогда не меняется
    fake = _FakeV2ProjectsOnly([stale])
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = s._reread_projects_until(lambda ps: ps[0].get("groupId") == "g_new")

    assert result[0]["groupId"] is None  # честный провал
    assert fake.calls == 1 + len(s._POSTVERIFY_RETRY_DELAYS_S)


# ---------------------------------------------------------------------------
# move_tasks — живой сценарий
# ---------------------------------------------------------------------------

class _FakeV2MoveOnly:
    def __init__(self, *, actually_moves=True):
        self.calls = []
        self.actually_moves = actually_moves

    def batch_move_tasks_raw(self, rows):
        self.calls.append(("move", [r["taskId"] for r in rows],
                           rows[0]["toProjectId"] if rows else None))
        return {}


def _wire_move(monkeypatch, reads, fake=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: {"p_old": "Старый", "p_new": "Новый"})
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "move-test0001")
    monkeypatch.setattr(s, "_open_by_id", reads)
    monkeypatch.setattr(s, "ticktick_v2", fake or _FakeV2MoveOnly())


async def test_move_tasks_postverify_survives_transient_v2_sync_lag(monkeypatch):
    """Ровно живой сценарий: задача реально перемещена (независимо
    подтверждено бы через официальный API), но /batch/check/0 сразу после
    мутации ЕЩЁ не видит её вовсе (пустой снимок — точь-в-точь «не найдена
    среди открытых»), и лишь на следующем чтении показывает её в новом
    проекте. Пост-проверка ОБЯЗАНА вернуть успех."""
    before = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_old"}}
    stale_after_move = {}  # «не найдена среди открытых» — симптом из живого прогона
    fresh_after_move = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_new"}}
    reads = _stale_then_fresh([before, stale_after_move, fresh_after_move])
    _wire_move(monkeypatch, reads)

    result = await s._move_tasks_impl(
        "Перемещаю", [{"taskId": "t1", "title": "Задача"}], "p_new", "Новый")

    assert "Перемещено 1" in result
    assert "«Задача»" in result
    assert "❌" not in result
    assert "НЕ ПОДТВЕРЖДЁН" not in result


async def test_move_tasks_postverify_survives_lag_deeper_than_old_window(monkeypatch):
    """Сквозной (tool-level, не только _reread_open_until напрямую) вариант
    ПОВТОРНОГО живого дефекта 2026-08-06 23:38 PT: задача «__AUTOTEST__dup-src»
    реально переместилась, но v2-снимок ЕЩЁ не видел её вовсе на 4 чтениях
    подряд после мутации — глубже, чем позволяло СТАРОЕ окно (2 попытки по
    0.7с = максимум 2 повторных чтения). На РЕАЛЬНОМ дефолтном расписании
    (не инжектированном) 5-е чтение обязано увидеть свежее состояние и
    итог — success, не ложный ❌."""
    before = {"t1": {"id": "t1", "title": "__AUTOTEST__dup-src", "projectId": "p_old"}}
    stale_after_move = {}  # «не найдена среди открытых» — точный симптом из живого прогона
    fresh_after_move = {"t1": {"id": "t1", "title": "__AUTOTEST__dup-src", "projectId": "p_new"}}
    reads = _stale_then_fresh(
        [before, stale_after_move, stale_after_move, stale_after_move,
         stale_after_move, fresh_after_move])
    _wire_move(monkeypatch, reads)

    result = await s._move_tasks_impl(
        "Перемещаю", [{"taskId": "t1", "title": "__AUTOTEST__dup-src"}], "p_new", "Новый")

    assert "Перемещено 1" in result
    assert "❌" not in result
    assert reads.calls["n"] == 6  # 1 pre-mutation + 1 initial post-verify + 4 повтора


async def test_move_tasks_postverify_stays_failed_when_move_never_lands(monkeypatch):
    """Негатив: задача правда осталась в старом проекте на ВСЕХ попытках —
    ретрай НЕ ДОЛЖЕН деградировать честный провал в ложный успех."""
    before = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_old"}}
    still_old = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_old"}}
    reads = _stale_then_fresh([before, still_old])
    _wire_move(monkeypatch, reads)

    result = await s._move_tasks_impl(
        "Перемещаю", [{"taskId": "t1", "title": "Задача"}], "p_new", "Новый")

    assert "❌ НЕ перемещено 1" in result
    assert "Перемещено 1" not in result


# ---------------------------------------------------------------------------
# set_task_parent
# ---------------------------------------------------------------------------

def _wire_parent(monkeypatch, reads):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"proj1": "Работа"})
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "parent-test0001")
    monkeypatch.setattr(s, "_open_by_id", reads)

    class _FakeV2Parent:
        def set_task_parents(self, rows):
            return {}
    monkeypatch.setattr(s, "ticktick_v2", _FakeV2Parent())


async def test_set_task_parent_postverify_survives_transient_v2_sync_lag(monkeypatch):
    before = {
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1",
                    "parentId": None},
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1", "parentId": None},
    }
    stale_after = {
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1",
                    "parentId": None},
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1", "parentId": None},
    }
    fresh_after = {
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1",
                    "parentId": None},
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1",
               "parentId": "parent1"},
    }
    reads = _stale_then_fresh([before, stale_after, fresh_after])
    _wire_parent(monkeypatch, reads)

    result = await s._set_task_parent_impl(
        "Вкладываю", [{"taskId": "t1", "title": "Ребёнок"}],
        "parent1", "proj1", "Родитель")

    assert "🔗 Вложено 1" in result
    assert "❌" not in result


async def test_set_task_parent_postverify_stays_failed_when_never_lands(monkeypatch):
    before = {
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1",
                    "parentId": None},
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1", "parentId": None},
    }
    still_unset = {
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1",
                    "parentId": None},
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1", "parentId": None},
    }
    reads = _stale_then_fresh([before, still_unset])
    _wire_parent(monkeypatch, reads)

    result = await s._set_task_parent_impl(
        "Вкладываю", [{"taskId": "t1", "title": "Ребёнок"}],
        "parent1", "proj1", "Родитель")

    assert "❌ НЕ вложено 1" in result
    assert "🔗 Вложено" not in result


# ---------------------------------------------------------------------------
# unset_task_parent
# ---------------------------------------------------------------------------

def _wire_unset(monkeypatch, reads):
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"proj1": "Работа"})
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "parent-test0002")
    monkeypatch.setattr(s, "_open_by_id", reads)

    class _FakeV2Unset:
        def unset_task_parent(self, task_id, parent_id, project_id):
            return {}
    monkeypatch.setattr(s, "ticktick_v2", _FakeV2Unset())


async def test_unset_task_parent_postverify_survives_transient_v2_sync_lag(monkeypatch):
    before = {
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1",
               "parentId": "parent1"},
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1"},
    }
    stale_after = {
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1",
               "parentId": "parent1"},  # ещё показывает старого родителя
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1"},
    }
    fresh_after = {
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1", "parentId": None},
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1"},
    }
    reads = _stale_then_fresh([before, stale_after, fresh_after])
    _wire_unset(monkeypatch, reads)

    result = await s._unset_task_parent_impl(
        "Ребёнок", "Родитель", "t1", "parent1", "proj1")

    assert "✅" in result
    assert "отцеплена" in result
    assert "❌" not in result


async def test_unset_task_parent_postverify_stays_failed_when_never_lands(monkeypatch):
    before = {
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1",
               "parentId": "parent1"},
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1"},
    }
    still_attached = {
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1",
               "parentId": "parent1"},
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1"},
    }
    reads = _stale_then_fresh([before, still_attached])
    _wire_unset(monkeypatch, reads)

    result = await s._unset_task_parent_impl(
        "Ребёнок", "Родитель", "t1", "parent1", "proj1")

    assert "❌ НЕ отцепил" in result
    assert "✅" not in result


# ---------------------------------------------------------------------------
# move_project_to_group — не использует _open_by_id (работает с проектами,
# не задачами), поэтому у него отдельный ретраер (_reread_projects_until).
# ---------------------------------------------------------------------------

class _FakeV2Group:
    def __init__(self, states):
        self.states = states
        self.calls = 0
        self.move_calls = []

    def move_project_to_group(self, project_id, group_id):
        self.move_calls.append((project_id, group_id))
        return {}

    def get_state(self, force=False):
        return {}

    def list_projects(self):
        i = min(self.calls, len(self.states) - 1)
        self.calls += 1
        return self.states[i]


def _wire_group(monkeypatch, fake):
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"proj1": "Проект"})

    async def _fake_live_groups(fresh=False):
        return [{"id": "group_new", "name": "Новая папка"}]
    monkeypatch.setattr(s, "_live_groups", _fake_live_groups)
    monkeypatch.setattr(s, "ticktick_v2", fake)


async def test_move_project_to_group_postverify_survives_transient_v2_sync_lag(monkeypatch):
    stale = [{"id": "proj1", "groupId": None}]
    fresh = [{"id": "proj1", "groupId": "group_new"}]
    fake = _FakeV2Group([stale, fresh])
    _wire_group(monkeypatch, fake)

    result = await s._move_project_to_group_impl("Проект", "proj1", "group_new")

    assert result.startswith("✅")
    assert "❌" not in result
    assert fake.move_calls == [("proj1", "group_new")]


async def test_move_project_to_group_postverify_stays_failed_when_never_lands(monkeypatch):
    stale = [{"id": "proj1", "groupId": None}]
    fake = _FakeV2Group([stale])  # так и не меняется
    _wire_group(monkeypatch, fake)

    result = await s._move_project_to_group_impl("Проект", "proj1", "group_new")

    assert result.startswith("❌")
    assert "НЕ переместился" in result


# ---------------------------------------------------------------------------
# restore_tasks — две отдельные точки ретрая: (1) сама видимость после
# /trash/restore, (2) после починочного перемещения, если TickTick уронил
# задачу не в тот список.
# ---------------------------------------------------------------------------

def _wire_restore(monkeypatch, reads, trash):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: {"p1": "Работа", "inbox": "Inbox"})
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "restore-test0001")
    monkeypatch.setattr(s, "_open_by_id", reads)

    class _FakeV2Restore:
        def get_trash(self, limit=500):
            return list(trash.values())

        def batch_restore_tasks(self, ids, to_project_id=None):
            return {}

        def batch_move_tasks_raw(self, rows):
            return {}
    monkeypatch.setattr(s, "ticktick_v2", _FakeV2Restore())


async def test_restore_tasks_postverify_survives_transient_visibility_lag(monkeypatch):
    """Первая точка: задача реально восстановилась (и сразу в правильный
    проект), но сразу после /trash/restore она ЕЩЁ отсутствует среди
    открытых вовсе — та же гонка, что у move_tasks."""
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    stale_after = {}  # ещё не появилась вовсе
    fresh_after = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    reads = _stale_then_fresh([stale_after, fresh_after])
    _wire_restore(monkeypatch, reads, trash)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])

    assert "снова среди открытых, в нужном списке" in result
    assert "❌" not in result


async def test_restore_tasks_postverify_stays_failed_when_never_reappears(monkeypatch):
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    never = {}
    reads = _stale_then_fresh([never])
    _wire_restore(monkeypatch, reads, trash)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])

    assert "❌ НЕ восстановлено 1" in result
    assert "снова среди открытых" not in result


async def test_restore_tasks_corrective_move_postverify_survives_transient_lag(monkeypatch):
    """Вторая точка: TickTick уронил задачу в Inbox (реальная особенность
    /trash/restore, не гонка), починочное move_tasks реально сработало, но
    следующее чтение сразу после него ещё показывает Inbox — прежде чем
    догонит целевой проект."""
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    landed_in_inbox = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "inbox"}}
    stale_after_fix = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "inbox"}}
    fresh_after_fix = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    reads = _stale_then_fresh(
        [landed_in_inbox, stale_after_fix, fresh_after_fix])
    _wire_restore(monkeypatch, reads, trash)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])

    assert "снова среди открытых, в нужном списке" in result
    assert "НЕ в исходный/запрошенный список" not in result
    assert "❌" not in result


async def test_restore_tasks_corrective_move_stays_failed_when_never_lands(monkeypatch):
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    landed_in_inbox = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "inbox"}}
    stuck_in_inbox = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "inbox"}}
    reads = _stale_then_fresh([landed_in_inbox, stuck_in_inbox])
    _wire_restore(monkeypatch, reads, trash)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])

    assert "НЕ в исходный/запрошенный список" in result
    assert "снова среди открытых, в нужном списке" not in result


# ---------------------------------------------------------------------------
# _verify_item("move", ...) — прямое доказательство, что сравнение и так шло
# против НОВОГО проекта (гипотеза Максима про инверсию не подтвердилась).
# ---------------------------------------------------------------------------

def test_verify_item_confirms_move_when_task_landed_in_new_project():
    names = {"p_old": "Старый", "p_new": "Новый"}
    live_map = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_new"}}
    item = {"taskId": "t1", "title": "Задача", "expect": {"projectId": "p_new"}}

    status, line = s._verify_item("move", item, live_map, names)

    assert status == "ok"
    assert "✅" in line
    assert "Новый" in line


def test_verify_item_flags_move_that_stayed_in_old_project():
    names = {"p_old": "Старый", "p_new": "Новый"}
    live_map = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_old"}}
    item = {"taskId": "t1", "title": "Задача", "expect": {"projectId": "p_new"}}

    status, line = s._verify_item("move", item, live_map, names)

    assert status == "bad"
    assert "❌" in line
    assert "Старый" in line


# ---------------------------------------------------------------------------
# _build_operation_report — независимая перепроверка (второе место, где тот
# же ложный провал прозвучал в живом прогоне: «📋 Независимая проверка...
# не найдена среди открытых (ожидалась живой)»).
# ---------------------------------------------------------------------------

def _write_journal(tmp_path, monkeypatch, record):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    path = tmp_path / "deletion_journal.jsonl"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def test_operation_report_survives_transient_v2_sync_lag_for_move(tmp_path, monkeypatch):
    record = {"record": "move-eefb3fee", "op": "move", "ts": "2026-08-06T22:50:00+00:00",
              "items": [{"taskId": "t1", "title": "__AUTOTEST__dup-src (copy)",
                        "expect": {"projectId": "p_new"}}]}
    _write_journal(tmp_path, monkeypatch, record)
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: {"p_old": "btn-tt-01-retest", "p_new": "btn-tt-02-new"})

    stale = {}  # «не найдена среди открытых» — точная формулировка из живого прогона
    fresh = {"t1": {"id": "t1", "title": "__AUTOTEST__dup-src (copy)", "projectId": "p_new"}}
    reads = _stale_then_fresh([stale, fresh])
    monkeypatch.setattr(s, "_open_by_id", reads)

    report = s._build_operation_report("move-eefb3fee")

    assert "✅ 1 подтверждено, ⚠️ 0 не проверено, ❌ 0 расхождений" in report
    assert "Статус операции: ✅" in report
    assert reads.calls["n"] == 2  # ровно та гонка, что была в проде — не больше


def test_operation_report_survives_lag_deeper_than_old_window(tmp_path, monkeypatch):
    """ВТОРОЙ живой прогон 2026-08-06 23:38 PT показал ложный ❌ ИМЕННО здесь,
    в operation_report, а не только в _move_tasks_impl — и уже НА фиксе со
    старым узким окном. 4 стейл-чтения подряд (глубже старых 2 повторов),
    5-е видит целевой projectId. Дефолтное расписание продакшена."""
    record = {"record": "move-deep-lag", "op": "move", "ts": "2026-08-06T23:38:00+00:00",
              "items": [{"taskId": "t1", "title": "__AUTOTEST__dup-src",
                        "expect": {"projectId": "p_new"}}]}
    _write_journal(tmp_path, monkeypatch, record)
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: {"p_old": "Старый", "p_new": "Новый"})

    stale = {}  # «не найдена среди открытых» — точная формулировка из живого прогона
    fresh = {"t1": {"id": "t1", "title": "__AUTOTEST__dup-src", "projectId": "p_new"}}
    reads = _stale_then_fresh([stale, stale, stale, stale, fresh])
    monkeypatch.setattr(s, "_open_by_id", reads)

    report = s._build_operation_report("move-deep-lag")

    assert "✅ 1 подтверждено, ⚠️ 0 не проверено, ❌ 0 расхождений" in report
    assert "Статус операции: ✅" in report
    assert reads.calls["n"] == 5  # 1 исходное + 4 повтора — глубже старого окна (макс. 3)
    OLD_WINDOW_READS = 3
    assert reads.calls["n"] > OLD_WINDOW_READS


def test_operation_report_stays_failed_when_move_genuinely_never_lands(tmp_path, monkeypatch):
    record = {"record": "move-genuine-fail", "op": "move", "ts": "2026-08-06T22:50:00+00:00",
              "items": [{"taskId": "t1", "title": "Задача",
                        "expect": {"projectId": "p_new"}}]}
    _write_journal(tmp_path, monkeypatch, record)
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: {"p_old": "Старый", "p_new": "Новый"})

    still_old = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_old"}}
    reads = _stale_then_fresh([still_old])
    monkeypatch.setattr(s, "_open_by_id", reads)

    report = s._build_operation_report("move-genuine-fail")

    assert "✅ 0 подтверждено, ⚠️ 0 не проверено, ❌ 1 расхождений" in report
    assert "Статус операции: ❌" in report
    assert "есть расхождения" in report


def test_operation_report_retry_is_scoped_to_identity_changing_ops_only(tmp_path, monkeypatch):
    """Ретрай применяется только к move/parent/restore (см. комментарий у
    _POSTVERIFY_RETRY_ATTEMPTS) — генуинный провал delete НЕ должен получать
    лишнюю задержку/попытки, которых для него никто живьём не наблюдал."""
    record = {"record": "delete-xyz", "op": "delete", "ts": "2026-08-06T22:50:00+00:00",
              "items": [{"taskId": "t1", "title": "Задача"}]}
    _write_journal(tmp_path, monkeypatch, record)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {})

    still_exists = {"t1": {"id": "t1", "title": "Задача"}}  # удаление не подтвердилось
    reads = _stale_then_fresh([still_exists])
    monkeypatch.setattr(s, "_open_by_id", reads)

    report = s._build_operation_report("delete-xyz")

    assert "❌ 1 расхождений" in report
    assert reads.calls["n"] == 1  # ни одной попытки повтора для НЕ-identity-changing op
