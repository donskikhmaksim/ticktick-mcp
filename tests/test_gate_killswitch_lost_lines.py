"""Kill-switch-ветки `plan_task_creation` / `plan_task_deletion` НЕ теряют
строки «исключено / не совпало / пропущено / ❓ НЕ уверен» — приёмка разбора
QA-2 (2026-08-19, дефект №4).

ЧТО БЫЛО СЛОМАНО. При `TICKTICK_MCP_GATE_DISABLED` обе ветки делали голый
`return await execute_task_*(mid, user_reply="")`, выбрасывая уже собранный
контекст плана: в создании — список `refused` («project_id не найден», «id
указывает не туда», отвалившиеся родители) и пометки «❓ НЕ уверен»
подсказчика проектов (задача создавалась в проекте, в котором сервер САМ не
уверен, и человек об этом не узнавал); в удалении — `_mismatch_report` и
`missing` (часть списка молча не тронута). Образец правильного поведения был
рядом всё это время — kill-switch-ветка `delete_tasks` приклеивает свои
предупреждения прямо к ответу.

Ни сети, ни Telegram: исполнители и подсказчик — двойники, как в
tests/test_gate_killswitch.py.
"""
import asyncio

import pytest

import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s


ENV = consent._GATE_DISABLED_ENV


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(consent._MANIFESTS)
    yield
    consent._MANIFESTS.clear()
    consent._MANIFESTS.update(before)


@pytest.fixture(autouse=True)
def _gate_on_by_default(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    yield


@pytest.fixture
def _creation_stand(monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    calls = []

    async def _fake_create(summary, raw):
        calls.append((summary, list(raw)))
        return "✅ Создано (двойник исполнителя)"

    monkeypatch.setattr(s, "_create_tasks_impl", _fake_create)
    return calls


# ===========================================================================
# 1. plan_task_creation: строка «Исключены …» доезжает до ответа
# ===========================================================================

def test_killswitch_creation_reports_the_refused_rows(
        monkeypatch, _creation_stand):
    monkeypatch.setenv(ENV, "1")
    out = asyncio.run(s.plan_task_creation("Создаю задачи", [
        {"title": "Годная", "project_id": "p1"},
        {"title": "Битая", "project_id": "px-не-существует"},
    ]))

    assert len(_creation_stand) == 1, "годная строка обязана исполниться"
    created_titles = [t.get("title") for t in _creation_stand[0][1]]
    assert created_titles == ["Годная"], "битая строка не создаётся"
    assert "двойник исполнителя" in out
    assert "Исключены 1" in out, \
        "исключённая строка обязана быть названа в ответе, а не потеряна"
    assert "Битая" in out and "не найден" in out


def test_killswitch_creation_without_refusals_has_no_excluded_line(
        monkeypatch, _creation_stand):
    """Контроль: когда исключать нечего, ответ — чистый отчёт исполнителя,
    без пустых рубрик."""
    monkeypatch.setenv(ENV, "1")
    out = asyncio.run(s.plan_task_creation(
        "Создаю задачу", [{"title": "Годная", "project_id": "p1"}]))
    assert len(_creation_stand) == 1
    assert "Исключены" not in out
    assert "❓" not in out


def test_default_mode_still_reports_refused_rows_in_the_plan(
        monkeypatch, _creation_stand):
    """Контроль: обычный режим (выключатель не задан) — строки «Исключены»
    по-прежнему в превью плана, мутации нет."""
    out = asyncio.run(s.plan_task_creation("Создаю задачи", [
        {"title": "Годная", "project_id": "p1"},
        {"title": "Битая", "project_id": "px-не-существует"},
    ]))
    assert _creation_stand == []
    assert "### 📋 План создания" in out
    assert "Исключены 1" in out


# ===========================================================================
# 2. plan_task_creation: пометка «❓ НЕ уверен» подсказчика проектов
# ===========================================================================

def test_killswitch_creation_reports_the_unsure_project_suggestion(
        monkeypatch, _creation_stand):
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(
        s, "_suggest_destinations",
        lambda titles, names: [{"project_id": "p1", "project": "Проект",
                                "confidence": "unsure",
                                "reason": "похоже на быт, но не уверен"}])
    out = asyncio.run(s.plan_task_creation(
        "Создаю задачу", [{"title": "Без адреса"}]))

    assert len(_creation_stand) == 1, \
        "задача создаётся (в предложенном проекте) — так было и до правки"
    assert "❓" in out and "НЕ уверен" in out, (
        "неуверенность сервера в выборе проекта обязана доехать до ответа — "
        "иначе человек не узнает, что адрес выбран наугад")
    assert "Без адреса" in out and "Проект" in out


def test_killswitch_creation_sure_suggestion_is_not_flagged(
        monkeypatch, _creation_stand):
    """Контроль: уверенное предложение подсказчика не рисует ❓."""
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(
        s, "_suggest_destinations",
        lambda titles, names: [{"project_id": "p1", "project": "Проект",
                                "confidence": "sure",
                                "reason": "подходит по смыслу"}])
    out = asyncio.run(s.plan_task_creation(
        "Создаю задачу", [{"title": "Без адреса"}]))
    assert len(_creation_stand) == 1
    assert "❓" not in out


# ===========================================================================
# 3. plan_task_deletion: «не совпало» и «не среди открытых» доезжают
# ===========================================================================

@pytest.fixture
def _deletion_stand(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})
    monkeypatch.setattr(
        s, "_open_by_id",
        lambda fresh=False: {"t1": {"id": "t1", "title": "Мусор",
                                    "projectId": "p1"}})
    calls = []

    async def _fake_delete(manifest_id, m=None):
        calls.append(manifest_id)
        return "✅ Удалено (двойник исполнителя)"

    monkeypatch.setattr(s, "_execute_task_deletion_impl", _fake_delete)
    return calls


def test_killswitch_deletion_reports_the_missing_rows(
        monkeypatch, _deletion_stand):
    monkeypatch.setenv(ENV, "1")
    out = asyncio.run(s.plan_task_deletion("Удаляю мусор", [
        {"taskId": "t1", "title": "Мусор", "projectId": "p1"},
        {"taskId": "t-призрак", "title": "Призрак", "projectId": "p1"},
    ]))

    assert len(_deletion_stand) == 1, "живая строка обязана исполниться"
    assert "двойник исполнителя" in out
    assert "не среди открытых" in out and "Призрак" in out, \
        "пропущенная строка обязана быть названа, а не потеряна молча"


def test_killswitch_deletion_reports_the_identity_mismatch(
        monkeypatch, _deletion_stand):
    monkeypatch.setenv(ENV, "1")
    out = asyncio.run(s.plan_task_deletion("Удаляю мусор", [
        {"taskId": "t1", "title": "Совсем другое название",
         "projectId": "p1"},
    ]))

    assert "id НЕ совпал с названием" in out, \
        "сработавший identity guard обязан быть виден в ответе"
    assert "Совсем другое название" in out


def test_killswitch_deletion_clean_plan_is_just_the_report(
        monkeypatch, _deletion_stand):
    """Контроль: без исключённых строк ответ — чистый отчёт исполнителя."""
    monkeypatch.setenv(ENV, "1")
    out = asyncio.run(s.plan_task_deletion("Удаляю мусор", [
        {"taskId": "t1", "title": "Мусор", "projectId": "p1"},
    ]))
    assert len(_deletion_stand) == 1
    assert "не среди открытых" not in out
    assert "id НЕ совпал" not in out


def test_default_mode_deletion_still_reports_in_the_plan(
        monkeypatch, _deletion_stand):
    """Контроль: обычный режим — те же строки в превью плана, мутации нет."""
    out = asyncio.run(s.plan_task_deletion("Удаляю мусор", [
        {"taskId": "t1", "title": "Мусор", "projectId": "p1"},
        {"taskId": "t-призрак", "title": "Призрак", "projectId": "p1"},
    ]))
    assert _deletion_stand == []
    assert "### 📋 План удаления" in out
    assert "Призрак" in out
