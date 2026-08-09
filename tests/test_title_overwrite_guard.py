"""Н9 (2026-08-09): переименование БЕЗ текущего названия отказывается ДО записи.

Дефект, который здесь заперт. `update_tasks` принимал строку вида
{"taskId": X, "new_title": "ЗАТЁРТО"} без поля `title`. Сверка личности
(`_names_agree`) на пустом ожидаемом имени честно отвечает «претензии нет,
сверять нечего» — и guard оказывался разоружён не хитростью, а тем, что поле
просто не заполнили. Имя живой задачи затиралось, а жалоба «выполнено БЕЗ
сверки названия» печаталась ПОСЛЕ записи, когда исправлять уже нечего:
журнальная запись хранит НОВОЕ имя, восстанавливать старое не из чего.

ЧТО ИМЕННО УТВЕРЖДАЮТ ТЕСТЫ И ПОЧЕМУ ИМЕННО ЭТО. Проверка «в ответе есть 🛑»
сама по себе НИЧЕГО не доказывает: прежнее поведение тоже печатало жалобу и
такую проверку прошло бы, затерев имя. Поэтому главное утверждение здесь —
`official.update_calls == []` и `"update" not in [c[0] for c in v2.calls]`:
поддельные клиенты не зарегистрировали НИ ОДНОГО вызова записи, то есть отказ
случился до обращения к TickTick, а не после него. Третье утверждение — живое
имя задачи не изменилось.

Подделки берутся из tests/test_create_link_and_tag_silent_failures.py (общая
фикстура `_wire`): двойники там честные — что не отправлено, того нет и в
живом состоянии.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import ticktick_mcp.src.server as s
from tests.test_create_link_and_tag_silent_failures import _wire

# Тот самый объект из приёмки: задача, чьё имя несёт срок, и его потеря
# необратима.
_LEASE = "Договор аренды — подписать до 15-го"


def _lease_live(title=_LEASE, tid="t_lease"):
    return {tid: {"id": tid, "title": title, "projectId": "pA"}}


def _no_writes(v2, official):
    """Единственная формулировка «канал не трогали» на весь файл."""
    return official.update_calls == [] and "update" not in [c[0] for c in v2.calls]


def _a_date(days):
    """Дата вычисляется от РЕАЛЬНЫХ часов, а не литералом: литерал протухает и
    однажды делает тест «зелёным по прошлому», а не по поведению."""
    return (datetime.now(ZoneInfo("America/Los_Angeles"))
            + timedelta(days=days)).strftime("%Y-%m-%d")


def _wire_stale_snapshot(monkeypatch, live, official_tasks):
    """Живая задача ВЫПАЛА из v2-снимка открытых задач, но существует и
    читается официальным Open API — инцидент 2026-08-07: задача отсутствовала в
    той выборке 25 минут, ради него guard и получил официальный запасной канал.

    Ровно это положение и разоружало сверку переименования: пакетный путь брал
    «живое имя» из снимка, получал по ЖИВОЙ задаче пустоту и читал её как
    «имени нет».

    `live` — то, что видно ОБОИМ каналам; задачи из `official_tasks` живут
    ТОЛЬКО в официальном, то есть в снимке их нет ни до записи, ни после."""
    v2, official = _wire(monkeypatch, live)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    official.get_task = lambda project_id, task_id: dict(
        official_tasks.get((project_id, task_id)) or {"error": "404 Not Found"})
    return v2, official


# ═══════════════ 1. Главное: отказ ДО записи, одиночный путь ═══════════════

async def test_rename_without_title_is_refused_before_the_write(monkeypatch):
    """Сторож отката. Убери отказ в одиночном пути `_update_tasks_impl` — и
    падают утверждения (2) и (3), а не только текст ответа."""
    live = _lease_live()
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("переименую", [
        {"taskId": "t_lease", "projectId": "pA", "new_title": "ЗАТЁРТО"}])

    # ПОРЯДОК УТВЕРЖДЕНИЙ НАМЕРЕННЫЙ. Главное — не текст ответа, а то, что
    # канала не касались: старое поведение печатало предупреждение и прошло бы
    # проверку «в ответе есть 🛑», затерев при этом имя. Поэтому при откате
    # правки первым падает именно это утверждение.
    # (2) записи не было вообще
    assert _no_writes(v2, official), \
        f"канал всё-таки дёрнули: official={official.update_calls}, v2={v2.calls}"
    # (3) живое имя цело
    assert live["t_lease"]["title"] == _LEASE
    # (1) отказ виден и НАЗЫВАЕТ недостающее поле
    assert out.startswith("🛑"), out
    assert "new_title" in out and "title" in out, out


async def test_rename_without_title_is_refused_in_the_batch_path_too(monkeypatch):
    """Второй вход в ту же запись: две и более строк уходят в пакетный путь
    (`batch_update_tasks`), у которого своя сборка `changes`. Отказ, стоящий
    только в одиночном пути, здесь бы не сработал."""
    live = {"t_lease": {"id": "t_lease", "title": _LEASE, "projectId": "pA"},
            "t2": {"id": "t2", "title": "Вторая", "projectId": "pA"}}
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("переименую двух", [
        {"taskId": "t_lease", "projectId": "pA", "new_title": "ЗАТЁРТО"},
        {"taskId": "t2", "projectId": "pA", "new_title": "ЗАТЁРТО-2"}])

    assert "🛑" in out and "new_title" in out, out
    assert _no_writes(v2, official), \
        f"канал всё-таки дёрнули: official={official.update_calls}, v2={v2.calls}"
    assert live["t_lease"]["title"] == _LEASE
    assert live["t2"]["title"] == "Вторая"


async def test_the_public_tool_refuses_it_at_the_plan_phase(monkeypatch,
                                                            tmp_path):
    """Через ПУБЛИЧНЫЙ интерфейс — тот, который видит модель. Тест на одну лишь
    внутреннюю функцию не доказал бы, что запрет вообще достижим снаружи.

    2026-08-09: отказ теперь приходит РАНЬШЕ — на фазе плана, единственной
    строке отказано, исполнимых не осталось, и манифеста не создаётся вовсе.
    Раньше здесь строился план, человек говорил «да», и только тогда получал
    🛑: согласие спрашивали на то, чего не будет. Ядро при этом остаётся
    сторожем и достижимо снаружи — см. `test_the_approved_plan_is_still_refused_by_the_core`
    и путь `automation_key` в tests/test_plan_knows_the_rename_rule.py."""
    live = _lease_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s.update_tasks("переименую", [
        {"taskId": "t_lease", "projectId": "pA", "new_title": "ЗАТЁРТО"}])

    assert "🛑" in out and "new_title" in out, out
    assert "manifest_id" not in out, out
    assert _no_writes(v2, official), \
        f"канал всё-таки дёрнули: official={official.update_calls}, v2={v2.calls}"
    assert live["t_lease"]["title"] == _LEASE


def _extract_manifest_id(preview: str) -> str:
    import re
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"в превью нет manifest_id: {preview!r}"
    return m.group(1)


# ═══════════════ 2. Законное не задето ═══════════════

async def test_rename_with_correct_title_still_works(monkeypatch, tmp_path):
    live = _lease_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("переименую", [
        {"taskId": "t_lease", "projectId": "pA", "title": _LEASE,
         "new_title": "Договор аренды — подписан"}])

    assert "🛑" not in out, out
    assert "обновлено (проверено)" in out, out
    assert live["t_lease"]["title"] == "Договор аренды — подписан"
    assert official.update_calls, "законное переименование не доехало до канала"


async def test_update_without_title_and_without_rename_still_works(monkeypatch,
                                                                   tmp_path):
    """Массовый законный случай: меняем срок и приоритет, названия не касаемся.
    Требовать имя здесь было бы издевательством над вызывающим — и наивный
    запрет «нет title → отказ» убил бы именно этот пласт вызовов."""
    live = _lease_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("подвину срок", [
        {"taskId": "t_lease", "projectId": "pA",
         "due_date": _a_date(3), "priority": 5}])

    assert "🛑" not in out, out
    assert "обновлено (проверено)" in out, out
    assert live["t_lease"]["title"] == _LEASE


# ═══════════════ 3. Маркер «названия нет» ═══════════════

async def test_rename_of_a_nameless_task_works_with_untitled_marker(monkeypatch,
                                                                    tmp_path):
    """Фотография чека без названия — законный объект, и назвать его должно
    быть можно. Дверь открыта СТРУКТУРНЫМ маркером, а не послаблением на
    строку."""
    live = {"t_receipt": {"id": "t_receipt", "title": "", "projectId": "pA"}}
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("назову чек", [
        {"taskId": "t_receipt", "projectId": "pA", "untitled": True,
         "new_title": "Чек Home Depot — возврат $374.92"}])

    assert "🛑" not in out, out
    assert live["t_receipt"]["title"] == "Чек Home Depot — возврат $374.92"


async def test_untitled_marker_is_refused_when_the_task_has_a_name(monkeypatch):
    """Маркер сам по себе — способ сказать «сверять нечего», то есть новый
    рычаг разоружения. Поэтому он сверяется с живым состоянием: на задаче, у
    которой имя ЕСТЬ, это отказ, а не поблажка."""
    live = _lease_live()
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("переименую", [
        {"taskId": "t_lease", "projectId": "pA", "untitled": True,
         "new_title": "ЗАТЁРТО"}])

    assert out.startswith("🛑"), out
    assert "untitled" in out, out
    assert _no_writes(v2, official)
    assert live["t_lease"]["title"] == _LEASE


@pytest.mark.parametrize("bad", ["true", "True", 1, "yes"])
async def test_untitled_marker_is_not_read_from_a_string_or_a_number(
        bad, monkeypatch):
    """Тип маркера проверяется строго (`is True`). Прочитанные «мягко», строка
    "true" и число 1 молча означали бы «маркера нет» — и вызывающий получил бы
    отказ про пустой title, не поняв, что его поле просто выбросили."""
    live = {"t_receipt": {"id": "t_receipt", "title": "", "projectId": "pA"}}
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("назову чек", [
        {"taskId": "t_receipt", "projectId": "pA", "untitled": bad,
         "new_title": "Чек Home Depot"}])

    assert out.startswith("🛑"), out
    assert "untitled" in out, out
    assert _no_writes(v2, official)
    assert live["t_receipt"]["title"] == ""


async def test_a_nameless_task_is_renamed_by_id_without_any_marker(monkeypatch,
                                                                   tmp_path):
    """Третья ветвь взведённости: имени нет НИ У КОГО — ни у вызывающего, ни у
    живой задачи, — значит сравнивать нечего и объект опознан идентификатором.
    Это не поблажка: сервер САМ прочитал живое состояние, а не поверил на
    слово."""
    live = {"t_receipt": {"id": "t_receipt", "title": "", "projectId": "pA"}}
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("назову чек", [
        {"taskId": "t_receipt", "projectId": "pA",
         "new_title": "Чек Home Depot"}])

    assert "🛑" not in out, out
    assert live["t_receipt"]["title"] == "Чек Home Depot"


# ═══════════════ 4. Числа в отчёте пакетного пути сходятся ═══════════════

async def test_batch_counts_add_up_when_one_row_is_refused(monkeypatch, tmp_path):
    """Появилась ЧЕТВЁРТАЯ корзина (отказано). Если её не напечатать рядом с
    остальными, «Обновлено N» перестанет сходиться с длиной запроса — ровно тот
    класс ошибок, который этой правкой и чинится."""
    live = {"t_lease": {"id": "t_lease", "title": _LEASE, "projectId": "pA"},
            "ok1": {"id": "ok1", "title": "Первая", "projectId": "pA"},
            "ok2": {"id": "ok2", "title": "Вторая", "projectId": "pA"}}
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("правлю трёх", [
        {"taskId": "ok1", "projectId": "pA", "title": "Первая", "priority": 3},
        {"taskId": "ok2", "projectId": "pA", "title": "Вторая", "priority": 3},
        {"taskId": "t_lease", "projectId": "pA", "new_title": "ЗАТЁРТО"}])

    assert "Обновлено 2 (проверено)" in out, out
    # Отказанная строка названа ПОИМЁННО, а не спрятана в число.
    assert _LEASE in out, out
    assert live["t_lease"]["title"] == _LEASE
    # Две законные строки при этом применились — отказ не перетянул весь пакет.
    assert live["ok1"]["priority"] == 3 and live["ok2"]["priority"] == 3
    # 2 обновлено + 1 отказано + 0 mismatch + 0 missing == 3 запрошенных
    assert out.count("🛑") == 1, out


# ═════ 5. Живая задача ВНЕ v2-снимка: «я не смотрел» ≠ «имени нет» ═════

async def test_batch_refuses_when_the_task_is_outside_the_v2_snapshot(
        monkeypatch, tmp_path):
    """ГЛАВНАЯ дыра доработки (воспроизведена тремя скептиками независимо).

    Задача «Отчёт для налоговой» жива и открыта, но выпала из v2-снимка —
    guard находит её официальным запасным каналом и пропускает в разрешённые.
    Пакетный путь брал живое имя из снимка, получал пустоту и разоружался:
    ни одного 🛑, канал записи дёрнут, имя затёрто, а в журнал ушло НОВОЕ имя.
    Верное значение всё это время лежало в руке — в строке `found`."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire_stale_snapshot(
        monkeypatch,
        live={"Y": {"id": "Y", "title": "Вторая", "projectId": "pA"}},
        official_tasks={("pA", "X"): {"id": "X", "title": "Отчёт для налоговой",
                                      "projectId": "pA", "status": 0}})

    out = await s._update_tasks_impl("правлю двух", [
        {"taskId": "X", "projectId": "pA", "new_title": "ЗАТЁРТО"},
        {"taskId": "Y", "projectId": "pA", "title": "Вторая", "priority": 3}])

    assert _no_writes(v2, official) or all(
        "X" != c.get("taskId") for call in v2.calls if call[0] == "update"
        for c in call[1]), f"строку X всё-таки отправили: {v2.calls}"
    assert "🛑" in out, out
    assert "Отчёт для налоговой" in out, out
    # И жалобы «выполнено БЕЗ сверки названия» после записи тоже быть не может.
    assert "БЕЗ сверки названия" not in out, out


async def test_batch_refuses_a_false_untitled_marker_outside_the_snapshot(
        monkeypatch, tmp_path):
    """Та же причина, второй симптом: ветка «маркер разошёлся с живым
    состоянием» сравнивала маркер с пустотой из снимка, и ложь о личности
    объекта проходила. Одиночным путём тот же вход отказывался — расхождение
    двух путей и выдавало ошибку."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire_stale_snapshot(
        monkeypatch,
        live={"Y": {"id": "Y", "title": "Вторая", "projectId": "pA"}},
        official_tasks={("pA", "X"): {"id": "X", "title": "Отчёт для налоговой",
                                      "projectId": "pA", "status": 0}})

    out = await s._update_tasks_impl("правлю двух", [
        {"taskId": "X", "projectId": "pA", "untitled": True,
         "new_title": "ЗАТЁРТО"},
        {"taskId": "Y", "projectId": "pA", "title": "Вторая", "priority": 3}])

    assert "🛑" in out and "untitled" in out, out
    assert "Отчёт для налоговой" in out, out


def test_the_predicate_refuses_when_the_live_name_was_never_read():
    """Единица различения, на которой держатся оба теста выше: «прочитали, там
    пусто» (пустая строка) разрешает опознание по id, «не читали» (None) — не
    разрешает ничего. Раньше это было одно и то же значение."""
    assert s._title_check_armed("", False, "") is True      # прочитали, пусто
    assert s._title_check_armed("", False, None) is False   # не читали
    assert s._title_check_armed("", True, None) is False    # маркер не спасает
    assert s._title_check_armed("", False, "Отчёт") is False


def test_the_guard_tells_a_nameless_task_from_an_answer_without_the_field(
        monkeypatch):
    """Источник этого различения — сам guard. Объект без ключа `title`
    (урезанный ответ канала) не имеет права выглядеть как задача без
    названия."""
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"pA": "Работа"})

    named = s._guard_task("t1", "", "pA",
                          by_id={"t1": {"id": "t1", "title": "", "projectId": "pA"}})
    fieldless = s._guard_task("t2", "", "pA",
                              by_id={"t2": {"id": "t2", "projectId": "pA"}})

    assert named.status == "ok" and named.title_known is True
    assert fieldless.status == "ok" and fieldless.title_known is False


# ═════ 6. Регресс: задача с невидимым именем снова переименовывается ═════

async def test_a_task_named_with_an_invisible_character_can_be_renamed(
        monkeypatch, tmp_path):
    """Внесённый регресс. План признаёт такое имя пустым по `_looks_untitled`,
    а исполнение отказывало по `_is_untitled` — два разных ответа на вопрос
    «пусто ли это» в одной цепочке, и переименовать задачу становилось нельзя
    вообще. Плюс текст отказа был непроходим для человека: «у живой задачи
    название ЕСТЬ: «​»» — пустые кавычки, подставить нечего."""
    live = {"t_zw": {"id": "t_zw", "title": "​", "projectId": "pA"}}
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("назову чек", [
        {"taskId": "t_zw", "projectId": "pA", "title": "",
         "new_title": "Чек Home Depot"}])

    assert "🛑" not in out, out
    assert live["t_zw"]["title"] == "Чек Home Depot"


# ═════ 7. Переименование В ПУСТОТУ при честно взведённой сверке ═════

async def test_an_empty_new_title_is_refused_even_with_a_correct_title(
        monkeypatch, tmp_path):
    """Сверка взведена честно — имя передано и совпало, — но `ch["title"] = ""`
    стирало название в пустоту, а отчёт рапортовал успех именем, которого уже
    нет. Взведённость сверки и осмысленность записи — разные вопросы."""
    live = {"t_lease": {"id": "t_lease", "title": _LEASE, "projectId": "pA"},
            "ok1": {"id": "ok1", "title": "Первая", "projectId": "pA"}}
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("правлю двух", [
        {"taskId": "t_lease", "projectId": "pA", "title": _LEASE,
         "new_title": ""},
        {"taskId": "ok1", "projectId": "pA", "title": "Первая", "priority": 3}])

    assert "🛑" in out and "new_title" in out, out
    assert live["t_lease"]["title"] == _LEASE


async def test_an_empty_new_title_is_refused_in_the_single_path_too(
        monkeypatch, tmp_path):
    """Одиночный путь молчал иначе: настоящий клиент фильтрует `if title:` и
    пустое имя не отправлял, а отчёт всё равно печатал «обновлено». Отказ
    обязан быть одинаковым на обоих путях."""
    live = _lease_live()
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("переименую", [
        {"taskId": "t_lease", "projectId": "pA", "title": _LEASE,
         "new_title": "   "}])

    assert out.startswith("🛑"), out
    assert _no_writes(v2, official)
    assert live["t_lease"]["title"] == _LEASE


# ═════ 8. Мелочи, за которые платит читатель отчёта ═════

async def test_a_refusal_does_not_mute_the_warning_about_another_row(
        monkeypatch, tmp_path):
    """Один и тот же id стоит в вызове дважды: первая строка законна, но без
    сверки (меняет приоритет), вторая отказана. Вычитание отказанных ПО id
    глушило честное предупреждение про первую заодно со второй."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "pA"},
            "t2": {"id": "t2", "title": "Вторая", "projectId": "pA"}}
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("правлю", [
        {"taskId": "t1", "projectId": "pA", "priority": 3},
        {"taskId": "t1", "projectId": "pA", "new_title": "ЗАТЁРТО"},
        {"taskId": "t2", "projectId": "pA", "title": "Вторая", "priority": 1}])

    assert "БЕЗ сверки названия" in out, \
        f"предупреждение про законную строку проглочено отказом по соседней:\n{out}"
    assert "🛑" in out, out
    assert live["t1"]["title"] == "Купить молоко"


async def test_a_refusal_never_claims_that_nothing_changed(monkeypatch, tmp_path):
    """«Ничего не изменено» в строке отказа — про строку, а читатель понимает
    про объект, который в этом же вызове действительно переименован."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "pA"},
            "t2": {"id": "t2", "title": "Вторая", "projectId": "pA"}}
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2, official = _wire(monkeypatch, live)

    out = await s._update_tasks_impl("правлю", [
        {"taskId": "t1", "projectId": "pA", "title": "Купить молоко",
         "new_title": "Купить молоко и хлеб"},
        {"taskId": "t2", "projectId": "pA", "new_title": "ЗАТЁРТО"}])

    assert "🛑" in out, out
    assert "Ничего не изменено" not in out, \
        f"отказ обещает нетронутый объект, а он переименован:\n{out}"
    assert live["t1"]["title"] == "Купить молоко и хлеб"


def test_the_predicate_survives_a_non_string_title():
    """Проверка безопасности не имеет права падать исключением на словаре,
    собранном моделью: `{"title": 123}` — это отказ, а не AttributeError."""
    assert s._title_check_armed(123, False, "Отчёт") is True
    assert s._title_check_armed(None, False, "") is True
    assert s._title_check_armed("", False, 0) is False


def test_is_untitled_matches_its_own_docstring_on_invisible_characters():
    """Докстринг `_is_untitled` обещал, что невидимые символы не вычищаются.
    Пробельные по Unicode (NBSP) `strip()`-ом снимаются, нулевой ширины — нет.
    Расхождение убрано в тексте докстринга; тест держит границу на месте."""
    assert s._is_untitled(" ") is True      # NBSP — пробельный
    assert s._is_untitled("​") is False     # zero-width — нет
    assert "U+00A0" in s._is_untitled.__doc__
