"""Н9, три пути мимо ядра (2026-08-09): фаза плана, `automation_key`, разбор.

Ядро `_update_tasks_impl` уже отказывается писать в поле названия без сверки.
Здесь заперты три способа не заметить этого правила:

1. **ФАЗА ПЛАНА МОЛЧАЛА.** На вход {"taskId": X, "new_title": "Затёрто"} план
   печатал «**«Купить молоко»** — название → «Затёрто»» без единой пометки.
   Человек жал «да» — и получал 🛑 на исполнении. На шестидесяти таких строках
   это карточка «всё исполнимо» против шестидесяти отказов после согласия.
   Причина плану ИЗВЕСТНА из уже прочитанного состояния: `_split_tasks_by_state`
   отдаёт живое имя (`live_title`), на котором guard принимал решение.

2. **`manual_triage` ТЕРЯЛ МАРКЕР.** Элементы для ядра строились как
   {"taskId", "projectId", "title": o.get("title") or "", **changes} — поле
   `untitled` по дороге пропадало. Законное переименование безымянной задачи
   при этом проходило, но НЕ маркером, а побочным основанием «имени нет ни у
   кого»; само же утверждение «у этой задачи имени нет» до ядра не доезжало и
   там не сверялось.

3. **`automation_key` ИДЁТ МИМО ПЛАНА ЦЕЛИКОМ** — исполняет с первого вызова.
   Проверка, стоящая только в плане, для него не существует.

ЧТО ЗДЕСЬ УТВЕРЖДАЕТСЯ. Для плана факт — это НЕ слово в тексте, а то, что
согласия на обречённую строку не просят: манифест не создан вовсе (все строки
обречены) либо строка помечена ⛔ ИМЕННО СВОИМ номером, а соседняя законная —
нет. Для двух других путей факт прежний и единственный: поддельный клиент не
зарегистрировал ни одного вызова записи.
"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import ticktick_mcp.src.server as s
from tests.test_create_link_and_tag_silent_failures import _wire
from tests.test_manual_triage import _mid
from tests.test_untitled_tasks import _receipt_task
from tests.test_untitled_tasks import _wire as _wire_triage

_LEASE = "Договор аренды — подписать до 15-го"
_MILK = "Купить молоко"


@pytest.fixture(autouse=True)
def _isolate_manifests():
    """`_MANIFESTS` — модульный глобал на всю сессию: работаем на чистом и
    возвращаем как было, иначе утверждение «плана не создалось» увидело бы
    чужой манифест."""
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _two_live():
    return {"t_lease": {"id": "t_lease", "title": _LEASE, "projectId": "pA"},
            "t_milk": {"id": "t_milk", "title": _MILK, "projectId": "pA"}}


def _a_date(days):
    """Дата от РЕАЛЬНЫХ часов: литерал однажды делает тест зелёным по
    прошлому, а не по поведению."""
    return (datetime.now(ZoneInfo("America/Los_Angeles"))
            + timedelta(days=days)).strftime("%Y-%m-%d")


def _marked_rows(preview: str) -> set:
    """Номера строк плана, которые несут ⛔. Разбирается ровно та карточка,
    которую видит человек: «N. …» и пометка отдельной строкой под ней."""
    rows, cur = set(), None
    for line in preview.splitlines():
        m = re.match(r"^(\d+)\. ", line)
        if m:
            cur = int(m.group(1))
        elif "⛔" in line and cur is not None:
            rows.add(cur)
    return rows


def _manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"в превью нет manifest_id: {preview!r}"
    return m.group(1)


# ═══════════════ 1. Фаза плана: правда о своей осведомлённости ═══════════════

async def test_plan_marks_the_blind_rename_row_and_leaves_the_legal_one_alone(
        monkeypatch, tmp_path):
    """Главный сторож пункта. Две строки: законное переименование (текущее имя
    передано) и слепое (имени нет). План обязан пометить ВТОРУЮ и не трогать
    первую — иначе он либо врёт про обречённое, либо перетягивает на живое."""
    live = _two_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _v2, _official = _wire(monkeypatch, live)

    preview = await s.update_tasks("правлю двух", [
        {"taskId": "t_lease", "projectId": "pA", "title": _LEASE,
         "new_title": "Договор аренды — подписан"},
        {"taskId": "t_milk", "projectId": "pA", "new_title": "Затёрто"}])

    assert _marked_rows(preview) == {2}, preview
    assert "Не применится строк: 1 из 2" in preview, preview
    # План при этом ПОСТРОЕН: законной строке отказывать не за что.
    assert _manifest_id(preview)


async def test_no_plan_is_built_when_every_row_is_a_blind_rename(monkeypatch):
    """Шестьдесят обречённых строк. Плана нет вовсе: подтверждать нечего, и
    манифест не заводится — согласия на то, чего не будет, не просят."""
    live = {f"t{i}": {"id": f"t{i}", "title": f"Задача {i}", "projectId": "pA"}
            for i in range(60)}
    _v2, official = _wire(monkeypatch, live)

    out = await s.update_tasks("массовое переименование", [
        {"taskId": f"t{i}", "projectId": "pA", "new_title": f"Затёрто {i}"}
        for i in range(60)])

    assert s._MANIFESTS == {}, "план на нулевом числе исполнимых строк построен"
    assert out.startswith("🛑"), out
    assert "manifest_id" not in out, out
    assert official.update_calls == []
    assert live["t0"]["title"] == "Задача 0"


async def test_plan_does_not_mark_a_rename_that_carries_the_current_title(
        monkeypatch, tmp_path):
    """Оборотная сторона: законное переименование обязано строиться без единой
    пометки. Пометка «не применится» на строке, которая применится, — та же
    ложь, только в другую сторону, и она отменяет план целиком, когда такая
    строка одна."""
    live = _two_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _wire(monkeypatch, live)

    preview = await s.update_tasks("переименую", [
        {"taskId": "t_lease", "projectId": "pA", "title": _LEASE,
         "new_title": "Договор аренды — подписан"}])

    assert "⛔" not in preview, preview
    assert _manifest_id(preview)


async def test_plan_does_not_mark_an_update_that_does_not_touch_the_title(
        monkeypatch, tmp_path):
    """Массовый законный вызов: срок и приоритет без имени. Правило про поле
    названия здесь молчит, и план обязан молчать вместе с ним."""
    live = _two_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _wire(monkeypatch, live)

    preview = await s.update_tasks("подвину сроки", [
        {"taskId": "t_lease", "projectId": "pA", "due_date": _a_date(3)},
        {"taskId": "t_milk", "projectId": "pA", "priority": 5}])

    assert "⛔" not in preview, preview
    assert _manifest_id(preview)


async def test_the_mark_belongs_to_the_row_not_to_the_id(monkeypatch, tmp_path):
    """Один и тот же id в двух строках: законной (срок) и слепой
    (переименование). Пометка привязана к СТРОКЕ — иначе законная строка
    получила бы ⛔ за соседнюю, а при двух таких строках плана не стало бы
    вовсе."""
    live = _two_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _wire(monkeypatch, live)

    preview = await s.update_tasks("две строки об одной задаче", [
        {"taskId": "t_milk", "projectId": "pA", "due_date": _a_date(2)},
        {"taskId": "t_milk", "projectId": "pA", "new_title": "Затёрто"}])

    assert _marked_rows(preview) == {2}, preview
    assert _manifest_id(preview)


async def test_the_plan_reason_is_the_same_text_the_executor_will_print(
        monkeypatch, tmp_path):
    """План и исполнитель обязаны объяснять отказ ОДНИМИ словами: человек,
    подтверждающий операцию, и человек, читающий отчёт, — один и тот же."""
    live = _two_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    _wire(monkeypatch, live)

    preview = await s.update_tasks("правлю двух", [
        {"taskId": "t_lease", "projectId": "pA", "title": _LEASE,
         "new_title": "Договор аренды — подписан"},
        {"taskId": "t_milk", "projectId": "pA", "new_title": "Затёрто"}])

    # Ожидаемое взято из ТЕКСТА ОТКАЗА, который человек получает на
    # исполнении, а не из проверяемой функции плана.
    assert "\"untitled\": true вместо него" in preview, preview
    assert "НЕТ текущего названия задачи (поле title)" in preview, preview


async def test_the_approved_plan_is_still_refused_by_the_core(monkeypatch,
                                                              tmp_path):
    """Пометка ⛔ ничего не отменяет: строка уезжает в манифест как была, и
    если человек всё равно подтвердил — отказывает ЯДРО. Тест держит ядро
    достижимым через публичный путь, чтобы правку плана нельзя было выдать за
    правку запрета."""
    live = _two_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    preview = await s.update_tasks("правлю двух", [
        {"taskId": "t_lease", "projectId": "pA", "title": _LEASE,
         "new_title": "Договор аренды — подписан"},
        {"taskId": "t_milk", "projectId": "pA", "new_title": "Затёрто"}])
    out = await s.update_tasks("правлю двух", manifest_id=_manifest_id(preview),
                               user_reply="да")

    assert "🛑" in out, out
    assert live["t_milk"]["title"] == _MILK
    assert live["t_lease"]["title"] == "Договор аренды — подписан"
    # Две строки идут ПАКЕТНЫМ каналом: в него уехала ровно одна, законная.
    written = [c["taskId"] for _kind, changes in v2.calls if _kind == "update"
               for c in changes]
    assert written == ["t_lease"], f"{v2.calls}, official={official.update_calls}"


# ═══════════════ 2. `automation_key` — мимо плана целиком ═══════════════

async def test_automation_key_path_refuses_the_blind_rename(monkeypatch,
                                                            tmp_path):
    """Headless-клиент с верным ключом исполняет с первого вызова: фазы плана
    у него нет. Запрет, стоящий только в плане, здесь не существует — а имя
    живой задачи стирается так же необратимо."""
    live = {"t_lease": {"id": "t_lease", "title": _LEASE, "projectId": "pA"}}
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "SECRET", "s3cret-key-for-the-robot")
    _v2, official = _wire(monkeypatch, live)

    out = await s.update_tasks(
        "робот переименовывает",
        [{"taskId": "t_lease", "projectId": "pA", "new_title": "ЗАТЁРТО"}],
        automation_key="s3cret-key-for-the-robot")

    assert official.update_calls == [], official.update_calls
    assert live["t_lease"]["title"] == _LEASE
    assert "🛑" in out and "new_title" in out, out
    # Именно исполнение, а не «ключ не сработал и вышел план».
    assert "manifest_id" not in out, out


async def test_automation_key_path_still_renames_with_the_current_title(
        monkeypatch, tmp_path):
    """Ключ не превращается в запрет на работу: законное переименование
    робота проходит с первого вызова, как и раньше."""
    live = {"t_lease": {"id": "t_lease", "title": _LEASE, "projectId": "pA"}}
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "SECRET", "s3cret-key-for-the-robot")
    _v2, official = _wire(monkeypatch, live)

    out = await s.update_tasks(
        "робот переименовывает",
        [{"taskId": "t_lease", "projectId": "pA", "title": _LEASE,
          "new_title": "Договор аренды — подписан"}],
        automation_key="s3cret-key-for-the-robot")

    assert "🛑" not in out, out
    assert live["t_lease"]["title"] == "Договор аренды — подписан"
    assert official.update_calls, "законное переименование не доехало до канала"


# ═══════════════ 3. `manual_triage` — маркер доезжает до ядра ═══════════════

async def test_triage_untitled_marker_is_verified_by_the_core_before_the_write(
        monkeypatch, tmp_path):
    """Гонка, ради которой маркер и обязан доезжать до ядра: разбор сверил
    «названия нет» и пропустил операцию, а между этой сверкой и записью
    задачу назвали в приложении. Ядро читает живое состояние САМО,
    непосредственно перед записью, — и обязано отказать: человек принимал
    решение о безымянном объекте, а id указывает уже на именованный.

    Переименования в строке НЕТ (меняется приоритет) — без маркера ядру не за
    что зацепиться вовсе, и запись уходит молча."""
    live = {"t_receipt": _receipt_task()}
    _fake, official = _wire_triage(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("разбираю пустышки", [
        {"op": "update", "task_id": "t_receipt", "untitled": True,
         "said": "этой пустой подними приоритет, я её узнал по чеку",
         "changes": {"priority": 5}}])
    assert "🛑" not in preview, preview

    # Часы гонки: задачу называют РОВНО после повторной сверки разбора и до
    # обращения к каналу записи.
    _drift = s._triage_drift_reason

    def _named_right_after_the_check(op, by_id, names):
        why = _drift(op, by_id, names)
        live["t_receipt"]["title"] = "Чек Home Depot — возврат $374.92"
        return why

    monkeypatch.setattr(s, "_triage_drift_reason", _named_right_after_the_check)

    out = await s.manual_triage("разбираю пустышки",
                                manifest_id=_mid(preview), user_reply="да")

    assert official.calls == [], f"канал всё-таки дёрнули: {official.calls}"
    assert "🛑" in out and "untitled" in out, out


async def test_triage_still_renames_a_nameless_task(monkeypatch, tmp_path):
    """Законный случай, ради которого маркер существует: у задачи имени
    действительно нет, и назвать её через разбор по-прежнему можно."""
    live = {"t_receipt": _receipt_task()}
    _fake, official = _wire_triage(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("называю чек", [
        {"op": "update", "task_id": "t_receipt", "untitled": True,
         "said": "подпиши её чеком из Home Depot",
         "changes": {"new_title": "Чек Home Depot — возврат $374.92"}}])
    out = await s.manual_triage("называю чек", manifest_id=_mid(preview),
                                user_reply="да")

    assert "🛑" not in out, out
    assert live["t_receipt"]["title"] == "Чек Home Depot — возврат $374.92"
    assert official.calls, "законное переименование не доехало до канала"
