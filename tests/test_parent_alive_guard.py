"""П19 (2026-08-09): «создаётся подзадачей — жив ли родитель и он ли это».

Дыра, которую закрывают эти тесты. При создании задачи с `parent_id`
сервер нигде не спрашивал, существует ли ещё родительская задача:
единственным обращением к живому состоянию был расчёт глубины, а
`_task_level` на неизвестном id молча отвечает «уровень 1». То есть
мёртвый родитель читался как живой корневой, задача создавалась и ложилась
ОТДЕЛЬНОЙ строкой в корень списка, а владелец узнавал об этом из
предупреждения ПОСЛЕ факта («не вложена под родителя»). На фазе плана слово
`parent_id` не встречалось вовсе — карточка на подзадачу удалённой задачи
показывалась как обычный план.

Оба входа проверяются отдельно и оба — ЧЕРЕЗ ПУБЛИЧНЫЕ ИНСТРУМЕНТЫ, те,
которые видит модель: `plan_task_creation` (фаза плана) и `create_tasks`
с `automation_key` (headless-путь мимо плана — именно он остаётся, когда
проверку ставят только в план). Утверждения — по ФАКТУ (канал не дёрнут,
живое состояние не изменилось, манифест пуст), а не по формулировке ответа:
старое поведение печатало предупреждение и прошло бы проверку «есть 🛑».

Отдельно зафиксировано различие «родителя нет» и «состояние не прочитано»:
недоступное чтение на плане НЕ отказ (разовый сбой не имеет права
заблокировать всякое создание), но на исполнении — отказ; а родитель,
которого нет в v2-снимке, но который жив по официальному Open API
(снимок открытых задач отстаёт — инцидент 2026-08-07), создание не
блокирует.
"""
import re

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ───────────────────────── двойники каналов ─────────────────────────
# Повторяют настоящие клиенты в том, что важно для этих тестов: v2 отвечает
# 200 с отказами ВНУТРИ тела, официальный точечный read отвечает {"error":…}
# на неизвестный id, привязка родителя реально проставляется в живом
# состоянии.

class _FakeV2:
    def __init__(self, projects, live, inbox_id="inbox1"):
        self.projects = projects
        self.live = live
        self.inbox_id = inbox_id
        self.calls = []          # ЛЮБОЕ обращение на запись
        self.reads = 0

    # ---- чтение ----
    def invalidate_cache(self):
        pass

    def get_state(self, force=False):
        return {"projectProfiles": self.projects, "inboxId": self.inbox_id,
                "tags": []}

    def get_open_tasks(self):
        self.reads += 1
        return list(self.live.values())

    def get_tags(self):
        return []

    # ---- запись ----
    def batch_create_tasks(self, tasks):
        self.calls.append(("create", [t["id"] for t in tasks]))
        for t in tasks:
            self.live[t["id"]] = {"id": t["id"], "title": t.get("title"),
                                  "projectId": t.get("projectId"), "tags": []}
        return {}

    def _request(self, method, path, json=None):
        self.calls.append((path, json or []))
        if path == "/batch/taskParent":
            return self._link(json or [])
        return {}

    def batch_set_task_parent(self, task_ids, parent_id, project_id):
        rows = [{"parentId": parent_id, "taskId": tid, "projectId": project_id}
                for tid in task_ids]
        self.calls.append(("/batch/taskParent", rows))
        return self._link(rows)

    def _link(self, rows):
        for r in rows:
            if r["taskId"] in self.live:
                self.live[r["taskId"]]["parentId"] = r["parentId"]
        return {}

    def batch_update_tasks(self, changes):
        self.calls.append(("update", changes))
        return {}

    def set_task_column(self, task_id, column_id):
        self.calls.append(("column", task_id))
        return {}


class _BlindV2(_FakeV2):
    """Живое состояние прочитать нельзя (v2 не отвечает) — это «я не
    смотрел», а не «задачи нет»."""

    def get_open_tasks(self):
        self.reads += 1
        raise RuntimeError("v2 unreachable")


class _FakeOfficial:
    """Официальный Open API: создание задач + точечное чтение.

    `extra` — задачи, которые ЗНАЕТ официальный API, но которых нет в
    v2-снимке открытых: ровно тот случай отставания снимка, ради которого
    в guard'е и живёт этот запасной канал."""

    def __init__(self, live, projects, extra=None):
        self.live = live
        self.projects = projects
        self.extra = extra or {}
        self.create_calls = []
        self.get_calls = []
        self._n = 0

    def get_projects(self):
        return list(self.projects)

    def get_task(self, project_id, task_id):
        self.get_calls.append((project_id, task_id))
        t = self.extra.get(task_id) or self.live.get(task_id)
        if not t or (t.get("projectId") or "") != project_id:
            return {"error": "404 task not found"}
        return dict(t)

    def create_task(self, title, project_id, content=None, start_date=None,
                    due_date=None, priority=0, is_all_day=False,
                    repeat_flag=None, reminders=None):
        self._n += 1
        tid = f"new{self._n}"
        self.create_calls.append({"title": title, "project_id": project_id})
        self.live[tid] = {"id": tid, "title": title, "projectId": project_id,
                          "tags": []}
        return {"id": tid}


_PROJECTS = [{"id": "pA", "name": "Работа"}]


def _wire(monkeypatch, live=None, extra=None, blind=False):
    live = {} if live is None else live
    v2 = (_BlindV2 if blind else _FakeV2)(_PROJECTS, live)
    official = _FakeOfficial(live, _PROJECTS, extra)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", official)
    return v2, official


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    s._MANIFESTS.clear()
    yield
    s._MANIFESTS.clear()


def _tg_on(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


def _capture_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "notify_plan",
                        lambda cfg, manifest_id, preview_body, tool:
                        (sent.append((manifest_id, tool)), (True, ""))[1])
    return sent


_MID_RE = re.compile(r"Манифест `([0-9a-f]{6,})`")


def _only_manifest(plan_out):
    """Состояние плана — по id, ВЫНУТОМУ ИЗ ТЕКСТА ответа: клиент MCP видит
    только его, реестра в памяти процесса у него нет."""
    m = _MID_RE.search(plan_out)
    assert m, f"в ответе нет id плана — снаружи его нечем назвать:\n{plan_out}"
    mid = m.group(1)
    assert mid in s._MANIFESTS, (
        f"напечатанный клиенту id {mid} не адресует ни один живой план")
    assert len(s._MANIFESTS) == 1, s._MANIFESTS
    return s._MANIFESTS[mid]


# ===========================================================================
# 1. Ядро мутации: отказ ДО записи (публичный headless-вход)
# ===========================================================================

async def test_creation_under_a_dead_parent_is_refused_before_the_write(
        monkeypatch):
    """Снимок открытых задач пуст, официальный API про `root1` тоже не
    знает — родителя нет. Публичный `create_tasks` (тот самый путь с
    `automation_key`, который проходит МИМО плана) обязан отказать до
    единого обращения на запись."""
    live = {}
    v2, official = _wire(monkeypatch, live)

    out = await s.create_tasks(
        "тест",
        [{"title": "Покрасить стену", "project_id": "pA",
          "parent_id": "root1", "parent_title": "Дом"}],
        automation_key=s.SECRET)

    # ФАКТ: канал не дёрнут, данные не изменились.
    assert official.create_calls == []
    assert v2.calls == []
    assert live == {}
    # …и только потом — что владельцу об этом сказали.
    assert "🛑" in out
    assert "Дом" in out
    assert "Создано" not in out


async def test_dead_parent_among_living_tasks_is_refused(monkeypatch):
    """Снимок НЕПУСТОЙ: в нём есть живые задачи, мёртв только родитель.
    Тест с полностью пустым снимком зеленел бы и от падения по другой
    причине — здесь такой лазейки нет: соседняя строка того же батча с
    ЖИВЫМ родителем создаётся как раньше."""
    live = {"alive1": {"id": "alive1", "title": "Дом", "projectId": "pA"},
            "alive2": {"id": "alive2", "title": "Дача", "projectId": "pA"}}
    v2, official = _wire(monkeypatch, live)

    out = await s.create_tasks(
        "тест",
        [{"title": "Покрасить стену", "project_id": "pA",
          "parent_id": "ghost9", "parent_title": "Снесённый дом"},
         {"title": "Помыть окна", "project_id": "pA",
          "parent_id": "alive1", "parent_title": "Дом"}],
        automation_key=s.SECRET)

    assert [c["title"] for c in official.create_calls] == ["Помыть окна"]
    assert "Покрасить стену" not in live_titles(live)
    assert "Снесённый дом" in out and "🛑" in out
    # живая половина батча доехала целиком, включая привязку
    made = next(t for t in live.values() if t.get("title") == "Помыть окна")
    assert made.get("parentId") == "alive1"


def live_titles(live):
    return {t.get("title") for t in live.values()}


async def test_parent_title_mismatch_is_refused_with_both_names(monkeypatch):
    """Родитель ЖИВ, но `parent_id` указывает не на ту задачу. Отказ обязан
    называть оба имени — иначе владельцу нечем понять, что он перепутал."""
    live = {"root1": {"id": "root1", "title": "Ремонт кухни",
                      "projectId": "pA"}}
    v2, official = _wire(monkeypatch, live)

    out = await s.create_tasks(
        "тест",
        [{"title": "Покрасить стену", "project_id": "pA",
          "parent_id": "root1", "parent_title": "Дом"}],
        automation_key=s.SECRET)

    assert official.create_calls == []
    assert v2.calls == []
    assert "это «Ремонт кухни»" in out
    assert "а НЕ «Дом»" in out


async def test_depth_is_never_computed_for_a_dead_parent(monkeypatch):
    """Порядок проверок: сперва существование, потом глубина. Иначе
    остаётся ветка, где «уровень 1» получен из пустоты, и владелец читает
    жалобу на вложенность вместо правды про родителя."""
    live = {}
    _wire(monkeypatch, live)
    calls = []
    monkeypatch.setattr(s, "_task_level",
                        lambda task_id, by_id: calls.append(task_id) or 1)

    out = await s.create_tasks(
        "тест",
        [{"title": "Покрасить стену", "project_id": "pA",
          "parent_id": "ghost9", "parent_title": "Дом",
          "subtasks": [{"title": "A", "subtasks": [
              {"title": "B", "subtasks": [{"title": "C", "subtasks": [
                  {"title": "D"}]}]}]}]}],
        automation_key=s.SECRET)

    assert calls == [], f"_task_level позвали по мёртвому id: {calls}"
    assert "Дом" in out
    assert "вложенность" not in out


async def test_a_parent_missing_from_the_stale_snapshot_is_still_alive(
        monkeypatch):
    """Обратная сторона того же различия: родителя НЕТ в v2-снимке
    открытых задач, но официальный Open API его отдаёт (снимок отстаёт —
    инцидент 2026-08-07). Это «я не увидел», а не «его нет», и создание
    проходит."""
    live = {}
    extra = {"root1": {"id": "root1", "title": "Дом", "projectId": "pA",
                       "status": 0}}
    v2, official = _wire(monkeypatch, live, extra=extra)

    out = await s.create_tasks(
        "тест",
        [{"title": "Покрасить стену", "project_id": "pA",
          "parent_id": "root1", "parent_title": "Дом"}],
        automation_key=s.SECRET)

    assert [c["title"] for c in official.create_calls] == ["Покрасить стену"]
    assert "🛑" not in out


async def test_unreadable_state_refuses_execution(monkeypatch):
    """Состояние прочитать не удалось — на ИСПОЛНЕНИИ это отказ (последняя
    линия), а не «данных нет — значит можно»."""
    live = {}
    v2, official = _wire(monkeypatch, live, blind=True)

    out = await s.create_tasks(
        "тест",
        [{"title": "Покрасить стену", "project_id": "pA",
          "parent_id": "root1", "parent_title": "Дом"}],
        automation_key=s.SECRET)

    assert official.create_calls == []
    assert v2.calls == []
    assert "🛑" in out


# ===========================================================================
# 2. Фаза плана: строка с мёртвым родителем в манифест не попадает
# ===========================================================================

async def test_dead_parent_row_never_enters_the_manifest(monkeypatch):
    """Не пометка в тексте, а исключение из манифеста: пометка оставила бы
    кнопку, нажатие на которую даёт ровно то, от чего защищаемся."""
    live = {"root1": {"id": "root1", "title": "Дом", "projectId": "pA"}}
    _wire(monkeypatch, live)

    out = await s.plan_task_creation("тест", [
        {"title": "Позвонить маме", "project_id": "pA"},
        {"title": "Покрасить стену", "project_id": "pA",
         "parent_id": "ghost9", "parent_title": "Снесённый дом"}])

    raw = _only_manifest(out)["raw"]
    assert len(raw) == 1
    assert raw[0]["title"] == "Позвонить маме"
    assert all(r.get("parent_id") != "ghost9" for r in raw)
    assert "Исключены 1" in out


async def test_no_manifest_and_no_telegram_when_every_row_is_refused(
        monkeypatch):
    """Прошедших строк не осталось — плана нет вовсе: ни манифеста, ни
    карточки с кнопкой в Telegram."""
    _wire(monkeypatch, {})
    _tg_on(monkeypatch)
    sent = _capture_notify(monkeypatch)

    out = await s.plan_task_creation("тест", [
        {"title": "Покрасить стену", "project_id": "pA",
         "parent_id": "ghost9", "parent_title": "Дом"}])

    assert s._MANIFESTS == {}
    assert sent == []
    assert "🛑" in out and "Дом" in out


async def test_plan_card_names_the_nesting(monkeypatch):
    """Владелец подтверждает вложение, которое он ПРОЧИТАЛ, и имя в
    карточке — живое, а не то, что прислала модель."""
    live = {"root1": {"id": "root1", "title": "Дом", "projectId": "pA"}}
    _wire(monkeypatch, live)

    out = await s.plan_task_creation("тест", [
        {"title": "Покрасить стену", "project_id": "pA",
         "parent_id": "root1", "parent_title": "Дом"}])

    assert "подзадача «Дом»" in out
    assert len(_only_manifest(out)["raw"]) == 1


async def test_unreadable_state_does_not_block_the_plan(monkeypatch):
    """Разовый сбой чтения не имеет права заблокировать всякое создание —
    план строится, но говорит вслух, что родитель не сверен."""
    _wire(monkeypatch, {}, blind=True)

    out = await s.plan_task_creation("тест", [
        {"title": "Покрасить стену", "project_id": "pA",
         "parent_id": "root1", "parent_title": "Дом"}])

    assert len(_only_manifest(out)["raw"]) == 1
    assert "НЕ сверено" in out
    assert "сверка повторится при исполнении" in out


async def test_parent_is_checked_even_when_the_project_was_suggested(
        monkeypatch):
    """Строка без `project_id` уходит к подсказчику назначения. Если бы
    сверка родителя стояла до этого, вызов без проекта обходил бы её
    целиком."""
    _wire(monkeypatch, {})
    monkeypatch.setattr(s, "_suggest_destinations",
                        lambda titles, names: [
                            {"project_id": "pA", "project": "Работа",
                             "confidence": "sure", "reason": "по смыслу"}
                            for _ in titles])

    out = await s.plan_task_creation("тест", [
        {"title": "Покрасить стену", "parent_id": "ghost9",
         "parent_title": "Дом"}])

    assert s._MANIFESTS == {}
    assert "Дом" in out and "🛑" in out


async def test_plan_without_parent_ids_reads_no_extra_live_state(monkeypatch):
    """Ленивость: план без единого `parent_id` не платит за эту проверку ни
    одним чтением. Число зафиксировано вручную (один вызов — радар
    дубликатов, он был здесь и до правки), а не вычислено из кода."""
    _wire(monkeypatch, {})
    n = {"calls": 0}
    real = s._open_by_id

    def _counted(fresh=False):
        n["calls"] += 1
        return real(fresh=fresh)

    monkeypatch.setattr(s, "_open_by_id", _counted)

    await s.plan_task_creation("тест", [
        {"title": "Позвонить маме", "project_id": "pA"},
        {"title": "Купить хлеб", "project_id": "pA"}])

    assert n["calls"] == 1
