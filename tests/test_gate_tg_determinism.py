"""Детерминизм гейта согласия при включённом Telegram-факторе (2026-08-06).

Баг, который эти тесты фиксируют навсегда (найден ночной QA-кампанией): на
ОДНОМ И ТОМ ЖЕ сервере, в ту же ночь, одно и то же удаление вело себя
по-разному — через `delete_tasks` требовало нажатия кнопки в Telegram, а
через `execute_task_deletion` исполнялось РЕАЛЬНО по одному лишь chat-«да».
Причина — не гонка и не среда: `execute_task_deletion` звал
`_require_consent(...)` без `tool=`/`manifest_id=`, а весь ТГ-блок внутри
стоял под `if tool and enabled_for(tool)` и при пустом `tool` молча
пропускался целиком.

Инвариант, который теперь проверяется: если план ЭТОЙ операции реально ушёл
в Telegram (манифест помечен `tg_notified` в `_maybe_tg_notify_plan`), то
исполнение требует approved-строку — независимо от того, каким путём и с
какими аргументами позвали execute. И зеркальный инвариант: при выключенном
слое (`TG_APPROVAL_ENABLED` не задан — дефолт) поведение прежнее, chat-«да»
достаточно, `tg_approval` не трогается вовсе.
"""
import time

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ───────────────────────── обвязка ─────────────────────────

class _FakeV2Delete:
    def __init__(self, live):
        self._live = live
        self.deleted_ids = []

    def batch_delete_tasks(self, items):
        for it in items:
            self._live.pop(it["taskId"], None)
            self.deleted_ids.append(it["taskId"])
        return {}


def _wire(monkeypatch, live, tmp_path):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    fake = _FakeV2Delete(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


def _tg_on(monkeypatch, allowlist=None):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=allowlist, ttl_s=3600))


def _tg_off(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


def _plan_manifest(mid, notified=False, tool="delete_tasks"):
    """Манифест удаления одной задачи t1, как его строит plan_task_deletion."""
    now = time.monotonic()
    items = [{"taskId": "t1", "projectId": "p1", "title": "Купить молоко",
              "project": "Покупки", "snapshot": {"title": "Купить молоко"}}]
    s._MANIFESTS[mid] = {
        "kind": "delete", "items": items, "created": now,
        "plan_shown_at": now, "summary": "⚠️ Удаляю 1", "consumed": False,
        "object_hash": s._manifest_object_hash("delete", ["t1"]),
    }
    if notified:
        s._MANIFESTS[mid].update({"tg_notified": True, "_tg_tool": tool,
                                  "_tg_manifest_id": mid})
    return s._MANIFESTS[mid]


@pytest.fixture(autouse=True)
def _clean_manifests():
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()


# ═══════════════════ 1. Сам баг: execute_task_deletion ═══════════════════

async def test_execute_task_deletion_refuses_chat_yes_when_plan_went_to_telegram(
        monkeypatch, tmp_path):
    """ГЛАВНЫЙ регрессионный тест: план ушёл в Telegram, кнопка НЕ нажата —
    голое «да» в чате НЕ должно удалять ничего (раньше удаляло)."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "pending")
    _plan_manifest("mid-tg")

    out = await s.execute_task_deletion("mid-tg", user_reply="да")

    assert fake.deleted_ids == []          # НИЧЕГО не удалено
    assert "t1" in live
    assert "Telegram" in out
    assert s._MANIFESTS["mid-tg"]["consumed"] is False  # план ещё жив


async def test_execute_task_deletion_does_not_run_on_text_after_button_approved(
        monkeypatch, tmp_path):
    """ПЕРЕПИСАН 2026-08-06 (button-only). Раньше здесь утверждалось обратное:
    «кнопка нажата → текстовое «да» исполняет». Теперь план, реально ушедший в
    Telegram (`tg_notified`), исполняет ТОЛЬКО фоновый поллер — текстовый путь
    закрыт даже при уже нажатой кнопке, а манифест обязан остаться живым,
    иначе поллеру нечего будет исполнять."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "approved")
    m = _plan_manifest("mid-tg-ok", notified=True)

    out = await s.execute_task_deletion("mid-tg-ok", user_reply="да")

    assert fake.deleted_ids == []          # текстом НЕ исполнено
    assert "t1" in live
    assert "исполняет" in out and "САМ" in out
    assert m["consumed"] is False          # поллер ещё должен его забрать


async def test_unnotified_manifest_with_tool_still_proceeds_when_approved(
        monkeypatch, tmp_path):
    """Остаточная ветка (б) в `_require_consent`: манифест НЕ помечен (плана в
    Telegram не было), но вызывающий передал `tool=`. Такой план button-only
    не касается — он исполняется по chat-«да», как и раньше. Тест держит
    границу изменения: закрыт ровно помеченный путь, не все подряд."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "approved")
    _plan_manifest("mid-tg-ok2")           # notified=False

    out = await s.execute_task_deletion("mid-tg-ok2", user_reply="да")

    assert fake.deleted_ids == ["t1"]
    assert "Купить молоко" in out


async def test_execute_task_deletion_rejected_button_kills_the_plan(
        monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "rejected")
    m = _plan_manifest("mid-tg-no")

    out = await s.execute_task_deletion("mid-tg-no", user_reply="да")

    assert fake.deleted_ids == []
    assert "Отклонено" in out
    assert m["consumed"] is True  # аннулирован, нужен новый план


async def test_execute_task_deletion_unchanged_when_tg_layer_is_off(
        monkeypatch, tmp_path):
    """Совместимость: слой выключен (дефолт) → chat-«да» достаточно, и
    tg_approval.check_approval не зовётся ни разу."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _tg_off(monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(tg, "check_approval",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "none")
    _plan_manifest("mid-off")

    out = await s.execute_task_deletion("mid-off", user_reply="да")

    assert fake.deleted_ids == ["t1"]
    assert calls["n"] == 0
    assert "Купить молоко" in out


async def test_execute_task_deletion_still_refuses_a_no(monkeypatch, tmp_path):
    """Отказ человека по-прежнему сильнее всего (и при включённом TG)."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "approved")
    m = _plan_manifest("mid-no")

    out = await s.execute_task_deletion("mid-no", user_reply="нет, отмена")

    assert fake.deleted_ids == []
    assert "НЕ подтвердил" in out
    assert m["consumed"] is True


# ═════════ 2. Два пути одной операции ведут себя ОДИНАКОВО ═════════

async def test_delete_tasks_and_execute_task_deletion_agree_under_tg(
        monkeypatch, tmp_path):
    """Суть найденного дефекта — расхождение веток. Оба пути на одинаковом
    состоянии (план в TG, кнопка не нажата) обязаны ОТКАЗАТЬ."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "pending")
    _plan_manifest("mid-a")
    _plan_manifest("mid-b")

    out_direct = await s.delete_tasks("⚠️ Удаляю 1", manifest_id="mid-a",
                                      user_reply="да")
    out_two_phase = await s.execute_task_deletion("mid-b", user_reply="да")

    assert fake.deleted_ids == []
    assert "Telegram" in out_direct
    assert "Telegram" in out_two_phase


# ═══════ 3. Юнит-уровень: пометка манифеста, а не аргумент вызова ═══════

def test_notify_plan_marks_the_manifest(monkeypatch):
    _tg_on(monkeypatch)
    monkeypatch.setattr(tg, "notify_plan", lambda *a, **k: (True, ""))
    m = _plan_manifest("mid-mark")

    s._maybe_tg_notify_plan("delete_tasks", "mid-mark", "### план")

    assert m["tg_notified"] is True
    assert m["_tg_tool"] == "delete_tasks"
    assert m["_tg_manifest_id"] == "mid-mark"


def test_notify_plan_does_not_mark_when_layer_off(monkeypatch):
    _tg_off(monkeypatch)
    m = _plan_manifest("mid-nomark")
    s._maybe_tg_notify_plan("delete_tasks", "mid-nomark", "### план")
    assert "tg_notified" not in m


def test_require_consent_honours_the_flag_even_without_tool_arg(monkeypatch):
    """Ровно форма бага: вызывающий забыл `tool=`/`manifest_id=`. Раньше это
    молча отключало ТГ-проверку; теперь решает пометка манифеста."""
    _tg_on(monkeypatch)
    monkeypatch.setattr(tg, "check_approval", lambda manifest_id: "pending")
    m = _plan_manifest("mid-flag", notified=True)

    cr = s._require_consent(action="delete", tier=2, manifest=m,
                            user_reply="да", object_ids=["t1"])

    assert cr.ok is False
    assert "Telegram" in cr.reason


def test_require_consent_flag_uses_manifest_own_id_for_lookup(monkeypatch):
    """`manifest_id=` не передали — id для check_approval берётся из самой
    пометки (`_tg_manifest_id`), а не теряется в пустую строку.

    ПЕРЕПИСАН 2026-08-06: проверяемый факт (id для поиска строки берётся из
    манифеста) прежний, изменился только исход — при button-only даже
    approved не открывает текстовый путь."""
    _tg_on(monkeypatch)
    seen = []
    monkeypatch.setattr(tg, "check_approval",
                        lambda manifest_id: seen.append(manifest_id) or "approved")
    m = _plan_manifest("mid-lookup", notified=True)

    cr = s._require_consent(action="delete", tier=2, manifest=m,
                            user_reply="да", object_ids=["t1"])

    assert cr.ok is False
    assert seen == ["mid-lookup"]


# ═══════ 4. Ловушка: пути БЕЗ TG-уведомления не должны ломаться ═══════

async def test_tool_outside_the_allowlist_does_not_break_execute(
        monkeypatch, tmp_path):
    """Вторая половина той же ловушки: слой включён, но `delete_tasks` НЕ в
    TG_APPROVAL_TOOLS → план в Telegram не уходил, строки подтверждения не
    существует. Передача `tool=` в execute не должна превратить это в вечный
    отказ — chat-«да» обязан работать, как и раньше."""
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    fake = _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch, allowlist={"execute_declutter"})
    calls = {"n": 0}
    monkeypatch.setattr(tg, "check_approval",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "none")
    _plan_manifest("mid-notlisted")

    out = await s.execute_task_deletion("mid-notlisted", user_reply="да")

    assert fake.deleted_ids == ["t1"]
    assert calls["n"] == 0
    assert "Купить молоко" in out


def test_unnotified_manifest_still_passes_on_plain_yes(monkeypatch):
    """Манифесты, чей план в Telegram НЕ уходил (_gate_batch/_gate_single/
    create), обязаны продолжать работать по chat-«да» даже когда слой
    глобально включён — иначе фикс превратил бы batch-тулы в вечный отказ
    («none» = строки подтверждения не существует)."""
    _tg_on(monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(tg, "check_approval",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or "none")
    now = time.monotonic()
    m = {"kind": "complete", "tasks": [{"taskId": "t1"}], "summary": "s",
         "created": now, "plan_shown_at": now, "consumed": False,
         "object_hash": s._manifest_object_hash("complete", ["t1"])}
    s._MANIFESTS["mid-batch"] = m

    out = s._gate_batch("complete", "complete_tasks", None, "s", "mid-batch",
                        "да", lambda t: "x")

    assert out.proceed is True
    assert calls["n"] == 0


# ═══════ 5. «Уже исполнено по кнопке» ≠ «истёк/не существовал» ═══════

async def test_message_after_button_auto_execution_is_explicit(monkeypatch, tmp_path):
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    _wire(monkeypatch, live, tmp_path)
    _tg_on(monkeypatch)
    _plan_manifest("mid-auto", notified=True)

    # то, что делает фоновый поллер по нажатию кнопки
    assert s._consume_manifest_for_auto_execute("mid-auto") is not None
    s._prune_manifests()

    out = await s.execute_task_deletion("mid-auto", user_reply="да")
    assert "кнопкой в Telegram" in out
    assert "УЖЕ исполнен" in out


async def test_message_for_a_truly_unknown_manifest_is_the_generic_one(
        monkeypatch, tmp_path):
    _wire(monkeypatch, {}, tmp_path)
    out = await s.execute_task_deletion("mid-never-existed", user_reply="да")
    assert "не найден/истёк" in out
