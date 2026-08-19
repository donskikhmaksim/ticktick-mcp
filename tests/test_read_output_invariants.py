"""ИНВАРИАНТЫ вывода читающих инструментов — проверяется не значение, а
САМОСОГЛАСОВАННОСТЬ ответа.

Почему именно так. Точечный тест «в выводе есть строка X» стареет вместе с
данными и ловит ровно тот случай, который в него записали. Инварианты ниже
не знают, сколько в аккаунте задач, и потому переживают любую смену фикстуры,
но валятся на КЛАССЕ дефектов, который весь день ловили руками:

  ИНВ-1  число в заголовке == числу напечатанных элементов
         (ловит «показано 100 из 497» без пометки и потерю подзадач);
  ИНВ-2  напечатанный id принимается следующим инструментом
         (ловит укороченный/чужой id — «скопировал и не работает»);
  ИНВ-3  в человеческом тексте нет сырых кодов и служебных чисел
         (`T_DONE`, `status: 2`, голый `+0000`);
  ИНВ-4  усечение всегда объявлено: отдали ровно `limit` — скажи, сколько
         всего и как дочитать;
  ИНВ-5  пустой аккаунт, `None` в полях и битые даты не роняют инструмент и
         не выглядят ошибкой;
  ИНВ-6  РЕЕСТР: каждый READONLY-инструмент сервера обязан быть в таблице
         ниже — новый читающий инструмент без разбора роняет набор.

Стенд — `tests/read_stand.py`: настоящие клиенты, подменён только транспорт,
вызов через `mcp.call_tool`. Проверяется исключительно текст ответа.
"""
import asyncio
import re
from datetime import timedelta

import pytest

import ticktick_mcp.src.server as s
from tests.read_stand import (
    HABIT_ID, P_WORK, TASK_ATT, TASK_ROOT, TODAY, build_state, call, wire)

_SINCE = (TODAY - timedelta(days=3)).isoformat()
_AFTER = (TODAY - timedelta(days=2)).isoformat()

# ─────────────────────────── таблица разбора ───────────────────────────
#
# На каждый READONLY-инструмент: чем его позвать, какой заголовок он обещает
# и что считать «элементом» в теле. `header`/`item` = None означает «этот
# инструмент не список» — тогда ИНВ-1 к нему не применяется, но ИНВ-3 и ИНВ-5
# применяются ко всем без исключения.

_ID = r"[0-9a-zA-Z_\-]+"


class Case:
    def __init__(self, args, header=None, item=None, *, paged=None,
                 task_ids=False, note=""):
        self.args = args
        self.header = header      # regex с одной группой — заявленное число
        self.item = item          # regex строки-элемента тела
        self.paged = paged        # имя параметра лимита, если инструмент постраничный
        self.task_ids = task_ids  # тело содержит id ЗАДАЧ (годятся для ИНВ-2)
        self.note = note


CASES = {
    # ── списки задач ──
    "get_all_tasks": Case({}, r"All open tasks \((\d+)\)", r"\(id:\S+ proj:",
                          paged="limit", task_ids=True),
    "get_project_tasks": Case({"project_id": P_WORK},
                              r"Found (\d+) tasks in project",
                              r"^\(id: \S+ \| project:", paged="limit",
                              task_ids=True),
    "get_inbox_tasks": Case({}, r"Inbox tasks \((\d+)\)", r"\(id:\S+ proj:",
                            paged="limit", task_ids=True),
    # task_ids=False намеренно: ИНВ-2 подаёт id в get_task_info, а тот по
    # построению читает ОТКРЫТЫЕ задачи и на завершённой честно отвечает «не
    # среди открытых — завершена, в корзине или в архивном проекте». Это
    # ограничение инструмента второго шага, а не кривой id.
    # paged=None намеренно: у этого инструмента `limit` — это ЗАПРОС к API
    # (уходит в /project/all/completed), а не срез печати, поэтому «сколько
    # там всего» сервер физически не знает и назвать не может. Его
    # собственная честная пометка — «просили больше, чем лента отдаёт за
    # вызов» — проверяется в tests/test_completed_limit.py.
    "get_completed_tasks": Case({}, r"Completed tasks \((\d+)\)",
                                r"\(id:\S+ proj:"),
    "get_trash": Case({}, r"Trashed tasks \((\d+)\)", r"\(id:\S+ proj:",
                      paged="limit"),
    "get_tasks_by_tag": Case({"tag": "тег01"}, r"Tasks tagged '[^']*' \((\d+)\)",
                             r"\(id:\S+ proj:", task_ids=True),
    "get_tasks_by_priority": Case({"priority_id": 5},
                                  r"^Tasks that are '[^\n]*' \((\d+)\):",
                                  r"\(id:\S+ proj:", paged="limit", task_ids=True),
    "get_tasks_by_assignee": Case({"assignee": "Ирина"},
                                  r"— (\d+):", r"\(id:\S+ proj:", task_ids=True),
    "get_tasks_due_today": Case({}, r"'due today' \((\d+)\)", r"\(id:\S+ proj:"),
    "get_tasks_due_tomorrow": Case({}, r"'due tomorrow' \((\d+)\)", r"\(id:\S+ proj:"),
    "get_tasks_due_this_week": Case({}, r"'due this week' \((\d+)\)", r"\(id:\S+ proj:"),
    "get_tasks_due_in_days": Case({"days": 2}, r"\((\d+)\)", r"\(id:\S+ proj:"),
    "get_overdue_tasks": Case({}, r"'overdue' \((\d+)\)", r"\(id:\S+ proj:"),
    "get_next_tasks": Case({}, r"'next' \((\d+)\)", r"\(id:\S+ proj:"),
    "get_engaged_tasks": Case({}, r"'engaged' \((\d+)\)", r"\(id:\S+ proj:"),
    "get_recurring_tasks": Case({}, None, None,
                                note="на стенде повторов нет — предмет ИНВ-5"),
    "search_tasks": Case({"search_term": "отчёт"},
                         r"Tasks matching '[^']*' \((\d+)\)", r"\(id:\S+ proj:",
                         task_ids=True),
    "search_all_tasks": Case({"query": "отчёт", "scope": "open"},
                             r"── Open projects \((\d+)\) ──", r"\(id:\S+ proj:",
                             task_ids=True),
    "run_filter": Case({"filter": "Только срочное"},
                       r"Filter '[^']*' — (\d+) task\(s\)", r"\(id:\S+ proj:",
                       paged="limit", task_ids=True),
    # ── прочие списки ──
    "get_projects": Case({}, r"Found (\d+) projects", r"^\(id: "),
    "list_project_groups": Case({}, r"Project groups \((\d+)\)", r"^- .*\(id: "),
    "list_tags": Case({}, r"Tags \((\d+)\)", r"^- "),
    "list_filters": Case({}, r"Filters \((\d+)\)", r"^- .*\(id: "),
    "list_project_columns": Case({"project_id": P_WORK},
                                 r"Columns of project \S+ \((\d+)\)",
                                 r"^- .*\(id: "),
    "get_project_members": Case({"project_id": P_WORK},
                                r"Участники проекта «[^»]*» \((\d+)\)", r"^- "),
    "list_task_attachments": Case({"task_id": TASK_ATT, "project_id": P_WORK},
                                  r"Attachments on task \S+ \((\d+)\)",
                                  r"^\s+\d+\. "),
    "get_habits": Case({}, r"Habits \((\d+)\)", r"^- .*\(id: "),
    "get_habit_checkins": Case({"habit_name": "Зарядка", "habit_id": HABIT_ID,
                                "after_date": _AFTER},
                               r"— (\d+) entries", r"^- \d{4}-\d{2}-\d{2}:"),
    "get_task_comments": Case({"task_title": "Собрать отчёт", "project_id": P_WORK,
                               "task_id": TASK_ROOT},
                              r"Comments on '[^']*' \((\d+)\)", r"^- \(id:"),
    "get_task_activity": Case({"task_id": TASK_ROOT, "project_id": P_WORK},
                              r"Activity log \((\d+) events", r"^  \d{4}-\d{2}-\d{2} "),
    "get_changes": Case({"since": _SINCE, "until": TODAY.isoformat()},
                        r"Изменения с \S+ по \S+ \((\d+)\)",
                        r"^\S+ \d{4}-\d{2}-\d{2} \d{2}:\d{2} ", paged="limit"),
    # ── одиночные карточки и справочники (не списки) ──
    "get_project": Case({"project_id": P_WORK}),
    "get_task": Case({"project_id": P_WORK, "task_id": TASK_ROOT}),
    "get_task_info": Case({"task_id": TASK_ROOT}),
    "get_statistics": Case({}),
    "get_attachment_download_url": Case({"task_id": TASK_ATT, "project_id": P_WORK,
                                         "index": 2}),
    "build_reminder": Case({"minutes_before": 30},
                           note="чистая функция, стенд не нужен"),
    "build_recurrence_rule": Case({"frequency": "weekly"},
                                  note="чистая функция, стенд не нужен"),
    "operation_report": Case({"record_id": "нет-такой-записи"},
                             note="отчёт по журналу; на стенде — честное «не найдено»"),
    "plan_task_creation": Case({"summary": "план",
                                "tasks": [{"title": "Купить бумагу",
                                           "project_id": P_WORK}]},
                               note="readOnlyHint СНЯТ (QA-2, 2026-08-19): "
                                    "kill-switch-ветка мутирует; здесь "
                                    "проверяется обычный плановый вывод"),
    "plan_task_deletion": Case({"summary": "план",
                                "tasks": [{"task_id": TASK_ROOT,
                                           "title": "Собрать отчёт"}]},
                               note="readOnlyHint СНЯТ (QA-2, 2026-08-19): "
                                    "kill-switch-ветка мутирует; здесь "
                                    "проверяется обычный плановый вывод"),
    # header=None/item=None: на стенде TASKS ни одна задача не безымянна —
    # ИНВ-1/2/4 (заголовок==телу, id годен дальше, усечение объявлено) здесь
    # проверять нечем, тот же случай, что у get_recurring_tasks выше.
    # П15 п.4 (1.3.5, 2026-08-09): count-разбивку и постраничность отдельно
    # покрывает tests/test_find_untitled.py — там же обе ловушки задания.
    "find_untitled_tasks": Case({}, None, None,
                                note="на стенде безымянных задач нет — предмет ИНВ-5"),
}

# Разобраны здесь ДОБРОВОЛЬНО, хотя readOnlyHint с них снят (2026-08-19,
# разбор QA-2, tests/test_readonly_hint_contract.py): kill-switch-ветка
# делает эти «планы» мутирующими, но их обычный (плановый) вывод по-прежнему
# обязан держать инварианты этого файла — терять разбор вместе с аннотацией
# было бы потерей покрытия, а не честностью.
_VOLUNTARY_CASES = {"plan_task_creation", "plan_task_deletion"}

_LIST_CASES = [n for n, c in CASES.items() if c.header]
_PAGED_CASES = [n for n, c in CASES.items() if c.paged]
_TASK_ID_CASES = [n for n, c in CASES.items() if c.task_ids]


@pytest.fixture
def stand(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    return wire(monkeypatch)


# ═════════ ИНВ-6. Реестр покрытия ═════════

def test_every_readonly_tool_is_covered_by_this_file():
    """Структурная ловушка (тот же приём, что в `test_doubles_do_not_cheat.py`):
    список читающих инструментов берётся ИЗ РЕЕСТРА СЕРВЕРА, а не из головы.
    Появился новый READONLY-инструмент — набор краснеет, пока его не разобрали
    здесь: аргументы, заголовок, элемент тела. Это единственная проверка,
    которая ловит НЕ НАПИСАННЫЙ тест."""
    tools = asyncio.run(s.mcp.list_tools())
    registry = {t.name for t in tools
                if getattr(t.annotations, "readOnlyHint", False)}
    missing = sorted(registry - set(CASES))
    stale = sorted(set(CASES) - registry - _VOLUNTARY_CASES)
    assert not missing, (
        f"READONLY-инструменты без разбора в CASES: {missing} — добавь их сюда "
        "вместе с аргументами и формой вывода, иначе они не проверяются ничем")
    assert not stale, (
        f"в CASES числятся инструменты, которых в реестре больше нет: {stale}")


# ═════════ ИНВ-1. Заголовок == телу ═════════

@pytest.mark.parametrize("name", _LIST_CASES)
async def test_header_count_equals_printed_items(name, stand):
    """Число, которым инструмент представляется, обязано совпадать с тем,
    сколько строк он реально напечатал.

    Именно этот инвариант ловит и потерю подзадач при постраничной нарезке, и
    «Trashed tasks (50)» при 497 в корзине, и вранью счётчика группы в
    search_all_tasks — не зная при этом ни одного значения из аккаунта."""
    case = CASES[name]
    out = await call(name, **case.args)

    m = re.search(case.header, out, re.MULTILINE)
    assert m, f"{name}: в выводе нет объявленного числа ({case.header}):\n{out}"
    declared = int(m.group(1))
    printed = len(re.findall(case.item, out, re.MULTILINE))
    assert declared == printed, (
        f"{name}: заголовок обещает {declared}, напечатано {printed}:\n{out}")


# ═════════ ИНВ-2. Напечатанный id годен для следующего шага ═════════

@pytest.mark.parametrize("name", _TASK_ID_CASES)
async def test_printed_task_ids_are_accepted_by_the_next_tool(name, stand):
    """Идентификатор берётся ИЗ ТЕКСТА ответа — единственного, что доходит до
    клиента, — и подаётся следующему инструменту. Укороченный, обрезанный или
    подменённый id виден сразу, а не в момент, когда человек пытается им
    воспользоваться."""
    case = CASES[name]
    out = await call(name, **case.args)

    ids = re.findall(r"\(id: ?(" + _ID + r")[ )|]", out)
    assert ids, f"{name}: в выводе нет ни одного id:\n{out}"
    for task_id in ids[:3]:
        card = await call("get_task_info", task_id=task_id)
        assert "not found" not in card.lower(), (
            f"{name} напечатал id {task_id}, а get_task_info его не принимает:\n{card}")


# ═════════ ИНВ-3. Никаких сырых кодов и служебных чисел ═════════

_RAW_ACTION_CODE = re.compile(r"\bT_[A-Z][A-Z_]{2,}\b")
_RAW_STATUS = re.compile(r"\bstatus: -?\d\b")
_RAW_PRIORITY = re.compile(r"\bpriority: [0-5]\b")
# Сырой инстант TickTick: «2026-03-17T12:00:00.000+0000» — человеку нечитаем
# и (главное) не переведён в зону владельца.
_RAW_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+0000")


@pytest.mark.parametrize("name", sorted(CASES))
async def test_output_speaks_words_not_wire_codes(name, stand):
    """Человеческий текст не должен содержать словаря протокола.

    Живой пример: «someone T_DONE» — код действия вывалился сырьём и читался
    как название действия, поэтому пропуск в словаре меток был невидим. Тот же
    класс — `status: 2` вместо «Completed» и сырой UTC-инстант вместо дня в
    зоне владельца."""
    case = CASES[name]
    out = await call(name, **case.args)

    body = _strip_rule_lines(out)
    assert not _RAW_ACTION_CODE.search(body), f"{name}: сырой код действия:\n{out}"
    assert not _RAW_STATUS.search(body), f"{name}: сырой status:\n{out}"
    assert not _RAW_PRIORITY.search(body), f"{name}: сырой priority:\n{out}"
    assert not _RAW_INSTANT.search(body), f"{name}: сырой UTC-инстант:\n{out}"


def _strip_rule_lines(out: str) -> str:
    """`list_filters` печатает правило фильтра ДОСЛОВНО — это машинный текст
    самого TickTick, и он обязан оставаться дословным (см.
    test_read_params_are_honoured). Единственное исключение, и оно именное."""
    return "\n".join(ln for ln in out.splitlines()
                     if not ln.strip().startswith("rule:"))


# ═════════ ИНВ-4. Усечение всегда объявлено ═════════

@pytest.mark.parametrize("name", _PAGED_CASES)
async def test_truncation_is_always_disclosed(name, stand):
    """Отдал ровно `limit` элементов — обязан сказать, сколько всего и как
    дочитать. Молчаливое усечение неотличимо от «это всё, что есть»: именно
    так «в корзине 50 задач» означало 497."""
    case = CASES[name]
    full = await call(name, **case.args)
    total = int(re.search(case.header, full, re.MULTILINE).group(1))
    if total < 2:
        pytest.skip(f"{name}: на стенде нечего усекать (всего {total})")

    cut = await call(name, **{**case.args, case.paged: 1})

    printed = len(re.findall(case.item, cut, re.MULTILINE))
    # Ровно `limit` печатать не обязательно: страница деревьев доезжает до
    # конца поддерева, чтобы подзадача не осиротела на границе. Обязательно
    # другое — сказать, что показано не всё.
    assert printed < total, (
        f"{name}: limit=1 ничего не сузил ({printed} из {total}):\n{cut}")
    assert str(total) in cut, (
        f"{name}: усечённый ответ не называет полное число {total}:\n{cut}")
    assert re.search(r"more|offset|из|showing", cut, re.IGNORECASE), (
        f"{name}: усечение не объявлено и нечем дочитать:\n{cut}")


# ═════════ ИНВ-5. Пусто и битое — не ошибка ═════════

# id совпадают с теми, которыми инструменты зовутся в CASES: иначе карточки
# ответили бы «задача не найдена» и битые поля не дошли бы до кода вовсе —
# тест бы «проходил», не проверив ничего.
_BROKEN_TASKS = [
    {"id": TASK_ROOT, "projectId": P_WORK, "title": None, "priority": None,
     "status": None, "dueDate": "не дата", "startDate": None, "tags": None,
     "items": None, "attachments": None, "content": None, "reminders": None,
     "createdTime": None, "modifiedTime": "битая метка"},
    {"id": TASK_ATT, "projectId": None, "title": "Без проекта",
     "dueDate": "2026-13-45T99:99:99.000+0000", "attachments": None,
     "content": None},
    {"id": "b3", "projectId": P_WORK, "title": "Пустые поля",
     "parentId": TASK_ROOT, "items": None, "tags": None},
]


@pytest.mark.parametrize("name", sorted(CASES))
async def test_empty_account_answers_plainly(name, monkeypatch):
    """Пустой аккаунт — это ответ, а не сбой: инструмент обязан вернуть
    строку и не выглядеть сломанным."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    wire(monkeypatch, state=build_state(tasks=[], tags=[], filters=[]),
         v2_kwargs={"completed": [], "trash": [], "activity": [], "members": [],
                    "comments": [], "columns": [], "checkins": [],
                    "statistics": {}, "habits": []},
         v1_tasks=[])

    out = await call(name, **CASES[name].args)

    assert isinstance(out, str) and out.strip(), f"{name}: пустой ответ"
    _assert_not_a_crash(name, out)


@pytest.mark.parametrize("name", sorted(CASES))
async def test_broken_fields_do_not_crash_the_tool(name, monkeypatch):
    """`None` вместо строки и неразбираемая дата приходят от живого API
    регулярно. Инструмент обязан их пережить: исключение здесь означает, что
    у владельца весь инструмент отвечает трейсбеком из-за одной кривой
    задачи."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    wire(monkeypatch, state=build_state(tasks=_BROKEN_TASKS),
         v1_tasks=list(_BROKEN_TASKS))

    out = await call(name, **CASES[name].args)

    assert isinstance(out, str) and out.strip(), f"{name}: пустой ответ"
    _assert_not_a_crash(name, out)


def _assert_not_a_crash(name: str, out: str) -> None:
    low = out.lower()
    for bad in ("traceback", "nonetype", "attributeerror", "typeerror",
                "keyerror", "valueerror"):
        assert bad not in low, f"{name}: ответ выглядит как авария:\n{out}"
