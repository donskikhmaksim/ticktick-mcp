"""Тесты на `delete_tags` — массовое удаление тегов ОДНИМ подтверждением
(2026-08-09, docs/TZ/ZAHOD1.md §1.3.6, П20 захода 1).

Зачем весь пункт: `delete_tag` берёт один тег за вызов. К удалению обычно
идёт несколько десятков сразу — это несколько десятков отдельных планов и
отдельных нажатий кнопки подряд. Человек, нажимающий кнопку в шестидесятый
раз, перестаёт читать, что на ней написано, — механизм подтверждения
обесценивается для ВСЕГО сервера, включая удаление задач.

Главная ловушка, которую эти тесты обязаны поймать: `object_hash` в
`_gate_batch`/`_generic_gate_rehash` считался по `t.get("taskId") or
t.get("task_id") or ""` — у элемента-тега нет ни того, ни другого, поэтому
ВСЕ идентификаторы становились пустой строкой и хэш переставал различать
планы (test_object_hash_distinguishes_tag_plans). Общий помощник
`_gate_item_id` (consent.py) закрывает это, добавляя `name` как третий
источник id.

Ни сети, ни Postgres — v2-клиент фейковый (тот же приём, что в
tests/test_gate_delete_tag_comment.py, plus `get_open_tasks()` для сирот и
радиуса поражения)."""
import re

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg_approval


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


class FakeV2:
    """Минимальный фейк v2-клиента: теги (с `parent`) + открытые задачи (с
    `tags`) — ровно то, что читают `_live_tag_records`/`_open_by_id`/
    `_live_tag_names`. `refuse` — имена, чьё `delete_tag` «отвечает успехом»,
    но НЕ убирает тег из состояния (симулирует ту же тихую пробуксовку API,
    от которой уже защищается одиночный `_delete_tag_impl` — post-verify
    свежим чтением, а не разбором ответа)."""

    def __init__(self, tags=None, open_tasks=None, refuse=None):
        self.tags = tags if tags is not None else []
        self.open_tasks = open_tasks if open_tasks is not None else []
        self.refuse = set(n.lower() for n in (refuse or []))
        self.calls = []

    def get_state(self, force=False):
        self.calls.append(("get_state", force))
        return {}

    def get_tags(self):
        return [dict(t) for t in self.tags]

    def get_open_tasks(self):
        return [dict(t) for t in self.open_tasks]

    def get_tasks_by_tag(self, name):
        self.calls.append(("get_tasks_by_tag", name))
        label = name.lstrip("#").lower()
        return [t for t in self.open_tasks
                if label in [x.lower() for x in (t.get("tags") or [])]]

    def delete_tag(self, name):
        self.calls.append(("delete_tag", name))
        if name.lower() in self.refuse:
            return  # «успех» по ответу API, тег на деле остаётся живым
        self.tags = [t for t in self.tags
                    if (t.get("name") or "").lower() != name.lower()]


def _wire(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)


def _tg_on(monkeypatch, seen=None):
    """Включает Telegram-approval-слой и считает уходящие в него планы —
    нужно для «отправлено ОДНО сообщение с кнопками», а не N."""
    monkeypatch.setattr(s, "_TG_CFG", tg_approval.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))

    def _notify(cfg, manifest_id, preview, tool):
        if seen is not None:
            seen.append(manifest_id)
        return True, ""
    monkeypatch.setattr(tg_approval, "notify_plan", _notify)


class _IsolateManifests:
    """_MANIFESTS — общий модульный dict (consent.py), на который server.py
    и tg_auto_execute.py всего лишь ссылаются по имени: снимок/очистка/
    восстановление ЭТОГО ЖЕ объекта (не переприсваивание `s._MANIFESTS`,
    которое отвязало бы имя от настоящего хранилища) — тот же приём, что
    `_isolate_manifests` в tests/test_tg_gate_all_tools.py."""

    def __enter__(self):
        self.before = dict(s._MANIFESTS)
        self.tombs = dict(s._MANIFEST_TOMBSTONES)
        s._MANIFESTS.clear()
        s._MANIFEST_TOMBSTONES.clear()
        return self

    def __exit__(self, *exc):
        s._MANIFESTS.clear()
        s._MANIFESTS.update(self.before)
        s._MANIFEST_TOMBSTONES.clear()
        s._MANIFEST_TOMBSTONES.update(self.tombs)


@pytest.fixture(autouse=True)
def _isolate_manifests():
    with _IsolateManifests():
        yield


# ===========================================================================
# 1. Сорок тегов — одно подтверждение
# ===========================================================================

async def test_forty_tags_one_confirmation(monkeypatch):
    names = [f"тег{i}" for i in range(40)]
    fake = FakeV2(tags=[{"name": n, "parent": None} for n in names])
    _wire(monkeypatch, fake)
    seen = []
    _tg_on(monkeypatch, seen)

    preview = await s.delete_tags("Удаляю 40 тегов", names)

    # Один план — один манифест, ничего ещё не удалено.
    assert not any(c[0] == "delete_tag" for c in fake.calls)
    assert len(s._MANIFESTS) == 1
    mid = _extract_manifest_id(preview)
    assert mid in s._MANIFESTS
    assert len(s._MANIFESTS[mid]["tasks"]) == 40
    # Одно сообщение с кнопками, а не сорок.
    assert seen == [mid]

    # TG-слой включён → текстовый путь ЗАКРЫТ (см. docs/DESIGN_approval_
    # gate.md / _TG_APPROVAL_NOTE): подтверждение — это САМО нажатие кнопки,
    # которое здесь симулируется прямым вызовом того же исполнителя, что
    # зовёт фоновый поллер по факту нажатия (`_generic_gate_auto_execute`).
    result = await s._generic_gate_auto_execute(mid, s._MANIFESTS[mid])
    assert "🛑" not in result

    after = set(await s._live_tag_names(force=True))
    assert not (set(n.lower() for n in names) & after), (
        "после подтверждения хотя бы один из сорока тегов всё ещё жив")
    assert sum(1 for c in fake.calls if c[0] == "delete_tag") == 40


# ===========================================================================
# 2. Радиус поражения по каждому тегу в превью, ноль — тоже явно
# ===========================================================================

async def test_preview_states_blast_radius_per_tag(monkeypatch):
    fake = FakeV2(
        tags=[{"name": "дом", "parent": None}, {"name": "пусто", "parent": None}],
        open_tasks=[{"id": "t1", "tags": ["дом"]}, {"id": "t2", "tags": ["дом"]}])
    _wire(monkeypatch, fake)

    preview = await s.delete_tags("Убираю 2 тега", ["дом", "пусто"])

    assert "«дом» — снимется с 2 открытых задач" in preview
    assert "«пусто» — снимется с 0 открытых задач" in preview, (
        "тег без носителей обязан печатать 0 явно, а не пропускать строку")


# ===========================================================================
# 3. Родительский тег с детьми — отказ, план не построен
# ===========================================================================

async def test_parent_tag_is_refused_with_children_named(monkeypatch):
    fake = FakeV2(tags=[
        {"name": "работа", "parent": None},
        {"name": "проект-А", "parent": "работа"},
        {"name": "проект-Б", "parent": "работа"},
    ])
    _wire(monkeypatch, fake)

    result = await s.delete_tags("Удаляю родителя", ["работа"])

    assert "🛑" in result
    assert "manifest_id" not in result, "план для родительского тега строиться не должен"
    assert "«проект-А»" in result and "«проект-Б»" in result
    assert len(s._MANIFESTS) == 0
    assert not any(c[0] == "delete_tag" for c in fake.calls)


# ===========================================================================
# 4. Теги-сироты классифицируются, а не пропадают молча
# ===========================================================================

async def test_orphan_tags_are_classified_not_silently_missing(monkeypatch):
    fake = FakeV2(
        tags=[{"name": "дом", "parent": None}],
        open_tasks=[
            {"id": "t1", "tags": ["дом", "старое"]},
            {"id": "t2", "tags": ["старое"]},
            {"id": "t3", "tags": ["старое"]},
        ])
    _wire(monkeypatch, fake)

    preview = await s.delete_tags("Разбираю теги", ["дом", "старое"])

    assert "сирот" in preview.lower()
    assert "«старое»" in preview
    assert "носителей: 3" in preview
    mid = _extract_manifest_id(preview)
    planned_names = {t["name"] for t in s._MANIFESTS[mid]["tasks"]}
    assert planned_names == {"дом"}, (
        f"сирота «старое» не должна попасть в план удаления: {planned_names}")


# ===========================================================================
# 5. list_tags печатает число тегов-сирот
# ===========================================================================

async def test_list_tags_reports_orphan_count(monkeypatch):
    fake = FakeV2(
        tags=[{"name": "дом", "parent": None}],
        open_tasks=[{"id": "t1", "tags": ["дом", "старое1"]},
                   {"id": "t2", "tags": ["старое2"]}])
    _wire(monkeypatch, fake)

    out = await s.list_tags()

    assert "2 мет" in out and "сирот" in out


# ===========================================================================
# 6. object_hash различает планы с разными наборами тегов
# ===========================================================================

async def test_object_hash_distinguishes_tag_plans(monkeypatch):
    fake = FakeV2(tags=[{"name": "раз", "parent": None},
                        {"name": "два", "parent": None}])
    _wire(monkeypatch, fake)

    p1 = await s.delete_tags("Удаляю раз", ["раз"])
    mid1 = _extract_manifest_id(p1)
    p2 = await s.delete_tags("Удаляю два", ["два"])
    mid2 = _extract_manifest_id(p2)

    h1 = s._MANIFESTS[mid1]["object_hash"]
    h2 = s._MANIFESTS[mid2]["object_hash"]
    assert h1 and h2 and h1 != h2, (
        "два разных набора тегов обязаны давать разные object_hash — "
        "до правки _gate_item_id оба были пустым списком id и совпадали")
    # rehash (кнопочный путь) обязан воспроизводить ТУ ЖЕ формулу.
    assert s._generic_gate_rehash(s._MANIFESTS[mid1]) == h1
    assert s._generic_gate_rehash(s._MANIFESTS[mid2]) == h2


# ===========================================================================
# 7. Частичный исход — назван поимённо, не одной цифрой
# ===========================================================================

async def test_partial_outcome_named_per_tag(monkeypatch):
    fake = FakeV2(
        tags=[{"name": "раз", "parent": None}, {"name": "два", "parent": None},
             {"name": "три", "parent": None}],
        refuse=["два"])
    _wire(monkeypatch, fake)

    preview = await s.delete_tags("Удаляю 3", ["раз", "два", "три"])
    mid = _extract_manifest_id(preview)
    result = await s.delete_tags("Удаляю 3", manifest_id=mid, user_reply="да")

    assert "«два»" in result
    # «два» обязано быть названо именно в неудачной части, а не в успешной.
    failed_section = result.split("НЕ удалилось", 1)[1] if "НЕ удалилось" in result else ""
    assert "«два»" in failed_section
    assert "«раз»" not in failed_section and "«три»" not in failed_section
    assert "удалено 39 из 40" not in result.lower()
