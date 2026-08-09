"""Создание задач: отказ ВНУТРИ успешного ответа обязан быть виден.

Класс дефекта (общий для всего сервера). Внутренний канал TickTick
(`/batch/*`) отвечает HTTP 200 и кладёт список непринятых элементов ВНУТРЬ
ответа, в карту `id2error`. Готовая проверка для этого — `id2error_failures`
в ticktick_v2_client.py. Пока её нет на конкретном вызове, отказ не даёт ни
исключения, ни строки в отчёте, и владелец читает «✓ создано» там, где
ничего не произошло.

Что закрывают тесты ниже:

  д5 — ПРИВЯЗКА. Два вызова `/batch/taskParent` в `_create_tasks_impl`
       (связи подзадач и привязка корня под уже существующего родителя)
       отправлялись, а ответ выбрасывался. Подзадачи при этом СУЩЕСТВУЮТ —
       просто лежат корневыми, — а пост-проверка смотрела только на
       существование, поэтому дерева не было ни в отчёте, ни в журнале.

  д6 — ТЕГИ И ИСПОЛНИТЕЛЬ. Простановка тегов при создании звала голый
       `ticktick_v2.set_task_tags`: без чтения отказов, без живой сверки и
       без регистрации тега в аккаунте (то есть с риском «тега-сироты» —
       тег висит на задаче, но не виден в list_tags и не удаляется
       delete_tag). Назначение исполнителя — та же дыра, тот же адрес.

Приём тестов: двойник канала ЧЕСТНЫЙ — отказ в `id2error` означает, что
изменение НЕ применилось к живому состоянию, ровно как у настоящего API.
Поэтому тесты, где живое состояние читается, краснели бы и от поломки
пост-проверки тоже; чтобы каждая проверка была прижата ОТДЕЛЬНО, тесты на
чтение ответа канала ставят живую перепроверку в положение «прочитать не
удалось» — единственным источником правды остаётся ответ канала.
"""
import json

import ticktick_mcp.src.server as s


class _FakeV2:
    """Канал v2: проекты, открытые задачи, теги — поверх одного изменяемого
    словаря, в который пишет и официальный клиент при создании.

    `reject` — множество операций, которые канал ОТКЛОНЯЕТ (кладёт id2error и
    ничего не применяет): "parent" (обе привязки), "tags", "assignee".
    `blind_after_write` — после первой записи живое чтение открытых задач
    падает: имитация «канал ответил, перепроверить не удалось».
    `reject_create_titles` — названия задач, которые канал НЕ создаёт: кладёт
    их в id2error и не пишет в живое состояние (ровно так /batch/task и
    отвечает на частично непринятое дерево)."""

    def __init__(self, projects, live, tags=None, reject=(), blind_after_write=False,
                 drop_parent=False, drop_tags=False, inbox_id="inbox1",
                 reject_create_titles=()):
        self.projects = projects
        self.live = live
        self.tags = tags if tags is not None else []
        self.reject = set(reject)
        self.blind_after_write = blind_after_write
        # «Молчаливая потеря»: канал НЕ сообщает об отказе, но изменение не
        # доезжает — ловится только живой пере-проверкой.
        self.drop_parent = drop_parent
        self.drop_tags = drop_tags
        self.inbox_id = inbox_id
        self.reject_create_titles = set(reject_create_titles)
        self.calls = []
        self._wrote = False

    # ---- чтение ----------------------------------------------------------
    def invalidate_cache(self):
        pass

    def get_state(self, force=False):
        return {"projectProfiles": self.projects, "inboxId": self.inbox_id,
                "tags": self.tags}

    def get_open_tasks(self):
        if self.blind_after_write and self._wrote:
            raise RuntimeError("live read unavailable")
        return list(self.live.values())

    def get_tags(self):
        return self.tags

    # ---- запись ----------------------------------------------------------
    def batch_create_tasks(self, tasks):
        self.calls.append(("create", [t["id"] for t in tasks]))
        errs = {}
        for t in tasks:
            if t.get("title") in self.reject_create_titles:
                errs[t["id"]] = "quota exceeded"
                continue
            self.live[t["id"]] = {"id": t["id"], "title": t.get("title"),
                                  "projectId": t.get("projectId"), "tags": []}
        self._wrote = True
        return {"id2error": errs} if errs else {}

    def _request(self, method, path, json=None):
        rows = json or []
        self.calls.append((path, rows))
        self._wrote = True
        if path == "/batch/taskParent":
            return self._link(rows)
        return {}

    def batch_set_task_parent(self, task_ids, parent_id, project_id):
        rows = [{"parentId": parent_id, "taskId": tid, "projectId": project_id}
                for tid in task_ids]
        self.calls.append(("/batch/taskParent", rows))
        self._wrote = True
        return self._link(rows)

    def _link(self, rows):
        if "parent" in self.reject:
            return {"id2error": {r["taskId"]: "no permission for this parent"
                                 for r in rows}}
        if not self.drop_parent:
            for r in rows:
                if r["taskId"] in self.live:
                    self.live[r["taskId"]]["parentId"] = r["parentId"]
        return {}

    def batch_update_tasks(self, changes):
        self.calls.append(("update", changes))
        self._wrote = True
        errs = {}
        for c in changes:
            if "tags" in c and "tags" in self.reject:
                errs[c["taskId"]] = "tag write refused"
                continue
            if "assignee" in c and "assignee" in self.reject:
                errs[c["taskId"]] = "assignee not a project member"
                continue
            t = self.live.setdefault(c["taskId"], {"id": c["taskId"]})
            if "tags" in c and not self.drop_tags:
                t["tags"] = c["tags"]
            if "assignee" in c:
                t["assignee"] = c["assignee"]
        return {"id2error": errs} if errs else {}

    def create_tag(self, name, color=None):
        # Как настоящий клиент: понижает регистр ключа, решётку срезает
        # вызывающий.
        self.calls.append(("create_tag", name))
        self.tags.append({"name": name.lower(), "label": name, "color": color})
        return {}

    def set_task_column(self, task_id, column_id):
        return {}


class _FakeOfficial:
    """Официальный API создания: кладёт задачу в общий словарь живых задач."""

    def __init__(self, live):
        self.live = live
        self.create_calls = []
        self._n = 0

    def create_task(self, title, project_id, content=None, start_date=None,
                    due_date=None, priority=0, is_all_day=False,
                    repeat_flag=None, reminders=None):
        self._n += 1
        tid = f"new{self._n}"
        self.create_calls.append({"title": title, "project_id": project_id})
        self.live[tid] = {"id": tid, "title": title, "projectId": project_id,
                          "tags": []}
        return {"id": tid}

    def update_task(self, task_id, project_id, title=None, content=None,
                    start_date=None, due_date=None, priority=None,
                    repeat_flag=None, reminders=None):
        t = self.live.setdefault(task_id, {"id": task_id})
        if title is not None:
            t["title"] = title
        return dict(t)


_PROJECTS = [{"id": "pA", "name": "Работа"}]


def _wire(monkeypatch, live=None, **kw):
    live = {} if live is None else live
    v2 = _FakeV2(_PROJECTS, live, **kw)
    official = _FakeOfficial(live)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", official)
    return v2, official


# ===========================================================================
# д5. Подзадачи: отказ по связи внутри 200 назван провалом ПРИВЯЗКИ
# ===========================================================================

async def test_subtask_link_rejection_inside_200_is_reported(monkeypatch):
    """Канал принял подзадачи и ОТКЛОНИЛ связи (id2error). Живая
    перепроверка недоступна — значит поймать это может только чтение ответа
    канала, и отчёт обязан назвать провал привязки."""
    v2, _ = _wire(monkeypatch, reject={"parent"}, blind_after_write=True)

    result = await s._create_tasks_impl("тест", [
        {"title": "Ремонт", "project_id": "pA",
         "subtasks": ["Купить краску", "Позвать мастера"]}])

    assert "связи родитель-подзадача не применились" in result
    assert "no permission for this parent" in result
    assert "НЕ" in result and "вложены" in result


async def test_subtasks_that_exist_but_are_not_nested_are_reported(monkeypatch):
    """Канал промолчал (успех без id2error), но привязка не доехала. Задачи
    существуют — прежняя пост-проверка смотрела только на это и молчала."""
    live = {}
    _wire(monkeypatch, live, drop_parent=True)

    result = await s._create_tasks_impl("тест", [
        {"title": "Ремонт", "project_id": "pA", "subtasks": ["Купить краску"]}])

    assert "НЕ вложены под родителя" in result
    assert "Купить краску" in result


async def test_nested_tree_link_rejection_is_reported(monkeypatch):
    """Та же гарантия для ветки дерева (вложенные подзадачи словарями) —
    ветка, где проверка стояла с самого начала: она не должна пропасть."""
    _wire(monkeypatch, reject={"parent"}, blind_after_write=True)

    result = await s._create_tasks_impl("тест", [
        {"title": "Ремонт", "project_id": "pA",
         "subtasks": [{"title": "Кухня", "subtasks": ["Плитка"]}]}])

    assert "связи родитель-подзадача не применились" in result


# ===========================================================================
# д5. Привязка СОЗДАВАЕМОЙ задачи под уже существующего родителя
# ===========================================================================

async def test_parent_link_rejection_for_new_task_is_reported(monkeypatch):
    """Канал отклонил привязку к существующему родителю. Раньше строка
    отчёта была чистым «✓ создано», а задача лежала корневой."""
    live = {"root1": {"id": "root1", "title": "Дом", "projectId": "pA"}}
    _wire(monkeypatch, live, reject={"parent"})

    result = await s._create_tasks_impl("тест", [
        {"title": "Покрасить стену", "project_id": "pA", "parent_id": "root1"}])

    assert "привязка к родителю НЕ применилась" in result
    assert "no permission for this parent" in result


async def test_parent_link_silently_lost_is_caught_by_post_verify(monkeypatch):
    """Канал промолчал, привязка не доехала — ловит блок проверки после
    операции (до правки привязки в нём не было вовсе)."""
    live = {"root1": {"id": "root1", "title": "Дом", "projectId": "pA"}}
    _wire(monkeypatch, live, drop_parent=True)

    result = await s._create_tasks_impl("тест", [
        {"title": "Покрасить стену", "project_id": "pA", "parent_id": "root1"}])

    assert "НЕ вложена под родителя" in result
    assert "Покрасить стену" in result


# ===========================================================================
# д5. Журнал и независимый отчёт знают про привязку и про подзадачи
# ===========================================================================

async def test_journal_records_parent_and_report_catches_broken_tree(
        monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    live = {}
    _wire(monkeypatch, live)

    result = await s._create_tasks_impl("тест", [
        {"title": "Ремонт", "project_id": "pA", "subtasks": ["Купить краску"]}])
    assert "operation_report" in result

    rec = json.loads((tmp_path / "deletion_journal.jsonl")
                     .read_text(encoding="utf-8").strip())
    assert rec["op"] == "create"
    subs = [i for i in rec["items"] if i["title"] == "Купить краску"]
    assert subs, f"подзадача не попала в журнал: {rec['items']}"
    parent_id = subs[0]["expect"]["parentId"]
    assert parent_id and parent_id in live

    # Независимая перепроверка ловит развалившееся дерево, даже если сам
    # инструмент этого не заметил (например задачу отцепили позже).
    live[subs[0]["taskId"]].pop("parentId")
    report = s._build_operation_report(rec["record"])
    assert "НЕ вложена под" in report
    assert "Купить краску" in report


async def test_journal_records_parent_of_a_task_created_under_existing_one(
        monkeypatch, tmp_path):
    """Вторая половина того же: КОРНЕВАЯ создаваемая задача, вложенная под
    уже существующего родителя. Её ожидаемый parentId — отдельная ветка
    записи в журнал, и без него независимый отчёт снова слеп."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    live = {"root1": {"id": "root1", "title": "Дом", "projectId": "pA"}}
    _wire(monkeypatch, live)

    await s._create_tasks_impl("тест", [
        {"title": "Покрасить стену", "project_id": "pA", "parent_id": "root1"}])

    rec = json.loads((tmp_path / "deletion_journal.jsonl")
                     .read_text(encoding="utf-8").strip())
    item = next(i for i in rec["items"] if i["title"] == "Покрасить стену")
    assert item["expect"]["parentId"] == "root1"

    live[item["taskId"]].pop("parentId")
    report = s._build_operation_report(rec["record"])
    assert "НЕ вложена под «Дом»" in report


# ===========================================================================
# д6. Теги при создании
# ===========================================================================

async def test_tag_rejection_at_creation_is_reported(monkeypatch):
    v2, _ = _wire(monkeypatch, reject={"tags"})

    result = await s._create_tasks_impl("тест", [
        {"title": "Оплатить счёт", "project_id": "pA", "tags": ["дом"]}])

    # Именно СЛОВА КАНАЛА — их неоткуда взять, кроме как из id2error.
    assert "tag write refused" in result
    assert "теги НЕ применились" in result


async def test_tag_silently_lost_at_creation_is_reported(monkeypatch):
    """Канал промолчал, тег не доехал — ловит живая сверка «что просили =
    что стало»."""
    _wire(monkeypatch, drop_tags=True)

    result = await s._create_tasks_impl("тест", [
        {"title": "Оплатить счёт", "project_id": "pA", "tags": ["дом"]}])

    assert "теги НЕ применились" in result


async def test_new_tag_at_creation_is_registered_in_the_account(monkeypatch):
    """Защита от «тега-сироты»: новый тег заводится в аккаунте ДО записи на
    задачу — тем же путём, что и в отдельной команде простановки тегов."""
    v2, _ = _wire(monkeypatch)

    result = await s._create_tasks_impl("тест", [
        {"title": "Оплатить счёт", "project_id": "pA", "tags": ["#Дом"]}])

    kinds = [c[0] for c in v2.calls]
    assert kinds.index("create_tag") < kinds.index("update")
    assert [t["name"] for t in v2.tags] == ["дом"]
    assert "теги проставлены (проверено)" in result
    assert "заведены в аккаунте" in result


async def test_tag_write_at_creation_is_normalised_by_the_server(monkeypatch):
    """Решётка и регистр снимаются СЕРВЕРОМ (двойник этого не делает) —
    иначе на задачу уехал бы тег-фантом «#Дом»."""
    v2, _ = _wire(monkeypatch)

    await s._create_tasks_impl("тест", [
        {"title": "Оплатить счёт", "project_id": "pA", "tags": ["#Дом"]}])

    written = [c[1] for c in v2.calls if c[0] == "update"]
    assert written and written[0][0]["tags"] == ["дом"], written


# ===========================================================================
# д6. Исполнитель — та же дыра рядом, оба адреса
# ===========================================================================

async def test_assignee_rejection_at_creation_is_reported(monkeypatch):
    _wire(monkeypatch, reject={"assignee"})

    result = await s._create_tasks_impl("тест", [
        {"title": "Согласовать смету", "project_id": "pA", "assignee": 42}])

    assert "исполнитель НЕ назначен" in result
    assert "assignee not a project member" in result


async def test_assignee_rejection_at_update_is_reported(monkeypatch):
    live = {"t1": {"id": "t1", "title": "Согласовать смету", "projectId": "pA"}}
    _wire(monkeypatch, live, reject={"assignee"})

    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "title": "Согласовать смету", "projectId": "pA",
         "assignee": 42}])

    assert "исполнитель НЕ назначен" in result
    assert "assignee not a project member" in result


# ===========================================================================
# Д11. Счёт в главной строке — по СОЗДАННОМУ, а не по запрошенному
# ===========================================================================

async def test_a_partially_rejected_tree_is_not_counted_as_whole(monkeypatch):
    """Д11 (2026-08-09). Канал принял корень и одну подзадачу из трёх,
    отклонив две. Число в ГЛАВНОЙ строке считалось по всему запрошенному
    списку — «+ 3 подзадач» — прямо противореча предупреждению об отказе,
    которое печаталось строкой ниже. Соседняя ветка того же метода (плоские
    подзадачи) отказы вычитала всегда."""
    v2, _ = _wire(monkeypatch,
                  reject_create_titles={"Позвать мастера", "Вывезти мусор"},
                  blind_after_write=True)

    result = await s._create_tasks_impl("тест", [
        {"title": "Ремонт", "project_id": "pA",
         "subtasks": [{"title": "Купить краску"}, {"title": "Позвать мастера"},
                      {"title": "Вывезти мусор"}]}])

    assert "+ 1 подзадач" in result, f"счёт завышен:\n{result}"
    assert "+ 3 подзадач" not in result
    assert "2 из 4" in result, f"не сказано, сколько из скольких:\n{result}"
    assert "TickTick отклонил 2 из 4" in result


async def test_a_rejected_root_is_not_reported_as_created(monkeypatch):
    """Крайний случай того же счёта: канал отклонил САМ корень. «✓ «Ремонт»»
    было бы ложью про задачу, а не только про число подзадач."""
    v2, _ = _wire(monkeypatch, reject_create_titles={"Ремонт"},
                  blind_after_write=True)

    result = await s._create_tasks_impl("тест", [
        {"title": "Ремонт", "project_id": "pA",
         "subtasks": [{"title": "Купить краску"}]}])

    assert "✓ «Ремонт»" not in result, f"отклонённый корень выдан за созданный:\n{result}"
    assert "САМА задача НЕ создана" in result
    assert "создано 1 из 2" in result


async def test_a_fully_accepted_tree_still_reads_exactly_as_before(monkeypatch):
    """Зеркало: когда канал не отклонил ничего, строка побайтово прежняя —
    правка счёта не имеет права переписывать успешный отчёт."""
    v2, _ = _wire(monkeypatch, blind_after_write=True)

    result = await s._create_tasks_impl("тест", [
        {"title": "Ремонт", "project_id": "pA",
         "subtasks": [{"title": "Купить краску"}, {"title": "Позвать мастера"}]}])

    assert "✓ «Ремонт» + 2 подзадач (дерево, 3 всего)" in result, result
