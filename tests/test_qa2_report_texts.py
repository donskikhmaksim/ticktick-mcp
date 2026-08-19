"""QA-2 (2026-08-19), дефекты №3/№5/№6: отчёт не обещает несуществующего.

№3. `operation_report` при «ok, но план и факт разошлись» печатал «см. разделы
ниже» БЕЗУСЛОВНО — а разделы («Пропущено» / «Не вошло в план») печатаются
только при skipped/not_planned. Дрейф же бывает и от пометки в самой строке
вердикта (создана, но уже переименована) — тогда «разделы ниже» вели в пустоту.

№5. Сообщения и докстринги ссылались на СКРЫТЫЕ (недоступные модели)
инструменты: «восстановление: restore_tasks…», «via delete_tasks», «as in
create_tasks/update_tasks». Модель, доверившаяся подсказке, зовёт
несуществующий тул. Все видимые поверхности обязаны называть только живые
маршруты (apply_task_changes(op=…), plan_/execute_ пары) — закреплено
инвариантом по ВСЕМУ листингу, а не точечными правками.

№6. Закрытие (complete/abandon) родителя не каскадится на подзадачи — это
осознанно (план не разрастается сверх названного), но ответ обязан говорить,
что дети остаются ОТКРЫТЫМИ: при выключенном гейте превью никто не видит,
поэтому предупреждение обязано быть в ОТЧЁТЕ ИСПОЛНЕНИЯ.
"""
import asyncio
import re

import pytest

import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s

GATE_ENV = consent._GATE_DISABLED_ENV


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
def _gate_on_by_default(monkeypatch):
    monkeypatch.delenv(GATE_ENV, raising=False)
    yield


# ═══════ №3: «см. разделы ниже» только когда разделы будут ═════════════════

def _report_stand(monkeypatch, tmp_path, live):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})


def test_drift_only_report_does_not_promise_sections_below(
        monkeypatch, tmp_path):
    """Дрейф ТОЛЬКО в строке вердикта (создана, но переименована): разделов
    внизу не будет — и отчёт не имеет права их обещать. Вместо этого он
    указывает на строки проверки выше."""
    live = {"t1": {"id": "t1", "title": "Новое имя", "projectId": "p1"}}
    _report_stand(monkeypatch, tmp_path, live)
    s._journal_write({"ts": "2026-08-19T12:00:00+00:00", "record": "mDrift",
                      "op": "create",
                      "items": [{"taskId": "t1", "title": "Старое имя",
                                 "expect": {"projectId": "p1"}}]})

    out = s._build_operation_report("mDrift")
    assert "Статус операции: ⚠️" in out, out
    assert "разошлись" in out
    assert "см. разделы ниже" not in out, (
        "отчёт обещает «разделы ниже», которых в нём нет")
    assert "в строках проверки выше" in out
    # Контроль честности: разделов действительно нет.
    assert "#### ⏭ Пропущено" not in out and "#### ❌ Не вошло" not in out


def test_report_with_sections_names_them_and_prints_them(
        monkeypatch, tmp_path):
    """Когда непошедшее ЕСТЬ — отчёт называет разделы по именам и реально
    печатает их ниже: обещание и содержимое из одного источника."""
    live = {"t1": {"id": "t1", "title": "Задача", "projectId": "p1"}}
    _report_stand(monkeypatch, tmp_path, live)
    s._journal_write({"ts": "2026-08-19T12:00:00+00:00", "record": "mSect",
                      "op": "create",
                      "items": [{"taskId": "t1", "title": "Задача",
                                 "expect": {"projectId": "p1"}}]})
    s._journal_write({"ts": "2026-08-19T12:00:01+00:00", "manifest": "mSect",
                      "op": s._JOURNAL_NOT_EXECUTED_OP,
                      "skipped": [],
                      "not_planned": [{"task_id": "x1", "op": "delete",
                                       "title": "Потерянная",
                                       "why": "не прошла сверку"}]})

    out = s._build_operation_report("mSect")
    assert "Статус операции: ⚠️" in out
    assert "«Не вошло в план»" in out and "ниже" in out, out
    assert "#### ❌ Не вошло в план" in out, "обещанный раздел не напечатан"
    assert "Потерянная" in out


# ═══════ №5: видимые поверхности не ссылаются на скрытые тулы ══════════════

def test_visible_tool_docstrings_never_name_hidden_tools():
    """ИНВАРИАНТ на весь листинг: докстринг видимого инструмента не имеет
    права упоминать скрытое имя (create_tasks, restore_tasks, delete_tasks…)
    — модель, доверившаяся такой подсказке, вызовет несуществующий для неё
    тул. Живые маршруты — apply_task_changes(op=…) и plan_/execute_ пары."""
    hidden = set(s._DEFAULT_HIDDEN_TOOLS)
    pat = re.compile(r"\b(" + "|".join(sorted(hidden, key=len, reverse=True))
                     + r")\b")
    offenders = []
    for t in asyncio.run(s.mcp.list_tools()):
        hits = sorted(set(pat.findall(t.description or "")))
        if hits:
            offenders.append(f"{t.name}: {hits}")
    assert not offenders, (
        "докстринги видимых тулов ссылаются на скрытые имена:\n"
        + "\n".join(offenders))


def test_trashed_note_points_to_a_live_route():
    """Текст отказа про корзину называет живой маршрут восстановления, а не
    скрытый restore_tasks."""
    assert "restore_tasks" not in s._TRASHED_TASK_NOTE
    assert 'apply_task_changes(op="restore")' in s._TRASHED_TASK_NOTE


def test_deletion_report_recovery_hint_is_a_live_route(monkeypatch, tmp_path):
    """Хвост отчёта удаления («восстановление: …») называет живой маршрут."""
    live = {"t1": {"id": "t1", "title": "Мусор", "projectId": "p1"}}
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))

    deleted = {}

    class _V2:
        def invalidate_cache(self):
            pass

        def batch_delete_tasks(self, rows):
            for r in rows:
                deleted[r["taskId"]] = live.pop(r["taskId"], None)
            return {}

    def _open(fresh=False):
        return dict(live)

    monkeypatch.setattr(s, "_open_by_id", _open)
    monkeypatch.setattr(s, "ticktick_v2", _V2())
    monkeypatch.setattr(s, "ticktick", None)
    m = {"kind": "delete", "consumed": False, "summary": "чистка",
         "created": 0.0, "plan_shown_at": 0.0,
         "items": [{"taskId": "t1", "projectId": "p1", "title": "Мусор",
                    "project": "Проект", "snapshot": {"title": "Мусор"}}]}
    out = asyncio.run(s._execute_task_deletion_impl("mX", m))
    assert "t1" in deleted
    assert "restore_tasks" not in out, out
    assert 'apply_task_changes(op="restore")' in out


# ═══════ №6: отчёт исполнения говорит, что дети остаются открытыми ═════════

async def test_killswitch_complete_report_warns_about_open_children(
        monkeypatch, tmp_path):
    """Гейт выключен → превью никто не видел → отчёт исполнения обязан сам
    сказать, что подзадачи остаются ОТКРЫТЫМИ под закрытым родителем."""
    monkeypatch.setenv(GATE_ENV, "1")
    live = {
        "par": {"id": "par", "title": "Родитель", "projectId": "p_in"},
        "kid": {"id": "kid", "title": "Ребёнок", "projectId": "p_in",
                "parentId": "par"},
        "grand": {"id": "grand", "title": "Внук", "projectId": "p_in",
                  "parentId": "kid"},
    }
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_in": "Входящие"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "ticktick", None)

    async def _cmp(summary, items):
        for i in items:
            live.pop(i.get("taskId"), None)
        return "### заглушка закрытия"

    monkeypatch.setattr(s, "_complete_tasks_impl", _cmp)

    out = await s.apply_task_changes("Закрываю", [
        {"op": "complete", "task_id": "par", "title": "Родитель",
         "said": "готово"}])
    assert "par" not in live
    assert "останется ОТКРЫТОЙ под закрытым родителем" in out, out
    assert 'op="complete"' in out, "не сказано, как закрыть детей"


async def test_complete_of_a_childless_task_has_no_warning(
        monkeypatch, tmp_path):
    """Контроль: у задачи без детей предупреждения нет."""
    monkeypatch.setenv(GATE_ENV, "1")
    live = {"solo": {"id": "solo", "title": "Одиночка", "projectId": "p_in"}}
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p_in": "Входящие"})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "ticktick", None)

    async def _cmp(summary, items):
        for i in items:
            live.pop(i.get("taskId"), None)
        return "### заглушка закрытия"

    monkeypatch.setattr(s, "_complete_tasks_impl", _cmp)

    out = await s.apply_task_changes("Закрываю", [
        {"op": "complete", "task_id": "solo", "title": "Одиночка",
         "said": "готово"}])
    assert "останется ОТКРЫТОЙ" not in out and "остаются ОТКРЫТЫМИ" not in out
