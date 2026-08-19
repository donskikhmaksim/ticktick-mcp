"""QA-2 (2026-08-19), дефекты №1/№2/№4/№7: закрытые задачи и честный отчёт.

№1 [критично]. Завершённую («Completed», status=2) и отменённую («Won't do»,
status=-1) задачу нельзя было удалить НИКАКИМ путём: и резолвер
apply_task_changes, и plan_task_deletion смотрели ТОЛЬКО в снимок открытых и
отвечали «не найдена среди открытых задач (кто-то удалил или закрыл её
вручную?)» / голым «Ничего не удалено.» — про живую, читаемую get_task'ом
задачу. Живой пример: проект 6a855f878f0886e356935eb6, задачи
«__QA2_TASK__ for complete» (Completed), «__QA2_TASK__ happy create 2»
(Won't do) — вечный мусор без единого пути удаления.

№2 [критично]. Знаменатель отчёта удаления считался по ИСПОЛНЕННОМУ («Удалено
9/9» при 11 запрошенных строках — «100% успех»), а смешанный батч создания
печатал «Создано 2» без знаменателя.

№4. Полностью невалидный батч удаления отвечал голым «Ничего не удалено.»
без причин.

№7. Удаление родителя без with_subtasks — отчёт исполнения молчал про
осиротевших детей (превью говорило, но при выключенном гейте превью никто
не видит).

Стенд — как в tests/test_triage_new_types.py: живое состояние это dict,
фейковые клиенты его реально мутируют; закрытые задачи живут в отдельной
ленте completed, корзина — в trash.
"""
import asyncio
import re

import pytest

import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s

GATE_ENV = consent._GATE_DISABLED_ENV

_NAMES = {"p_in": "Входящие", "p_work": "Работа"}


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


@pytest.fixture(autouse=True)
def _gate_on_by_default(monkeypatch):
    monkeypatch.delenv(GATE_ENV, raising=False)
    yield


def _mid(preview: str) -> str:
    m = re.search(r"Манифест `([0-9a-f]+)`", preview)
    assert m, f"в превью нет id манифеста:\n{preview}"
    return m.group(1)


class _FakeV2:
    """Двойник v2: открытые/завершённые/корзина — три РАЗНЫЕ ленты, как у
    настоящего клиента. batch_delete_tasks честно удаляет и из закрытых —
    иначе перепроверка закрытых лент судила бы по несуществующему миру."""

    def __init__(self, live, completed=None, trash=None):
        self.live = live
        self.completed = completed if completed is not None else {}
        self.trash = trash if trash is not None else {}
        self.calls = []

    def invalidate_cache(self):
        self.calls.append(("invalidate",))

    def get_open_tasks(self):
        return list(self.live.values())

    def find_task_any_state(self, task_id):
        if task_id in self.live:
            return self.live[task_id], "open"
        if task_id in self.completed:
            return self.completed[task_id], "completed"
        if task_id in self.trash:
            return self.trash[task_id], "trash"
        return None, None

    def get_trash(self, limit=500):
        return list(self.trash.values())[:limit]

    def batch_delete_tasks(self, rows):
        self.calls.append(("delete", [r["taskId"] for r in rows]))
        for r in rows:
            gone = (self.live.pop(r["taskId"], None)
                    or self.completed.pop(r["taskId"], None))
            if gone is not None:
                self.trash[r["taskId"]] = dict(gone)
        return {}

    def batch_complete_tasks(self, ids):
        self.calls.append(("complete", list(ids)))
        for tid in ids:
            gone = self.live.pop(tid, None)
            if gone is not None:
                gone["status"] = 2
                self.completed[tid] = gone
        return {}


def _wire(monkeypatch, live, tmp_path, completed=None, trash=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(_NAMES))
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2 = _FakeV2(live, completed=completed, trash=trash)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", None)
    return v2


def _closed(tid, title, status=2, pid="p_in"):
    return {"id": tid, "title": title, "projectId": pid, "status": status}


# ═══════ №1: apply_task_changes(op="delete") удаляет закрытую задачу ═══════

async def test_delete_of_a_completed_task_goes_through(monkeypatch, tmp_path):
    """Сквозной цикл: delete по завершённой задаче входит в план и реально
    удаляет её — судим по лентам фейка, а не по тексту."""
    live = {"a1": {"id": "a1", "title": "Открытая", "projectId": "p_in"}}
    completed = {"cc": _closed("cc", "__QA2_TASK__ for complete", status=2)}
    v2 = _wire(monkeypatch, live, tmp_path, completed=completed)

    preview = await s.apply_task_changes("Чищу мусор", [
        {"op": "delete", "task_id": "cc", "title": "__QA2_TASK__ for complete",
         "said": "удали завершённую"}])
    assert "❌ Не вошло" not in preview, preview
    assert "кто-то удалил" not in preview

    out = await s.apply_task_changes("Чищу мусор", manifest_id=_mid(preview),
                                     user_reply="да, удаляй")
    assert "cc" not in v2.completed, "задача обязана исчезнуть из завершённых"
    assert ("delete", ["cc"]) in v2.calls
    assert "__QA2_TASK__ for complete" in out


async def test_delete_of_a_wont_do_task_goes_through(monkeypatch, tmp_path):
    """То же для «Won't do» (status=-1) — фикс обязан покрывать ОБА статуса."""
    live = {}
    completed = {"wd": _closed("wd", "__QA2_TASK__ happy create 2", status=-1)}
    v2 = _wire(monkeypatch, live, tmp_path, completed=completed)

    preview = await s.apply_task_changes("Чищу мусор", [
        {"op": "delete", "task_id": "wd", "title": "__QA2_TASK__ happy create 2",
         "said": "удали отменённую"}])
    assert "❌ Не вошло" not in preview, preview

    await s.apply_task_changes("Чищу мусор", manifest_id=_mid(preview),
                               user_reply="да")
    assert "wd" not in v2.completed


async def test_delete_of_a_closed_task_checks_the_title(monkeypatch, tmp_path):
    """Сверка личности у закрытой задачи НЕ мягче обычной: чужое название —
    отказ строки, ничего не удалено."""
    completed = {"cc": _closed("cc", "Настоящее имя")}
    v2 = _wire(monkeypatch, {}, tmp_path, completed=completed)

    out = await s.apply_task_changes("Чищу", [
        {"op": "delete", "task_id": "cc", "title": "Совсем другое имя",
         "said": "удали"}])
    assert "название не совпало" in out
    assert "cc" in v2.completed and v2.calls == []


# ═══════ №1: не-delete операции над закрытой — честная причина ═════════════

async def test_update_of_a_completed_task_names_the_real_state(
        monkeypatch, tmp_path):
    """update по завершённой: причина отказа называет СОСТОЯНИЕ («завершена»)
    и путь наружу (op="delete") — а не намекает, что задачу кто-то удалил."""
    completed = {"cc": _closed("cc", "Готовая задача", status=2)}
    _wire(monkeypatch, {}, tmp_path, completed=completed)

    out = await s.apply_task_changes("Правлю", [
        {"op": "update", "task_id": "cc", "title": "Готовая задача",
         "changes": {"new_title": "Новое имя"}, "said": "переименуй"}])
    assert "завершена" in out and "Completed" in out
    assert 'op="delete"' in out
    assert "кто-то удалил" not in out


async def test_complete_of_a_wont_do_task_says_wont_do(monkeypatch, tmp_path):
    """Отменённая («Won't do») задача называется отменённой — не «завершена»
    и не «не найдена»."""
    completed = {"wd": _closed("wd", "Брошенная", status=-1)}
    _wire(monkeypatch, {}, tmp_path, completed=completed)

    out = await s.apply_task_changes("Закрываю", [
        {"op": "complete", "task_id": "wd", "title": "Брошенная",
         "said": "закрой"}])
    assert "отменена" in out and "Won't do" in out


async def test_trashed_task_points_to_restore(monkeypatch, tmp_path):
    """Задача в корзине: причина говорит «в корзине» и называет реальный
    путь восстановления (op="restore" этого же инструмента)."""
    trash = {"tt": {"id": "tt", "title": "Удалённая", "projectId": "p_in"}}
    _wire(monkeypatch, {}, tmp_path, trash=trash)

    out = await s.apply_task_changes("Правлю", [
        {"op": "update", "task_id": "tt", "title": "Удалённая",
         "changes": {"new_title": "X"}, "said": "поправь"}])
    assert "КОРЗИНЕ" in out and 'op="restore"' in out


async def test_unknown_id_is_reported_as_not_found_anywhere(
        monkeypatch, tmp_path):
    """id, которого нет НИ В ОДНОЙ ленте: текст больше не гадает «кто-то
    удалил или закрыл вручную?», а честно перечисляет, где искали."""
    _wire(monkeypatch, {}, tmp_path)

    out = await s.apply_task_changes("Чищу", [
        {"op": "delete", "task_id": "ghost", "title": "Призрак",
         "said": "удали"}])
    assert "не найдена ни среди открытых" in out
    assert "кто-то удалил" not in out


# ═══════ №1 + №2: plan_task_deletion видит закрытые и честно считает ═══════

async def test_plan_deletion_includes_the_completed_task(monkeypatch, tmp_path):
    """plan_task_deletion (гейт включён): закрытая задача попадает В ПЛАН с
    пометкой состояния, а не в «Исключены»."""
    live = {"a1": {"id": "a1", "title": "Открытая", "projectId": "p_in"}}
    completed = {"cc": _closed("cc", "__QA2_READ__ to complete")}
    _wire(monkeypatch, live, tmp_path, completed=completed)

    out = await s.plan_task_deletion("Удаляю мусор", [
        {"taskId": "a1", "title": "Открытая", "projectId": "p_in"},
        {"taskId": "cc", "title": "__QA2_READ__ to complete"},
    ])
    assert "### 📋 План удаления — 2" in out, out
    assert "завершена («Completed»)" in out
    assert "Исключены" not in out
    m = s._MANIFESTS[_mid(out)]
    closed_items = [it for it in m["items"] if it.get("closed")]
    assert [it["taskId"] for it in closed_items] == ["cc"]


async def test_killswitch_deletion_denominator_counts_the_request(
        monkeypatch, tmp_path):
    """Гейт выключен, смешанный батч из 3 строк (открытая, завершённая,
    несуществующая): отчёт считает по ЗАПРОШЕННОМУ («Удалено 2/3»), а
    несуществующая строка названа с причиной — не молча потеряна."""
    monkeypatch.setenv(GATE_ENV, "1")
    live = {"a1": {"id": "a1", "title": "Открытая", "projectId": "p_in"}}
    completed = {"cc": _closed("cc", "Завершённая")}
    v2 = _wire(monkeypatch, live, tmp_path, completed=completed)

    def _open_by_id_live(fresh=False):
        return dict(live)
    monkeypatch.setattr(s, "_open_by_id", _open_by_id_live)

    out = await s.plan_task_deletion("Удаляю мусор", [
        {"taskId": "a1", "title": "Открытая", "projectId": "p_in"},
        {"taskId": "cc", "title": "Завершённая"},
        {"taskId": "ghost", "title": "Призрак"},
    ])
    assert "Удалено 2/3" in out, out
    assert "Призрак" in out and "Исключены 1" in out
    assert "cc" not in v2.completed, "завершённая обязана удалиться"
    assert "a1" not in live or ("delete", ["a1", "cc"]) in v2.calls


# ═══════ №4: полностью невалидный батч объясняет причины ═══════════════════

async def test_fully_invalid_deletion_batch_names_every_reason(
        monkeypatch, tmp_path):
    """Все строки невалидны: вместо голого «Ничего не удалено.» — «Плана
    нет» с причиной по КАЖДОЙ строке; манифест не создаётся."""
    live = {"a1": {"id": "a1", "title": "Открытая", "projectId": "p_in"}}
    _wire(monkeypatch, live, tmp_path)
    before = dict(s._MANIFESTS)

    out = await s.plan_task_deletion("Удаляю", [
        {"taskId": "ghost", "title": "Призрак"},
        {"taskId": "a1", "title": "Не то имя"},
    ])
    assert "Ничего не удалено." not in out
    assert "Плана нет" in out
    assert "Призрак" in out and "не среди открытых" in out
    assert "Не то имя" in out
    assert s._MANIFESTS == before, "манифест на пустом плане не создаётся"


async def test_fully_invalid_deletion_batch_with_killswitch_same_answer(
        monkeypatch, tmp_path):
    """То же при ВЫКЛЮЧЕННОМ гейте: пустой план не доходит до исполнителя
    (нечего исполнять), человек получает причины, а не «Ничего не удалено.»"""
    monkeypatch.setenv(GATE_ENV, "1")
    v2 = _wire(monkeypatch, {}, tmp_path)

    out = await s.plan_task_deletion("Удаляю", [
        {"taskId": "ghost", "title": "Призрак"},
    ])
    assert "Ничего не удалено." not in out
    assert "Плана нет" in out and "Призрак" in out
    assert v2.calls == []


# ═══════ №2 (создание): знаменатель по строкам запроса ═════════════════════

def test_create_report_counts_the_requested_rows(monkeypatch):
    """Движок создания: 3 строки, одна с priority=99 → заголовок говорит
    «Создано 2 из 3 строк запроса», а не голое «Создано 2»."""
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})

    class _Off:
        def create_task(self, title, project_id, content=None, start_date=None,
                        due_date=None, priority=0, is_all_day=False,
                        repeat_flag=None, reminders=None):
            return {"id": f"id-{title}", "title": title,
                    "projectId": project_id}

    monkeypatch.setattr(s, "ticktick", _Off())
    out = asyncio.run(s._create_tasks_impl("Создаю", [
        {"title": "Годная-1", "project_id": "p1"},
        {"title": "bad priority", "project_id": "p1", "priority": 99},
        {"title": "Годная-2", "project_id": "p1"},
    ]))
    assert "Создано 2 из 3 строк запроса" in out, out
    assert "неверный приоритет" in out


# ═══════ №7: отчёт исполнения говорит про осиротевших детей ════════════════

async def test_killswitch_delete_report_warns_about_orphans(
        monkeypatch, tmp_path):
    """Гейт выключен, apply_task_changes(op="delete") родителя БЕЗ
    with_subtasks: превью никто не видел, поэтому отчёт исполнения обязан
    сам сказать, сколько детей осталось без родителя."""
    monkeypatch.setenv(GATE_ENV, "1")
    live = {
        "par": {"id": "par", "title": "Родитель", "projectId": "p_in"},
        "kid": {"id": "kid", "title": "Ребёнок", "projectId": "p_in",
                "parentId": "par"},
    }
    _wire(monkeypatch, live, tmp_path)

    out = await s.apply_task_changes("Чищу", [
        {"op": "delete", "task_id": "par", "title": "Родитель",
         "said": "удали"}])
    assert "par" not in live
    assert "останется без родителя" in out or "остаются БЕЗ родителя" in out, out
