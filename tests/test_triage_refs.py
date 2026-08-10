"""Временные метки, зависимости и порядок волн (1.3.3/изм-10).

Сценарий, ради которого всё это: «создай общую задачу „Workers' Compensation"
и привяжи к ней вот эти три» — ОДНИМ планом и ОДНИМ подтверждением. До меток
это было невозможно: id новой задачи не существует в момент, когда план
показывают человеку, а выдумывать его нельзя.

Здесь закреплены семь требований приёмки ТЗ 1.3.3 (пункты 7 и 8) и три
следствия раздела 5 дизайна про волны против пакетных исполнителей.
"""
import re

import pytest

import ticktick_mcp.src.server as s


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _mid(text: str) -> str:
    m = re.search(r"Манифест `([0-9a-f]+)`", text)
    assert m, f"нет id манифеста:\n{text}"
    return m.group(1)


_NAMES = {"p_in": "Входящие", "p_work": "Работа"}


class _FakeV2:
    def __init__(self, live):
        self.live = live
        self.calls = []

    def invalidate_cache(self):
        pass

    def get_open_tasks(self):
        return list(self.live.values())

    def get_state(self, force=False):
        return {"tags": []}

    def get_tags(self):
        return []

    def set_task_parents(self, rows):
        self.calls.append(("parent", [r["taskId"] for r in rows],
                           rows[0]["parentId"] if rows else None))
        for r in rows:
            if r["taskId"] in self.live:
                self.live[r["taskId"]]["parentId"] = r["parentId"]
        return {}

    def batch_update_tasks(self, changes):
        self.calls.append(("update", [c["taskId"] for c in changes]))
        for c in changes:
            t = self.live.setdefault(c["taskId"], {"id": c["taskId"]})
            for k, v in c.items():
                if k != "taskId":
                    t[k] = v
        return {}

    def batch_delete_tasks(self, rows):
        self.calls.append(("delete", [r["taskId"] for r in rows]))
        for r in rows:
            self.live.pop(r["taskId"], None)
        return {}

    def batch_complete_tasks(self, ids):
        self.calls.append(("complete", list(ids)))
        for tid in ids:
            self.live.pop(tid, None)
        return {}


class _FakeOfficial:
    def __init__(self, live, fail_create=False):
        self.live = live
        self.calls = []
        self.fail_create = fail_create
        self._n = 0

    def create_task(self, title, project_id, content=None, start_date=None,
                    due_date=None, priority=0, is_all_day=False,
                    repeat_flag=None, reminders=None):
        self.calls.append(("create", title, project_id))
        if self.fail_create:
            return {"error": "TickTick отклонил создание"}
        self._n += 1
        tid = f"new{self._n}"
        self.live[tid] = {"id": tid, "title": title, "projectId": project_id,
                          "tags": []}
        return {"id": tid, "title": title, "projectId": project_id}

    def update_task(self, task_id, project_id, title=None, content=None,
                    start_date=None, due_date=None, priority=None,
                    repeat_flag=None, reminders=None):
        self.calls.append(("update", task_id))
        t = self.live.setdefault(task_id, {"id": task_id})
        if title is not None:
            t["title"] = title
        if content is not None:
            t["content"] = content
        if priority is not None:
            t["priority"] = priority
        return {"id": task_id}

    def complete_task(self, project_id, task_id):
        self.calls.append(("complete", task_id))
        self.live.pop(task_id, None)
        return {"id": task_id}


def _wire(monkeypatch, live, tmp_path, fail_create=False):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(_NAMES))
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2 = _FakeV2(live)
    official = _FakeOfficial(live, fail_create=fail_create)
    # ОБЩИЙ журнал вызовов на оба канала: порядок между каналами — это ровно
    # то, что проверяет тест про необратимую волну, и два раздельных списка
    # его не показывают.
    official.calls = v2.calls
    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", official)
    return v2, official


def _three_kids():
    return {
        "k1": {"id": "k1", "title": "Позвонить адвокату", "projectId": "p_work"},
        "k2": {"id": "k2", "title": "Собрать документы", "projectId": "p_work"},
        "k3": {"id": "k3", "title": "Написать заявление", "projectId": "p_work"},
    }


def _wc_plan():
    """Создать общую задачу и привязать к ней три существующие — одним планом."""
    return [
        {"op": "create", "title": "Workers' Compensation",
         "to_project_id": "p_work", "new_ref": "wc",
         "said": "заведи общую задачу под всю эту историю"},
        {"op": "parent", "task_id": "k1", "title": "Позвонить адвокату",
         "parent_ref": "wc", "said": "это часть той истории"},
        {"op": "parent", "task_id": "k2", "title": "Собрать документы",
         "parent_ref": "wc", "said": "это часть той истории"},
        {"op": "parent", "task_id": "k3", "title": "Написать заявление",
         "parent_ref": "wc", "said": "это часть той истории"},
    ]


# ═════════ 1. Одним подтверждением: создать родителя и привязать троих ══════

async def test_create_parent_and_nest_three_in_one_confirmation(
        monkeypatch, tmp_path):
    live = _three_kids()
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю историю", _wc_plan())
    out = await s.manual_triage("Разбираю историю", manifest_id=_mid(preview),
                                user_reply="да")

    made = [t for t in live.values() if t["title"] == "Workers' Compensation"]
    assert len(made) == 1, "родитель создан"
    parent_id = made[0]["id"]
    for kid in ("k1", "k2", "k3"):
        assert live[kid].get("parentId") == parent_id, \
            f"{kid} не вложена под созданного родителя: {live[kid]}"
    assert "✅ Выполнено 4 из 4" in out, out


# ═════════ 2. Срыв создания помечает все зависимые пропущенными ════════════

async def test_failed_create_skips_dependent_ops(monkeypatch, tmp_path):
    """Задачи при этом НЕ ТРОНУТЫ: привязка под несозданного родителя — это
    осиротение, а не «сделаем что можем»."""
    live = _three_kids()
    before = {k: dict(v) for k, v in live.items()}
    v2, official = _wire(monkeypatch, live, tmp_path, fail_create=True)

    preview = await s.manual_triage("Разбираю историю", _wc_plan())
    out = await s.manual_triage("Разбираю историю", manifest_id=_mid(preview),
                                user_reply="да")

    assert live == before, "ни одна задача не должна быть тронута"
    assert not any(c[0] == "parent" for c in v2.calls), \
        f"вложение всё-таки ушло в канал: {v2.calls}"
    assert "не создан" in out
    assert out.count("не создан") >= 3, "пропущены должны быть ВСЕ три"


# ═══════════════ 3-5. План НЕ строится: три класса дурных ссылок ═══════════

async def test_reference_to_a_missing_label_refuses_the_plan(
        monkeypatch, tmp_path):
    live = _three_kids()
    v2, official = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "parent", "task_id": "k1", "title": "Позвонить адвокату",
         "parent_ref": "ghost", "said": "вложи"}])

    assert "🛑" in out and "которой в плане нет" in out
    assert s._MANIFESTS == {} and v2.calls == [] and official.calls == []


async def test_reference_to_a_non_creating_op_refuses_the_plan(
        monkeypatch, tmp_path):
    """Ссылаться можно только на create и duplicate: у прочих операций
    подставлять на место метки нечего."""
    live = _three_kids()
    v2, official = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "update", "task_id": "k2", "title": "Собрать документы",
         "changes": {"priority": 5}, "new_ref": "doc", "said": "подними"},
        {"op": "parent", "task_id": "k1", "title": "Позвонить адвокату",
         "parent_ref": "doc", "said": "вложи"}])

    assert "🛑" in out and "ничего не создаёт" in out
    assert s._MANIFESTS == {} and v2.calls == [] and official.calls == []


async def test_cyclic_reference_refuses_the_plan(monkeypatch, tmp_path):
    """Цикл через `after`: операции ждут друг друга по кругу, начать нечем."""
    live = _three_kids()
    v2, official = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "update", "task_id": "k1", "title": "Позвонить адвокату",
         "changes": {"priority": 5}, "new_ref": "a", "after": ["b"],
         "said": "подними"},
        {"op": "update", "task_id": "k2", "title": "Собрать документы",
         "changes": {"priority": 3}, "new_ref": "b", "after": ["a"],
         "said": "подними"}])

    assert "🛑" in out and "ЦИКЛ" in out
    assert s._MANIFESTS == {} and v2.calls == [] and official.calls == []


async def test_duplicate_label_refuses_the_plan(monkeypatch, tmp_path):
    """Повторное объявление метки: ссылка на неё означала бы то одну
    операцию, то другую, и выбирал бы порядок объявления."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "create", "title": "Первая", "to_project_id": "p_work",
         "new_ref": "x", "said": "заведи"},
        {"op": "create", "title": "Вторая", "to_project_id": "p_work",
         "new_ref": "x", "said": "заведи"}])

    assert "🛑" in out and "объявлена дважды" in out
    assert s._MANIFESTS == {} and official.calls == []


# ═══════════════════ Метка не видна человеку в превью ══════════════════════

async def test_ref_label_absent_from_preview(monkeypatch, tmp_path):
    """Служебный токен в превью создаёт иллюзию, что читатель что-то
    проверил, — а проверить метку он не может ничем. Человек обязан читать
    ИМЯ объекта."""
    live = _three_kids()
    _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю историю", _wc_plan())

    assert "wc" not in preview.replace("Workers' Compensation", ""), preview
    assert "parent_ref" not in preview and "new_ref" not in preview
    # …и вместо метки в строке привязки стоит настоящее имя будущего родителя.
    assert preview.count("«Workers' Compensation»") >= 3


# ═════════════ Три следствия волн против пакетных исполнителей ═════════════

def test_plan_without_refs_is_exactly_one_wave():
    """План без единой ссылки даёт РОВНО ОДИН уровень, а внутри него — те же
    партии, что и до появления волн: сначала обратимое, необратимое последним,
    удаление и объединение ОДНОЙ партией (это физически один вызов)."""
    ops = [{"op": "delete", "task_id": "a"}, {"op": "update", "task_id": "b"},
           {"op": "merge", "task_id": "c", "keep_task_id": "z"},
           {"op": "move", "task_id": "d"}, {"op": "complete", "task_id": "e"}]
    edges = s._triage_dependency_edges(ops, s._triage_labels_of(ops))
    levels = s._triage_topo_levels(ops, edges)

    assert levels is not None and len(levels) == 1, levels
    batches = s._triage_batches_of_level([ops[i] for i in levels[0]])
    assert [[o["op"] for o in b] for b in batches] == [
        ["update"], ["move"], ["complete"], ["merge", "delete"]], batches


async def test_no_interim_verification_when_there_are_no_refs(
        monkeypatch, tmp_path):
    """Следствие того же: у сегодняшних планов не появляется НИ ОДНОЙ
    промежуточной сверки между волнами — вердикт по каждой операции по-прежнему
    считается один раз, в конце."""
    live = _three_kids()
    _wire(monkeypatch, live, tmp_path)
    seen = []
    real = s._verify_triage_op
    monkeypatch.setattr(s, "_verify_triage_op",
                        lambda op, m, n: (seen.append(op["task_id"]),
                                          real(op, m, n))[1])

    preview = await s.manual_triage("Разбираю", [
        {"op": "update", "task_id": "k1", "title": "Позвонить адвокату",
         "changes": {"priority": 5}, "said": "подними"},
        {"op": "complete", "task_id": "k2", "title": "Собрать документы",
         "said": "сделал"}])
    await s.manual_triage("Разбираю", manifest_id=_mid(preview),
                          user_reply="да")

    assert sorted(seen) == ["k1", "k2"], f"сверка звалась лишний раз: {seen}"


async def test_a_ref_splits_the_batch_but_not_the_level_membership(
        monkeypatch, tmp_path):
    """Появление ссылки дробит партию, но не меняет СОСТАВ операций: те же
    четыре действия исполняются, просто в две волны."""
    live = _three_kids()
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю историю", _wc_plan())
    await s.manual_triage("Разбираю историю", manifest_id=_mid(preview),
                          user_reply="да")

    # Одно создание и ОДИН пакетный вызов вложения на всех троих детей:
    # партия внутри волны не рассыпалась в три вызова.
    assert sum(1 for c in official.calls if c[0] == "create") == 1
    parent_calls = [c for c in v2.calls if c[0] == "parent"]
    assert len(parent_calls) == 1, f"вложение раздробилось: {parent_calls}"
    assert sorted(parent_calls[0][1]) == ["k1", "k2", "k3"]


async def test_irreversible_wave_is_always_last(monkeypatch, tmp_path):
    """Необратимая волна всегда последняя: ранг типа не может поднять её выше
    её собственного уровня. Здесь удаление зависит от правки (`after`) — и
    всё равно идёт после неё, а не «удаление последним рангом, но раньше
    волны»."""
    live = _three_kids()
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "k3", "title": "Написать заявление",
         "after": ["kept"], "said": "лишняя, снеси"},
        {"op": "update", "task_id": "k1", "title": "Позвонить адвокату",
         "changes": {"content": "телефон из k3"}, "new_ref": "kept",
         "said": "перенеси телефон сюда"}])

    # В превью правка стоит ВЫШЕ удаления — топологический порядок, он же
    # порядок показа.
    assert preview.index("Изменить") < preview.index("Удалить"), preview

    await s.manual_triage("Разбираю", manifest_id=_mid(preview),
                          user_reply="да")

    kinds = [c[0] for c in v2.calls]
    assert "delete" in kinds
    assert kinds.index("update") < kinds.index("delete"), kinds
    assert "k3" not in live and "k1" in live


async def test_delete_waits_for_the_update_it_depends_on(monkeypatch, tmp_path):
    """Связка update+delete (дизайн, раздел 4): правка не удалась — удаление
    НЕ идёт, и деталь не теряется. «Отправлено» выполненным не считается:
    вердикт берётся из независимого чтения живого состояния."""
    live = _three_kids()
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "k3", "title": "Написать заявление",
         "after": ["kept"], "said": "лишняя, снеси"},
        {"op": "update", "task_id": "k1", "title": "Позвонить адвокату",
         "changes": {"new_title": "Позвонить адвокату (тел. из k3)"},
         "new_ref": "kept", "said": "перенеси телефон сюда"}])

    # Канал «принимает» правку, но живое состояние её НЕ показывает — ровно
    # то, ради чего вердикт судится независимым чтением.
    monkeypatch.setattr(v2, "batch_update_tasks",
                        lambda changes: {"id2error": {}})
    monkeypatch.setattr(official, "update_task",
                        lambda *a, **kw: {"id": a[0] if a else ""})

    out = await s.manual_triage("Разбираю", manifest_id=_mid(preview),
                                user_reply="да")

    assert "k3" in live, "удаление обязано не состояться"
    assert not any(c[0] == "delete" for c in v2.calls), v2.calls
    assert "не выполнен" in out or "не создан" in out, out
