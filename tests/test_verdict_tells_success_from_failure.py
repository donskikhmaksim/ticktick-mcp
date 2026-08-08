"""ВЕРДИКТ ОБЯЗАН РАЗЛИЧАТЬ «сделано» и «упало» (живая приёмка 2026-08-07).

Четыре операции над ОДНОЙ завершённой задачей: три реально произошли
(комментарий создан, файл приложен, комментарий изменён и удалён —
подтверждено чтением), одна упала. Все четыре пришли владельцу под ОДНИМ
значком «❓ НЕ подтверждено (проверить не удалось)». Человек читает вердикт,
а не отчёт исполнителя мелким шрифтом, а действия за этими двумя случаями
разные: «сделано, перепроверить нечем» → проверить позже; «упало» → повторить.

ЧЕМ ЭТОТ ФАЙЛ ОТЛИЧАЕТСЯ ОТ tests/test_auto_execute_report.py. Там правило
вынесения вердикта проверяется на ФЕЙКОВЫХ самоотчётах (строки, написанные
прямо в тесте) — и это правильно для проверки самого правила. Но через СТЫК
«настоящий самоотчёт настоящего исполнителя → настоящий
`_verified_auto_execute_report`» не проходил никто, и именно через него
проехала регрессия коммита 7021ce1: понижение вердикта убрали на уровне
строки исполнителя, а оно живёт уровнем выше — в поллере, по маркеру ⚠️
где угодно в тексте.

Поэтому здесь исполнители зовутся ЧЕРЕЗ ТОТ ЖЕ ПУТЬ, что и кнопка ✅ в
Telegram: план (call #1) → `_resolve_auto_executor(...).execute(...)` →
`_verified_auto_execute_report(...)`. Ничего из этой цепочки не
монкейпатчится; подменён только HTTP-транспорт (tests/read_stand.py).

РАЗВЕДЕНИЕ КАНАЛОВ ℹ️/⚠️ — вторая половина файла. До правки ОДИН символ ⚠️
означал и «часть операции не подтверждена» (оценка проверки), и «задача
завершена» (факт о состоянии объекта). Пока это один символ, любой честный
факт о состоянии автоматически понижает вердикт — что и делало «❓» из
успеха. Теперь: факт — ℹ️, сомнение проверки — ⚠️, и `_EXEC_WARN_MARKERS`
ловит только вторые.
"""
import re

import pytest

import ticktick_mcp.src.server as s
from tests import read_stand as rs

DONE_TITLE = "Купить бумагу"          # rs.COMPLETED[0]: status=2, проект P_WORK
OPEN_TITLE = "Собрать отчёт"          # rs.TASK_ROOT


@pytest.fixture(autouse=True)
def isolate_manifests():
    before, tombs = dict(s._MANIFESTS), dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


@pytest.fixture(autouse=True)
def empty_journal(tmp_path, monkeypatch):
    """Журнал мутаций РЕАЛЬНЫЙ, просто пустой — ровно положение дел для этого
    класса инструментов (комментарии и вложения в `_op_journal` не пишутся
    вообще). `_build_operation_report` НЕ подменяется: проверяется в том числе
    то, как вердикт обходится с его честным «журнала нет»."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))


def _wire(monkeypatch):
    """Стенд, где завершённая задача существует по-настоящему: она в ленте
    завершённых v2 и читается точечно официальным API, но её нет в снимке
    открытых."""
    return rs.wire(monkeypatch, v1_tasks=list(rs.TASKS) + list(rs.COMPLETED))


async def _run_as_the_button_does(tool: str, **args):
    """План (call #1) → исполнение ТЕМ ЖЕ исполнителем, которого зовёт
    фоновый поллер после нажатия ✅ → вердикт. Возвращает (самоотчёт, вердикт).

    Это и есть проверяемый стык: `entry.execute` — тот самый вызов из
    `_tg_auto_execute_tick`, а `_verified_auto_execute_report` — тот самый,
    что решает, какой значок увидит человек."""
    plan = await rs.call(tool, **args)
    mid = re.search(r'manifest_id="([0-9a-f]+)"', plan)
    assert mid, f"{tool} не построил план:\n{plan}"
    mid = mid.group(1)
    m = s._MANIFESTS[mid]
    entry = s._resolve_auto_executor(s._auto_execute_tool_of(m) or tool, m)
    assert entry is not None, f"у {tool} нет авто-исполнителя — кнопка ✅ мертва"
    # Метку манифеста ставит поллер вокруг вызова исполнителя — без неё
    # записи журнала (у тех тулов, что вообще журналируют) не находятся по
    # manifest_id, и стык проверялся бы не тот.
    token = s._TG_AUTO_EXECUTE_MANIFEST.set(mid)
    try:
        self_report = await entry.execute(mid, m)
    finally:
        s._TG_AUTO_EXECUTE_MANIFEST.reset(token)
    _full_md, verdict = s._verified_auto_execute_report(mid, tool, self_report)
    return self_report, verdict


# ===========================================================================
# 1. ГЛАВНЫЙ ТЕСТ ФАЙЛА: успех на завершённой задаче — не «❓»
# ===========================================================================

async def test_comment_on_completed_task_is_reported_as_done(monkeypatch):
    _wire(monkeypatch)

    report, verdict = await _run_as_the_button_does(
        "add_task_comment", task_id=rs.TASK_COMPLETED, task_title=DONE_TITLE,
        project_id=rs.P_WORK, text="чек приложен, работу принял")

    assert verdict == "ok", (
        "комментарий РЕАЛЬНО создан, а вердикт говорит «не подтверждено» — "
        f"человек не отличит это от провала.\nсамоотчёт: {report!r}")


async def test_attachment_on_completed_task_is_reported_as_done(monkeypatch):
    _wire(monkeypatch)

    report, verdict = await _run_as_the_button_does(
        "attach_file_to_task", task_id=rs.TASK_COMPLETED, task_title=DONE_TITLE,
        project_id=rs.P_WORK, content_base64="0LDQsdCy", filename="чек.pdf")

    assert verdict == "ok", (
        "файл РЕАЛЬНО прикреплён (он виден на задаче через тот же источник, "
        f"которым его читает list_task_attachments), а вердикт — «{verdict}»."
        f"\nсамоотчёт: {report!r}")


async def test_comment_edit_and_delete_on_completed_task_are_reported_as_done(monkeypatch):
    _wire(monkeypatch)
    v2, _v1, transport = _wire(monkeypatch)
    # Комментарии у ЗАВЕРШЁННОЙ задачи: проверяемый стоит НЕ последним —
    # пост-проверка обязана искать по id, а не смотреть на хвост списка.
    transport.comments_by_task[(rs.P_WORK, rs.TASK_COMPLETED)] = [
        {"id": "cmt-A", "title": "принято в работу"},
        {"id": rs.COMMENT_ID, "title": "старый текст"},
        {"id": "cmt-Z", "title": "закрыто"},
    ]
    del v2

    _report_u, verdict_u = await _run_as_the_button_does(
        "update_task_comment", task_id=rs.TASK_COMPLETED, task_title=DONE_TITLE,
        project_id=rs.P_WORK, comment_id=rs.COMMENT_ID, text="новый текст")
    assert verdict_u == "ok", f"правка комментария → «{verdict_u}»"

    _report_d, verdict_d = await _run_as_the_button_does(
        "delete_task_comment", task_id=rs.TASK_COMPLETED, task_title=DONE_TITLE,
        project_id=rs.P_WORK, comment_id=rs.COMMENT_ID)
    assert verdict_d == "ok", f"удаление комментария → «{verdict_d}»"


async def test_comment_post_verify_looks_for_its_own_record(monkeypatch):
    """Тихий отказ: сервер отвечает 2xx, а записи не появляется. Пост-проверка
    обязана искать СВОЮ запись по id, а не смотреть, «есть ли вообще
    комментарии» и не брать хвост списка — иначе любой чужой комментарий,
    лежавший на задаче раньше, сойдёт за доказательство и ✅ будет ложным."""
    v2, _v1, transport = _wire(monkeypatch)
    transport.comments_by_task[(rs.P_WORK, rs.TASK_COMPLETED)] = [
        {"id": "cmt-old-1", "title": "чужой комментарий"},
        {"id": "cmt-old-2", "title": "и ещё один"},
    ]
    base = v2._request

    def swallowing(method, path, **kw):
        if method == "POST" and path.endswith("/comment"):
            return dict(kw.get("json") or {})   # 2xx, но ничего не записано
        return base(method, path, **kw)

    v2._request = swallowing

    report, verdict = await _run_as_the_button_does(
        "add_task_comment", task_id=rs.TASK_COMPLETED, task_title=DONE_TITLE,
        project_id=rs.P_WORK, text="этот текст никуда не записался")

    assert not report.lstrip().startswith("✅"), (
        f"тихий отказ выдан за успех:\n{report!r}")
    assert verdict != "ok", verdict


async def test_failure_and_success_do_not_share_one_verdict(monkeypatch):
    """Тот самый вывод из живой приёмки: три успеха и один провал пришли под
    одним значком. Провал обязан читаться иначе, чем успех, — иначе вердикт
    не несёт информации вообще."""
    v2, _v1, _tr = _wire(monkeypatch)

    _ok_report, ok_verdict = await _run_as_the_button_does(
        "add_task_comment", task_id=rs.TASK_COMPLETED, task_title=DONE_TITLE,
        project_id=rs.P_WORK, text="успешная операция")

    # Провал по-настоящему: запись комментария падает на стороне TickTick.
    base = v2._request

    def broken(method, path, **kw):
        if method == "POST" and path.endswith("/comment"):
            raise RuntimeError("TickTick: duplicate comment")
        return base(method, path, **kw)

    v2._request = broken
    bad_report, bad_verdict = await _run_as_the_button_does(
        "add_task_comment", task_id=rs.TASK_COMPLETED, task_title=DONE_TITLE,
        project_id=rs.P_WORK, text="упавшая операция")

    assert bad_verdict != ok_verdict, (
        "упавшая и успешная операции получили ОДИН вердикт "
        f"«{bad_verdict}» — ровно дефект живой приёмки.\n"
        f"успех:  {_ok_report!r}\nпровал: {bad_report!r}")
    assert bad_verdict in ("failed", "mismatch"), (
        f"провал показан как «{bad_verdict}», а не как провал: {bad_report!r}")
    assert s._VERDICT_EMOJI[bad_verdict] != "❓", bad_verdict


# ===========================================================================
# 2. Каждый исполнитель класса приведён к формату: голова самоотчёта — ✅
# ===========================================================================

CLASS_ARGS = {
    "add_task_comment": dict(project_id=rs.P_WORK, text="дописал вывод"),
    "attach_file_to_task": dict(project_id=rs.P_WORK, content_base64="0LDQsdCy",
                                filename="акт.pdf"),
    "update_task_comment": dict(project_id=rs.P_WORK, comment_id=rs.COMMENT_ID,
                                text="исправленный текст"),
    "delete_task_comment": dict(project_id=rs.P_WORK, comment_id=rs.COMMENT_ID),
    "duplicate_task": dict(summary="как шаблон на следующий месяц"),
}


@pytest.mark.parametrize("tool", sorted(CLASS_ARGS))
async def test_every_executor_of_the_class_starts_its_report_with_a_tick(tool, monkeypatch):
    """`_auto_execute_report_is_success` — единственный признак успеха, когда
    журнала нет, и он смотрит на ВЕДУЩИЙ ✅. Исполнитель без него честно
    получает «не доказано»; список приведённых к формату (см. докстринг той
    функции) обязан быть полным, а не «почти полным»."""
    _v2, _v1, transport = _wire(monkeypatch)
    transport.comments_by_task[(rs.P_WORK, rs.TASK_ROOT)].append(
        {"id": rs.COMMENT_ID + "-tail", "title": "хвост"})

    report, verdict = await _run_as_the_button_does(
        tool, task_id=rs.TASK_ROOT, task_title=OPEN_TITLE, **CLASS_ARGS[tool])

    assert s._auto_execute_report_is_success(report), (
        f"{tool} вернул успех без ведущего ✅ — вердикт по нему доказать "
        f"нечем:\n{report!r}")
    assert verdict == "ok", f"{tool} → «{verdict}»:\n{report!r}"


# ===========================================================================
# 3. Разведение каналов: факт о состоянии ≠ сомнение проверки
# ===========================================================================

async def test_completed_note_is_a_fact_not_a_doubt(monkeypatch):
    """Пометка «задача завершена» — факт о состоянии объекта, а не оговорка
    проверки. Пока она несёт ⚠️, `_EXEC_WARN_MARKERS` понижает по ней вердикт,
    и любой честный факт о состоянии стоит исполнителю его собственного «✅».

    Проверяется на ЖИВОМ тексте исполнителя, а не на константе: вклейка
    пометки и её символ живут в разных местах, и именно вклейка решает."""
    _wire(monkeypatch)

    report, _verdict = await _run_as_the_button_does(
        "add_task_comment", task_id=rs.TASK_COMPLETED, task_title=DONE_TITLE,
        project_id=rs.P_WORK, text="дописал вывод")

    assert "завершена" in report.lower(), (
        f"исполнитель молчит о состоянии задачи:\n{report!r}")
    doubts = [mark for mark in s._EXEC_WARN_MARKERS if mark in report]
    assert not doubts, (
        f"факт о состоянии задачи прочитан как сомнение проверки {doubts} и "
        f"понижает вердикт:\n{report!r}")


def test_real_doubt_still_downgrades_the_verdict(monkeypatch):
    """Обратная сторона: ослабить понижение вообще — значит снова начать
    выдавать непроверенное за проверенное. Настоящее сомнение (⚠️ «исход не
    подтверждён») обязано понижать «ok» до «partial», как и раньше."""
    monkeypatch.setattr(
        s, "_build_operation_report",
        lambda rid: ("### 🧾 Независимый отчёт — `m1`\n\n- ✅ **«X»** — на месте"
                     "\n\n**Итог: ✅ 1 подтверждено, ⚠️ 0 не проверено, "
                     "❌ 0 расхождений.**"))
    _md, verdict = s._verified_auto_execute_report(
        "m1", "attach_file_to_task",
        f"✅ Прикреплён файл «акт.pdf» к «Оплатить счёт»\n{s._UNVERIFIED_MSG}")
    assert verdict == "partial", verdict


async def test_duplicate_of_open_task_is_not_downgraded_by_its_own_caveat(monkeypatch):
    """Тот же класс: `duplicate_task` дописывает к успешному отчёту список
    полей, которые в копию не переносятся. Это ФАКТ о продукте копирования
    (известный заранее), а не сомнение в том, что копия создана, — и он не
    имеет права понижать вердикт успешного дублирования."""
    _wire(monkeypatch)

    report, verdict = await _run_as_the_button_does(
        "duplicate_task", task_id=rs.TASK_ROOT, task_title=OPEN_TITLE,
        summary="как шаблон")

    assert "переносятся" in report, (
        "оговорка про непереносимые поля исчезла — тест перестал проверять "
        f"то, ради чего написан:\n{report!r}")
    assert verdict == "ok", (
        f"успешное дублирование понижено до «{verdict}» собственной "
        f"оговоркой:\n{report!r}")
