"""Рубежи против дублей при создании задач (1.3.3, дизайн раздел 8).

Живой случай, ради которого всё это написано: владелец подтвердил два похожих
манифеста подряд и получил ПЯТЬ ЗАДАЧ ДВАЖДЫ. Защита «один манифест — одно
исполнение» этого не ловит: манифестов было два, каждый исполнился ровно один
раз.

Рубежи:
  0 — внутри одного плана (точное совпадение → ОТКАЗ, близкое → предупреждение);
  1 — против живого состояния (предупреждение: «сделать ещё раз то же» бывает
      законным);
  2 — против чужих живых планов (предупреждение с идентификатором того плана);
  3 — при исполнении (исключает совпавшую позицию, остальные исполняются).

Проверяется ФАКТ в живом состоянии (сколько задач получилось), а не текст
ответа: инцидент выглядел именно как «в отчёте всё хорошо, а в проекте десять
задач вместо пяти».
"""
import re

import pytest

import ticktick_mcp.src.server as s


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


def _mid(text: str) -> str:
    m = re.search(r"Манифест `([0-9a-f]+)`", text)
    assert m, f"нет id манифеста:\n{text}"
    return m.group(1)


_NAMES = {"p_in": "Входящие", "p_work": "Работа"}


class _FakeV2:
    def __init__(self, live):
        self.live = live
        self.calls = []

    def invalidate_cache(self):
        pass

    def get_open_tasks(self):
        return list(self.live.values())

    def get_state(self, force=False):
        return {"tags": []}

    def get_tags(self):
        return []

    def batch_update_tasks(self, changes):
        self.calls.append(("update", [c["taskId"] for c in changes]))
        return {}


class _FakeOfficial:
    def __init__(self, live):
        self.live = live
        self.calls = []
        self._n = 0

    def create_task(self, title, project_id, content=None, start_date=None,
                    due_date=None, priority=0, is_all_day=False,
                    repeat_flag=None, reminders=None):
        self._n += 1
        tid = f"new{self._n}"
        self.calls.append(("create", title, project_id))
        self.live[tid] = {"id": tid, "title": title, "projectId": project_id,
                          "tags": []}
        return {"id": tid, "title": title, "projectId": project_id}


def _wire(monkeypatch, live, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(_NAMES))
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    v2 = _FakeV2(live)
    official = _FakeOfficial(live)
    monkeypatch.setattr(s, "ticktick_v2", v2)
    monkeypatch.setattr(s, "ticktick", official)
    return v2, official


def _create(title, project="p_work", said="заведи"):
    return {"op": "create", "title": title, "to_project_id": project,
            "said": said}


# ════════════════════════════ рубеж 0 ═════════════════════════════════════

async def test_stage0_identical_creates_in_one_plan_refuse_the_whole_plan(
        monkeypatch, tmp_path):
    """Два создания с одинаковым названием в один проект — план НЕ строится.

    Это не «сделай дважды», а почти всегда ошибка сборки плана: исполнив её,
    сервер выдал бы два объекта там, где человек назвал один."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)

    out = await s.manual_triage("Заношу дела", [
        _create("Позвонить в страховую"),
        _create("позвонить в страховую  "),   # то же после нормализации
    ])

    assert "🛑" in out and "одинаковым названием" in out
    assert s._MANIFESTS == {}, "отказ не имеет права строить план"
    assert official.calls == [] and live == {}


async def test_stage0_same_title_in_different_projects_is_allowed(
        monkeypatch, tmp_path):
    """Одинаковое имя в РАЗНЫХ проектах — законно (у каждого списка своя
    «Оплатить счёт»), и рубеж 0 его не трогает."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Заношу дела", [
        _create("Оплатить счёт", project="p_work"),
        _create("Оплатить счёт", project="p_in"),
    ])

    assert "Манифест" in preview
    await s.manual_triage("Заношу дела", manifest_id=_mid(preview),
                          user_reply="да")
    assert len(live) == 2


async def test_stage0_similar_titles_only_warn(monkeypatch, tmp_path):
    """Близкие названия — предупреждение, план строится: «купить молоко» и
    «купить молоко 2л» бывают разными делами, и запрет стоил бы законного
    плана."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Заношу дела", [
        _create("Купить молоко и хлеб"),
        _create("Купить молоко, хлеб"),
    ])

    assert "Манифест" in preview, preview
    assert "ПОХОЖИХ создания" in preview
    await s.manual_triage("Заношу дела", manifest_id=_mid(preview),
                          user_reply="да")
    assert len(live) == 2, "предупреждение НЕ блокирует"


# ════════════════════════════ рубеж 1 ═════════════════════════════════════

async def test_stage1_exact_match_with_live_state_warns_and_plans(
        monkeypatch, tmp_path):
    """Такая задача уже открыта в этом проекте → предупреждение, но план
    строится: повторить то же бывает законно."""
    live = {"old": {"id": "old", "title": "Позвонить в страховую",
                    "projectId": "p_work"}}
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Заношу дела",
                                    [_create("Позвонить в страховую")])

    assert "Манифест" in preview
    assert "уже есть" in preview and "создастся ВТОРАЯ" in preview


async def test_stage1_near_match_warns(monkeypatch, tmp_path):
    live = {"old": {"id": "old", "title": "Купить молоко и хлеб",
                    "projectId": "p_work"}}
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Заношу дела",
                                    [_create("Купить молоко, хлеб")])

    assert "Манифест" in preview
    assert "похоже на уже открытую" in preview


async def test_stage1_ignores_other_projects(monkeypatch, tmp_path):
    """Сравнение — В ПРЕДЕЛАХ ПРОЕКТА НАЗНАЧЕНИЯ: одноимённая задача в другом
    списке не повод пугать человека."""
    live = {"old": {"id": "old", "title": "Позвонить в страховую",
                    "projectId": "p_in"}}
    v2, official = _wire(monkeypatch, live, tmp_path)

    preview = await s.manual_triage("Заношу дела",
                                    [_create("Позвонить в страховую",
                                             project="p_work")])

    assert "уже есть" not in preview and "похоже на уже открытую" not in preview


async def test_stage1_reads_fresh_state_never_cache(monkeypatch, tmp_path):
    """Первой причиной, по которой радар не сработал в живом инциденте, было
    чтение НЕСВЕЖЕГО списка. Здесь закреплено, что снимок берётся с
    fresh=True — и что это тот же снимок, которым идёт сверка (лишних чтений
    рубеж не добавляет)."""
    live = {"old": {"id": "old", "title": "Позвонить в страховую",
                    "projectId": "p_work"}}
    seen = []

    def _open(fresh=False):
        seen.append(fresh)
        return dict(live)

    _wire(monkeypatch, live, tmp_path)
    monkeypatch.setattr(s, "_open_by_id", _open)

    await s.manual_triage("Заношу дела", [_create("Позвонить в страховую")])

    assert seen == [True], f"фаза плана читает живое состояние один раз, свежим: {seen}"


# ════════════════════════════ рубеж 2 ═════════════════════════════════════

async def test_stage2_names_the_other_live_plan(monkeypatch, tmp_path):
    """Чужой ЖИВОЙ план с таким же созданием → предупреждение, НАЗЫВАЮЩЕЕ его
    идентификатор: иначе человек знает, что где-то есть второй план, но не
    знает какой, и отменить его не может."""
    live = {}
    _wire(monkeypatch, live, tmp_path)
    monkeypatch.setattr(s.manifest_store, "list_live", lambda tool="", window_ms=0: [
        {"manifest_id": "deadbeef01", "tool": "manual_triage",
         "payload": {"tasks": [{"op": "create", "title": "Позвонить в страховую",
                                "_to_project_id": "p_work"}]}}])

    preview = await s.manual_triage("Заношу дела",
                                    [_create("Позвонить в страховую")])

    assert "Манифест" in preview, "рубеж 2 предупреждает, а не блокирует"
    assert "deadbeef01" in preview, preview
    assert "уже ждёт подтверждения" in preview


async def test_stage2_survives_an_unreadable_manifest_store(monkeypatch, tmp_path):
    """Сбой хранилища планов не имеет права превратиться в отказ строить
    план: рубеж 2 — предупреждение."""
    live = {}
    _wire(monkeypatch, live, tmp_path)

    def _boom(tool="", window_ms=0):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(s.manifest_store, "list_live", _boom)

    preview = await s.manual_triage("Заношу дела",
                                    [_create("Позвонить в страховую")])

    assert "Манифест" in preview


# ════════════════════════════ рубеж 3 ═════════════════════════════════════

async def _plan_and_execute(ops, summary="Заношу дела"):
    preview = await s.manual_triage(summary, ops)
    assert "Манифест" in preview, preview
    return await s.manual_triage(summary, manifest_id=_mid(preview),
                                 user_reply="да")


async def test_incident_two_plans_five_creations_each(monkeypatch, tmp_path):
    """ВОСПРОИЗВЕДЕНИЕ РЕАЛЬНОГО ИНЦИДЕНТА.

    Владелец подтвердил два похожих манифеста подряд и получил пять задач
    дважды. Здесь: оба плана строятся ДО исполнения любого из них (как и было
    — второй план уже висел, когда исполнялся первый), исполняется первый,
    затем второй. Итог судится ПО ФАКТУ: в проекте ровно пять задач, а не
    десять.

    По НОВОЙ политике владельца (2026-08-10) второй манифест не глохнет
    молча: раз совпали ВСЕ его позиции, отчёт говорит «манифест, похоже, уже
    исполнен» — формулировка называет вероятную причину, а не выглядит
    безмолвным провалом «выполнено 0 из 5»."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)
    titles = [f"Дело {i}" for i in range(1, 6)]

    first = await s.manual_triage("Заношу дела", [_create(t) for t in titles])
    second = await s.manual_triage("Заношу дела", [_create(t) for t in titles])

    await s.manual_triage("Заношу дела", manifest_id=_mid(first),
                          user_reply="да")
    assert len(live) == 5, "первый план создал пять"

    out = await s.manual_triage("Заношу дела", manifest_id=_mid(second),
                                user_reply="да")

    assert len(live) == 5, f"в проекте должно остаться РОВНО 5 задач: {live}"
    assert len(official.calls) == 5, "второй план не дёргал канал создания"
    assert "УЖЕ ИСПОЛНЕН" in out.upper(), out
    assert "Выполнено 0 из" not in out, "это не безмолвный провал"
    # Дешёвый законный повтор назван прямо в отчёте.
    assert "confirmed_repeat" in out


async def test_partial_overlap_executes_the_rest(monkeypatch, tmp_path):
    """НОВАЯ ПОЛИТИКА, главное её следствие: совпавшая позиция исключается
    ОДНА, остальные исполняются. Старая («полная остановка манифеста») здесь
    не создала бы ничего."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)

    first = await s.manual_triage("Заношу", [_create("Дело A")])
    await s.manual_triage("Заношу", manifest_id=_mid(first), user_reply="да")
    assert len(live) == 1

    out = await _plan_and_execute([_create("Дело A"), _create("Дело B"),
                                   _create("Дело C")])

    titles = sorted(t["title"] for t in live.values())
    assert titles == ["Дело A", "Дело B", "Дело C"], titles
    assert "УЖЕ ИСПОЛНЕН" not in out.upper(), "остановки манифеста быть не должно"
    assert "✅ Выполнено 2 из" in out
    assert "уже создана" in out


async def test_confirmed_repeat_passes_stage3(monkeypatch, tmp_path):
    """Дешёвый законный повтор: одна операция с явным флагом, без пересборки
    манифеста. Рубеж 3 для неё не срабатывает повторно."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)
    first = await s.manual_triage("Заношу", [_create("Дело A")])
    await s.manual_triage("Заношу", manifest_id=_mid(first), user_reply="да")

    op = _create("Дело A")
    op["confirmed_repeat"] = True
    out = await _plan_and_execute([op])

    assert len([t for t in live.values() if t["title"] == "Дело A"]) == 2, \
        "осознанный повтор обязан создать вторую"
    assert "✅ Выполнено 1 из 1" in out


async def test_confirmed_repeat_must_be_a_real_boolean(monkeypatch, tmp_path):
    """«true» строкой молча прочиталось бы как «флага нет», и рубеж сработал
    бы снова — а вызывающий считал бы, что снял его."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)
    op = _create("Дело A")
    op["confirmed_repeat"] = "true"

    out = await s.manual_triage("Заношу", [op])

    assert "🛑" in out and "confirmed_repeat" in out
    assert s._MANIFESTS == {} and official.calls == []


async def test_incident_via_journal_only(monkeypatch, tmp_path):
    """Тот же инцидент, но вторая партия видна ТОЛЬКО в журнале: живая
    выборка отстаёт и только что созданную задачу ещё не отдаёт. Без чтения
    журнала рубеж 3 пропустил бы дубль."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)

    first = await s.manual_triage("Заношу", [_create("Дело A")])
    await s.manual_triage("Заношу", manifest_id=_mid(first), user_reply="да")
    created_id = next(iter(live))
    assert live[created_id]["title"] == "Дело A"

    second = await s.manual_triage("Заношу", [_create("Дело A")])

    # ЖИВАЯ ВЫБОРКА ОТСТАЁТ: задача есть в состоянии под своим id, но НЕ под
    # своим названием (так выглядит незасинхронизировавшийся снимок для
    # сравнения по имени). Единственный, кто ещё помнит создание, — журнал.
    live[created_id]["title"] = ""

    out = await s.manual_triage("Заношу", manifest_id=_mid(second),
                                user_reply="да")

    assert len(official.calls) == 1, "по журналу дубль обязан быть исключён"
    assert "журнала" in out, out
    assert "УЖЕ ИСПОЛНЕН" in out.upper()


async def test_journal_hit_without_a_live_object_does_not_block(
        monkeypatch, tmp_path):
    """Условие 1 владельца: совпадение по журналу засчитывается ТОЛЬКО при
    живом подтверждении. Задачу из журнала с тех пор удалили — мёртвая запись
    не имеет права остановить живую операцию."""
    live = {}
    v2, official = _wire(monkeypatch, live, tmp_path)

    first = await s.manual_triage("Заношу", [_create("Дело A")])
    await s.manual_triage("Заношу", manifest_id=_mid(first), user_reply="да")
    live.clear()          # задачу удалили — в журнале запись осталась

    out = await _plan_and_execute([_create("Дело A")])

    assert len(live) == 1, "создание обязано пройти: объекта из журнала нет"
    assert "✅ Выполнено 1 из 1" in out


async def test_stage3_near_match_does_not_block(monkeypatch, tmp_path):
    """Близкое совпадение на рубеже 3 не исключает и не останавливает —
    только печатается: иначе законное «сделай ещё раз то же самое» стало бы
    невозможным."""
    live = {"old": {"id": "old", "title": "Купить молоко и хлеб",
                    "projectId": "p_work"}}
    v2, official = _wire(monkeypatch, live, tmp_path)

    out = await _plan_and_execute([_create("Купить молоко, хлеб")])

    assert len(live) == 2, "близкое совпадение ничего не блокирует"
    assert "Похожее рядом" in out
