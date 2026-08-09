"""П15 — задача БЕЗ НАЗВАНИЯ должна быть видима и управляема.

Случай (живая приёмка): из плана на удаление в 14 задач исполнилось 11, а три
не прошли — все три с пустым названием. Опознание шло по названию, названия
нет → «её нет в живом состоянии аккаунта». Задача при этом существует: её
карточка читается, из неё скачивается вложение. Последствий три — не
удаляется, не переименовывается, и (опаснее всего) в списках выглядит ПУСТОЙ
СТРОКОЙ, неотличимо от мусора: две из трёх пустышек владельца были
содержательными — фотография чека Home Depot на возврат $374.92 в Inbox и
скриншот с описанием дефекта в Vibe Coding Notes.

Здесь проверяется всё три: показ (видно, что внутри файл), переименование,
удаление — и ГРАНИЦА послабления предохранителя: пустое имя разрешено ТОЛЬКО
когда оно пустое С ОБЕИХ СТОРОН. Любое одностороннее «пусто» — по-прежнему
расхождение и отказ, иначе можно было бы удалить не тот объект.
"""
import re
import time

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


# Задача-«пустышка», которая на самом деле документ: чек Home Depot приложен
# файлом, названия нет. Ровно тот объект, который чуть не удалили.
def _receipt_task(tid="t_receipt", pid="p_inbox"):
    return {"id": tid, "projectId": pid, "title": "",
            "attachments": [{"fileName": "home-depot-receipt.jpg",
                             "id": "a" * 24}]}


class _FakeV2:
    """Двойник v2-клиента: ровно те вызовы, которые делают проверяемые пути."""

    def __init__(self, live, trash=None, tags=None):
        self.live = live
        self.trash = trash or {}
        self.calls = []
        self.tags = tags if tags is not None else []

    def invalidate_cache(self):
        pass

    def get_state(self, force=False):
        return {"tags": self.tags}

    def get_tags(self):
        return self.tags

    def get_open_tasks(self):
        return list(self.live.values())

    def batch_update_tasks(self, changes):
        self.calls.append(("update", changes))
        for c in changes:
            t = self.live.setdefault(c["taskId"], {"id": c["taskId"]})
            if "title" in c:
                t["title"] = c["title"]
        return {}

    def batch_delete_tasks(self, items):
        self.calls.append(("delete", [it["taskId"] for it in items]))
        for it in items:
            self.live.pop(it["taskId"], None)
        return {}

    def find_task_any_state(self, task_id):
        # Настоящий клиент ищет по трём лентам и возвращает (задача, где).
        # Двойник обязан отвечать так же, иначе «ненайденная задача» в тесте
        # проходила бы через AttributeError, а не через честный поиск.
        if task_id in self.live:
            return self.live[task_id], "open"
        if task_id in self.trash:
            return self.trash[task_id], "trash"
        return None, None

    def get_trash(self, limit=500):
        return list(self.trash.values())[:limit]


class _FakeOfficial:
    """Двойник официального клиента — одиночный update идёт через него."""

    def __init__(self, live):
        self.live = live
        self.calls = []

    def update_task(self, task_id, project_id, title=None, content=None,
                    start_date=None, due_date=None, priority=None,
                    repeat_flag=None, reminders=None):
        self.calls.append(("update", task_id))
        t = self.live.setdefault(task_id, {"id": task_id})
        if title is not None:
            t["title"] = title
        return {"id": task_id}


def _wire(monkeypatch, live, tmp_path=None, names=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: names or {"p_inbox": "Inbox", "p2": "Работа"})
    if tmp_path is not None:
        monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = _FakeV2(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    official = _FakeOfficial(live)
    monkeypatch.setattr(s, "ticktick", official)
    return fake, official


def _delete_manifest(mid, items, summary="уборка"):
    now = time.monotonic()
    s._MANIFESTS[mid] = {
        "kind": "delete", "items": items, "created": now,
        "plan_shown_at": now, "summary": summary, "consumed": False,
        "object_hash": s._manifest_object_hash(
            "delete", [it["taskId"] for it in items]),
    }


# ═════════════ 1. ПОКАЗ: пустая строка ≠ пустая задача ═════════════

def test_untitled_task_with_attachment_shows_the_file_in_a_list():
    """Приёмка: «задача без названия, но с вложением, показывается в списке
    так, что видно: там файл»."""
    line = s.format_task_line(_receipt_task(), "Inbox")
    assert "(без названия · 📎 1 файл)" in line
    assert "(no title)" not in line


def test_untitled_task_with_text_is_told_apart_from_an_empty_one():
    """Разница между «есть текст» и «пусто» — это разница между документом и
    мусором; ради неё вся правка и делалась. «Пусто» законно только когда
    источник ДЕЙСТВИТЕЛЬНО перечислил вложения (здесь — пустым списком)."""
    with_text = s.format_task_line(
        {"id": "t1", "title": "", "attachments": [],
         "content": "проверить возврат"}, "Inbox")
    empty = s.format_task_line({"id": "t2", "title": "", "attachments": []},
                               "Inbox")
    assert "(без названия · есть текст)" in with_text
    assert "(без названия · пусто)" in empty
    assert with_text.split("(id:")[0] != empty.split("(id:")[0]


def test_a_file_is_never_hidden_behind_the_text_label():
    """M4. Задача, где есть И файл, И подпись к нему, — это ДОКУМЕНТ. Если
    порядок в заменителе перевернуть (сначала текст), файл исчезнет из строки
    списка — ровно то, из-за чего чек Home Depot попал в план на удаление."""
    task = {"id": "t1", "title": "",
            "attachments": [{"fileName": "receipt.jpg", "id": "d" * 24}],
            "content": "вернуть до конца месяца"}
    line = s.format_task_line(task, "Inbox")
    assert "(без названия · 📎 1 файл)" in line
    assert "есть текст" not in line


def test_unreadable_attachment_field_never_claims_the_task_is_empty():
    """M6. Сбой или неожиданная форма ответа про вложения — это «не знаю», а
    не «пусто». «Пусто» — приговор документу, и выносить его по незнанию
    нельзя."""
    broken = s.format_task_line({"id": "t1", "title": "",
                                 "attachments": "внезапно строка"}, "Inbox")
    assert "(без названия)" in broken
    assert "пусто" not in broken


def test_official_api_task_without_attachment_field_says_nothing_about_files():
    """`format_task` печатает и ответ ОФИЦИАЛЬНОГО v1 API (get_task,
    get_project_tasks), который поля `attachments` не отдаёт вовсе. Утверждать
    по такому ответу «пусто» — значит объявить пустой карточку задачи, в
    которой лежит файл."""
    v1_task = {"id": "t1", "projectId": "p_inbox", "title": "", "status": 0}
    out = s.format_task(v1_task)
    assert "Title: (без названия)\n" in out
    assert "пусто" not in out


def test_whitespace_only_title_is_shown_as_untitled_everywhere():
    """M1 — ГЛАВНОЕ. Название из одних пробелов truthy, поэтому показ по
    `title or заменитель` пропускал его целиком: задача с фотографией чека
    внутри печаталась пустой строкой. Все точки показа обязаны отвечать
    одинаково."""
    task = dict(_receipt_task(), title="   ")

    assert "(без названия · 📎 1 файл)" in s.format_task_line(task, "Inbox")
    assert "(без названия · 📎 1 файл)" in s.format_task(task)

    import ticktick_mcp.src.server as srv
    old = srv.ticktick_v2
    srv.ticktick_v2 = _FakeV2({"t_receipt": task})
    try:
        assert s._lookup_task_title("t_receipt") == "(без названия · 📎 1 файл)"
        assert s._live_task_title("t_receipt") == "(без названия · 📎 1 файл)"
    finally:
        srv.ticktick_v2 = old


def test_invisible_only_title_is_shown_as_untitled():
    """Название из одного zero-width space человек видит как пустое место —
    значит и показ обязан видеть его так же."""
    task = dict(_receipt_task(), title="​")
    assert "(без названия · 📎 1 файл)" in s.format_task_line(task, "Inbox")


async def test_one_object_is_named_the_same_way_in_a_list_and_in_a_plan(
        monkeypatch, tmp_path):
    """Согласованность точек показа: строка списка и строка плана про ОДИН
    объект обязаны называть его одинаково. Расхождение здесь и было признаком
    того, что показ живёт по двум разным правилам."""
    task = dict(_receipt_task(), title="  ")
    live = {"t_receipt": task}
    _wire(monkeypatch, live, tmp_path)

    line = s.format_task_line(task, "Inbox")
    plan = await s.plan_task_deletion("уборка", [{"taskId": "t_receipt"}])

    assert "(без названия · 📎 1 файл)" in line
    assert "(без названия · 📎 1 файл)" in plan


def test_attachment_known_only_from_inline_content_ref_still_counts():
    """У части аккаунтов структурный массив приходит пустым, и ссылка в
    тексте — единственное свидетельство файла. Такая задача обязана читаться
    как файл, а не как «есть текст»."""
    task = {"id": "t1", "title": "",
            "content": "![file](" + "b" * 24 + "/receipt.jpg)"}
    assert "(без названия · 📎 1 файл)" in s.format_task_line(task)


def test_two_attachments_are_not_double_counted_across_both_sources():
    """Один файл лежит и в массиве, и ссылкой в тексте — «2 файла» было бы
    ложью о том, сколько документов внутри."""
    task = {"id": "t1", "title": "",
            "attachments": [{"fileName": "receipt.jpg", "id": "c" * 24}],
            "content": "![file](" + "c" * 24 + "/receipt.jpg)"}
    assert "(без названия · 📎 1 файл)" in s.format_task_line(task)


def test_untitled_card_does_not_print_the_no_title_placeholder():
    """Карточка задачи (format_task) страдала тем же: «No title» одинаково
    выглядел и у чека, и у пустышки."""
    out = s.format_task(_receipt_task())
    assert "No title" not in out
    assert "(без названия · 📎 1 файл)" in out


def test_untitled_task_found_alive_is_never_named_by_its_bare_id():
    """`_lookup_task_title` подставлял `[task 6a757123…]` — идентификатор в
    позицию имени. Найденная безымянная задача обязана называться тем, что в
    ней лежит, а по-настоящему ненайденная — оставаться `[task …]`."""
    live = {"t_receipt": _receipt_task()}
    fake = _FakeV2(live)
    import ticktick_mcp.src.server as srv
    old = srv.ticktick_v2
    srv.ticktick_v2 = fake
    try:
        assert s._lookup_task_title("t_receipt") == "(без названия · 📎 1 файл)"
        assert s._lookup_task_title("t_ghost") == "[task t_ghost…]"
    finally:
        srv.ticktick_v2 = old


def test_live_task_title_of_an_untitled_task_is_its_content_not_none(
        monkeypatch):
    """`_live_task_title` питает карточки, ВРУЧАЮЩИЕ право записи в аккаунт
    (create_attachment_upload_url). None у найденной безымянной задачи
    заставлял их печатать «её нет в живом состоянии аккаунта» — про задачу,
    которая тут же и прочиталась. None остаётся только за ненайденной."""
    live = {"t_receipt": _receipt_task()}
    _wire(monkeypatch, live)

    assert s._live_task_title("t_receipt") == "(без названия · 📎 1 файл)"
    assert s._live_task_title("t_ghost") is None


# ═════════════ 2. ПРЕВЬЮ ПЛАНА: опознание по id названо вслух ═════════════

async def test_deletion_plan_shows_the_content_and_says_identified_by_id(
        monkeypatch, tmp_path):
    """Приёмка: «в превью плана видно, что объект опознан по id, а не по
    названию». И видно, ЧТО в задаче лежит."""
    live = {"t_receipt": _receipt_task()}
    _wire(monkeypatch, live, tmp_path)

    plan = await s.plan_task_deletion("уборка", [{"taskId": "t_receipt"}])

    assert "(без названия · 📎 1 файл)" in plan
    assert "опознана ПО id" in plan
    assert "стоит сначала её назвать" in plan
    # Прежний текст — прямая ложь про существующий объект.
    assert "нет в живом состоянии аккаунта" not in plan
    assert "**«»**" not in plan


async def test_update_preview_of_an_untitled_task_says_identified_by_id(
        monkeypatch):
    """Тот же язык в превью ЛЮБОЙ операции — здесь переименование."""
    live = {"t_receipt": _receipt_task()}
    _wire(monkeypatch, live)

    preview = await s.update_tasks("назвать чек", [
        {"taskId": "t_receipt", "projectId": "p_inbox",
         "new_title": "Чек Home Depot — возврат $374.92"}])

    assert "(без названия · 📎 1 файл)" in preview
    assert "опознана ПО id" in preview
    assert "нет в живом состоянии аккаунта" not in preview


def test_triage_line_names_an_untitled_task_by_its_content(monkeypatch):
    """Строка плана manual_triage печатала про безымянную задачу «её нет в
    живом состоянии аккаунта» — при том, что живое состояние её только что
    вернуло. Теперь она названа тем, что в ней лежит, с признанием, что
    личность сверена по id."""
    live = {"t_receipt": _receipt_task()}
    _wire(monkeypatch, live)

    ops = s._resolve_triage_ops(
        [{"op": "delete", "task_id": "t_receipt", "said": "убери пустышки"}],
        dict(live), {"p_inbox": "Inbox"})
    line = s._describe_triage_op(ops[0])

    assert ops[0].get("_skip") is None
    assert "(без названия · 📎 1 файл)" in line
    assert "опознана ПО id" in line
    assert "нет в живом состоянии аккаунта" not in line


# ═════════════ 3. ПЕРЕИМЕНОВАНИЕ ═════════════

async def test_untitled_task_is_renamed_normally(monkeypatch):
    """Приёмка: «такая задача переименовывается штатно, без предупреждений о
    несуществующем объекте»."""
    live = {"t_receipt": _receipt_task()}
    fake, official = _wire(monkeypatch, live)

    preview = await s.update_tasks("назвать чек", [
        {"taskId": "t_receipt", "projectId": "p_inbox",
         "new_title": "Чек Home Depot — возврат $374.92"}])
    mid = _extract_manifest_id(preview)
    out = await s.update_tasks("назвать чек", manifest_id=mid,
                               user_reply="да")

    assert live["t_receipt"]["title"] == "Чек Home Depot — возврат $374.92"
    assert "🛑" not in out
    assert "не среди открытых" not in out
    assert "нет в живом состоянии аккаунта" not in out


def test_unarmed_note_calls_an_untitled_row_identified_by_id_not_unchecked(
        monkeypatch):
    """Строка без имени с обеих сторон — не «выполнено БЕЗ сверки», а
    «опознано по id»: ⚠️ здесь пугало бы владельца там, где всё в порядке, и
    прятало бы настоящие непроверенные строки среди ложных."""
    note = s._unarmed_note([
        {"taskId": "t_receipt", "title": "(без названия · 📎 1 файл)",
         "armed": False, "by_id": True},
        {"taskId": "t2", "title": "Обычная", "armed": False, "by_id": False},
    ])
    assert "ℹ️ 1 опознано ПО id" in note
    assert "⚠️ 1 выполнено БЕЗ сверки названия" in note
    assert "(без названия · 📎 1 файл)" in note


def test_split_marks_untitled_row_as_identified_by_id(monkeypatch):
    """Признак `by_id` ставится ТОЛЬКО когда пусто с обеих сторон."""
    live = {"t_receipt": _receipt_task(),
            # Пробельное название — тот же класс, что пустое: строка,
            # выглядящая пустым местом, обязана получить заменитель, иначе
            # ⚠️/ℹ️-сводка операции напечатает ««   »» (аудит 2026-08-09).
            "t_spaces": dict(_receipt_task("t_spaces"), title="   "),
            "t_named": {"id": "t_named", "projectId": "p_inbox",
                        "title": "Купить молоко"}}
    _wire(monkeypatch, live)

    found, mismatch, missing = s._split_tasks_by_state(
        [{"taskId": "t_receipt"},
         {"taskId": "t_spaces", "title": "   "},
         {"taskId": "t_named", "title": "Купить молоко"}],
        by_id=dict(live))

    assert mismatch == [] and missing == []
    by_task = {f["taskId"]: f for f in found}
    assert by_task["t_receipt"]["by_id"] is True
    # `title` остаётся ПУСТЫМ (он таким и есть у задачи) — иначе заменитель
    # уехал бы в манифест как настоящее имя и сломал сверку на исполнении;
    # человекочитаемое имя живёт отдельно, в `label`.
    assert by_task["t_receipt"]["title"] == ""
    assert by_task["t_receipt"]["label"] == "(без названия · 📎 1 файл)"
    assert by_task["t_spaces"]["by_id"] is True
    assert by_task["t_spaces"]["label"] == "(без названия · 📎 1 файл)"
    assert by_task["t_named"]["by_id"] is False
    assert by_task["t_named"]["label"] == "Купить молоко"

    # …и то же имя доезжает до сводки, которую читает владелец.
    note = s._unarmed_note(found)
    assert note.count("(без названия · 📎 1 файл)") == 2


# ═════════════ 4. УДАЛЕНИЕ и ГРАНИЦА ПОСЛАБЛЕНИЯ ═════════════

async def test_untitled_task_is_deleted_when_both_sides_are_untitled(
        monkeypatch, tmp_path):
    """Приёмка: «такая задача удаляется штатно, если её действительно решили
    удалить». Единственный новый разрешённый случай: пусто И в плане, И в
    живом состоянии."""
    live = {"t_receipt": _receipt_task()}
    fake, _official = _wire(monkeypatch, live, tmp_path)
    _delete_manifest("mid-untitled-ok", [
        {"taskId": "t_receipt", "projectId": "p_inbox", "title": "",
         "project": "Inbox", "snapshot": {}}])

    out = await s.execute_task_deletion("mid-untitled-ok", user_reply="да")

    assert "t_receipt" not in live
    assert ("delete", ["t_receipt"]) in fake.calls
    assert "Удалено 1" in out
    # Отчёт обязан НАЗЫВАТЬ удалённое, а не печатать «»
    assert "«(без названия · 📎 1 файл)»" in out


async def test_planned_untitled_but_live_has_a_title_is_still_refused(
        monkeypatch, tmp_path):
    """ГРАНИЦА, сторона 1. Пусто в плане, а живая задача названа — id ведёт
    на другой объект, и послабление сюда не распространяется."""
    live = {"t1": {"id": "t1", "title": "Что-то живое", "projectId": "p_inbox"}}
    fake, _official = _wire(monkeypatch, live, tmp_path)
    _delete_manifest("mid-half-1", [
        {"taskId": "t1", "projectId": "p_inbox", "title": "",
         "project": "Inbox", "snapshot": {}}])

    out = await s.execute_task_deletion("mid-half-1", user_reply="да")

    assert "t1" in live
    assert [c for c in fake.calls if c[0] == "delete"] == []
    assert "Пропущены" in out


async def test_planned_title_but_live_is_untitled_is_refused(
        monkeypatch, tmp_path):
    """ГРАНИЦА, сторона 2 — САМАЯ ОПАСНАЯ. В плане было имя, а живая задача
    по этому id безымянна: это ДРУГОЙ объект (возможно, тот самый чек), и
    удалить его вместо одобренного нельзя."""
    live = {"t1": _receipt_task("t1")}
    fake, _official = _wire(monkeypatch, live, tmp_path)
    _delete_manifest("mid-half-2", [
        {"taskId": "t1", "projectId": "p_inbox", "title": "Старый мусор",
         "project": "Inbox", "snapshot": {"title": "Старый мусор"}}])

    out = await s.execute_task_deletion("mid-half-2", user_reply="да")

    assert "t1" in live
    assert [c for c in fake.calls if c[0] == "delete"] == []
    assert "Пропущены" in out



async def test_a_named_task_that_drifted_is_still_refused_after_the_relaxation(
        monkeypatch, tmp_path):
    """Контроль: обычное расхождение имён продолжает ловиться — послабление
    не должно было тронуть ничего, кроме случая «пусто с обеих сторон»."""
    live = {"t1": {"id": "t1", "title": "Переименована", "projectId": "p_inbox"}}
    fake, _official = _wire(monkeypatch, live, tmp_path)
    _delete_manifest("mid-drift", [
        {"taskId": "t1", "projectId": "p_inbox", "title": "Как было",
         "project": "Inbox", "snapshot": {"title": "Как было"}}])

    out = await s.execute_task_deletion("mid-drift", user_reply="да")

    assert "t1" in live
    assert [c for c in fake.calls if c[0] == "delete"] == []
    assert "Пропущены" in out


async def test_untitled_relaxation_does_not_delete_a_neighbouring_task(
        monkeypatch, tmp_path):
    """Послабление действует ПОСТРОЧНО: безымянная строка исполняется, а
    строка, чьё имя разошлось, — по-прежнему пропускается, и соседняя задача,
    которой в манифесте нет, не тронута."""
    live = {"t_receipt": _receipt_task(),
            "t_drift": {"id": "t_drift", "title": "Стало другое",
                        "projectId": "p_inbox"},
            "t_bystander": {"id": "t_bystander", "title": "Соседняя",
                            "projectId": "p_inbox"}}
    fake, _official = _wire(monkeypatch, live, tmp_path)
    _delete_manifest("mid-mixed", [
        {"taskId": "t_receipt", "projectId": "p_inbox", "title": "",
         "project": "Inbox", "snapshot": {}},
        {"taskId": "t_drift", "projectId": "p_inbox", "title": "Было это",
         "project": "Inbox", "snapshot": {"title": "Было это"}}])

    out = await s.execute_task_deletion("mid-mixed", user_reply="да")

    assert "t_receipt" not in live
    assert "t_drift" in live and "t_bystander" in live
    assert ("delete", ["t_receipt"]) in fake.calls
    assert "Удалено 1" in out and "Пропущены 1" in out



# ═════════════ 5. ПОЛНЫЙ ЦИКЛ ═════════════

async def test_untitled_task_with_attachment_goes_through_show_rename_delete(
        monkeypatch, tmp_path):
    """Приёмка целиком: задача с пустым названием и вложением проходит показ,
    переименование и удаление."""
    live = {"t_receipt": _receipt_task()}
    fake, official = _wire(monkeypatch, live, tmp_path)

    # показ
    shown = s.format_task_list([live["t_receipt"]])
    assert "(без названия · 📎 1 файл)" in shown

    # переименование
    preview = await s.update_tasks("назвать", [
        {"taskId": "t_receipt", "projectId": "p_inbox",
         "new_title": "Чек Home Depot"}])
    mid = _extract_manifest_id(preview)
    await s.update_tasks("назвать", manifest_id=mid, user_reply="да")
    assert live["t_receipt"]["title"] == "Чек Home Depot"

    # удаление — уже по НОВОМУ имени, обычным путём
    plan = await s.plan_task_deletion("убрать", [
        {"taskId": "t_receipt", "title": "Чек Home Depot"}])
    dmid = _extract_manifest_id(plan)
    out = await s.execute_task_deletion(dmid, user_reply="да")

    assert "t_receipt" not in live
    assert "Удалено 1" in out
