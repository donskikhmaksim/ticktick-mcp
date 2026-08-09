"""Д1 — безымянная задача достижима через АГРЕГАТОР (`manual_triage`).

Пакет П15 научил сервер показывать и удалять задачу без названия, но через
`manual_triage` этот случай оставался недостижим: валидация требовала
непустой `title`, а любое непустое название не сходилось с пустым живым — и
операция выбрасывалась из плана. Мёртвым был весь новый код: `_label` и
`_untitled` присваиваются ПОСЛЕ сверки, которую безымянная задача не
проходила никогда. Речь про реальные объекты владельца: фотография чека Home
Depot на возврат $374.92 и скриншот дефекта — обе в списке выглядели пустой
строкой.

Незамеченным дефект остался потому, что старый тест звал `_resolve_triage_ops`
НАПРЯМУЮ и вообще без ключа названия — в форме, которую валидация отвергает
первой же строкой. Поэтому здесь всё идёт через сам `manual_triage()`, оба
вызова (план и исполнение), и проверяется факт в живом состоянии.

Граница послабления — та же, что у П15, и она здесь главная: маркер
`untitled: true` это УТВЕРЖДЕНИЕ про живой объект, а не отмена сверки. Стоит
маркер, а название у задачи есть → операция в план не попадает; появилось
название между планом и кнопкой → операция не исполняется.
"""
import pytest

import ticktick_mcp.src.server as s
from tests.test_manual_triage import _mid
from tests.test_untitled_tasks import _receipt_task, _wire


@pytest.fixture(autouse=True)
def _isolate_manifests():
    """`_MANIFESTS` — модульный глобал на всю сессию: работаем на чистом и
    возвращаем как было, иначе «отказ не создал манифеста» увидел бы чужой."""
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


# 2026-08-09: разделитель ВНУТРИ заменителя — двоеточие, а не «·».
# Причина в Д3: «·» с теми же пробелами вокруг — это ещё и разделитель
# мета-полей компактной строки (замороженный контракт, по нему разбирает
# вывод внешний бот). У безымянной задачи с вложением и сроком получалось ДВА
# вхождения « · », и регулярка бота разъезжалась: в «название» попадало
# «(без названия», в «мета» — «📎 1 файл) · due …».
#
# Тест ветки Д1 писался ДО этой правки и после слияния ждал старую форму —
# git сложил обе ветки без конфликта (разные файлы), разошёлся только смысл.
# Строка захардкожена намеренно: она и есть контракт вывода, вычислять её из
# функции сервера значило бы проверять тождество.
_LABEL = "(без названия: 📎 1 файл)"


def _named(tid="t_named", title="Купить молоко", pid="p_inbox"):
    return {"id": tid, "projectId": pid, "title": title}


# ═════════ 1. ПРИЁМКА: полный цикл через сам инструмент ═════════

async def test_untitled_task_with_attachment_is_deleted_through_manual_triage(
        monkeypatch, tmp_path):
    """Приёмка Д1 целиком: безымянная задача с вложением попадает в план,
    показывается там заменителем и удаляется — оба вызова через
    `manual_triage`, без единого обращения к внутренним функциям."""
    live = {"t_receipt": _receipt_task()}
    fake, _official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю пустышки", [
        {"op": "delete", "task_id": "t_receipt", "untitled": True,
         "said": "эту пустую убери, я посмотрел — чек уже не нужен"}])

    # …в план ВОШЛА и названа тем, что в ней лежит.
    assert "🛑" not in preview and "❌ Не вошло" not in preview
    assert _LABEL in preview
    assert live["t_receipt"], "call #1 не имеет права ничего менять"

    out = await s.manual_triage("Разбираю пустышки", manifest_id=_mid(preview),
                                user_reply="да, удаляй")

    assert "t_receipt" not in live
    assert ("delete", ["t_receipt"]) in fake.calls
    assert "✅ Выполнено 1" in out
    # Отчёт называет удалённое так же, как называл план.
    assert _LABEL in out


async def test_untitled_marker_survives_the_manifest_into_execution(
        monkeypatch, tmp_path):
    """Маркер обязан ПЕРЕЖИТЬ манифест: исполнение — отдельный вызов, живое
    состояние там читается заново, и без сохранённого маркера повторная
    сверка не знала бы, что именно утверждал план."""
    live = {"t_receipt": _receipt_task()}
    _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "t_receipt", "untitled": True,
         "said": "пустышку убери"}])
    stored = s._MANIFESTS[_mid(preview)]["tasks"]

    assert [o["task_id"] for o in stored] == ["t_receipt"]
    assert stored[0].get("untitled") is True
    assert not str(stored[0].get("title") or "").strip(), \
        "заменитель не имеет права уехать в манифест как настоящее название"


# ═════════ 2. ГРАНИЦА: маркер — утверждение, а не отмена сверки ═════════

async def test_marker_on_a_task_that_has_a_title_is_dropped_from_the_plan(
        monkeypatch, tmp_path):
    """САМОЕ ОПАСНОЕ. Маркер «названия нет» на задаче, у которой название
    ЕСТЬ, — это другой объект, чем имел в виду вызывающий. Такая операция в
    план не попадает, кнопка к ней не относится, задача цела; соседняя
    честная строка того же плана при этом исполняется."""
    live = {"t_receipt": _receipt_task(), "t_named": _named()}
    fake, _official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "t_receipt", "untitled": True,
         "said": "пустышку убери"},
        {"op": "delete", "task_id": "t_named", "untitled": True,
         "said": "и эту тоже"}])

    assert "❌ Не вошло: 1" in preview
    # …и справка НАЗЫВАЕТ объект его живым именем. У маркерной операции
    # своего названия нет вовсе, поэтому строка справки печатала про
    # найденную задачу «её нет в живом состоянии аккаунта» — ложь, причём в
    # той же строке рядом стояло настоящее название.
    assert "Купить молоко" in preview
    assert "нет в живом состоянии аккаунта" not in preview

    out = await s.manual_triage("Разбираю", manifest_id=_mid(preview),
                                user_reply="да")

    assert "t_named" in live, "маркер удалил задачу С НАЗВАНИЕМ"
    assert "t_receipt" not in live
    assert ("delete", ["t_receipt"]) in fake.calls
    assert [c for c in fake.calls if c[0] == "delete" and "t_named" in c[1]] == []
    assert "✅ Выполнено 1" in out


@pytest.mark.parametrize("extra", [
    {"op": "delete"},
    {"op": "complete"},
    {"op": "update", "changes": {"priority": 5}},
    {"op": "move", "to_project_id": "p2"},
])
async def test_marker_never_reaches_a_named_task_whatever_the_operation(
        monkeypatch, tmp_path, extra):
    """Сверка одна на все типы операций — и проверяется это по всем типам, а
    не по одному удалению: маркер не должен становиться универсальной
    отмычкой к чужому объекту ни в одной ветке."""
    live = {"t_named": _named()}
    fake, _official = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        dict(extra, task_id="t_named", untitled=True, said="сделай с ней это")])

    assert "🛑" in out and "план НЕ построен" in out
    assert s._MANIFESTS == {}
    assert live["t_named"] == _named(), "живая задача тронута"
    assert fake.calls == []


async def test_title_given_after_the_plan_blocks_the_marked_operation(
        monkeypatch, tmp_path):
    """Дрейф между планом и кнопкой. Через `_names_agree` маркер не
    проверяется ВООБЩЕ (у такой операции `title` пуст, а пустое ожидание —
    «претензии нет»), поэтому повторная сверка перед мутацией обязана иметь
    для него отдельную ветку: иначе задача, которую владелец за это время
    назвал, была бы удалена как безымянная."""
    live = {"t_receipt": _receipt_task()}
    fake, _official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "t_receipt", "untitled": True,
         "said": "пустышку убери"}])
    # …владелец успел назвать её руками, пока план ждал подтверждения.
    live["t_receipt"]["title"] = "Чек Home Depot — возврат $374.92"

    out = await s.manual_triage("Разбираю", manifest_id=_mid(preview),
                                user_reply="да")

    assert "t_receipt" in live
    assert [c for c in fake.calls if c[0] == "delete"] == []
    assert "НИЧЕГО НЕ ВЫПОЛНЕНО" in out
    assert "дали название" in out


async def test_a_wrong_title_is_still_dropped_when_the_task_is_untitled(
        monkeypatch, tmp_path):
    """Контроль обратной стороны: маркер — ЕДИНСТВЕННАЯ дверь. Обычное
    название, посланное за безымянную задачу, по-прежнему не проходит сверку
    (в том числе и текст заменителя, который печатает сам сервер: это подпись
    для человека, а не имя объекта)."""
    live = {"t_receipt": _receipt_task()}
    fake, _official = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "t_receipt", "title": _LABEL,
         "said": "пустышку убери"}])

    assert "🛑" in out and "план НЕ построен" in out
    assert s._MANIFESTS == {}
    assert "t_receipt" in live
    assert [c for c in fake.calls if c[0] == "delete"] == []


# ═════════ 3. ВАЛИДАЦИЯ формы маркера ═════════

async def test_title_and_marker_together_are_refused_outright(
        monkeypatch, tmp_path):
    """Два разных утверждения про один объект («названия нет» и «название
    такое») — сервер не выбирает за вызывающего, какое проверять."""
    live = {"t_receipt": _receipt_task()}
    _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "t_receipt", "title": "Чек",
         "untitled": True, "said": "убери"}])

    assert "🛑" in out and "одновременно" in out
    assert s._MANIFESTS == {}
    assert "t_receipt" in live


@pytest.mark.parametrize("value", ["true", 1, "yes", []])
async def test_marker_that_is_not_a_real_boolean_is_refused(
        monkeypatch, tmp_path, value):
    """Строка "true" или 1 молча читались бы как «маркера нет», и вызывающий
    получил бы отказ про пустой title, не поняв, что его поле выброшено."""
    live = {"t_receipt": _receipt_task()}
    _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "t_receipt", "untitled": value,
         "said": "убери"}])

    assert "🛑" in out and "untitled" in out
    assert s._MANIFESTS == {}
    assert "t_receipt" in live


async def test_missing_title_without_the_marker_still_refuses_and_teaches(
        monkeypatch, tmp_path):
    """Название по-прежнему обязательно — просто теперь отказ говорит, ЧТО
    делать, если названия у задачи действительно нет."""
    live = {"t_receipt": _receipt_task()}
    _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Разбираю", [
        {"op": "delete", "task_id": "t_receipt", "said": "убери"}])

    assert "🛑" in out and "пустой title" in out
    assert "untitled=true" in out
    assert s._MANIFESTS == {}


# ═════════ 4. merge: оставляемая копия тоже бывает безымянной ═════════

async def test_two_untitled_duplicates_can_be_merged(monkeypatch, tmp_path):
    """Без `keep_untitled` объединить два безымянных дубля было нечем: у
    оставляемой копии требовалось непустое `keep_title`, а любое непустое не
    сошлось бы с её пустым живым названием."""
    live = {"t_dup": _receipt_task("t_dup"), "t_keep": _receipt_task("t_keep")}
    fake, _official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Убираю дубль", [
        {"op": "merge", "task_id": "t_dup", "untitled": True,
         "keep_task_id": "t_keep", "keep_untitled": True,
         "said": "это одно и то же, оставь одну"}])

    assert "🛑" not in preview and "❌ Не вошло" not in preview

    out = await s.manual_triage("Убираю дубль", manifest_id=_mid(preview),
                                user_reply="да")

    assert "t_dup" not in live and "t_keep" in live
    assert ("delete", ["t_dup"]) in fake.calls
    assert "✅ Выполнено 1" in out


async def test_keep_marker_on_a_named_copy_blocks_the_merge(
        monkeypatch, tmp_path):
    """Та же граница для оставляемой копии: сказали «у неё нет названия», а
    она названа → дубль НЕ удаляем (иначе объединили бы не с тем объектом)."""
    live = {"t_dup": _receipt_task("t_dup"), "t_keep": _named("t_keep")}
    fake, _official = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Убираю дубль", [
        {"op": "merge", "task_id": "t_dup", "untitled": True,
         "keep_task_id": "t_keep", "keep_untitled": True,
         "said": "это одно и то же"}])

    assert "🛑" in out and "план НЕ построен" in out
    assert "t_dup" in live and "t_keep" in live
    assert [c for c in fake.calls if c[0] == "delete"] == []
