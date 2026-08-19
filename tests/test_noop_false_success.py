"""Аудит класса «сервер рапортует успех, которого не было» (2026-08-19, ночной
QA, ветка fixnight/k). Этой ночью были закрыты два представителя класса —
update_tasks с пустым набором изменений и move_tasks «уже в целевом списке»
(no-op) — и этот файл закрывает ОСТАЛЬНЫХ найденных представителей той же
природы: инструмент принимает вход, при котором TickTick заведомо нечего
менять (значение уже равно целевому / объект уже существует), молча получает
200 и рапортует «✅ … (проверено)» — пост-верификация подтверждает СОСТОЯНИЕ,
а не ИЗМЕНЕНИЕ, и для no-op входа проходит тривиально.

Дыры, доказанные этим файлом (каждый тест падал ДО фикса):
  1. set_task_parent  — задача УЖЕ вложена под этого родителя → «🔗 Вложено
     1 … (проверено)» об операции, которой не было.
  2. set_task_tags    — запрошенный набор тегов РАВЕН текущему → «🏷 Теги
     обновлены у 1 (проверено)».
  3. move_project_to_group — проект УЖЕ в целевой папке (или уже без папки
     при group_id="NONE") → «✅ … перемещён … (проверено)».
  4. archive_project  — проект УЖЕ в запрошенном состоянии (активен при
     archived=False, архивирован при archived=True) → «✅ … (проверено)».
  5. update_project   — каждое переданное поле РАВНО текущему живому
     значению → «✅ … обновлён (проверено)».
  6. update_task_comment — новый текст ПОСИМВОЛЬНО равен текущему → «✅ …
     обновлён (проверено)».
  7. create_tag       — тег УЖЕ существует в аккаунте → «✅ Тег … создан
     (проверено)» (создания не было, был 200-no-op).
  8. _verify_item(op="update") с ПУСТЫМ expect.changes — вакуумная истина
     («сверять было нечего» читалось как «сверили и совпало»); вход закрыт
     ночным фиксом _update_item_has_changes, но сам вердикт оставался
     дырявым для любых старых/чужих журнальных записей.
  9. manual_triage (apply_task_changes): op="move"/"parent"/"tags" с уже
     достигнутым целевым состоянием — секция исполнителя говорила честно
     («ℹ️ уже …»), но независимая сверка считала операцию выполненной и
     заголовок печатал «✅ Выполнено N из N».

Починка — по образцу ночного фикса move_tasks: no-op распознаётся ДО
отправки мутации (по тому же живому чтению, что и identity-guard) и уходит в
ответ ЯВНЫМ фактом («ℹ️ … — без изменений»), а не успехом; в журнал такие
позиции не пишутся (сверять нечего). В manual_triage no-op строка не входит
в план (как уже делал op="unparent": «и так не подзадача — отцеплять
нечего») и блокируется на drift-сверке, если целевое состояние наступило
между планом и подтверждением."""
import ticktick_mcp.src.server as s


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _reads(state):
    """_open_by_id-заглушка: всегда одно и то же живое состояние (для no-op
    сценариев мир не меняется — мутаций быть не должно)."""
    def _fake(fresh=False):
        return state
    return _fake


# ---------------------------------------------------------------------------
# 1. set_task_parent — задача уже вложена под этого родителя
# ---------------------------------------------------------------------------

class _FakeV2Parent:
    def __init__(self):
        self.calls = []

    def set_task_parents(self, rows):
        self.calls.append(rows)
        return {}


async def test_set_task_parent_noop_already_nested_is_not_success(monkeypatch):
    state = {
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1",
                    "parentId": None},
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "proj1",
               "parentId": "parent1"},   # УЖЕ вложена
    }
    fake = _FakeV2Parent()
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"proj1": "Работа"})
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "parent-noop-0001")
    monkeypatch.setattr(s, "_open_by_id", _reads(state))
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = await s._set_task_parent_impl(
        "Вкладываю", [{"taskId": "t1", "title": "Ребёнок"}],
        "parent1", "proj1", "Родитель")

    assert "🔗 Вложено" not in result, result
    assert "уже" in result.lower() and "без изменений" in result, result
    assert fake.calls == [], "no-op не должен отправляться в API"


async def test_set_task_parent_mixed_noop_and_real_row(monkeypatch):
    """Смешанная пачка: одна строка no-op, одна настоящая — настоящая
    вкладывается и рапортуется, no-op помечается отдельно."""
    state = {
        "parent1": {"id": "parent1", "title": "Родитель", "projectId": "proj1",
                    "parentId": None},
        "t1": {"id": "t1", "title": "Уже там", "projectId": "proj1",
               "parentId": "parent1"},
        "t2": {"id": "t2", "title": "Новичок", "projectId": "proj1",
               "parentId": None},
    }
    after = {**{k: dict(v) for k, v in state.items()}}
    after["t2"]["parentId"] = "parent1"
    fake = _FakeV2Parent()
    seq = {"n": 0}

    def _read(fresh=False):
        seq["n"] += 1
        return state if seq["n"] == 1 else after

    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"proj1": "Работа"})
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "parent-noop-0002")
    monkeypatch.setattr(s, "_open_by_id", _read)
    monkeypatch.setattr(s, "ticktick_v2", fake)

    result = await s._set_task_parent_impl(
        "Вкладываю", [{"taskId": "t1", "title": "Уже там"},
                      {"taskId": "t2", "title": "Новичок"}],
        "parent1", "proj1", "Родитель")

    assert "🔗 Вложено 1" in result, result
    assert "«Новичок»" in result
    assert "«Уже там»" in result and "без изменений" in result, result
    # В API ушла только настоящая строка
    assert len(fake.calls) == 1 and [r["taskId"] for r in fake.calls[0]] == ["t2"]


# ---------------------------------------------------------------------------
# 2. set_task_tags — запрошенный набор равен текущему
# ---------------------------------------------------------------------------

class _FakeV2Tags:
    def __init__(self, tags):
        self._tags = tags
        self.batch_calls = []
        self.created = []

    def batch_update_tasks(self, changes):
        self.batch_calls.append(changes)
        return {}

    def create_tag(self, name, color=None):
        self.created.append(name)
        return {}

    def get_state(self, force=False):
        return {}

    def get_tags(self):
        return [{"name": t, "label": t} for t in self._tags]


async def test_set_task_tags_noop_same_set_is_not_success(monkeypatch):
    state = {"t1": {"id": "t1", "title": "Задача", "projectId": "p1",
                    "tags": ["дом", "звонки"]}}
    fake = _FakeV2Tags(["дом", "звонки"])
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", _reads(state))
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "tags-noop-0001")

    result = await s._set_task_tags_impl(
        "Теги", [{"taskId": "t1", "title": "Задача",
                  "tags": ["дом", "звонки"]}])

    assert "Теги обновлены" not in result, result
    assert "уже" in result.lower() and "без изменений" in result, result
    assert fake.batch_calls == [], "no-op не должен отправляться в API"


async def test_set_task_tags_mixed_noop_and_real(monkeypatch):
    state = {
        "t1": {"id": "t1", "title": "Как есть", "projectId": "p1",
               "tags": ["дом"]},
        "t2": {"id": "t2", "title": "Менять", "projectId": "p1",
               "tags": []},
    }
    fake = _FakeV2Tags(["дом"])
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", _reads(state))
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "tags-noop-0002")

    async def _post_read(fresh=False):
        return state

    # после отправки живое состояние: t2 получил тег
    def _read2(fresh=False):
        if fake.batch_calls:
            return {
                "t1": {"id": "t1", "title": "Как есть", "projectId": "p1",
                       "tags": ["дом"]},
                "t2": {"id": "t2", "title": "Менять", "projectId": "p1",
                       "tags": ["дом"]},
            }
        return state

    monkeypatch.setattr(s, "_open_by_id", _read2)

    result = await s._set_task_tags_impl(
        "Теги", [{"taskId": "t1", "title": "Как есть", "tags": ["дом"]},
                 {"taskId": "t2", "title": "Менять", "tags": ["дом"]}])

    assert "Теги обновлены у 1" in result, result
    assert "«Менять»" in result
    assert "«Как есть»" in result and "без изменений" in result, result
    assert len(fake.batch_calls) == 1
    assert [c["taskId"] for c in fake.batch_calls[0]] == ["t2"]


# ---------------------------------------------------------------------------
# 3. move_project_to_group — проект уже в целевой папке / уже без папки
# ---------------------------------------------------------------------------

class _FakeV2ProjGroup:
    def __init__(self, projects, groups):
        self._projects = projects
        self._groups = groups
        self.moves = []

    def move_project_to_group(self, project_id, group_id):
        self.moves.append((project_id, group_id))
        for p in self._projects:
            if p.get("id") == project_id:
                p["groupId"] = None if group_id == "NONE" else group_id
        return {}

    def get_state(self, force=False):
        return {}

    def list_projects(self):
        return [dict(p) for p in self._projects]

    def get_project_groups(self):
        return [dict(g) for g in self._groups]


def _wire_proj_group(monkeypatch, fake):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(s, "ticktick_v2", fake)

    async def _groups(fresh=True):
        return fake.get_project_groups()

    monkeypatch.setattr(s, "_live_groups", _groups)


async def test_move_project_to_group_noop_already_in_group(monkeypatch):
    fake = _FakeV2ProjGroup(
        [{"id": "p1", "name": "Проект", "groupId": "g1"}],
        [{"id": "g1", "name": "Папка"}])
    _wire_proj_group(monkeypatch, fake)

    result = await s._move_project_to_group_impl("Проект", "p1", "g1")

    assert "перемещён" not in result, result
    assert "уже" in result.lower() and "без изменений" in result, result
    assert fake.moves == []


async def test_move_project_to_group_noop_already_ungrouped(monkeypatch):
    fake = _FakeV2ProjGroup(
        [{"id": "p1", "name": "Проект", "groupId": None}], [])
    _wire_proj_group(monkeypatch, fake)

    result = await s._move_project_to_group_impl("Проект", "p1", "NONE")

    assert "перемещён" not in result, result
    assert "уже" in result.lower() and "без изменений" in result, result
    assert fake.moves == []


async def test_move_project_to_group_real_move_still_works(monkeypatch):
    fake = _FakeV2ProjGroup(
        [{"id": "p1", "name": "Проект", "groupId": None}],
        [{"id": "g1", "name": "Папка"}])
    _wire_proj_group(monkeypatch, fake)

    result = await s._move_project_to_group_impl("Проект", "p1", "g1")

    assert "✅" in result and "перемещён" in result, result
    assert fake.moves == [("p1", "g1")]


# ---------------------------------------------------------------------------
# 4. archive_project — проект уже в запрошенном состоянии
# ---------------------------------------------------------------------------

class _FakeV2Archive:
    def __init__(self, projects):
        self._projects = projects
        self.archive_calls = []

    def archive_project(self, project_id, closed=True):
        self.archive_calls.append((project_id, closed))
        for p in self._projects:
            if p.get("id") == project_id:
                p["closed"] = closed
        return {}

    def get_state(self, force=False):
        return {}

    def list_projects(self):
        return [dict(p) for p in self._projects]


def _wire_archive(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})


async def test_archive_project_noop_already_active(monkeypatch):
    fake = _FakeV2Archive([{"id": "p1", "name": "Работа", "closed": False}])
    _wire_archive(monkeypatch, fake)

    result = await s._archive_project_impl("Работа", "p1", archived=False)

    assert "разархивирован (проверено)" not in result, result
    assert "уже" in result.lower() and "без изменений" in result, result
    assert fake.archive_calls == []


async def test_archive_project_noop_already_archived(monkeypatch):
    fake = _FakeV2Archive([{"id": "p1", "name": "Работа", "closed": True}])
    _wire_archive(monkeypatch, fake)

    result = await s._archive_project_impl("Работа", "p1", archived=True)

    assert "заархивирован (проверено)" not in result, result
    assert "уже" in result.lower() and "без изменений" in result, result
    assert fake.archive_calls == []


async def test_archive_project_real_archive_still_works(monkeypatch):
    fake = _FakeV2Archive([{"id": "p1", "name": "Работа", "closed": False}])
    _wire_archive(monkeypatch, fake)

    result = await s._archive_project_impl("Работа", "p1", archived=True)

    assert result.startswith("### ✅"), result
    assert "заархивирован" in result
    assert fake.archive_calls == [("p1", True)]


# ---------------------------------------------------------------------------
# 5. update_project — все переданные значения равны текущим живым
# ---------------------------------------------------------------------------

class _FakeOfficialUpdate:
    def __init__(self, fresh):
        self._fresh = fresh
        self.update_calls = []

    def update_project(self, project_id, name=None, color=None, view_mode=None):
        self.update_calls.append((project_id, name, color, view_mode))
        return dict(self._fresh)

    def get_project(self, project_id):
        return dict(self._fresh)


async def test_update_project_noop_all_values_equal_current(monkeypatch):
    fake = _FakeOfficialUpdate({"id": "p1", "name": "Работа",
                                "color": "#111111", "viewMode": "list"})
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})

    result = await s._update_project_impl(
        "Работа", "p1", name="Работа", color="#111111")

    assert "обновлён (проверено)" not in result, result
    assert "уже" in result.lower() and "без изменений" in result, result
    assert fake.update_calls == []


async def test_update_project_partial_match_still_updates(monkeypatch):
    """Хотя name совпадает, color другой — это НАСТОЯЩЕЕ изменение, мутация
    обязана уйти и подтвердиться как раньше."""
    fresh = {"id": "p1", "name": "Работа", "color": "#111111",
             "viewMode": "list"}

    class _Fake(_FakeOfficialUpdate):
        def update_project(self, project_id, name=None, color=None,
                           view_mode=None):
            self.update_calls.append((project_id, name, color, view_mode))
            if color is not None:
                self._fresh["color"] = color
            return dict(self._fresh)

    fake = _Fake(fresh)
    monkeypatch.setattr(s, "ticktick", fake)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})

    result = await s._update_project_impl(
        "Работа", "p1", name="Работа", color="#222222")

    assert "### ✅" in result and "обновлён (проверено)" in result, result
    assert len(fake.update_calls) == 1


# ---------------------------------------------------------------------------
# 6. update_task_comment — текст равен текущему
# ---------------------------------------------------------------------------

class _FakeV2Comment:
    def __init__(self, comments):
        self._comments = comments
        self.update_calls = []

    def update_task_comment(self, project_id, task_id, comment_id, text):
        self.update_calls.append((comment_id, text))
        for c in self._comments:
            if c.get("id") == comment_id:
                c["title"] = text
        return {}

    def get_task_comments(self, project_id, task_id):
        return [dict(c) for c in self._comments]


def _wire_comment(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)

    class _G:
        status = "open"
        title = "Задача"
        project_id = "p1"

    monkeypatch.setattr(s, "_guard_task_incl_completed", lambda *a, **k: _G())
    monkeypatch.setattr(s, "_guard_or_refuse", lambda *a, **k: (None, ""))


async def test_update_task_comment_noop_same_text(monkeypatch):
    fake = _FakeV2Comment([{"id": "c1", "title": "Итог: готово"}])
    _wire_comment(monkeypatch, fake)

    result = await s._update_task_comment_impl(
        "Задача", "Итог: готово", "p1", "t1", "c1")

    assert "обновлён (проверено)" not in result, result
    assert "без изменений" in result, result
    assert fake.update_calls == []


async def test_update_task_comment_real_edit_still_works(monkeypatch):
    fake = _FakeV2Comment([{"id": "c1", "title": "Старый текст"}])
    _wire_comment(monkeypatch, fake)

    result = await s._update_task_comment_impl(
        "Задача", "Новый текст", "p1", "t1", "c1")

    assert "✅" in result and "обновлён (проверено)" in result, result
    assert fake.update_calls == [("c1", "Новый текст")]


async def test_update_task_comment_missing_comment_is_refused(monkeypatch):
    """comment_id, которого нет на задаче, — отказ ДО мутации (как у
    delete_task_comment), а не слепая отправка."""
    fake = _FakeV2Comment([{"id": "c1", "title": "Единственный"}])
    _wire_comment(monkeypatch, fake)

    result = await s._update_task_comment_impl(
        "Задача", "Текст", "p1", "t1", "c-нет-такого")

    assert "🛑" in result, result
    assert fake.update_calls == []


# ---------------------------------------------------------------------------
# 7. create_tag — тег уже существует
# ---------------------------------------------------------------------------

async def test_create_tag_noop_already_exists(monkeypatch):
    calls = []

    class _FakeV2:
        def create_tag(self, name, color=None):
            calls.append(name)
            return {}

    monkeypatch.setattr(s, "ticktick_v2", _FakeV2())

    async def _names(force=True):
        return ["дом", "работа"]

    monkeypatch.setattr(s, "_live_tag_names", _names)

    result = await s._create_tag_impl("Работа")

    assert "создан (проверено)" not in result, result
    assert "уже" in result.lower() and "без изменений" in result, result
    assert calls == []


async def test_create_tag_real_creation_still_works(monkeypatch):
    tags = ["дом"]
    calls = []

    class _FakeV2:
        def create_tag(self, name, color=None):
            calls.append(name)
            tags.append(name.lower())
            return {}

    monkeypatch.setattr(s, "ticktick_v2", _FakeV2())

    async def _names(force=True):
        return list(tags)

    monkeypatch.setattr(s, "_live_tag_names", _names)

    result = await s._create_tag_impl("Работа")

    assert "✅" in result and "создан (проверено)" in result, result
    assert calls == ["Работа"]


# ---------------------------------------------------------------------------
# 8. _verify_item(op="update") с пустым expect.changes — вакуумная истина
# ---------------------------------------------------------------------------

def test_verify_item_update_empty_changes_is_not_vacuous_ok():
    live = {"t1": {"id": "t1", "title": "Задача", "projectId": "p1"}}
    item = {"taskId": "t1", "title": "Задача", "expect": {"changes": {}}}

    status, line = s._verify_item("update", item, live, {})

    assert status != "ok", (status, line)
    assert "нечего" in line.lower() or "не провер" in line.lower(), line


def test_verify_item_update_real_changes_still_ok():
    live = {"t1": {"id": "t1", "title": "Задача", "projectId": "p1",
                   "priority": 5}}
    item = {"taskId": "t1", "title": "Задача",
            "expect": {"changes": {"priority": 5}}}

    status, line = s._verify_item("update", item, live, {})

    assert status == "ok", (status, line)


# ---------------------------------------------------------------------------
# 9. manual_triage: no-op строки не входят в план / блокируются на drift
# ---------------------------------------------------------------------------

def _plan_ctx(by_id, names=None):
    return s._TriagePlanCtx(by_id=by_id, names=names or {}, kids={})


def test_triage_move_plan_rejects_noop_already_in_destination(monkeypatch):
    by_id = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_new"}}
    e = {"op": "move", "task_id": "t1", "title": "Задача",
         "to_project_id": "p_new", "said": "перенеси"}
    names = {"p_new": "Новый"}
    ctx = _plan_ctx(by_id, names)
    # _op_move_plan сверяет ЛИЧНОСТЬ проекта назначения через
    # _resolve_triage_destination -> _guard_project_or_refuse, а та (по
    # дизайну, require_known=True, fail-closed) читает ЖИВЫЕ имена через
    # глобальный _v2_project_names(), а не через ctx.names — двух РАЗНЫХ
    # источников для одного и того же имени в проде не бывает (ctx.names
    # сам построен из свежего _v2_project_names() перед стартом плана), но
    # в юнит-тесте это нужно свести вручную, иначе guard всегда фейлится
    # «проект не найден» ещё ДО того, как код доходит до no-op-сверки,
    # которую тест на самом деле проверяет.
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)

    why = s._op_move_plan(e, ctx)

    assert why, "no-op move обязан не войти в план"
    assert "уже" in why.lower(), why


def test_triage_move_plan_keeps_real_move(monkeypatch):
    by_id = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_old"}}
    e = {"op": "move", "task_id": "t1", "title": "Задача",
         "to_project_id": "p_new", "said": "перенеси"}
    names = {"p_new": "Новый", "p_old": "Старый"}
    ctx = _plan_ctx(by_id, names)
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)  # см. коммент выше

    assert s._op_move_plan(e, ctx) == ""


def test_triage_move_drift_blocks_noop_reached_between_plan_and_yes():
    by_id = {"t1": {"id": "t1", "title": "Задача", "projectId": "p_new"}}
    op = {"op": "move", "task_id": "t1", "title": "Задача",
          "_to_project_id": "p_new"}

    why = s._op_move_drift(op, by_id, {"p_new": "Новый"})

    assert why, "no-op move обязан блокироваться на drift-сверке"
    assert "уже" in why.lower(), why


def test_triage_parent_plan_rejects_noop_already_nested():
    by_id = {
        "par": {"id": "par", "title": "Родитель", "projectId": "p1",
                "parentId": None},
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "p1",
               "parentId": "par"},
    }
    e = {"op": "parent", "task_id": "t1", "title": "Ребёнок",
         "to_task_id": "par", "to_title": "Родитель", "said": "вложи",
         "_project_id": "p1"}
    ctx = _plan_ctx(by_id, {"p1": "Проект"})

    why = s._op_parent_plan(e, ctx)

    assert why, "no-op parent обязан не войти в план"
    assert "уже" in why.lower(), why


def test_triage_parent_plan_keeps_real_nesting():
    by_id = {
        "par": {"id": "par", "title": "Родитель", "projectId": "p1",
                "parentId": None},
        "t1": {"id": "t1", "title": "Ребёнок", "projectId": "p1",
               "parentId": None},
    }
    e = {"op": "parent", "task_id": "t1", "title": "Ребёнок",
         "to_task_id": "par", "to_title": "Родитель", "said": "вложи",
         "_project_id": "p1"}
    ctx = _plan_ctx(by_id, {"p1": "Проект"})

    assert s._op_parent_plan(e, ctx) == ""


def test_triage_tags_plan_rejects_noop_same_set(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", None)
    by_id = {"t1": {"id": "t1", "title": "Задача", "projectId": "p1",
                    "tags": ["дом", "звонки"]}}
    e = {"op": "tags", "task_id": "t1", "title": "Задача",
         "changes": {"tags": ["Дом", "#звонки"]}, "said": "теги"}
    ctx = _plan_ctx(by_id, {"p1": "Проект"})

    why = s._op_tags_plan(e, ctx)

    assert why, "no-op tags обязан не войти в план"
    assert "уже" in why.lower(), why


def test_triage_tags_plan_keeps_real_change(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", None)
    by_id = {"t1": {"id": "t1", "title": "Задача", "projectId": "p1",
                    "tags": ["дом"]}}
    e = {"op": "tags", "task_id": "t1", "title": "Задача",
         "changes": {"tags": ["дом", "звонки"]}, "said": "теги"}
    ctx = _plan_ctx(by_id, {"p1": "Проект"})

    assert s._op_tags_plan(e, ctx) == ""
