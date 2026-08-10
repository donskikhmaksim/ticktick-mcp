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
