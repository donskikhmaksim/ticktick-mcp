"""СТЕНД читающих инструментов: настоящие клиенты, подменён только ТРАНСПОРТ.

Зачем отдельный модуль, а не очередной класс `FakeV2` в каждом файле.

Мутационный эксперимент (2026-08-07) показал, чем кончается двойник, который
ПОВТОРЯЕТ логику клиента: снять фильтр по Inbox в НАСТОЯЩЕМ
`TickTickV2Client.get_inbox_tasks` (боевой ответ «во входящих 1477 задач»)
весь набор из 2072 тестов не заметил — фильтровал за него двойник. То же с
выборкой по тегу, с правилом фильтра и с порядком аргументов
`get_task_comments(project_id, task_id)`: двойник принимал их в любом порядке.

Здесь подменяется ровно один слой — HTTP:
  * v2  — `_request` (JSON-эндпоинты) и `session.get` (лента активности ходит
          мимо `_request`, см. `TickTickV2Client.get_task_activity`);
  * v1  — `_make_request`.
Всё, что выше транспорта (фильтрация, слияние источников вложений, разбор
правила фильтра, сортировка колонок), исполняется БОЕВОЕ. Транспорт при этом
не добрее живого API: неизвестный путь — это `AssertionError`, а не пустой
список, поэтому переставленные местами `project_id`/`task_id` видны как
ошибка, а не как «комментариев нет».

ВРЕМЯ. Снимок не зависит от момента запуска: «сегодня» — литерал `TODAY`, и
`s._today_local` подменяется на него же (тот самый приём, что в
`test_resolve_relative_date.py`). Сроки в снимке считаются ОТ `TODAY`, а не от
реальных часов, поэтому тест не может протухнуть через N дней и не может
покраснеть в первые часы суток по UTC.
"""
import json
import time
from datetime import date, timedelta

import ticktick_mcp.src.server as s
from ticktick_mcp.src.ticktick_client import TickTickClient
from ticktick_mcp.src.ticktick_v2_client import TickTickV2Client

# ─────────────────────────── постоянные снимка ───────────────────────────

TODAY = date(2026, 3, 15)          # «сегодня» стенда — литерал, не часы
INBOX_ID = "inbox-6a11"
P_WORK = "6a20work"
P_HOME = "6a21home"
P_ARCH = "6a22arch"                # закрытый/архивный проект
TASK_ROOT = "6a30root"
TASK_KID = "6a31kid"
TASK_GRANDKID = "6a32grandkid"
TASK_INBOX_1 = "6a33inbox"
TASK_INBOX_2 = "6a34inbox"
TASK_TAGGED = "6a35tagged"
TASK_HIGH = "6a36high"
TASK_MID = "6a37mid"
TASK_TODAY = "6a44today"
TASK_HIGH_2 = "6a45high"
TASK_HIGH_3 = "6a46high"
TASK_ASSIGNED_OPEN = "6a38asgopen"
TASK_ASSIGNED_DONE = "6a39asgdone"
TASK_ARCHIVED = "6a40arch"
TASK_COMPLETED = "6a41done"
TASK_TRASHED = "6a42trash"
TASK_ATT = "6a43att"
TASK_ATT_CONTENT = "6a51attc"
TASK_ATT_NOID = "6a52attn"
HABIT_ID = "6a50habit"
FILTER_ID = "6a60filter"
COL_A, COL_B, COL_C = "col-a", "col-b", "col-c"
# id вложения у TickTick — РОВНО 24 hex-символа: короче/иначе и разбор
# content-ссылок (`![file](<id>/<имя>)`) просто их не увидит, а тест
# «слияние двух источников» тихо перестанет что-либо проверять.
ATT_1 = "0ae67755c0e5411ab297476a"
ATT_2 = "1bf78866d1f6522bc3a8587b"
ATT_3 = "2ca89977e2a7633cd4b9698c"
ATT_CONTENT_ONLY = "3db9aa88f3b8744de5ca79ad"
ATT_NO_ID = "4ecabb99a4c9855ef6db8abe"
MEMBER_ME, MEMBER_OTHER = "111222", "333444"
COMMENT_ID = "cmt-9001"


def _noon(d: date) -> str:
    """Полдень UTC этого календарного дня — середина суток, поэтому день не
    уезжает ни в одну сторону ни при какой зоне владельца (±12 ч)."""
    return d.strftime("%Y-%m-%dT12:00:00.000+0000")


def _stamp(d: date, hh: int = 9) -> str:
    return d.strftime(f"%Y-%m-%dT{hh:02d}:30:00.000+0000")


def _tag(label):
    return {"name": label, "label": label}


TAGS = [_tag(f"тег{i:02d}") for i in range(1, 31)]   # 30 тегов: T-TAGS-2

TASKS = [
    {"id": TASK_ROOT, "projectId": P_WORK, "title": "Собрать отчёт",
     "status": 0, "priority": 3, "dueDate": _noon(TODAY + timedelta(days=2)),
     "createdTime": _stamp(TODAY - timedelta(days=5)),
     "modifiedTime": _stamp(TODAY - timedelta(days=1)),
     "columnId": COL_B, "commentCount": 1},
    {"id": TASK_KID, "projectId": P_WORK, "title": "Взять цифры у бухгалтерии",
     "status": 0, "priority": 0, "parentId": TASK_ROOT},
    {"id": TASK_GRANDKID, "projectId": P_WORK, "title": "Уточнить курс",
     "status": 0, "priority": 0, "parentId": TASK_KID},
    {"id": TASK_INBOX_1, "projectId": INBOX_ID, "title": "Разобрать входящее",
     "status": 0, "priority": 0},
    {"id": TASK_INBOX_2, "projectId": INBOX_ID, "title": "Позвонить в банк",
     "status": 0, "priority": 1},
    {"id": TASK_TAGGED, "projectId": P_HOME, "title": "Полить цветы",
     "status": 0, "priority": 0, "tags": ["тег01"]},
    {"id": TASK_HIGH, "projectId": P_HOME, "title": "Оплатить страховку",
     "status": 0, "priority": 5, "isAllDay": True,
     "dueDate": _noon(TODAY + timedelta(days=1))},
    {"id": TASK_MID, "projectId": P_HOME, "title": "Записаться к врачу",
     "status": 0, "priority": 3},
    # Срок ровно «сегодня» стенда: без него списки «на сегодня» пусты, и
    # инвариант «заголовок == телу» проверял бы пустоту (то есть ничего).
    {"id": TASK_TODAY, "projectId": P_HOME, "title": "Позвонить маме",
     "status": 0, "priority": 1, "dueDate": _noon(TODAY)},
    # Ещё два «срочных»: списки с одним элементом нечем усечь, а без усечения
    # инвариант «показано не всё — скажи об этом» ничего не проверяет.
    {"id": TASK_HIGH_2, "projectId": P_WORK, "title": "Продлить домен",
     "status": 0, "priority": 5},
    {"id": TASK_HIGH_3, "projectId": P_HOME, "title": "Забрать посылку",
     "status": 0, "priority": 5},
    {"id": TASK_ASSIGNED_OPEN, "projectId": P_WORK, "title": "Согласовать смету",
     "status": 0, "priority": 0, "assignee": MEMBER_OTHER},
    {"id": TASK_ASSIGNED_DONE, "projectId": P_WORK, "title": "Отправить договор",
     "status": 2, "priority": 0, "assignee": MEMBER_OTHER,
     "completedTime": _stamp(TODAY - timedelta(days=1))},
    # Вложение, известное ТОЛЬКО из текста задачи: структурный массив у
    # некоторых аккаунтов пуст — если код перестанет читать второй источник,
    # файл станет недостижим, и молча.
    {"id": TASK_ATT_CONTENT, "projectId": P_WORK, "title": "Скан паспорта",
     "status": 0, "priority": 0,
     "content": f"Скан во вложении\n![file]({ATT_CONTENT_ONLY}/паспорт.pdf)\n"},
    # Обратный случай: структурный массив есть, но БЕЗ id — id достаётся из
    # content по совпадению имени файла (наблюдалось живьём).
    {"id": TASK_ATT_NOID, "projectId": P_WORK, "title": "Полис",
     "status": 0, "priority": 0,
     "content": f"![file]({ATT_NO_ID}/полис.pdf)\n",
     "attachments": [{"fileName": "полис.pdf", "size": 512}]},
    {"id": TASK_ATT, "projectId": P_WORK, "title": "Оплатить счёт",
     "status": 0, "priority": 0,
     "content": (f"Документы\n![file]({ATT_1}/договор.pdf)\n"
                 f"![file]({ATT_2}/квитанция.pdf)\n![file]({ATT_3}/акт.pdf)\n"),
     "attachments": [
         {"id": ATT_1, "fileName": "договор.pdf", "size": 4096},
         {"id": ATT_2, "fileName": "квитанция.pdf", "size": 2048},
         {"id": ATT_3, "fileName": "акт.pdf", "size": 1024},
     ]},
]

COMPLETED = [
    {"id": TASK_COMPLETED, "projectId": P_WORK, "title": "Купить бумагу",
     "status": 2, "priority": 0,
     "completedTime": _stamp(TODAY - timedelta(days=2)),
     "createdTime": _stamp(TODAY - timedelta(days=9)),
     "modifiedTime": _stamp(TODAY - timedelta(days=2))},
    {"id": "6a47done", "projectId": P_HOME, "title": "Заплатить за свет",
     "status": 2, "priority": 0,
     "completedTime": _stamp(TODAY - timedelta(days=3)),
     "modifiedTime": _stamp(TODAY - timedelta(days=3))},
    {"id": "6a48done", "projectId": P_HOME, "title": "Сдать анализы",
     "status": 2, "priority": 0,
     "completedTime": _stamp(TODAY - timedelta(days=4)),
     "modifiedTime": _stamp(TODAY - timedelta(days=4))},
]

TRASHED = [
    {"id": TASK_TRASHED, "projectId": P_HOME, "title": "Старая затея",
     "status": 0, "priority": 0,
     "createdTime": _stamp(TODAY - timedelta(days=20)),
     "modifiedTime": _stamp(TODAY - timedelta(days=3))},
    {"id": "6a49trash", "projectId": P_WORK, "title": "Черновик письма",
     "status": 0, "priority": 0,
     "modifiedTime": _stamp(TODAY - timedelta(days=4))},
    {"id": "6a50trash", "projectId": P_WORK, "title": "Дубль задачи",
     "status": 0, "priority": 0,
     "modifiedTime": _stamp(TODAY - timedelta(days=5))},
]

ARCHIVED_TASKS = [
    {"id": TASK_ARCHIVED, "projectId": P_ARCH, "title": "Отчёт за прошлый год",
     "status": 0, "priority": 0},
]

PROJECTS = [
    {"id": P_WORK, "name": "Работа", "groupId": "grp-1", "kind": "TASK"},
    {"id": P_HOME, "name": "Дом", "kind": "TASK"},
    {"id": P_ARCH, "name": "Архив", "closed": True, "kind": "TASK"},
]

GROUPS = [{"id": "grp-1", "name": "Рабочее"}]

# Правило только по приоритету: `dueDate`-условие клиент считает от РЕАЛЬНЫХ
# часов (`_due_token_matches`), а стенд обязан быть от них независим.
FILTER_RULE = {"and": [{"conditionName": "priority", "or": [5]}]}
FILTERS = [{"id": FILTER_ID, "name": "Только срочное",
            "rule": json.dumps(FILTER_RULE)}]

HABITS = [{"id": HABIT_ID, "name": "Зарядка", "goal": 1.0, "unit": "Count",
           "totalCheckIns": 3, "status": 0, "type": "Boolean"}]

# Даты чек-инов — от TODAY, ключ YYYYMMDD (как у живого API).
def _cstamp(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


CHECKINS = [
    {"id": "ci-1", "habitId": HABIT_ID, "checkinStamp": _cstamp(TODAY - timedelta(days=2)),
     "status": 2, "value": 1, "goal": 1},
    {"id": "ci-2", "habitId": HABIT_ID, "checkinStamp": _cstamp(TODAY - timedelta(days=1)),
     "status": 1, "value": 0, "goal": 1},
    {"id": "ci-3", "habitId": HABIT_ID, "checkinStamp": _cstamp(TODAY),
     "status": 2, "value": 1, "goal": 1},
]

COLUMNS = [   # намеренно НЕ в порядке sortOrder: порядок обязан навести код
    {"id": COL_C, "name": "Готово", "sortOrder": 30, "projectId": P_WORK},
    {"id": COL_A, "name": "К работе", "sortOrder": 10, "projectId": P_WORK},
    {"id": COL_B, "name": "В работе", "sortOrder": 20, "projectId": P_WORK},
]

MEMBERS = [
    {"userId": MEMBER_ME, "displayName": "Максим", "username": "maksim",
     "isOwner": True, "accepted": True},
    {"userId": MEMBER_OTHER, "displayName": "Ирина", "username": "irina",
     "accepted": True},
]

COMMENTS = [
    {"id": COMMENT_ID, "title": "Цифры будут в пятницу",
     "userProfile": {"displayName": "Ирина"},
     "createdTime": _stamp(TODAY - timedelta(days=1))},
]

ACTIVITY = [
    {"id": "act-1", "action": "T_DONE", "when": _stamp(TODAY - timedelta(days=1), 14),
     "whoProfile": {"isMyself": True, "displayName": "Максим"}},
    {"id": "act-2", "action": "T_COLUMN", "when": _stamp(TODAY - timedelta(days=1), 15),
     "whoProfile": {"isMyself": False, "displayName": "Ирина"}},
    {"id": "act-3", "action": "T_DUE", "when": _stamp(TODAY - timedelta(days=2), 10),
     "whoProfile": {"isMyself": True},
     "dueDateBefore": _noon(TODAY), "dueDate": _noon(TODAY + timedelta(days=3))},
]

STATISTICS = {"score": 4210, "level": 7, "todayCompleted": 3,
              "yesterdayCompleted": 5, "totalCompleted": 918}


# ─────────────────────────── транспорт ───────────────────────────

class _V2Transport:
    """HTTP-слой v2. Ведёт журнал вызовов (путь + параметры), чтобы тест мог
    утверждать не только «что напечатано», но и «с чем позвали»."""

    def __init__(self, state, *, completed=None, trash=None, activity=None,
                 members=None, comments=None, columns=None, checkins=None,
                 statistics=None, habits=None):
        self.state = state
        self.completed = list(COMPLETED if completed is None else completed)
        self.trash = list(TRASHED if trash is None else trash)
        self.activity = list(ACTIVITY if activity is None else activity)
        self.members = list(MEMBERS if members is None else members)
        self.comments = list(COMMENTS if comments is None else comments)
        self.columns = list(COLUMNS if columns is None else columns)
        self.checkins = list(CHECKINS if checkins is None else checkins)
        self.statistics = dict(STATISTICS if statistics is None else statistics)
        self.habits = list(HABITS if habits is None else habits)
        self.calls = []          # [(method, path, kwargs), …]

    # ---- JSON-эндпоинты (TickTickV2Client._request) ----
    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        params = kwargs.get("params") or {}
        body = kwargs.get("json") or {}
        if path == "/batch/check/0":
            return self.state
        if path == "/project/all/completed":
            return list(self.completed)[:int(params.get("limit") or 50)]
        if path == "/project/all/trash/pagination":
            return {"tasks": list(self.trash)[:int(params.get("limit") or 50)]}
        if path == "/habits":
            return list(self.habits)
        if path == "/habitCheckins/query":
            after = int(body.get("afterStamp") or 0)
            # Живой API отдаёт СТРОГО позже afterStamp — сервер обязан сам
            # сдвинуть границу на день, иначе запрошенный день теряется.
            rows = [c for c in self.checkins if int(c["checkinStamp"]) > after]
            return {"checkins": {hid: rows for hid in (body.get("habitIds") or [])}}
        if path == f"/project/{P_WORK}/task/{TASK_ROOT}/comments":
            return list(self.comments)
        if path == f"/project/{P_WORK}/shares":
            return list(self.members)
        if path == "/statistics/general":
            return dict(self.statistics)
        if path == f"/column/project/{P_WORK}":
            return list(self.columns)
        if path.startswith("/column/project/"):
            return []
        if path.endswith("/comments"):
            # Существующий проект/задача, но пары такой нет — как у живого API.
            return []
        raise AssertionError(f"стенд не знает пути v2: {method} {path} {kwargs}")

    # ---- лента активности ходит мимо _request, через session.get ----
    def session_get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, {"params": params}))
        rows = [] if (params or {}).get("skip") else list(self.activity)
        outer = self

        class _Resp:
            status_code = 200
            text = "[]" if not rows else "[…]"

            def json(self_inner):
                return rows

            def raise_for_status(self_inner):
                return None

        del outer
        return _Resp()


class _V1Transport:
    """HTTP-слой официального API (TickTickClient._make_request)."""

    def __init__(self, projects, tasks, columns):
        self.projects = list(projects)
        self.tasks = list(tasks)
        self.columns = list(columns)
        self.calls = []

    def make_request(self, method, endpoint, data=None):
        self.calls.append((method, endpoint, data))
        if endpoint == "/project":
            return list(self.projects)
        if endpoint.endswith("/data"):
            pid = endpoint.split("/")[2]
            return {"project": next((p for p in self.projects if p["id"] == pid), {}),
                    "tasks": [t for t in self.tasks if t.get("projectId") == pid],
                    "columns": list(self.columns) if pid == P_WORK else []}
        parts = endpoint.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "project":
            p = next((x for x in self.projects if x["id"] == parts[1]), None)
            return p if p else {"error": f"project {parts[1]} not found"}
        if len(parts) == 4 and parts[0] == "project" and parts[2] == "task":
            t = next((x for x in self.tasks if x.get("id") == parts[3]), None)
            return t if t else {"error": f"task {parts[3]} not found"}
        raise AssertionError(f"стенд не знает пути v1: {method} {endpoint}")


def build_state(tasks=None, *, projects=None, tags=None, filters=None,
                inbox_id=INBOX_ID):
    return {
        "inboxId": inbox_id,
        "projectProfiles": [dict(p) for p in (PROJECTS if projects is None else projects)],
        "projectGroups": [dict(g) for g in GROUPS],
        "tags": [dict(t) for t in (TAGS if tags is None else tags)],
        "filters": [dict(f) for f in (FILTERS if filters is None else filters)],
        "syncTaskBean": {"update": [dict(t) for t in (TASKS if tasks is None else tasks)]},
    }


def wire(monkeypatch, *, state=None, v2_kwargs=None, v1_tasks=None):
    """Поднимает стенд и возвращает (v2_client, v1_client, v2_transport).

    Возвращаются НАСТОЯЩИЕ клиенты — у них подменён только транспорт."""
    state = build_state() if state is None else state
    transport = _V2Transport(state, **(v2_kwargs or {}))

    v2 = TickTickV2Client(token="stand-token")
    v2._request = transport.request
    v2._state_cache = state
    v2._state_ts = time.monotonic()

    class _Session:
        get = staticmethod(transport.session_get)

    v2.session = _Session()

    v1 = TickTickClient()
    all_v1_tasks = list(state["syncTaskBean"]["update"]) + list(ARCHIVED_TASKS)
    v1_transport = _V1Transport(PROJECTS,
                                all_v1_tasks if v1_tasks is None else v1_tasks,
                                COLUMNS)
    v1._make_request = v1_transport.make_request
    v1.transport = v1_transport

    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", v1)
    # Часы стенда: «сегодня» — литерал TODAY, от него же построены сроки.
    monkeypatch.setattr(s, "_today_local", lambda: TODAY)
    return v2, v1, transport


async def call(name: str, **args) -> str:
    """Ровно то, что уедет MCP-клиенту: вызов ПО ИМЕНИ через реестр сервера,
    склеенный текст ответа. Никакого доступа к состоянию процесса."""
    res = await s.mcp.call_tool(name, args)
    blocks = res[0] if isinstance(res, tuple) else res
    return "\n".join(getattr(b, "text", "") or "" for b in blocks)
