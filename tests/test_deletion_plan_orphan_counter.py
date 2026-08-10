"""Д16 (1.1.8, docs/TZ/ZAHOD1.md:1022-1062) — план удаления БЕЗ `with_subtasks`
молчал о живых детях, которых удаление родителя осиротит: строка называла
только имя, проект и id. `manual_triage` про то же самое действие честно
предупреждает всегда (`_triage_orphan_note`), так что два пути к одному
результату давали разное предупреждение.

`plan_task_deletion` уже строит индекс детей `kids` безусловно (ради
разворачивания поддерева по `with_subtasks`) из уже прочитанного живого
снимка — без единого лишнего сетевого чтения. Правка использует ЭТОТ ЖЕ
индекс, чтобы для строк без `with_subtasks` посчитать детей, которых НЕТ в
самом плане, и печатает про них текст `_triage_orphan_note` — ту же формулу,
которой уже пользуется `manual_triage`, а не вторую формулировку.

Что здесь закреплено:
  1. строка родителя без `with_subtasks` называет число живых детей;
  2. строка с `with_subtasks` про сирот не говорит — дети уже в самом плане;
  3. частичный случай: один из трёх детей отдельной строкой уже в том же
     плане — родитель говорит про двух, не про трёх;
  4. бездетная задача хвоста не получает;
  5. числовые формы (1/2/5) посимвольно совпадают с тем, что печатает
     `manual_triage` для того же числа детей — один и тот же вызов
     `_triage_orphan_note`, а не независимая копия текста;
  6. `_open_by_id` вызывается ровно один раз за план — счётчик живых детей не
     стоил ни одного лишнего сетевого чтения;
  7. начало строки плана бездетной задачи (имя, проект, id) не изменилось.
"""
import re

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


@pytest.fixture(autouse=True)
def _isolate_manifests():
    """`_MANIFESTS` — модульный глобал на весь процесс тестов; чужой манифест
    из соседнего файла не должен просочиться сюда и наоборот."""
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


_NAMES = {"p1": "Проект"}


def _live_with_children():
    """Родитель с тремя открытыми детьми плюс одна бездетная задача. `kids` в
    `plan_task_deletion` строится ровно из такого словаря (по `parentId`),
    поэтому третий, «закрытый», ребёнок сюда намеренно не входит — открытых
    задач в live-снимке и так только эти пять; закрытые в `_open_by_id`
    (v2 live snapshot открытых задач) не попадают в принципе, а значит и в
    `kids` не попадут тоже (способ подделки №3 из TZ: счёт НЕ по завершённым
    детям)."""
    return {
        "parent": {"id": "parent", "title": "Ремонт квартиры", "projectId": "p1"},
        "c1": {"id": "c1", "title": "Выбрать плитку", "projectId": "p1",
               "parentId": "parent"},
        "c2": {"id": "c2", "title": "Вызвать замерщика", "projectId": "p1",
               "parentId": "parent"},
        "c3": {"id": "c3", "title": "Купить обои", "projectId": "p1",
               "parentId": "parent"},
        "solo": {"id": "solo", "title": "Оплатить интернет", "projectId": "p1"},
    }


def _wire(monkeypatch, live, names=None):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(names or _NAMES))
    # Слой Telegram выключен явно (2026-08-06, тот же паттерн, что в
    # tests/test_plan_id_visible.py) — иначе поведение зависело бы от того,
    # заданы ли TG_* переменные окружения в момент прогона.
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


def _numbered_lines(plan_text: str):
    """Строки плана вида `N. ...` — по одной на каждый пункт манифеста."""
    return re.findall(r"^\d+\.\s.*$", plan_text, flags=re.M)


# ═══════ 1. Без with_subtasks — родитель называет число живых детей ═════════

async def test_plan_without_subtree_flag_counts_live_children(monkeypatch):
    live = _live_with_children()
    _wire(monkeypatch, live)

    out = await s.plan_task_deletion("Удаляю родителя", [
        {"taskId": "parent", "title": "Ремонт квартиры"}])

    assert "3 открытые подзадачи — они останутся без родителя" in out
    # план на этой ветке — РОВНО одна строка (дети не разворачиваются без
    # with_subtasks): считать по ним ничего, кроме хвоста, не должно.
    assert len(_numbered_lines(out)) == 1


# ═══════ 2. С with_subtasks — дети уже в плане, про сирот речи нет ═════════

async def test_children_already_in_the_plan_are_not_counted_as_orphans(
        monkeypatch):
    live = _live_with_children()
    _wire(monkeypatch, live)

    out = await s.plan_task_deletion("Удаляю родителя с поддеревом", [
        {"taskId": "parent", "title": "Ремонт квартиры",
         "with_subtasks": True}])

    # родитель и три ребёнка — четыре строки плана.
    assert len(_numbered_lines(out)) == 4
    assert "останутся без родителя" not in out


# ═══════ 3. Частичный случай: один ребёнок уже отдельной строкой в плане ════

async def test_partial_case_counts_only_children_not_already_in_the_plan(
        monkeypatch):
    live = _live_with_children()
    _wire(monkeypatch, live)

    out = await s.plan_task_deletion(
        "Удаляю родителя и одного ребёнка отдельно", [
            {"taskId": "parent", "title": "Ремонт квартиры"},
            {"taskId": "c1", "title": "Выбрать плитку"}])

    assert "2 открытые подзадачи — они останутся без родителя" in out
    assert "3 открытые подзадачи" not in out
    assert "3 открытых подзадач" not in out


# ═══════ 4. Бездетная задача хвоста не получает ═════════════════════════════

async def test_childless_task_gets_no_orphan_tail(monkeypatch):
    live = _live_with_children()
    _wire(monkeypatch, live)

    out = await s.plan_task_deletion("Удаляю бездетную задачу", [
        {"taskId": "solo", "title": "Оплатить интернет"}])

    assert "останутся без родителя" not in out
    assert "подзадач" not in out


# ═══════ 5. Числовые формы (1/2/5) посимвольно совпадают с manual_triage ════

async def _plan_deletion_tail_for(monkeypatch, n_children: int) -> str:
    live = {"parent": {"id": "parent", "title": "Родитель", "projectId": "p1"}}
    for i in range(n_children):
        live[f"c{i}"] = {"id": f"c{i}", "title": f"Ребёнок {i}",
                         "projectId": "p1", "parentId": "parent"}
    _wire(monkeypatch, live)
    return await s.plan_task_deletion(f"Удаляю родителя ({n_children})", [
        {"taskId": "parent", "title": "Родитель"}])


async def _manual_triage_tail_for(monkeypatch, tmp_path, n_children: int) -> str:
    live = {"parent": {"id": "parent", "title": "Родитель", "projectId": "p1"}}
    for i in range(n_children):
        live[f"c{i}"] = {"id": f"c{i}", "title": f"Ребёнок {i}",
                         "projectId": "p1", "parentId": "parent"}
    _wire(monkeypatch, live)
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    return await s.manual_triage(f"Разбираю ({n_children})", [
        {"op": "delete", "task_id": "parent", "title": "Родитель",
         "said": "тест"}])


async def test_numeric_forms_match_manual_triage_for_1_2_5(monkeypatch,
                                                            tmp_path):
    for n in (1, 2, 5):
        expected = s._triage_orphan_note(n)
        deletion_out = await _plan_deletion_tail_for(monkeypatch, n)
        triage_out = await _manual_triage_tail_for(monkeypatch, tmp_path, n)
        assert expected in deletion_out, (
            f"plan_task_deletion, n={n}: {deletion_out}")
        assert expected in triage_out, (
            f"manual_triage, n={n}: {triage_out}")


# ═══════ 6. Ни одного лишнего сетевого чтения ═══════════════════════════════

async def test_no_extra_open_by_id_calls_for_the_counter(monkeypatch):
    live = _live_with_children()
    calls = []

    def _counting_open_by_id(fresh=False):
        calls.append(fresh)
        return dict(live)

    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", _counting_open_by_id)
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(_NAMES))
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))

    await s.plan_task_deletion("Удаляю родителя", [
        {"taskId": "parent", "title": "Ремонт квартиры"}])

    assert len(calls) == 1, (
        f"счётчик живых детей стоил лишних чтений живого состояния: {calls}")


# ═══════ 7. Начало строки плана бездетной задачи не изменилось ═════════════

async def test_line_start_for_childless_task_is_unchanged(monkeypatch):
    live = _live_with_children()
    _wire(monkeypatch, live)

    out = await s.plan_task_deletion("Удаляю бездетную задачу", [
        {"taskId": "solo", "title": "Оплатить интернет"}])

    line = _numbered_lines(out)[0]
    assert line == "1. **«Оплатить интернет»** — Проект (`solo`)", line
