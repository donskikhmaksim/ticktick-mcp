"""Служебная инструкция ДЛЯ МОДЕЛИ не должна печататься человеку в карточке
плана (2026-08-06, дефект №2, живой прогон на update_project/create_project).

Что наблюдалось живьём: в Telegram-карточке плана владелец читал дословно
«Покажи это пользователю дословно и ДОЖДИСЬ его отдельного ответа... вызови
update_project СНОВА с теми же аргументами и добавь manifest_id="...",
user_reply="..."». Это инструкция ДЛЯ МОДЕЛИ (следующий вызов инструмента), а
не для человека — и активно вводит в заблуждение: рядом в том же ответе
`_maybe_tg_notify_plan` пишет ПРОТИВОПОЛОЖНОЕ («Повторно вызывать этот
инструмент НЕ нужно»), но в Telegram уехала именно вредная человеку половина.

Корень был в том, что `_maybe_tg_notify_plan(tool, manifest_id, preview_text)`
получала ОДИН текст и использовала его И как ответ модели, И как то, что
уходит в `tg_approval.notify_plan()`. Теперь у функции второй, опциональный
параметр `agent_tail` — инструкция для модели приклеивается ТОЛЬКО к ответу,
`notify_plan()` его никогда не видит.

`tests/test_tg_gate_all_tools.py::test_telegram_card_never_carries_the_agents_own_instructions`
уже покрывает ВСЕ 24 тула общего гейта (`_gate_single`/`_gate_batch`). Этот
файл — про пять БЕСПОЗВОНОЧНЫХ путей со своей, не общей, сборкой текста:
`plan_task_creation`, `plan_task_deletion`, прямой `delete_tasks` (одна
задача, без плана-протокола), `delete_project` и merge-ветку `rename_tag`.

Ни сети, ни Postgres, ни TickTick — та же обвязка, что в соседних
test_plan_id_visible.py/test_tg_create_and_direct.py.
"""
import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ───────────────────────── обвязка (копия соседних файлов) ─────────────────

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


def _tg_on(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


def _capture_notify(monkeypatch):
    seen = []

    def _fake(cfg, manifest_id, preview_body, tool):
        seen.append({"manifest_id": manifest_id, "tool": tool,
                     "preview": preview_body})
        return True, ""

    monkeypatch.setattr(tg, "notify_plan", _fake)
    return seen


# Фразы, адресованные МОДЕЛИ, а не человеку — «по смыслу», не одна точная
# строка (см. постановку задачи): любая из них в тексте карточки плана —
# утечка служебной инструкции.
_AGENT_ONLY_MARKERS = (
    "Покажи это пользователю дословно",
    "Покажи этот план пользователю дословно",
    "СНОВА",
    "manifest_id=",
    "user_reply=",
)


def _assert_no_agent_instructions(text, where):
    for marker in _AGENT_ONLY_MARKERS:
        assert marker not in text, (
            f"{where}: служебная фраза «{marker}» уехала в Telegram:\n{text}")


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


# ═══════ plan_task_creation ═══════

async def test_plan_task_creation_card_has_no_agent_instructions(monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.plan_task_creation(
        "Создаю 1", [{"title": "Позвонить маме", "project_id": "p1"}])

    assert len(seen) == 1
    _assert_no_agent_instructions(seen[0]["preview"], "plan_task_creation")
    # Модель по-прежнему получает полную инструкцию — этот канал цел.
    assert "execute_task_creation(manifest_id=" in out
    assert "user_reply=" in out


# ═══════ plan_task_deletion ═══════

async def test_plan_task_deletion_card_has_no_agent_instructions(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.plan_task_deletion(
        "Удаляю 1", [{"taskId": "t1", "title": "Купить молоко"}])

    assert len(seen) == 1
    _assert_no_agent_instructions(seen[0]["preview"], "plan_task_deletion")
    assert "execute_task_deletion(manifest_id=" in out
    assert "user_reply=" in out


# ═══════ delete_tasks (прямой одноходовый путь, без plan_task_deletion) ════

async def test_delete_tasks_direct_card_has_no_agent_instructions(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.delete_tasks(
        "Удаляю 1", [{"taskId": "t1", "title": "Купить молоко"}])

    assert len(seen) == 1
    _assert_no_agent_instructions(seen[0]["preview"], "delete_tasks (прямой)")
    assert "delete_tasks(summary=" in out
    assert "user_reply=" in out


# ═══════ delete_project ═══════

async def test_delete_project_card_has_no_agent_instructions(monkeypatch, tmp_path):
    _wire_delete_project(monkeypatch, tmp_path)
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.delete_project("Работа", "p1")

    assert len(seen) == 1
    _assert_no_agent_instructions(seen[0]["preview"], "delete_project")
    # Человеку в карточке — предупреждение и id манифеста, это остаётся.
    assert "Манифест `" in seen[0]["preview"]
    # Модель по-прежнему получает инструкцию вызвать тул снова.
    assert 'delete_project(project_name="' in out
    assert "user_reply=" in out


# ═══════ rename_tag (слияние) ═══════

async def test_rename_tag_merge_card_has_no_agent_instructions(monkeypatch):
    _wire_rename_tag(monkeypatch)
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    out = await s.rename_tag("a", "b", allow_merge=True)

    assert len(seen) == 1
    _assert_no_agent_instructions(seen[0]["preview"], "rename_tag (слияние)")
    assert "Манифест `" in seen[0]["preview"]
    # Человеческая часть предупреждения остаётся в карточке.
    assert "СЛИЯНИЕ" in seen[0]["preview"]
    # Модель по-прежнему получает инструкцию повторить вызов.
    assert "allow_merge=true" in out
    assert "user_reply=" in out


# ═══════ Манифест-id по-прежнему печатается правильно (ориентир, не менять) ═

async def test_manifest_id_line_survives_the_split(monkeypatch):
    """Максим прямо попросил оставить как есть: «Манифест `id` · ...» —
    единственная строка с идентификатором — остаётся в человеческой части."""
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    _tg_on(monkeypatch)
    seen = _capture_notify(monkeypatch)

    await s.plan_task_creation(
        "Создаю 1", [{"title": "Позвонить маме", "project_id": "p1"}])

    assert len(seen) == 1
    assert "Манифест `" in seen[0]["preview"]
    assert seen[0]["tool"] == "create_tasks"
