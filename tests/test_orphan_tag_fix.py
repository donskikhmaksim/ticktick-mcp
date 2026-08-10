"""Тег-сирота: дыра закрыта на ВСЮ длину пути (1.3.3/изм-4, дизайн раздел 9).

Что такое сирота. У TickTick тег живёт в ДВУХ местах: в списке тегов аккаунта
(его видит `list_tags`, по нему работает `delete_tag`) и отдельной меткой на
самой задаче. Тег, записанный обновлением задачи, попадает ТОЛЬКО во второе
место: `list_tags` его не покажет, `delete_tag` ответит «не существует», а
метка на задаче останется. Это порча данных, а не косметика: массовое удаление
по списку из `list_tags` такой тег не найдёт никогда.

Здесь проверяется ровно то, что требует ТЗ 1.3.3, пункт 6 приёмки: задать
через агрегатор тег, которого нет в аккаунте → он ПОЯВИЛСЯ в `list_tags` и
УДАЛЯЕТСЯ `delete_tag`. Оба конца пути настоящие, через публичные инструменты,
а не через внутренние функции — именно так дыру и обнаружили.
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


class _FakeV2:
    """Двойник, который ведёт ДВА хранилища тега отдельно — как настоящий
    бэкенд. Именно их расхождение и есть сирота, поэтому склеивать их в одно
    в двойнике нельзя: тест перестал бы видеть проблему, которую сторожит."""

    def __init__(self, live, tags):
        self.live = live
        self.account_tags = list(tags)   # то, что видит list_tags
        self.calls = []

    def invalidate_cache(self):
        pass

    def get_open_tasks(self):
        return list(self.live.values())

    def get_state(self, force=False):
        return {"tags": list(self.account_tags)}

    def get_tags(self):
        return list(self.account_tags)

    def create_tag(self, name):
        self.calls.append(("create_tag", name))
        self.account_tags.append({"name": str(name).lower(), "label": name})
        return {}

    def delete_tag(self, name):
        self.calls.append(("delete_tag", name))
        self.account_tags = [t for t in self.account_tags
                             if t["name"] != str(name).lower()]
        for task in self.live.values():
            task["tags"] = [t for t in (task.get("tags") or [])
                            if t != str(name).lower()]
        return {}

    def get_tasks_by_tag(self, name):
        return [t for t in self.live.values()
                if str(name).lower() in (t.get("tags") or [])]

    def batch_update_tasks(self, changes):
        self.calls.append(("update", [c["taskId"] for c in changes]))
        for c in changes:
            t = self.live.setdefault(c["taskId"], {"id": c["taskId"]})
            for k, v in c.items():
                if k != "taskId":
                    t[k] = v
        return {}


async def test_tag_set_via_aggregator_appears_in_list_tags(monkeypatch, tmp_path):
    """Полный путь: агрегатор ставит НЕЗНАКОМЫЙ тег → он виден в list_tags →
    delete_tag его удаляет и снимает с задачи."""
    live = {"t1": {"id": "t1", "title": "Позвонить в банк",
                   "projectId": "p_in", "tags": []}}
    v2 = _FakeV2(live, tags=[{"name": "старый", "label": "старый"}])
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_in": "Входящие"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "ticktick_v2", v2)

    # 1. Ставим тег, которого в аккаунте НЕТ.
    preview = await s.manual_triage("Разбираю", [
        {"op": "tags", "task_id": "t1", "title": "Позвонить в банк",
         "changes": {"tags": ["Ипотека"]}, "said": "пометь ипотекой"}])
    await s.manual_triage("Разбираю", manifest_id=_mid(preview),
                          user_reply="да")

    assert live["t1"]["tags"] == ["ипотека"], "метка на задаче обязана стоять"

    # 2. Тег ВИДЕН в списке тегов аккаунта — то, чего сирота не умеет.
    listed = await s.list_tags()
    assert "Ипотека" in listed, listed

    # 3. …и УДАЛЯЕТСЯ штатным delete_tag (сирота получал бы «не существует»).
    plan = await s.delete_tag("Ипотека")
    done = await s.delete_tag("Ипотека", manifest_id=_mid(plan),
                              user_reply="да")

    assert "✅" in done, done
    assert all(t["name"] != "ипотека" for t in v2.account_tags)
    assert live["t1"]["tags"] == [], "метка снята с задачи вместе с тегом"


async def test_orphan_path_is_closed_at_the_source(monkeypatch, tmp_path):
    """Проверка откатом «убрать регистрацию тега у типа tags» с другой
    стороны: путь, которым сирота РАНЬШЕ появлялся (`changes.tags` у
    `update`), теперь отвергается ещё до всякой мутации."""
    live = {"t1": {"id": "t1", "title": "Позвонить в банк",
                   "projectId": "p_in", "tags": []}}
    v2 = _FakeV2(live, tags=[])
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_in": "Входящие"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "ticktick_v2", v2)

    out = await s.manual_triage("Разбираю", [
        {"op": "update", "task_id": "t1", "title": "Позвонить в банк",
         "changes": {"tags": ["ипотека"]}, "said": "пометь ипотекой"}])

    assert "🛑" in out and 'op="tags"' in out
    assert v2.calls == [], "ни одной мутации"
    assert live["t1"]["tags"] == [] and v2.account_tags == []
