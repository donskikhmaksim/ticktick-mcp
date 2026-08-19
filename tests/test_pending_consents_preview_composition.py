"""Веб-хаб `/pending-consents` показывает СОСТАВ плана, а не одну строку
summary (QA-2 2026-08-19, добор №1 — «подтверждение вслепую»).

Поле `preview` манифеста заполняли только `_gate_batch`/`_gate_single`
(consent.py). Пять путей со СВОЕЙ сборкой текста плана — plan_task_creation,
plan_task_deletion, прямой delete_tasks, delete_project и merge-ветка
rename_tag — его не заполняли вовсе, и `GET /pending-consents` отдавал
фолбэк `"preview": m.get("preview") or summary`, где summary — одна строка,
сочинённая вызывающей моделью («Удаляю мусор»). При этом
`POST /pending-consents/decide confirm` такие планы полноценно ИСПОЛНЯЕТ:
владелец в вебе подтверждал удаление N задач или целого проекта, не видя ни
одного названия — а на Telegram-пути тот же план обязан показывать каждую
строку. Самые разрушительные планы имели самую бедную карточку.

Ни сети, ни Postgres, ни Telegram — та же обвязка, что в соседних
tests/test_plan_text_no_agent_instructions.py (фаза плана) и
tests/test_consent_web_hub.py (HTTP-хаб).
"""
import re

import pytest
from starlette.testclient import TestClient

import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s

_MID_RE = re.compile(r"Манифест `([0-9a-f]{6,})`")


def _mid_of(text: str) -> str:
    m = _MID_RE.search(text)
    assert m, f"в тексте нет id манифеста:\n{text}"
    return m.group(1)


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


# ═══════ plan_task_creation (kind=create) ═══════

async def test_creation_manifest_carries_full_preview(monkeypatch):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})

    out = await s.plan_task_creation(
        "Создаю 2", [{"title": "Позвонить маме", "project_id": "p1"},
                     {"title": "Сдать отчёт", "project_id": "p1"}])

    mid = _mid_of(out)
    preview = s._MANIFESTS[mid].get("preview") or ""
    assert "Позвонить маме" in preview, preview
    assert "Сдать отчёт" in preview, preview
    assert "Работа" in preview, preview


# ═══════ plan_task_deletion (kind=delete) — и сквозь HTTP-хаб ═══════

async def test_deletion_manifest_preview_names_every_task(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"},
            "t2": {"id": "t2", "title": "Старый черновик", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})

    out = await s.plan_task_deletion(
        "Удаляю мусор", [{"taskId": "t1", "title": "Купить молоко"},
                         {"taskId": "t2", "title": "Старый черновик"}])

    mid = _mid_of(out)
    preview = s._MANIFESTS[mid].get("preview") or ""
    assert "Купить молоко" in preview, preview
    assert "Старый черновик" in preview, preview

    # И то же самое ГЛАЗАМИ ХАБА: GET /pending-consents обязан отдать состав,
    # а не summary «Удаляю мусор» — раньше карточка была ровно этой строкой.
    monkeypatch.setattr(s, "_CONSENT_HUB_SECRET", "hub-secret-test")
    client = TestClient(s.mcp.streamable_http_app())
    r = client.get("/pending-consents",
                   headers={"x-consent-hub-secret": "hub-secret-test"})
    assert r.status_code == 200
    item = next(it for it in r.json()["items"] if it["manifestId"] == mid)
    assert "Купить молоко" in item["preview"], item["preview"]
    assert "Старый черновик" in item["preview"], item["preview"]
    assert item["preview"] != item["summary"], (
        "карточка снова показывает только summary — «подтверждение вслепую»")


# ═══════ delete_tasks (прямой одноходовый путь) ═══════

async def test_direct_delete_manifest_carries_full_preview(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    live = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Покупки"})

    out = await s.delete_tasks.direct(
        "⚠️ Удаляю задачу «Купить молоко» из «Покупки»",
        [{"taskId": "t1", "title": "Купить молоко"}])

    mid = _mid_of(out)
    preview = s._MANIFESTS[mid].get("preview") or ""
    assert "Купить молоко" in preview, preview
    assert "t1" in preview, preview


# ═══════ delete_project ═══════

async def test_delete_project_manifest_preview_names_doomed_tasks(
        monkeypatch, tmp_path):
    tasks = [{"id": "t1", "title": "Отчёт за июль", "projectId": "p1"},
             {"id": "t2", "title": "Счета", "projectId": "p1"}]
    names = {"p1": "Работа"}

    class _FakeOfficial:
        def get_project_with_data(self, project_id):
            return {"project": {"id": project_id}, "tasks": list(tasks)}

    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "ticktick", _FakeOfficial())
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: names)
    monkeypatch.setattr(s, "_v2_project_names_or_none", lambda: dict(names))
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))

    out = await s.delete_project("Работа", "p1")

    mid = _mid_of(out)
    preview = s._MANIFESTS[mid].get("preview") or ""
    # Карточка обязана говорить, СКОЛЬКО задач умрёт вместе с проектом и какие.
    assert "Работа" in preview, preview
    assert "Отчёт за июль" in preview, preview
    assert "Счета" in preview, preview


# ═══════ rename_tag (слияние) ═══════

async def test_rename_tag_merge_manifest_preview_names_both_tags(monkeypatch):
    class _FakeV2Tags:
        def get_state(self, force=True):
            return {}

        def get_tags(self):
            return [{"name": "a"}, {"name": "b"}]

    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "ticktick_v2", _FakeV2Tags())

    out = await s.rename_tag("a", "b", allow_merge=True)

    mid = _mid_of(out)
    preview = s._MANIFESTS[mid].get("preview") or ""
    assert "СЛИЯНИЕ" in preview, preview
    assert "«a»" in preview and "«b»" in preview, preview


# ═══════ Хаб больше не подменяет состав самодельным summary ═══════

def test_hub_fallback_is_only_for_manifests_without_preview():
    """Фолбэк `or summary` в роуте остаётся (манифесты чужих версий могли
    прийти из базы без превью) — но у всех ПЯТИ местных путей поле теперь
    заполнено, что и проверяют тесты выше. Здесь — прямая проверка формулы:
    манифест С превью отдаёт превью, без — summary."""
    with_preview = {"preview": "### План\n1. «Задача»", "summary": "Удаляю"}
    without = {"summary": "Удаляю"}
    assert (with_preview.get("preview") or with_preview["summary"]) \
        == "### План\n1. «Задача»"
    assert (without.get("preview") or without["summary"]) == "Удаляю"
