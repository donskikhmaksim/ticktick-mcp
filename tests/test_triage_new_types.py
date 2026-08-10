"""Новые типы операций агрегатора — по одному сквозному тесту на тип (1.3.3).

Требование ТЗ 1.3.3, пункт 4 приёмки, дословно: «Каждый проверяет исход
ЧТЕНИЕМ ЖИВОГО СОСТОЯНИЯ, а не текстом ответа». Поэтому здесь нет ни одной
проверки вида «в ответе есть ✅»: живое состояние — обычный dict, который
фейковые клиенты честно мутируют, и утверждения делаются про НЕГО. Текст
ответа проверяется отдельно и только там, где сам текст и есть предмет
требования (например запрет ветки «тип не проверяется автоматически»).

Стенд общий для всех типов и повторяет настоящие каналы в том, что важно:
v2 отвечает 200 с отказами ВНУТРИ тела, привязка/теги/статусы реально
проставляются в живом состоянии, корзина — отдельная лента.
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


def _mid(preview: str) -> str:
    m = re.search(r"Манифест `([0-9a-f]+)`", preview)
    assert m, f"в превью нет id манифеста:\n{preview}"
    return m.group(1)


_NAMES = {"p_in": "Входящие", "p_work": "Работа"}


class _FakeV2:
    """Двойник v2-клиента. Каждый метод РЕАЛЬНО меняет живое состояние —
    иначе независимая сверка агрегатора судила бы по пустоте и любой тест
    проходил бы на неработающем коде."""

    def __init__(self, live, trash=None, tags=None, completed=None):
        self.live = live
        self.trash = trash if trash is not None else {}
        self.completed = completed if completed is not None else {}
        self.account_tags = list(tags or [])
        self.abandoned = {}
        self.calls = []

    def invalidate_cache(self):
        pass

    def get_open_tasks(self):
        return list(self.live.values())

    def get_state(self, force=False):
        return {"tags": list(self.account_tags)}

    # ---- теги ----
    def get_tags(self):
        return list(self.account_tags)

    def create_tag(self, name):
        """Как настоящий клиент: `name` — ключ нижним регистром, `label` —
        написание, которое человек увидит в списке тегов."""
        self.calls.append(("create_tag", name))
        self.account_tags.append({"name": str(name).lower(), "label": name})
        return {}

    # ---- родитель ----
    def set_task_parents(self, rows):
        self.calls.append(("parent", [r["taskId"] for r in rows],
                           rows[0]["parentId"] if rows else None))
        for r in rows:
            if r["taskId"] in self.live:
                self.live[r["taskId"]]["parentId"] = r["parentId"]
        return {}

    def unset_task_parent(self, task_id, parent_id, project_id):
        self.calls.append(("unparent", task_id, parent_id))
        if task_id in self.live:
            self.live[task_id].pop("parentId", None)
        return {}

    # ---- прочее ----
    def batch_update_tasks(self, changes):
        self.calls.append(("update", [c["taskId"] for c in changes]))
        for c in changes:
            t = self.live.setdefault(c["taskId"], {"id": c["taskId"]})
            for k, v in c.items():
                if k != "taskId":
                    t[k] = v
        return {}

    def batch_complete_tasks(self, ids):
        self.calls.append(("complete", list(ids)))
        for tid in ids:
            self.live.pop(tid, None)
        return {}

    def duplicate_task(self, task_id):
        """Копия — новый объект в ТОМ ЖЕ проекте. Завершённый оригинал даёт
        завершённую копию: среди открытых её нет никогда."""
        src = self.live.get(task_id) or self.completed.get(task_id) or {}
        cid = f"copy-{task_id}"
        copy = {"id": cid, "title": src.get("title"),
                "projectId": src.get("projectId")}
        self.calls.append(("duplicate", task_id, cid))
        if task_id in self.completed:
            self.completed[cid] = copy
        else:
            self.live[cid] = copy
        return dict(copy)

    def find_task_any_state(self, task_id):
        if task_id in self.live:
            return self.live[task_id], "open"
        if task_id in self.completed:
            return self.completed[task_id], "completed"
        if task_id in self.trash:
            return self.trash[task_id], "trash"
        return None, None

    def abandon_task(self, task_id):
        """«Не буду делать» — задача уходит из открытых и получает статус -1
        (именно так её помечает TickTick), а не удаляется."""
        self.calls.append(("abandon", task_id))
        gone = self.live.pop(task_id, None)
        if gone is not None:
            gone["status"] = -1
            self.abandoned[task_id] = gone
        return {}

    def batch_delete_tasks(self, rows):
        self.calls.append(("delete", [r["taskId"] for r in rows]))
        for r in rows:
            self.live.pop(r["taskId"], None)
        return {}


class _FakeOfficial:
    """Официальный Open API — второй канал сервера. Нужен там, где ядро ходит
    именно в него (закрытие задач, точечное чтение)."""

    def __init__(self, live):
        self.live = live
        self.calls = []

    def complete_task(self, project_id, task_id):
        self.calls.append(("complete", task_id))
        self.live.pop(task_id, None)
        return {"id": task_id}

    def update_task(self, task_id, project_id, title=None, content=None,
                    start_date=None, due_date=None, priority=None,
                    repeat_flag=None, reminders=None):
        self.calls.append(("update", task_id))
        t = self.live.setdefault(task_id, {"id": task_id})
        if title is not None:
            t["title"] = title
        if priority is not None:
            t["priority"] = priority
        return {"id": task_id}


def _wire(monkeypatch, live, tmp_path, names=None, trash=None, tags=None,
          completed=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(names or _NAMES))
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2 = _FakeV2(live, trash=trash, tags=tags, completed=completed)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", _FakeOfficial(live))
    return v2


async def _run(ops, summary="Разбираю"):
    """call #1 → call #2 одним помощником: предмет каждого теста — исход, а
    не механика гейта (она закреплена в tests/test_manual_triage.py)."""
    preview = await s.manual_triage(summary, ops)
    assert "🛑" not in preview.splitlines()[0], preview
    out = await s.manual_triage(summary, manifest_id=_mid(preview),
                                user_reply="да, давай")
    return preview, out


# ═══════════════════════════════ parent ════════════════════════════════════

async def test_parent_attaches_and_verifies(monkeypatch, tmp_path):
    """Вложение существующей задачи под существующего родителя: судим по
    ЖИВОМУ `parentId`, а не по строке отчёта."""
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p_in"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p_in"},
        "zz": {"id": "zz", "title": "Посторонняя", "projectId": "p_in"},
    }
    v2 = _wire(monkeypatch, live, tmp_path)

    preview, out = await _run([
        {"op": "parent", "task_id": "kid", "title": "Позвонить в банк",
         "to_task_id": "par", "to_title": "Ипотека",
         "said": "это часть ипотеки"}])

    assert live["kid"]["parentId"] == "par"
    assert "parentId" not in live["zz"], "чужая задача не тронута"
    assert ("parent", ["kid"], "par") in v2.calls
    # Превью называет родителя ЖИВЫМ ИМЕНЕМ, а не голым id.
    assert "«Ипотека»" in preview and "par" not in _plan_lines(preview)[0]
    assert "✅ Выполнено 1 из 1" in out


async def test_parent_under_a_dead_parent_never_reaches_the_plan(
        monkeypatch, tmp_path):
    """Родитель мёртв → строка не входит в план вовсе: «вложение под мёртвого
    родителя осиротит задачу»."""
    live = {"kid": {"id": "kid", "title": "Позвонить в банк",
                    "projectId": "p_in"}}
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "parent", "task_id": "kid", "title": "Позвонить в банк",
         "to_task_id": "ghost", "to_title": "Ипотека", "said": "часть ипотеки"}])

    assert "🛑" in out and "осиротит" in out
    assert v2.calls == []
    assert "parentId" not in live["kid"]


async def test_parent_across_projects_never_reaches_the_plan(
        monkeypatch, tmp_path):
    """TickTick не вкладывает через проекты. Раньше такая строка дошла бы до
    исполнителя и вернулась отказом уже ПОСЛЕ «да»."""
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p_in"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p_work"},
    }
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "parent", "task_id": "kid", "title": "Позвонить в банк",
         "to_task_id": "par", "to_title": "Ипотека", "said": "часть ипотеки"}])

    assert "🛑" in out and "через проекты" in out
    assert v2.calls == [] and "parentId" not in live["kid"]


async def test_parent_cycle_never_reaches_the_plan(monkeypatch, tmp_path):
    """Вложить задачу под собственного потомка — порвать дерево."""
    live = {
        "top": {"id": "top", "title": "Ипотека", "projectId": "p_in"},
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p_in",
                "parentId": "top"},
    }
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "parent", "task_id": "top", "title": "Ипотека",
         "to_task_id": "kid", "to_title": "Позвонить в банк",
         "said": "вложи наоборот"}])

    assert "🛑" in out and "цикл" in out
    assert v2.calls == [] and live["top"].get("parentId") is None


async def test_parent_without_parent_title_is_refused_outright(
        monkeypatch, tmp_path):
    """`to_task_id` без `to_title` — id, не подтверждённый ничем: задача уехала
    бы под тот объект, на который id указывает СЕЙЧАС."""
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p_in"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p_in"},
    }
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "parent", "task_id": "kid", "title": "Позвонить в банк",
         "to_task_id": "par", "said": "часть ипотеки"}])

    assert "🛑" in out and "to_title" in out
    assert s._MANIFESTS == {}, "отказ не имеет права строить план"
    assert v2.calls == []


async def test_parent_renamed_between_plan_and_yes_is_skipped(
        monkeypatch, tmp_path):
    """Дрейф РОДИТЕЛЯ между планом и «да» — операция не исполняется."""
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p_in"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p_in"},
    }
    v2 = _wire(monkeypatch, live, tmp_path)
    preview = await s.manual_triage("Разбираю", [
        {"op": "parent", "task_id": "kid", "title": "Позвонить в банк",
         "to_task_id": "par", "to_title": "Ипотека", "said": "часть ипотеки"}])

    live["par"]["title"] = "Ипотека (закрыта)"
    out = await s.manual_triage("Разбираю", manifest_id=_mid(preview),
                                user_reply="да")

    assert "parentId" not in live["kid"]
    assert v2.calls == []
    assert "переименовали" in out


# ══════════════════════════════ unparent ══════════════════════════════════

async def test_unparent_detaches_and_verifies(monkeypatch, tmp_path):
    """Отцепление подзадачи: судим по ЖИВОМУ `parentId` — его не должно
    остаться, а сама задача обязана остаться среди открытых."""
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p_in",
                "parentId": "par"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p_in"},
        "sib": {"id": "sib", "title": "Собрать документы", "projectId": "p_in",
                "parentId": "par"},
    }
    v2 = _wire(monkeypatch, live, tmp_path)

    preview, out = await _run([
        {"op": "unparent", "task_id": "kid", "title": "Позвонить в банк",
         "said": "это не про ипотеку, вынеси отдельно"}])

    assert "parentId" not in live["kid"], "родитель обязан быть снят"
    assert "kid" in live, "отцепление не удаляет задачу"
    assert live["sib"]["parentId"] == "par", "соседняя подзадача не тронута"
    assert ("unparent", "kid", "par") in v2.calls
    assert "«Ипотека»" in preview
    assert "✅ Выполнено 1 из 1" in out


async def test_unparent_of_a_root_task_never_reaches_the_plan(
        monkeypatch, tmp_path):
    """У задачи нет родителя → строка не входит в план: «и так не подзадача».
    Без этой ветки отчёт отрапортовал бы про операцию, которой не было."""
    live = {"solo": {"id": "solo", "title": "Позвонить в банк",
                     "projectId": "p_in"}}
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "unparent", "task_id": "solo", "title": "Позвонить в банк",
         "said": "вынеси отдельно"}])

    assert "🛑" in out and "и так не подзадача" in out
    assert v2.calls == []


async def test_unparent_detached_between_plan_and_yes_is_skipped(
        monkeypatch, tmp_path):
    """Родителя сняли руками между планом и «да» — это НЕ успех операции."""
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p_in",
                "parentId": "par"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p_in"},
    }
    v2 = _wire(monkeypatch, live, tmp_path)
    preview = await s.manual_triage("Разбираю", [
        {"op": "unparent", "task_id": "kid", "title": "Позвонить в банк",
         "said": "вынеси отдельно"}])

    live["kid"].pop("parentId")
    out = await s.manual_triage("Разбираю", manifest_id=_mid(preview),
                                user_reply="да")

    assert v2.calls == [], "второй раз отцеплять нечего — канал не дёргается"
    assert "уже не подзадача" in out
    assert "✅ Выполнено" not in out


async def test_unparent_rejects_fields_of_other_types(monkeypatch, tmp_path):
    """Поля чужого типа отвергаются, а не игнорируются молча: превью
    показывало бы одно, а исполнялось бы другое."""
    live = {
        "kid": {"id": "kid", "title": "Позвонить в банк", "projectId": "p_in",
                "parentId": "par"},
        "par": {"id": "par", "title": "Ипотека", "projectId": "p_in"},
    }
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "unparent", "task_id": "kid", "title": "Позвонить в банк",
         "to_project_id": "p_work", "said": "вынеси отдельно"}])

    assert "🛑" in out and "to_project_id" in out
    assert s._MANIFESTS == {} and v2.calls == []
    assert live["kid"]["parentId"] == "par"


# ════════════════════════════════ tags ════════════════════════════════════

async def test_tags_replaces_set_and_registers_tag(monkeypatch, tmp_path):
    """Набор тегов ЗАМЕНЯЕТСЯ целиком, а незнакомый тег заводится в аккаунте.
    Оба факта проверяются по живому состоянию: `tags` на задаче и список
    тегов аккаунта."""
    live = {
        "t1": {"id": "t1", "title": "Позвонить в банк", "projectId": "p_in",
               "tags": ["старый"]},
        "zz": {"id": "zz", "title": "Посторонняя", "projectId": "p_in",
               "tags": ["старый"]},
    }
    v2 = _wire(monkeypatch, live, tmp_path,
               tags=[{"name": "старый", "label": "старый"}])

    preview, out = await _run([
        {"op": "tags", "task_id": "t1", "title": "Позвонить в банк",
         "changes": {"tags": ["Ипотека", "звонки"]},
         "said": "пометь ипотекой и звонками"}])

    assert set(live["t1"]["tags"]) == {"ипотека", "звонки"}, \
        "набор заменяется целиком, старый тег обязан уйти"
    assert live["zz"]["tags"] == ["старый"], "чужая задача не тронута"
    # Незнакомый тег ЗАВЕДЁН в аккаунте — иначе он тег-сирота.
    assert {t["name"] for t in v2.account_tags} >= {"ипотека", "звонки"}
    assert ("create_tag", "Ипотека") in v2.calls
    # Превью говорит «было → станет» и называет, что придётся завести.
    assert "«старый» →" in preview and "будут заведены" in preview
    assert "✅ Выполнено 1 из 1" in out


async def test_tags_on_update_is_refused_and_names_the_replacement(
        monkeypatch, tmp_path):
    """Дыра тега-сироты закрыта запретом, а не тихой переадресацией: `update`
    с тегами и `tags` — разные строки превью, и подменять одно другим ПОСЛЕ
    подтверждения человека нельзя."""
    live = {"t1": {"id": "t1", "title": "Позвонить в банк",
                   "projectId": "p_in", "tags": []}}
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "update", "task_id": "t1", "title": "Позвонить в банк",
         "changes": {"tags": ["ипотека"]}, "said": "пометь ипотекой"}])

    assert "🛑" in out and 'op="tags"' in out
    assert s._MANIFESTS == {} and v2.calls == []
    assert live["t1"]["tags"] == []


async def test_tags_rejects_foreign_change_keys(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Позвонить в банк",
                   "projectId": "p_in", "tags": []}}
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "tags", "task_id": "t1", "title": "Позвонить в банк",
         "changes": {"tags": ["ипотека"], "priority": 5},
         "said": "пометь и подними приоритет"}])

    assert "🛑" in out and "priority" in out
    assert s._MANIFESTS == {} and v2.calls == []


async def test_tags_refuses_non_string_list(monkeypatch, tmp_path):
    """Типизация переехала на собственный тип: раньше её держал общий
    `_triage_change_refusal` у `update`, куда теги больше не ходят."""
    live = {"t1": {"id": "t1", "title": "Позвонить в банк",
                   "projectId": "p_in", "tags": []}}
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "tags", "task_id": "t1", "title": "Позвонить в банк",
         "changes": {"tags": [1, 2]}, "said": "перетегируй"}])

    assert "🛑" in out and "списком СТРОК" in out
    assert s._MANIFESTS == {} and v2.calls == []


async def test_tags_plan_survives_unreadable_account_tag_list(
        monkeypatch, tmp_path):
    """Список тегов аккаунта читается BEST-EFFORT: он нужен только чтобы
    сказать, какие теги придётся завести. Его недоступность план НЕ роняет, а
    превью честно говорит «сказать не могу» вместо тишины."""
    live = {"t1": {"id": "t1", "title": "Позвонить в банк",
                   "projectId": "p_in", "tags": []}}
    v2 = _wire(monkeypatch, live, tmp_path)

    def _boom():
        raise RuntimeError("v2 недоступен")

    monkeypatch.setattr(v2, "get_tags", _boom)

    preview = await s.manual_triage("Разбираю", [
        {"op": "tags", "task_id": "t1", "title": "Позвонить в банк",
         "changes": {"tags": ["ипотека"]}, "said": "пометь ипотекой"}])

    assert "Манифест" in preview, "план обязан строиться"
    assert "сказать не могу" in preview


# ══════════════════════════════ abandon ═══════════════════════════════════

async def test_abandon_marks_wont_do(monkeypatch, tmp_path):
    """Задача уходит из открытых со статусом «не буду делать» — и это видно
    по ЖИВОМУ состоянию, а не по строке отчёта.

    Второй предмет теста — способ подделки №1 из ТЗ: тип, добавленный в
    список и завёрнутый в существующие ветки, падает в дефолтную ветку
    `_verify_item` «тип не проверяется автоматически», и отчёт печатает
    «записана в журнал», что владелец читает как «сделано». Поэтому строка
    проверяется явно."""
    live = {
        "a1": {"id": "a1", "title": "Учить испанский", "projectId": "p_in"},
        "zz": {"id": "zz", "title": "Посторонняя", "projectId": "p_in"},
    }
    v2 = _wire(monkeypatch, live, tmp_path)

    preview, out = await _run([
        {"op": "abandon", "task_id": "a1", "title": "Учить испанский",
         "said": "не буду я этим заниматься"}])

    assert "a1" not in live, "заброшенная задача уходит из открытых"
    assert v2.abandoned["a1"]["status"] == -1, "статус «не буду делать»"
    assert ("abandon", "a1") in v2.calls
    assert "zz" in live, "чужая задача не тронута"
    # ГЛАВНОЕ: своя ветка проверки, а не дефолтная.
    assert "не проверяется автоматически" not in out, out
    assert "не буду делать" in out.lower()
    assert "✅ Выполнено 1 из 1" in out
    # …и в превью глагол свой, не «закрыть».
    assert "🚫 Не буду делать:" in preview and "✅ Закрыть" not in preview


async def test_abandon_is_not_reported_as_completed(monkeypatch, tmp_path):
    """«Закрыть» и «не буду делать» — разные решения человека, и отчёт не
    имеет права их смешивать: у abandon свой глагол вердикта."""
    live = {"a1": {"id": "a1", "title": "Учить испанский", "projectId": "p_in"},
            "c1": {"id": "c1", "title": "Оплатить интернет",
                   "projectId": "p_in"}}
    _wire(monkeypatch, live, tmp_path)

    _preview, out = await _run([
        {"op": "abandon", "task_id": "a1", "title": "Учить испанский",
         "said": "не буду"},
        {"op": "complete", "task_id": "c1", "title": "Оплатить интернет",
         "said": "оплатил"}])

    verdicts = [ln for ln in out.splitlines() if ln.startswith("- ")]
    wont = [ln for ln in verdicts if "Учить испанский" in ln]
    done = [ln for ln in verdicts if "Оплатить интернет" in ln]
    assert wont and "не буду делать" in wont[0]
    assert done and "закрыта" in done[0]
    assert "не буду делать" not in done[0]


async def test_abandon_rejects_changes_and_destinations(monkeypatch, tmp_path):
    live = {"a1": {"id": "a1", "title": "Учить испанский", "projectId": "p_in"}}
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "abandon", "task_id": "a1", "title": "Учить испанский",
         "to_project_id": "p_work", "said": "не буду"}])

    assert "🛑" in out and "to_project_id" in out
    assert s._MANIFESTS == {} and v2.calls == []
    assert "a1" in live


async def test_abandon_warns_about_orphaned_children(monkeypatch, tmp_path):
    """Дети НЕ трогаются (план не имеет права разрастаться сверх названного),
    но человек обязан видеть, что после «да» они осиротеют."""
    live = {
        "a1": {"id": "a1", "title": "Учить испанский", "projectId": "p_in"},
        "k1": {"id": "k1", "title": "Купить учебник", "projectId": "p_in",
               "parentId": "a1"},
    }
    _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю", [
        {"op": "abandon", "task_id": "a1", "title": "Учить испанский",
         "said": "не буду"}])

    assert "останется без родителя" in preview


# ═════════════════════════════ duplicate ══════════════════════════════════

async def test_duplicate_creates_copy_in_same_project(monkeypatch, tmp_path):
    """Копия существует, лежит в ТОМ ЖЕ проекте и названа как оригинал —
    судим по живому состоянию, не по строке ответа."""
    live = {"t1": {"id": "t1", "title": "Чек-лист переезда",
                   "projectId": "p_work"}}
    v2 = _wire(monkeypatch, live, tmp_path)

    preview, out = await _run([
        {"op": "duplicate", "task_id": "t1", "title": "Чек-лист переезда",
         "said": "сделай копию, буду править"}])

    copies = [t for tid, t in live.items() if tid != "t1"]
    assert len(copies) == 1, "ровно одна копия"
    assert copies[0]["title"] == "Чек-лист переезда"
    assert copies[0]["projectId"] == "p_work", "тот же проект"
    assert live["t1"]["title"] == "Чек-лист переезда", "оригинал не тронут"
    assert "не проверяется автоматически" not in out, out
    assert "✅ Выполнено 1 из 1" in out


async def test_duplicate_of_a_completed_task_is_legitimate(monkeypatch, tmp_path):
    """Дублирование ЗАВЕРШЁННОЙ задачи как шаблона — законный сценарий.
    Общая сверка ищет только среди открытых и выбросила бы эту строку ровно
    на том основании, ради которого операцию и затевали."""
    live = {}
    done = {"t1": {"id": "t1", "title": "Чек-лист переезда",
                   "projectId": "p_work", "status": 2}}
    v2 = _wire(monkeypatch, live, tmp_path, completed=done)

    preview, out = await _run([
        {"op": "duplicate", "task_id": "t1", "title": "Чек-лист переезда",
         "said": "сделай из неё шаблон"}])

    assert "copy-t1" in v2.completed, "копия завершённой наследует её статус"
    assert v2.completed["copy-t1"]["projectId"] == "p_work"
    # Копии нет среди открытых — и это НЕ провал: вердикт обязан это назвать.
    assert "не проверяется автоматически" not in out
    assert "✅ Выполнено 1 из 1" in out
    assert "копия завершённой" in out


async def test_duplicate_of_a_vanished_task_never_reaches_the_plan(
        monkeypatch, tmp_path):
    """Исчезнувший из ОБЕИХ лент оригинал — отказ, а не «дублируем что
    найдётся»."""
    live = {}
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "duplicate", "task_id": "ghost", "title": "Чек-лист переезда",
         "said": "сделай копию"}])

    assert "🛑" in out and "ни среди завершённых" in out
    assert v2.calls == []


async def test_duplicate_from_trash_is_refused(monkeypatch, tmp_path):
    """«Дубликат удалённого» почти всегда значит, что человек хотел возврат."""
    live = {}
    v2 = _wire(monkeypatch, live, tmp_path,
               trash={"t1": {"id": "t1", "title": "Чек-лист переезда",
                             "projectId": "p_work"}})

    out = await s.manual_triage("Разбираю", [
        {"op": "duplicate", "task_id": "t1", "title": "Чек-лист переезда",
         "said": "сделай копию"}])

    assert "🛑" in out and "КОРЗИН" in out.upper()
    assert v2.calls == []


async def test_duplicate_rejects_changes(monkeypatch, tmp_path):
    """Правка копии — отдельная операция по НЕЙ, а не поле здесь: иначе
    changes молча не применились бы, а отчёт сказал бы «выполнено»."""
    live = {"t1": {"id": "t1", "title": "Чек-лист переезда",
                   "projectId": "p_work"}}
    v2 = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "duplicate", "task_id": "t1", "title": "Чек-лист переезда",
         "changes": {"new_title": "Копия"}, "said": "скопируй и переименуй"}])

    assert "🛑" in out and "changes" in out
    assert s._MANIFESTS == {} and v2.calls == []


def _plan_lines(preview: str):
    return [ln for ln in preview.splitlines() if re.match(r"^\d+\. ", ln)]
