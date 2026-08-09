"""Тест-повторитель для П15 п.4 (1.3.6, докс/TZ/ZAHOD1.md, строки 174–191, 237):
«пройти по всем открытым задачам аккаунта с условием «название пустое» —
сколько таких, где лежат, есть ли вложения».

Роль этого файла — не «выборка сама по себе», а ПОВТОРИТЕЛЬ: он заново
прогоняет ТУ ЖЕ выборку на подставных данных и сверяет с зафиксированным
результатом (набором id). Порча условия выборки (например, снятие проверки
на пробельные/невидимые названия) обязана уронить его — это проверено ниже
руками (см. `test_a_broken_predicate_would_have_missed_cases` и отчёт в
финальном сообщении агента).

Определение «название пустое» НЕ пишется здесь заново — оно берётся из уже
существующего и отдельно протестированного `s._looks_untitled`
(ticktick_mcp/src/server.py), того самого предохранителя показа, что закрыл
живой инцидент с чеком Home Depot и скриншотом дефекта (см.
tests/test_untitled_tasks.py). Использовать готовую, проверенную функцию —
а не изобретать вторую копию того же правила — единственный способ не
разойтись с тем, что сервер УЖЕ считает «пустым» на боевом аккаунте.

`_looks_untitled` целиком покрывает список случаев из задания:
  * title is None;
  * title == "" (пустая строка);
  * title из одних пробелов/табов (`str.strip()`);
  * title из одного неразрывного пробела \\xa0 (тоже `.isspace()`, тоже режется
    `.strip()`);
  * title из одних невидимых символов — zero-width space, BOM, ZWJ, LRM/RLM,
    variation selector (`_INVISIBLE`, отдельный проход, раз `.strip()` их не
    видит).

Подзадачи в TickTick — обычные объекты в том же плоском списке (`parentId`
отличает их от корневых), поэтому отдельного обхода дерева не нужно: скан
идёт по плоскому списку, ровно как это делает production-код после того, как
все страницы `get_all_tasks`/`get_project_tasks` собраны в одну ленту.
"""
from typing import Dict, List, Optional, TypedDict

import ticktick_mcp.src.server as s


class UntitledHit(TypedDict):
    id: str
    project_id: str
    has_attachments: Optional[bool]  # None = источник не сообщил ничего (не «пусто»)
    has_text: bool


def scan_untitled(tasks: List[Dict]) -> List[UntitledHit]:
    """Ровно та выборка, что описана в задании: все задачи (включая
    подзадачи — они здесь обычные элементы списка), у которых `title`
    «выглядит пустым местом» по `s._looks_untitled`. Для каждой — проект,
    есть ли вложения (по `attachments` и по inline-ссылкам в `content`/
    `desc`), есть ли текст."""
    hits: List[UntitledHit] = []
    for t in tasks:
        if not s._looks_untitled(t.get("title")):
            continue
        n = s._task_attachment_count(t)
        hits.append({
            "id": t["id"],
            "project_id": t.get("projectId", ""),
            "has_attachments": (n > 0) if n is not None else None,
            "has_text": s._task_has_text(t),
        })
    return hits


# ─────────────────────────── подставные данные ───────────────────────────
# Задачи двух подпроектов + Inbox, часть — подзадачи (parentId), безымянные
# по РАЗНЫМ причинам из списка задания; остальные — обычные, с названиями,
# контрольная группа, которую выборка обязана НЕ тронуть.

INBOX = "inbox-fake"
P_WORK = "proj-work"
P_ARCH = "proj-archive"

TASKS = [
    # ── контрольная группа: обычные, с названиями — выборка их не трогает ──
    {"id": "t_named_1", "projectId": INBOX, "title": "Купить молоко"},
    {"id": "t_named_2", "projectId": P_WORK, "title": "Отправить инвойс"},
    {"id": "t_named_sub", "projectId": P_WORK, "title": "Подзадача с именем",
     "parentId": "t_named_2"},
    # заголовок начинается/кончается пробелом, но НЕ пуст целиком — контроль
    # на то, что `.strip()` не режет лишнего
    {"id": "t_named_padded", "projectId": INBOX, "title": "  Реальное дело  "},

    # ── None ──
    {"id": "t_none", "projectId": INBOX, "title": None},

    # ── пустая строка, БЕЗ вложений и БЕЗ текста → «пусто» ──
    {"id": "t_empty", "projectId": INBOX, "title": "", "attachments": []},

    # ── пробелы/табы ──
    {"id": "t_spaces", "projectId": P_WORK, "title": "   "},
    {"id": "t_tabs", "projectId": P_WORK, "title": "\t\t"},

    # ── неразрывный пробел (U+00A0) ──
    {"id": "t_nbsp", "projectId": P_WORK, "title": "\xa0\xa0\xa0"},

    # ── невидимые символы: zero-width space, BOM, их смесь с пробелом ──
    {"id": "t_zwsp", "projectId": INBOX, "title": "​"},
    {"id": "t_bom", "projectId": INBOX, "title": "﻿"},
    {"id": "t_mixed_invisible", "projectId": P_WORK, "title": " ​\xa0 "},

    # ── подзадача, безымянная (та самая «подзадачи — тоже задачи») ──
    {"id": "t_untitled_sub", "projectId": P_WORK, "title": "",
     "parentId": "t_named_2"},

    # ── безымянная, но с вложением (структурный массив) — как чек Home Depot ──
    {"id": "t_receipt", "projectId": INBOX, "title": "",
     "attachments": [{"fileName": "home-depot-receipt.jpg", "id": "a" * 24}]},

    # ── безымянная, с вложением, известным ТОЛЬКО из inline-ссылки в content ──
    {"id": "t_inline_att", "projectId": P_ARCH, "title": "",
     "content": "![file](" + "b" * 24 + "/scan.pdf)"},

    # ── безымянная, но с текстом в content (не вложение) ──
    {"id": "t_content_text", "projectId": P_WORK, "title": "",
     "attachments": [], "content": "вернуть до конца месяца"},

    # ── безымянная, текст лежит в desc, а не в content ──
    {"id": "t_desc_text", "projectId": P_ARCH, "title": "",
     "attachments": [], "desc": "проверить возврат"},
]

# Множество id, которые выборка ОБЯЗАНА найти — зафиксированный результат,
# с которым повторитель сверяется. Ровно 10: None, пустая строка, пробелы,
# табы, nbsp, zwsp, bom, смешанные невидимые, безымянная подзадача, чек,
# inline-вложение, текст-в-content, текст-в-desc — погоди, посчитаем точно.
EXPECTED_UNTITLED_IDS = {
    "t_none", "t_empty", "t_spaces", "t_tabs", "t_nbsp", "t_zwsp", "t_bom",
    "t_mixed_invisible", "t_untitled_sub", "t_receipt", "t_inline_att",
    "t_content_text", "t_desc_text",
}

# Контрольная группа — эти id выборка НИКОГДА не должна вернуть.
CONTROL_NAMED_IDS = {"t_named_1", "t_named_2", "t_named_sub", "t_named_padded"}


def test_scan_finds_exactly_the_untitled_set_no_more_no_less():
    """Приёмка повторителя: выборка на подставном наборе находит РОВНО
    зафиксированные id — ни лишних (ложные срабатывания на настоящих
    названиях), ни пропущенных (какой-то класс пустоты не пойман)."""
    hits = scan_untitled(TASKS)
    found_ids = {h["id"] for h in hits}

    assert found_ids == EXPECTED_UNTITLED_IDS, (
        f"пропущено: {EXPECTED_UNTITLED_IDS - found_ids}, "
        f"лишнее: {found_ids - EXPECTED_UNTITLED_IDS}")
    assert found_ids.isdisjoint(CONTROL_NAMED_IDS)


def test_padded_but_non_empty_title_is_not_flagged():
    """Контроль на пере-срабатывание: название с пробелами по краям, но с
    реальным текстом внутри, — НЕ безымянная задача."""
    hits = scan_untitled(TASKS)
    assert "t_named_padded" not in {h["id"] for h in hits}


def test_subtask_is_scanned_the_same_way_as_a_top_level_task():
    """«Подзадачи — они тоже задачи»: безымянная подзадача (`parentId`
    указывает на именованного родителя) обязана попасть в выборку наравне с
    корневыми."""
    hits = scan_untitled(TASKS)
    by_id = {h["id"]: h for h in hits}
    assert "t_untitled_sub" in by_id
    assert by_id["t_untitled_sub"]["project_id"] == P_WORK


def test_project_count_across_the_fixture():
    """Сколько ПРОЕКТОВ затронуто — второе из трёх требуемых чисел. В
    подставных данных безымянные распределены по трём: Inbox, P_WORK,
    P_ARCH."""
    hits = scan_untitled(TASKS)
    projects = {h["project_id"] for h in hits}
    assert projects == {INBOX, P_WORK, P_ARCH}
    assert len(projects) == 3


def test_attachment_flag_matches_source_not_guessed():
    """Третье число — вложения. Задача с файлом (в массиве ИЛИ только по
    inline-ссылке) обязана быть отмечена `has_attachments=True`; задача с
    текстом, но без файла — `False`, не `None` (массив вложений присутствовал
    и был пуст, значит источник ответил, не промолчал)."""
    hits = scan_untitled(TASKS)
    by_id = {h["id"]: h for h in hits}

    assert by_id["t_receipt"]["has_attachments"] is True
    assert by_id["t_inline_att"]["has_attachments"] is True
    assert by_id["t_content_text"]["has_attachments"] is False
    assert by_id["t_content_text"]["has_text"] is True
    assert by_id["t_desc_text"]["has_text"] is True
    assert by_id["t_empty"]["has_attachments"] is False
    assert by_id["t_empty"]["has_text"] is False

    total_with_attachments = sum(1 for h in hits if h["has_attachments"])
    assert total_with_attachments == 2  # t_receipt, t_inline_att


def test_a_broken_predicate_would_have_missed_cases():
    """Документирует В КОДЕ, зачем нельзя срезать угол на «просто `not
    title`»: такой урезанный предикат (без `.strip()` и без обхода
    `_INVISIBLE`) пропускает ЦЕЛЫЙ класс — пробелы, табы, nbsp и невидимые
    символы читаются им как «название есть». Живой прогон той же порчи в
    `s._looks_untitled` руками — отдельно, см. финальный отчёт: там показано,
    что `pytest` в этом файле реально краснеет, если убрать эту защиту в
    самой функции."""
    def broken_predicate(title):
        return not title  # ни .strip(), ни _INVISIBLE — намеренно урезано

    broken_hits = {t["id"] for t in TASKS if broken_predicate(t.get("title"))}
    real_hits = {h["id"] for h in scan_untitled(TASKS)}

    missed_by_broken = real_hits - broken_hits
    # Все классы «выглядит пустым, но truthy как строка» ускользают от урезанной проверки.
    assert missed_by_broken == {
        "t_spaces", "t_tabs", "t_nbsp", "t_zwsp", "t_bom", "t_mixed_invisible",
    }
    assert len(missed_by_broken) == 6
