"""Д6, второй слой защиты на создании — теги (ветка дерева) и исполнитель
(оба пути создания, одиночный путь изменения).

Контекст (см. docs/TZ/ZAHOD1.md, «второй слой защиты на создании — теги и
исполнитель»). У остальных дефектов этого семейства защита ДВУХСЛОЙНАЯ:
сервер читает отказ, спрятанный внутри успешного ответа канала (id2error,
см. tests/test_create_link_and_tag_silent_failures.py), И ОТДЕЛЬНО перечитывает
живое состояние, сверяя «что просили = что стало», И кладёт ожидание в
журнал, чтобы независимый отчёт (operation_report) мог поймать расхождение
позже, даже если сам вызов ничего не заметил. У назначения исполнителя при
создании был только первый слой; у тегов в ветке создания ДЕРЕВА (вложенные
подзадачи словарями) не было ни одного — теги ехали внутри полезной нагрузки
создания, мимо проверенного ядра простановки тегов (_apply_tags_verified).

Приём тестов ниже — ПО ФАКТУ, а не по тексту ответа: журнальная запись
(её обязана прочитать независимая перепроверка) и список вызовов подделки
канала (`v2.calls`), а не строки, которые первый слой печатает и на старом
коде. Ровно это отличает второй слой от первого — см. «Проверка откатом» в
ЗАХОД1.md."""
import json

import ticktick_mcp.src.server as s
from tests.test_create_link_and_tag_silent_failures import (
    _FakeV2, _FakeOfficial, _PROJECTS, _wire,
)


class _FakeV2AssigneeTypeDrift(_FakeV2):
    """Тот же честный канал, что и в tests/test_create_link_and_tag_silent_failures.py,
    но исполнитель в живом состоянии оседает СТРОКОЙ, даже когда вызывающий
    передал число — воспроизводит реальный дрейф типов TickTick (assignee
    приходит то числом, то строкой). Правка «сравнивать str(got)==str(want)»
    не имеет права путать это с настоящим провалом назначения."""

    def batch_update_tasks(self, changes):
        resp = super().batch_update_tasks(changes)
        for c in changes:
            if "assignee" in c and c["taskId"] in self.live:
                self.live[c["taskId"]]["assignee"] = str(c["assignee"])
        return resp


class _FakeV2SilentAssigneeDrop(_FakeV2):
    """Канал ПРИНИМАЕТ /batch/task без единой записи в id2error, но значение
    "assignee" не доезжает до живого состояния — молчаливая потеря, которую
    первый слой (чтение id2error) физически не может увидеть: ловит её
    только живая пере-проверка «что просили = что стало»."""

    def batch_update_tasks(self, changes):
        resp = super().batch_update_tasks(changes)
        for c in changes:
            if "assignee" in c and c["taskId"] in self.live:
                self.live[c["taskId"]].pop("assignee", None)
        return resp


def _journal_record(tmp_path):
    return json.loads((tmp_path / "deletion_journal.jsonl")
                      .read_text(encoding="utf-8").strip())


# ===========================================================================
# Готово-когда 1 / Проверка откатом 1 (правка 1: обычный путь создания)
# ===========================================================================

async def test_assignee_expectation_reaches_the_journal(monkeypatch, tmp_path):
    """Ожидание по исполнителю обязано попасть в журнал операции создания —
    это то, что даёт независимой перепроверке (operation_report) шанс
    поймать тихий провал ПОЗЖЕ, даже если сам вызов инструмента ничего не
    заметил (первый слой уже печатает текст и на старом коде — сверка по
    тексту ничего не доказала бы).

    ПРОВЕРКА ОТКАТОМ (ЗАХОД1.md, Д6): откати правку 1 (расширение to_verify
    и журнального ожидания в обычном пути _create_tasks_impl) — этот тест
    обязан упасть на ОТСУТСТВИИ ключа "assignee" в журнальной записи, а не
    на тексте ответа."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _wire(monkeypatch)

    result = await s._create_tasks_impl("тест", [
        {"title": "Согласовать смету", "project_id": "pA", "assignee": 42}])
    assert "operation_report" in result

    rec = _journal_record(tmp_path)
    item = next(i for i in rec["items"] if i["title"] == "Согласовать смету")
    assert item["expect"].get("assignee") == 42, (
        f"ожидание по исполнителю не попало в журнал: {item['expect']}")


# ===========================================================================
# Готово-когда 4 / Проверка откатом 2 (правка 2: ветка дерева, теги корня)
# ===========================================================================

async def test_tree_branch_tags_go_through_the_verified_core(monkeypatch):
    """Ветка дерева (вложенные подзадачи словарями) обязана проводить теги
    КОРНЯ через то же проверенное ядро (_apply_tags_verified), что и соседняя
    ветка без вложенности — иначе тег уезжает МИМО регистрации в аккаунте
    (риск тега-сироты: висит на задаче, но не виден list_tags и не удаляется
    delete_tag) и мимо чтения id2error.

    ПРОВЕРКА ОТКАТОМ (ЗАХОД1.md, Д6): откати правку 2 — тест обязан упасть
    на ОТСУТСТВИИ вызова ('create_tag', …) у подделки, а не на тексте: до
    правки тег ехал внутри полезной нагрузки /batch/task, которую подделка
    (как и настоящий канал) применяет молча, без единого обращения к
    /batch/tag — то есть строка «tag write refused» тоже не появилась бы,
    потому что отдельного обновления тегов не было бы вовсе."""
    v2, _ = _wire(monkeypatch, reject={"tags"})

    result = await s._create_tasks_impl("тест", [
        {"title": "Ремонт", "project_id": "pA", "tags": ["дом"],
         "subtasks": [{"title": "Кухня"}]}])

    assert "теги НЕ применились" in result
    assert "tag write refused" in result
    kinds = [c[0] for c in v2.calls]
    assert "create_tag" in kinds, (
        f"тег не зарегистрирован в аккаунте — рискует остаться сиротой: {v2.calls}")


# ===========================================================================
# Готово-когда 5 (правка 2: оба ожидания корня дерева, не только теги)
# ===========================================================================

async def test_tree_branch_root_expectations_include_tags_and_assignee(
        monkeypatch, tmp_path):
    """У КОРНЯ дерева в to_verify/журнал обязаны попасть ОБА ожидания — до
    правки теги жёстко подставлялись None, а исполнителя там не было вовсе
    (см. «Дополнительная находка» в ЗАХОД1.md, Д6)."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _wire(monkeypatch)

    result = await s._create_tasks_impl("тест", [
        {"title": "Ремонт", "project_id": "pA", "tags": ["дом"], "assignee": 7,
         "subtasks": [{"title": "Кухня"}]}])
    assert "operation_report" in result

    rec = _journal_record(tmp_path)
    root = next(i for i in rec["items"] if i["title"] == "Ремонт")
    assert root["expect"].get("tags") == ["дом"], root["expect"]
    assert root["expect"].get("assignee") == 7, root["expect"]


# ===========================================================================
# Готово-когда 6 (правка 6: сравнение исполнителя строкой, не по типу)
# ===========================================================================

async def test_assignee_type_mismatch_is_not_a_false_alarm(monkeypatch, tmp_path):
    """assignee=42 запрошен числом, канал после записи отдаёт его строкой
    "42" (реальный дрейф типов TickTick) — независимый отчёт обязан читать
    это как совпадение, а не как непримененного исполнителя."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    live = {}
    v2 = _FakeV2AssigneeTypeDrift(_PROJECTS, live)
    official = _FakeOfficial(live)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", official)

    result = await s._create_tasks_impl("тест", [
        {"title": "Согласовать смету", "project_id": "pA", "assignee": 42}])
    assert "operation_report" in result

    rec = _journal_record(tmp_path)
    report = s._build_operation_report(rec["record"])
    assert "✅" in report and "Согласовать смету" in report, report
    assert "ожидался" not in report, (
        f"ложная тревога на разошедшихся типах assignee: {report}")


# ===========================================================================
# Готово-когда 7 (проверка не срабатывает, когда исполнителя не просили)
# ===========================================================================

async def test_assignee_not_requested_leaves_no_trace(monkeypatch, tmp_path):
    """Исполнителя не просили — в ответе о нём ни слова, и в журнальном
    ожидании ключа "assignee" нет вовсе (не пустое значение — ОТСУТСТВИЕ
    ключа, иначе личный проект без общих участников кричал бы ложной
    тревогой на каждой созданной задаче)."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _wire(monkeypatch)

    result = await s._create_tasks_impl("тест", [
        {"title": "Оплатить счёт", "project_id": "pA"}])

    assert "исполнитель" not in result

    rec = _journal_record(tmp_path)
    item = next(i for i in rec["items"] if i["title"] == "Оплатить счёт")
    assert "assignee" not in item["expect"], item["expect"]


# ===========================================================================
# Готово-когда 8 (двойного отчёта об одном провале нет)
# ===========================================================================

async def test_assignee_rejection_is_named_once_not_twice(monkeypatch, tmp_path):
    """Канал ОТКАЗАЛ явно (id2error) — первый слой уже назвал провал; второй
    слой не имеет права повторить его другими словами, поэтому ожидание в
    журнал при отказе не попадает вовсе (ни ответ не дублирует строку, ни
    журнал не несёт ожидания, которое независимый отчёт назвал бы провалом
    ВТОРОЙ раз)."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _wire(monkeypatch, reject={"assignee"})

    result = await s._create_tasks_impl("тест", [
        {"title": "Согласовать смету", "project_id": "pA", "assignee": 42}])

    assert result.count("исполнитель") == 1, result

    rec = _journal_record(tmp_path)
    item = next(i for i in rec["items"] if i["title"] == "Согласовать смету")
    assert "assignee" not in item["expect"], item["expect"]


# ===========================================================================
# Готово-когда 3 (правка 4: одиночный путь update_tasks)
# ===========================================================================

async def test_single_update_assignee_expectation_reaches_the_journal(
        monkeypatch, tmp_path):
    """Одиночный путь _update_tasks_impl — то же ожидание, что и у создания:
    исполнитель попадает в журнал (внутри "changes") ТОЛЬКО когда канал не
    отказал."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    live = {"t1": {"id": "t1", "title": "Согласовать смету", "projectId": "pA"}}
    _wire(monkeypatch, live)

    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "title": "Согласовать смету", "projectId": "pA",
         "assignee": 42}])
    assert "operation_report" in result

    rec = _journal_record(tmp_path)
    item = next(i for i in rec["items"] if i["taskId"] == "t1")
    assert item["expect"]["changes"].get("assignee") == 42, item["expect"]


async def test_single_update_assignee_silently_lost_is_caught_by_post_verify(
        monkeypatch):
    """Канал промолчал (успех без id2error), но значение не доехало до
    живого состояния — первый слой (чтение id2error) тут бессилен по
    определению; ловит только второй (живая пере-проверка _verify_item,
    которая теперь знает про поле "assignee")."""
    live = {"t1": {"id": "t1", "title": "Согласовать смету", "projectId": "pA"}}
    v2 = _FakeV2SilentAssigneeDrop(_PROJECTS, live)
    official = _FakeOfficial(live)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", official)

    result = await s._update_tasks_impl("тест", [
        {"taskId": "t1", "title": "Согласовать смету", "projectId": "pA",
         "assignee": 42}])

    assert "изменения НЕ видны" in result
    assert "assignee" in result


# ===========================================================================
# Прямые проверки правок 3/6 на _verify_item (по образцу
# tests/test_move_tasks_postverify.py и tests/test_restore_tasks_project.py,
# которые тем же приёмом дёргают _verify_item напрямую)
# ===========================================================================

def test_verify_item_create_reports_assignee_mismatch_by_person():
    """Правка 3: сравнение исполнителя в ветке "create" рядом с тегами, в
    человеческом виде — через _person_label, чтобы отчёт называл человека
    (или хотя бы userId с пометкой «имя неизвестно»), а не голый номер без
    подписи."""
    item = {"taskId": "t1", "title": "Согласовать смету",
            "expect": {"projectId": "pA", "assignee": 5}}
    live_map = {"t1": {"id": "t1", "projectId": "pA", "assignee": 9}}
    status, line = s._verify_item("create", item, live_map, {"pA": "Работа"})
    assert status == "warn", line
    assert "исполнитель" in line
    assert "userId 9" in line
    assert "userId 5" in line


def test_verify_item_update_assignee_is_compared_as_string():
    """Правка 6: тот же дефект в общем цикле "update" (используется и
    одиночным, и пакетным путём _update_tasks_impl, и manual_triage) — типы
    разошлись (число vs строка), значение то же самое, ложной тревоги нет."""
    item = {"taskId": "t1", "title": "Согласовать смету",
            "expect": {"changes": {"assignee": 42}}}
    live_map = {"t1": {"id": "t1", "assignee": "42"}}
    status, line = s._verify_item("update", item, live_map, {})
    assert status == "ok", line


# ===========================================================================
# Правка 5 (_triage_expected_changes: перевод assignee для manual_triage)
# ===========================================================================

def test_triage_expected_changes_translates_assignee():
    """Раньше докстринг _triage_expected_changes называл исполнителя
    «невидимым в списке открытых задач» — неверно: _open_by_id/
    ticktick_v2.get_open_tasks() отдают тот же сырой объект задачи, что несёт
    "assignee" (get_task_info читает его оттуда же). Перевод обязан класть
    поле в exp, иначе независимая перепроверка ручного разбора
    (_verify_triage_op → _verify_item) никогда не поймает тихий провал
    назначения исполнителя через manual_triage."""
    exp = s._triage_expected_changes({"assignee": 42})
    assert exp.get("assignee") == 42, exp
