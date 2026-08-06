"""Встроенный Inbox в списке проектов.

Дыра: официальный Open API вообще не знает о встроенном Inbox — GET /project
его не отдаёт, а GET /project/<inboxId>[/data] отвечает ошибкой. `get_projects`
строился ровно на этом эндпоинте, поэтому на «покажи мои проекты» приходил
список БЕЗ входящих: пользователь делал вывод, что Inbox пуст или исчез, хотя
задачи в нём лежат. Второй, менее заметный конец той же дыры — докстринг
`create_project_column` уже предлагал взять «the Inbox id from get_projects»,
которого этот инструмент не выдавал.

Двойники здесь НАМЕРЕННО не добрее живого сервиса (в этом репозитории уже
ловили обратное — фейк, который сам чинил то, что настоящий клиент делал
неправильно):

  * FakeOfficialV1 НЕ отдаёт Inbox в get_projects() и отвечает `error` на
    любое обращение по Inbox-id — ровно как настоящий Open API. Если фикс
    попробует «просто спросить у v1», тест это увидит;
  * FakeV2 повторяет логику настоящего TickTickV2Client: Inbox — это
    `inboxId` из sync-состояния, а задачи Inbox отбираются фильтром
    projectId == inboxId (копия ticktick_v2_client.get_inbox_tasks).

И смотрят тесты не на внутренние функции, а на то, ЧТО ВИДИТ КЛИЕНТ: вызов
инструмента по имени через реестр сервера (mcp.call_tool) и текст ответа.
Идентификатор Inbox в проверках «работает ли он дальше» не берётся из фикстуры
— он ВЫРЕЗАЕТСЯ РЕГУЛЯРКОЙ ИЗ ТЕКСТА, который клиент только что получил.
"""
import re

import ticktick_mcp.src.server as s

INBOX_ID = "inbox115781412"


# ───────────────────────────── двойники ─────────────────────────────

class FakeOfficialV1:
    """Официальный TickTick Open API, как он есть: Inbox для него не проект.

    get_projects() его не перечисляет, get_project()/get_project_with_data()
    по его id возвращают ошибку. Создание задачи повторяет задокументированное
    в самом сервере поведение бэкенда: projectId, который Open API не считает
    живым проектом (а inbox<id> для него именно такой), не отвергается — задача
    тихо оказывается в Inbox.
    """

    def __init__(self, projects, inbox_id=INBOX_ID):
        self._projects = [dict(p) for p in projects]
        self._inbox_id = inbox_id
        self.created = []
        self.data_calls = []

    def get_projects(self):
        return [dict(p) for p in self._projects]

    def get_project(self, project_id):
        p = next((dict(x) for x in self._projects if x["id"] == project_id), None)
        return p or {"error": f"project not found: {project_id}"}

    def get_project_with_data(self, project_id):
        self.data_calls.append(project_id)
        p = next((dict(x) for x in self._projects if x["id"] == project_id), None)
        if not p:
            return {"error": f"project not found: {project_id}"}
        return {"project": p, "tasks": [{"id": "t-x", "title": "Обычная задача",
                                         "projectId": project_id}]}

    def create_task(self, title, project_id, **kw):
        known = any(x["id"] == project_id for x in self._projects)
        landed = project_id if known else self._inbox_id
        task = {"id": f"new-{len(self.created) + 1}", "title": title,
                "projectId": landed}
        self.created.append(task)
        return dict(task)


class FakeV2:
    """Стенд-ин под TickTickV2Client: sync-снапшот + чтение Inbox тем же
    фильтром, что и в настоящем клиенте. Считает обращения к состоянию, чтобы
    листинг не начал читать его дважды."""

    def __init__(self, projects=None, groups=None, tasks=None,
                 inbox_id=INBOX_ID, state_error=None):
        self._projects = projects or []
        self._groups = groups or []
        self._tasks = tasks or []
        self.inbox_id = inbox_id
        self._state_error = state_error
        self.state_calls = 0

    def get_state(self, force=False):
        self.state_calls += 1
        if self._state_error:
            raise RuntimeError(self._state_error)
        st = {"projectProfiles": [dict(p) for p in self._projects],
              "projectGroups": [dict(g) for g in self._groups],
              "syncTaskBean": {"update": [dict(t) for t in self._tasks]}}
        if self.inbox_id:
            st["inboxId"] = self.inbox_id
        return st

    def get_open_tasks(self):
        return self.get_state().get("syncTaskBean", {}).get("update", []) or []

    def get_inbox_tasks(self):
        state = self.get_state()
        inbox = self.inbox_id or state.get("inboxId")
        tasks = state.get("syncTaskBean", {}).get("update", []) or []
        return [t for t in tasks if t.get("projectId") == inbox]

    def invalidate_cache(self):
        pass


_OFFICIAL_PROJECTS = [
    {"id": "p1", "name": "Работа", "groupId": "g1"},
    {"id": "p2", "name": "Покупки"},
]

_INBOX_TASKS = [
    {"id": "i1", "title": "Разобрать входящее", "projectId": INBOX_ID},
    {"id": "i2", "title": "Позвонить в банк", "projectId": INBOX_ID},
]


def _wire(monkeypatch, official=None, v2=None):
    official = official if official is not None else FakeOfficialV1(_OFFICIAL_PROJECTS)
    if v2 is None:
        v2 = FakeV2(projects=_OFFICIAL_PROJECTS,
                    groups=[{"id": "g1", "name": "Личное"}],
                    tasks=_INBOX_TASKS + [{"id": "t-x", "title": "Обычная задача",
                                           "projectId": "p1"}])
    monkeypatch.setattr(s, "ticktick", official)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    return official, v2


async def _client_call(name, **args) -> str:
    """Ровно то, что получает MCP-клиент по сети: инструмент вызывается ПО
    ИМЕНИ через реестр сервера, ответ склеивается из текстовых блоков. Никакого
    доступа к состоянию процесса — только то, что уехало бы в сокет."""
    res = await s.mcp.call_tool(name, args)
    blocks = res[0] if isinstance(res, tuple) else res
    return "\n".join(getattr(b, "text", "") or "" for b in blocks)


def _inbox_id_from_listing(text: str) -> str:
    """Достаёт id Inbox ИЗ КЛИЕНТСКОГО ТЕКСТА листинга — так проверяется, что
    напечатанный идентификатор годен для следующего вызова, а не только
    красиво выглядит."""
    m = re.search(r"Name: Inbox\n(?:.*\n)*?\(id: ([^)]+)\)", text)
    assert m, f"в листинге нет записи Inbox с id:\n{text}"
    return m.group(1)


# ─────────────────── 1. Inbox виден клиенту в списке ───────────────────

async def test_client_sees_inbox_in_project_listing(monkeypatch):
    _wire(monkeypatch)

    out = await _client_call("get_projects")

    assert "Name: Inbox" in out
    assert f"(id: {INBOX_ID})" in out
    # счётчик учитывает Inbox: 2 официальных + Inbox
    assert "Found 3 projects:" in out
    # обычные проекты никуда не делись
    assert "Name: Работа" in out and "Name: Покупки" in out


async def test_inbox_is_listed_first(monkeypatch):
    """Inbox — первым, как в самом TickTick; порядок остальных не меняется."""
    _wire(monkeypatch)

    out = await _client_call("get_projects")

    assert out.index("Name: Inbox") < out.index("Name: Работа") < out.index("Name: Покупки")


async def test_only_inbox_is_not_reported_as_no_projects(monkeypatch):
    """Пустой официальный список + существующий Inbox — это НЕ «проектов нет»."""
    _wire(monkeypatch, official=FakeOfficialV1([]),
          v2=FakeV2(projects=[], tasks=_INBOX_TASKS))

    out = await _client_call("get_projects")

    assert "No projects found." not in out
    assert "Name: Inbox" in out
    assert "Found 1 projects:" in out


# ──────────── 2. Напечатанный id реально работает дальше ────────────

async def test_inbox_id_from_listing_reads_inbox_tasks(monkeypatch):
    """Полный маршрут пользователя: «покажи проекты» → берём id Inbox из
    ответа → «покажи задачи этого проекта». Официальный API на этот id
    отвечает ошибкой, поэтому чтение обязано уйти в v2."""
    official, _ = _wire(monkeypatch)

    listing = await _client_call("get_projects")
    inbox_id = _inbox_id_from_listing(listing)

    out = await _client_call("get_project_tasks", project_id=inbox_id)

    assert "Разобрать входящее" in out and "Позвонить в банк" in out
    # Напечатанный id — НАСТОЯЩИЙ projectId задач Inbox, а не декоративная
    # метка, которую сервер потом сам же и распознаёт: клиент видит его в
    # строке каждой задачи.
    assert f"proj:{inbox_id})" in out       # закрывающая скобка = точное совпадение id
    assert "error" not in out.lower()
    # чужие задачи не подмешаны
    assert "Обычная задача" not in out
    # к официальному /project/<id>/data по Inbox-id не ходили вовсе
    assert official.data_calls == []


async def test_inbox_id_from_listing_opens_project_card(monkeypatch):
    official, _ = _wire(monkeypatch)

    listing = await _client_call("get_projects")
    inbox_id = _inbox_id_from_listing(listing)

    out = await _client_call("get_project", project_id=inbox_id)

    assert "Name: Inbox" in out
    assert f"(id: {inbox_id})" in out
    assert "Folder: (none)" in out
    assert "not found" not in out


async def test_create_into_inbox_id_from_listing_is_delivered(monkeypatch):
    """Создание по тому же id: identity-guard обязан признать Inbox живым
    проектом (иначе фикс печатал бы id, по которому нельзя создать задачу),
    а post-verify — подтвердить, что задача действительно в Inbox."""
    official, v2 = _wire(monkeypatch)

    listing = await _client_call("get_projects")
    inbox_id = _inbox_id_from_listing(listing)

    # created задача должна появиться в живом состоянии — как после реальной
    # записи, иначе post-verify честно скажет «не подтвердилось».
    orig_create = official.create_task

    def _create_and_sync(title, project_id, **kw):
        task = orig_create(title, project_id, **kw)
        v2._tasks.append(dict(task))
        return task

    official.create_task = _create_and_sync

    out = await s._create_tasks_impl(
        "Создаю во входящие",
        [{"title": "Из чата", "project_id": inbox_id, "project_name": "Inbox"}])

    assert "Создано 1" in out
    assert "Отказ" not in out and "Ошибки" not in out
    assert "а НЕ в запрошенный" not in out          # post-verify не ругается
    assert official.created and official.created[0]["projectId"] == inbox_id


# ─────────────── 3. Не дублировать и не выдумывать Inbox ───────────────

async def test_inbox_not_duplicated_if_official_api_ever_returns_it(monkeypatch):
    """Если TickTick когда-нибудь начнёт отдавать Inbox сам — записей должно
    остаться ровно одна."""
    official = FakeOfficialV1(
        [{"id": INBOX_ID, "name": "Inbox"}] + _OFFICIAL_PROJECTS)
    _wire(monkeypatch, official=official)

    out = await _client_call("get_projects")

    assert out.count("Name: Inbox") == 1
    assert out.count(f"(id: {INBOX_ID})") == 1
    assert "Found 3 projects:" in out


async def test_no_inbox_invented_without_v2(monkeypatch):
    """v2 недоступен → id Inbox взять НЕОТКУДА. Лучше не показать, чем
    напечатать выдуманный идентификатор."""
    _wire(monkeypatch, v2=None)
    monkeypatch.setattr(s, "ticktick_v2", None)

    out = await _client_call("get_projects")

    assert "Name: Inbox" not in out
    assert "Found 2 projects:" in out
    assert "Error" not in out


async def test_unreadable_v2_state_does_not_break_listing(monkeypatch):
    """Состояние v2 не прочиталось — листинг обычных проектов обязан
    работать (деградация, а не ошибка на весь инструмент)."""
    _wire(monkeypatch, v2=FakeV2(state_error="v2 down"))

    out = await _client_call("get_projects")

    assert "Found 2 projects:" in out
    assert "Name: Работа" in out
    assert "Name: Inbox" not in out


# ────────────── 4. Не сломаны папки и цена листинга ──────────────

async def test_folder_lines_survive_and_inbox_has_no_folder(monkeypatch):
    _wire(monkeypatch)

    out = await _client_call("get_projects")

    assert "Folder: Личное (id: g1)" in out          # у «Работы» папка на месте
    assert out.count("Folder:") == 3                 # строка есть у каждого
    inbox_block = out.split("Name: Inbox", 1)[1].split("Project ", 1)[0]
    assert "Folder: (none)" in inbox_block


async def test_listing_reads_v2_state_once(monkeypatch):
    """Папки и Inbox берутся из ОДНОГО снимка состояния — фикс не должен
    добавлять серверу второй поход за тем же самым."""
    _, v2 = _wire(monkeypatch)

    await _client_call("get_projects")

    assert v2.state_calls == 1


# ───────────────── 5. Двойник не добрее живого API ─────────────────

def test_fake_official_api_behaves_like_the_real_one():
    """Страховка на сам стенд: если фейк начнёт отдавать Inbox или отвечать
    на его id, тесты выше станут доказывать несуществующее."""
    official = FakeOfficialV1(_OFFICIAL_PROJECTS)
    assert all(p["id"] != INBOX_ID for p in official.get_projects())
    assert "error" in official.get_project(INBOX_ID)
    assert "error" in official.get_project_with_data(INBOX_ID)
