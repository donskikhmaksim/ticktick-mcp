"""Идентификатор плана обязан быть В ТЕКСТЕ ОТВЕТА — у КАЖДОГО гейтованного
инструмента (2026-08-06).

Почему это отдельный файл, а не строчка в чужом тесте. Клиент MCP видит от
сервера РОВНО ОДНУ вещь — строку ответа. Ни `_MANIFESTS` (обычный dict в
памяти ОДНОГО процесса), ни таблицы `tg_approvals` в Postgres у него нет и
быть не может: он на другом конце HTTP. При этом адресовать план можно ТОЛЬКО
идентификатором манифеста — это и `manifest_id=` второго вызова, и PRIMARY KEY
строки в `tg_approvals`, и `callback_data` кнопок ✅/🛑 в Telegram
(`tg_approval.notify_plan`). Не напечатали id — план снаружи безымянный.

Два независимых подтверждения, что это не теория:
  1. Агент уборки, удаляя проекты через `delete_project`, получил 19 планов,
     которые нечем было назвать, и вынужден был УГАДЫВАТЬ свой план по
     разнице списка ожидающих подтверждений до вызова и после (годится, только
     пока новая строка ровно одна — то есть ломается на любой параллельной
     работе).
  2. Агент боевого прогона кнопки для merge-ветки `rename_tag` смог нажать
     кнопку лишь потому, что доставал план из `_MANIFESTS` ВНУТРИ СВОЕГО ЖЕ
     процесса — путь, физически недоступный сетевому клиенту. Ровно так же
     (`next(i for i, m in s._MANIFESTS.items() ...)`) добывают id и старые
     тесты в test_button_only_execution.py — поэтому они дыру и не ловили.

Здесь id берётся ТОЛЬКО из текста ответа (`_network_client_reads_plan_id`) —
любой тест этого файла падает, если печать идентификатора убрать.

Ни сети, ни Postgres, ни TickTick: `tg_approval`-функции, клиенты и все
`_<tool>_impl` монкейпатчатся, как в соседних тестах гейта.
"""
import ast
import pathlib
import re

import pytest

import ticktick_mcp.src.server as s
# Автоуборка вынесена за пределы пакета (attic/declutter_disabled.py,
# пункт 1.2.4 захода 1, 2026-08-09) — загрузчик возвращает её определения
# в пространство имён `s`, где их ищут тесты ниже. См. tests/attic_loader.py.
from tests import attic_loader as _attic  # noqa: F401
import ticktick_mcp.src.tg_approval as tg
# Таблица гейтованных тулов живёт в соседнем файле и УЖЕ сверяется с кодом
# (test_the_table_covers_every_gated_tool_in_the_code разбирает server.py и
# требует, чтобы каждый вызов _gate_batch/_gate_single был в ней). Импорт, а
# не копия: копия разъехалась бы, и новый тул опять остался бы непроверенным.
from tests.test_tg_gate_all_tools import ALL_TOOLS


# ───────────────────────── взгляд сетевого клиента ─────────────────────────

# Ровно та же формула, по которой id ищут в тексте другие тесты и человек
# глазами. Соответствует `server._plan_id_line`.
_PLAN_ID_RE = re.compile(r"Манифест `([0-9a-f]{6,})`")


def _network_client_reads_plan_id(response_text):
    """Всё, что доступно клиенту по сети: СТРОКА ответа. Никакого `s._MANIFESTS`
    внутри этой функции нет намеренно — она моделирует чужой процесс."""
    found = _PLAN_ID_RE.findall(response_text)
    assert found, ("в ответе нет идентификатора плана — назвать этот план "
                   f"снаружи нечем:\n{response_text}")
    assert len(set(found)) == 1, (f"в ответе несколько разных id планов: "
                                  f"{sorted(set(found))}\n{response_text}")
    return found[0]


# ───────────────────────── обвязка ─────────────────────────

@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


@pytest.fixture(autouse=True)
def _public_base_url(monkeypatch):
    """`create_attachment_upload_url` отказывает ДО гейта без известного
    публичного адреса — задаём, иначе call #1 вернул бы не план. Остальным
    тулам переменная безразлична."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)


def _tg_on(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


def _tg_off(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


def _capture_notify(monkeypatch):
    """Перехватывает отправку в Telegram. `manifest_id` в этих вызовах — то
    самое, что уходит в `callback_data` кнопки и в PRIMARY KEY `tg_approvals`,
    поэтому сверка «id из текста == id здесь» и означает «клиент может нажать
    кнопку по тому, что ему напечатали»."""
    seen = []

    def _fake(cfg, manifest_id, preview_body, tool):
        seen.append({"manifest_id": manifest_id, "tool": tool,
                     "preview": preview_body})
        return True, ""

    monkeypatch.setattr(tg, "notify_plan", _fake)
    return seen


# Детерминированное «живое состояние» для тулов, которые сверяют переданные
# id ДО гейта. Раньше это был только `manual_triage`; def-116 follow-up
# (group B, 2026-08-07) добавила ТУ ЖЕ фазу плана к ещё шести тулам
# (create_subtask, unset_task_parent, delete_project_group,
# move_project_to_group, add_task_comment, duplicate_task) — без этой
# подмены их call #1 либо отвечает «состояние TickTick недоступно», либо (что
# опаснее для ЭТОГО файла) честно отказывает по несовпавшей паре id/название,
# и до печати id дело просто не доходит. Держать в синхроне с ТОЙ ЖЕ правкой
# в tests/test_tg_gate_all_tools.py, откуда берётся таблица ALL_TOOLS —
# "par1"/"p1" обязаны резолвиться в те же "Родитель"/"Проект", что таблица
# заявляет для create_subtask/unset_task_parent/move_project_to_group и
# соседей (create_project_column/archive_project/update_project).
_LIVE_TASKS = {
    "t1": {"id": "t1", "title": "A", "projectId": "p1"},
    "par1": {"id": "par1", "title": "Родитель", "projectId": "p1"},
}
_LIVE_PROJECTS = {"p1": "Проект", "p2": "Проект-2"}
# delete_tags (2026-08-09, П20) — держать в синхроне с ТОЙ ЖЕ правкой в
# tests/test_tg_gate_all_tools.py: её "раз" резолвится в тег без родителя.
_LIVE_TAGS = [{"name": "раз", "label": "раз", "parent": None}]


def _no_client_checks(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id",
                        lambda fresh=False: {k: dict(v)
                                             for k, v in _LIVE_TASKS.items()})
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(_LIVE_PROJECTS))
    monkeypatch.setattr(s, "_live_tag_records",
                        lambda force=True: [dict(t) for t in _LIVE_TAGS])


def _stub_impl(monkeypatch, tool):
    rec = []

    async def _impl(*a, **kw):
        rec.append((a, kw))
        return f"### ✅ {tool} исполнен (заглушка)"

    monkeypatch.setattr(s, f"_{tool}_impl", _impl)
    return rec


def _wire_delete_project(monkeypatch, tmp_path, tasks=()):
    names = {"p1": "Работа"}
    deleted = []

    class _FakeOfficial:
        def get_project_with_data(self, project_id):
            return {"project": {"id": project_id}, "tasks": list(tasks)}

        def delete_project(self, project_id):
            names.pop(project_id, None)
            deleted.append(project_id)
            return {}

    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "ticktick", _FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)
    monkeypatch.setattr(s, "_v2_project_names_or_none", lambda: dict(names))
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    return deleted


class _FakeV2Tags:
    def __init__(self, names):
        self._names = names

    def get_state(self, force=True):
        return {}

    def get_tags(self):
        return [{"name": n} for n in self._names]

    def rename_tag(self, old_name, new_name):
        self._names = [n for n in self._names if n != old_name.lower()]
        if new_name.lower() not in self._names:
            self._names.append(new_name.lower())
        return {}


def _wire_rename_tag(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake = _FakeV2Tags(["a", "b"])
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


# ═══════ 1. Главное: сетевой клиент называет план по одному лишь тексту ═════
#
# Это тот самый сценарий, из-за которого всё началось. Проверяется цепочка
# целиком: напечатанный id → это ровно тот id, что ушёл в кнопку → по нему
# сервер находит и исполняет ИМЕННО этот план.

async def test_delete_project_plan_is_addressable_from_the_response_alone(
        monkeypatch, tmp_path):
    deleted = _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    response = await s.delete_project("Работа", "p1")     # фаза плана

    # (а) клиент читает id ИЗ ТЕКСТА — единственное, что у него есть
    plan_id = _network_client_reads_plan_id(response)
    # (б) это ровно тот id, что ушёл в callback_data кнопки / в tg_approvals
    assert len(seen) == 1
    assert seen[0]["manifest_id"] == plan_id, (
        "напечатан один id, а кнопка отправлена с другим — нажать по "
        "напечатанному нельзя")
    # (в) по нему сервер находит ИМЕННО этот план и исполняет его
    assert deleted == []
    entry = s._resolve_auto_executor("delete_project", s._MANIFESTS[plan_id])
    consumed = tg.try_auto_execute(
        manifest_id=plan_id, tool="delete_project",
        get_manifest=lambda i: s._MANIFESTS.get(i),
        consume_manifest=s._consume_manifest_for_auto_execute,
        rehash=entry.rehash)
    assert consumed is not None, "id из ответа не открывает свой же план"
    await entry.execute(plan_id, consumed)
    assert deleted == ["p1"]


async def test_rename_tag_merge_plan_is_addressable_from_the_response_alone(
        monkeypatch):
    fake = _wire_rename_tag(monkeypatch)
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    response = await s.rename_tag("a", "b", allow_merge=True)   # фаза плана

    plan_id = _network_client_reads_plan_id(response)
    assert len(seen) == 1
    assert seen[0]["manifest_id"] == plan_id
    assert fake._names == ["a", "b"]                  # ещё ничего не слито
    entry = s._resolve_auto_executor("rename_tag", s._MANIFESTS[plan_id])
    consumed = tg.try_auto_execute(
        manifest_id=plan_id, tool="rename_tag",
        get_manifest=lambda i: s._MANIFESTS.get(i),
        consume_manifest=s._consume_manifest_for_auto_execute,
        rehash=entry.rehash)
    assert consumed is not None, "id из ответа не открывает свой же план"
    await entry.execute(plan_id, consumed)
    assert fake._names == ["b"]


async def test_plan_id_is_visible_even_with_the_telegram_layer_off(
        monkeypatch, tmp_path):
    """Слой кнопок выключен (дефолт самохостера) — id всё равно нужен: он же
    единственный способ сослаться на план в логах/отчётах и опознать его
    среди параллельных."""
    _wire_delete_project(monkeypatch, tmp_path)
    _tg_off(monkeypatch)

    response = await s.delete_project("Работа", "p1")

    plan_id = _network_client_reads_plan_id(response)
    assert plan_id in s._MANIFESTS


async def test_rename_tag_plan_id_is_visible_even_with_the_layer_off(monkeypatch):
    _wire_rename_tag(monkeypatch)
    _tg_off(monkeypatch)

    response = await s.rename_tag("a", "b", allow_merge=True)

    assert _network_client_reads_plan_id(response) in s._MANIFESTS


# ═══════ 2. Повторный вызов при живом плане тоже называет его ═══════
#
# Ровно положение уборщика: план уже висит и ждёт нажатия, инструмент зовут
# снова. Ответ обязан назвать ТОТ ЖЕ план, а не промолчать.

async def test_delete_project_repeat_call_reports_the_same_live_plan_id(
        monkeypatch, tmp_path):
    deleted = _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)
    first = await s.delete_project("Работа", "p1")
    plan_id = _network_client_reads_plan_id(first)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "pending")

    second = await s.delete_project("Работа", "p1", user_reply="да")

    assert deleted == []                                  # ничего не удалено
    assert _network_client_reads_plan_id(second) == plan_id, (
        "повторный вызов не сказал, КАКОЙ план он ждёт")
    assert s._MANIFESTS[plan_id]["consumed"] is False      # план жив


async def test_rename_tag_repeat_call_reports_the_same_live_plan_id(monkeypatch):
    fake = _wire_rename_tag(monkeypatch)
    _tg_on(monkeypatch)
    _capture_notify(monkeypatch)
    first = await s.rename_tag("a", "b", allow_merge=True)
    plan_id = _network_client_reads_plan_id(first)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "pending")

    second = await s.rename_tag("a", "b", allow_merge=True, user_reply="да")

    assert fake._names == ["a", "b"]
    assert _network_client_reads_plan_id(second) == plan_id
    assert s._MANIFESTS[plan_id]["consumed"] is False


# ═══════ 3. Каждый тул общего гейта печатает свой id ═══════

@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
async def test_every_gated_tool_prints_its_own_plan_id(tool, monkeypatch):
    _no_client_checks(monkeypatch)
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)
    _stub_impl(monkeypatch, tool)

    response = await getattr(s, tool)(**ALL_TOOLS[tool])

    plan_id = _network_client_reads_plan_id(response)
    assert plan_id in s._MANIFESTS, f"{tool}: напечатан id несуществующего плана"
    assert seen and seen[0]["manifest_id"] == plan_id
    # и этот id реально работает как ключ второго вызова
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "pending")
    out2 = await getattr(s, tool)(**ALL_TOOLS[tool], manifest_id=plan_id,
                                  user_reply="да")
    assert "не найден" not in out2, (
        f"{tool}: сервер не узнал id, который сам же напечатал: {out2}")


# ═══════ 4. Отдельные plan_*-тулы (у них своя, не общая, сборка текста) ═════

async def test_plan_task_creation_prints_its_plan_id(monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    _tg_off(monkeypatch)

    response = await s.plan_task_creation(
        "Создаю 1", [{"title": "Позвонить маме", "project_id": "p1"}])

    assert _network_client_reads_plan_id(response) in s._MANIFESTS


async def test_plan_task_deletion_prints_its_plan_id(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})
    _tg_off(monkeypatch)

    response = await s.plan_task_deletion(
        "Удаляю 1", [{"taskId": "t1", "title": "Купить молоко"}])

    assert _network_client_reads_plan_id(response) in s._MANIFESTS


async def test_delete_tasks_first_call_prints_its_plan_id(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})
    _tg_off(monkeypatch)

    response = await s.delete_tasks(
        "Удаляю 1", [{"taskId": "t1", "title": "Купить молоко"}])

    assert _network_client_reads_plan_id(response) in s._MANIFESTS


async def test_plan_declutter_prints_its_plan_id(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    tasks = {f"t{i}": {"id": f"t{i}", "title": f"Задача {i}", "projectId": "p1",
                       "priority": 0, "createdTime": "2026-01-01T00:00:00.000+0000",
                       "modifiedTime": "2026-07-20T00:00:00.000+0000",
                       "content": ""} for i in range(3)}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: tasks)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    monkeypatch.setattr(s, "_dc_shim_available", lambda: False)
    _tg_off(monkeypatch)

    response = await s.plan_declutter()

    assert _network_client_reads_plan_id(response) in s._MANIFESTS


# ═══════ 5. Ловушка на будущее: ни одной точки постройки плана без теста ════

_PLAN_SITES_COVERED_HERE = {
    # inline-планы (у тула нет параметра manifest_id) — тесты 1 и 2
    "delete_project", "rename_tag",
    # отдельные plan_*/call #1 — тесты 4
    "create_tasks", "delete_tasks", "execute_declutter",
}

# Вызовы `_maybe_tg_notify_plan(tool_name, ...)` с ПЕРЕМЕННОЙ вместо имени
# тула: это два общих гейта, их покрывает параметризованный тест 3 по таблице
# ALL_TOOLS (сама таблица сверяется с кодом в test_tg_gate_all_tools.py).
_GENERIC_GATE_FUNCS = {"_gate_batch", "_gate_single"}


def _plan_sites():
    """Все места в server.py, где план УХОДИТ наружу (через
    `_maybe_tg_notify_plan` — единственную общую воронку фазы плана).
    Возвращает (константные имена тулов, имена функций с неконстантным
    аргументом).

    Учитываются ДВЕ формы вызова, и это не педантизм — на второй ловушка уже
    один раз ослепла (слияние 2026-08-06):

      1. прямой вызов        `await _maybe_tg_notify_plan("delete_tasks", mid, …)`
      2. косвенный, через    `await _run_blocking(_maybe_tg_notify_plan,
                              "delete_tasks", mid, …)`

    Вторая появилась, когда отправку плана унесли в отдельный поток (она
    синхронная и ходит в сеть — из корутины она морозила event loop). Для AST
    это уже НЕ вызов `_maybe_tg_notify_plan`, а передача функции аргументом,
    поэтому прежняя проверка перестала видеть сразу три точки
    (`create_tasks`, оба `delete_tasks`) и молча сообщала, что они «исчезли».

    С #115 в коде осталась только форма 1: хук стал корутиной и уводит
    сетевую часть в поток сам, одинаково для всех гейтованных тулов. Разбор
    формы 2 оставлен намеренно — как страховка на случай, если кто-то снова
    начнёт заказывать поток снаружи, — а не потому, что она где-то есть."""
    # Разнос главного файла (1.2.4 захода 1, 2026-08-09) развёл места сборки
    # планов по трём файлам: часть осталась в server.py, гейт уехал в
    # consent.py, автоуборка — в attic/declutter_disabled.py. Пока тест читал
    # ОДИН server.py, он переставал видеть уехавшие точки и тихо сужал
    # проверку до остатка. Теперь обходится ВЕСЬ пакет плюс attic/.
    root = pathlib.Path(s.__file__).resolve().parents[2]
    sources = sorted((root / "ticktick_mcp" / "src").glob("*.py"))
    sources += sorted((root / "attic").glob("*.py"))
    const_tools, dynamic_in = set(), set()

    def _account(args, fn_name):
        if not args:
            return
        first = args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            const_tools.add(first.value)
        else:
            dynamic_in.add(fn_name)

    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id == "_maybe_tg_notify_plan":
                    _account(node.args, fn.name)
                elif (node.func.id == "_run_blocking" and node.args
                      and isinstance(node.args[0], ast.Name)
                      and node.args[0].id == "_maybe_tg_notify_plan"):
                    # `_run_blocking(fn, *args)` — имя тула здесь ВТОРОЙ аргумент.
                    _account(node.args[1:], fn.name)
    return const_tools, dynamic_in


def test_every_place_that_builds_a_plan_is_covered_by_a_test():
    """Появится новый гейтованный тул со своей сборкой текста (как когда-то
    delete_project и rename_tag) — этот тест покраснеет, пока для него не
    напишут проверку печати id. Именно отсутствие такой ловушки и позволило
    двум планам годами остаться безымянными."""
    const_tools, dynamic_in = _plan_sites()

    uncovered = sorted(const_tools - _PLAN_SITES_COVERED_HERE)
    assert not uncovered, (
        f"планы без теста на печать id: {uncovered} — снаружи их, возможно, "
        "нечем назвать")

    stale = sorted(_PLAN_SITES_COVERED_HERE - const_tools)
    assert not stale, f"в списке покрытых есть исчезнувшие места: {stale}"

    assert dynamic_in <= _GENERIC_GATE_FUNCS, (
        f"план строится с неконстантным именем тула вне общих гейтов: "
        f"{sorted(dynamic_in - _GENERIC_GATE_FUNCS)} — таблица ALL_TOOLS его "
        "не покрывает")


def test_plan_id_line_is_the_single_formula_and_the_regex_reads_it():
    """Формат один на всех (`_plan_id_line`), и он же разбирается регуляркой,
    которой пользуются клиент/тесты. Разъедутся — id снова станет ненаходимым."""
    line = s._plan_id_line("abc123def456", "ничего ещё не удалено")
    assert _PLAN_ID_RE.search(line).group(1) == "abc123def456"
    assert "ничего ещё не удалено" in line
    # и в самом server.py не осталось самодельных копий формулы: единственное
    # место, где строка собирается, — тело самого `_plan_id_line`.
    src = pathlib.Path(s.__file__).read_text(encoding="utf-8")
    handmade = [ln.strip() for ln in src.splitlines()
                if "Манифест `" in ln
                and 'return f"_Манифест `{manifest_id}`' not in ln]
    assert not handmade, (f"формула id продублирована в обход _plan_id_line: "
                          f"{handmade}")
