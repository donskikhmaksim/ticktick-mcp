"""Вложения задачи, которой уже нет среди ОТКРЫТЫХ (завершена или в корзине).

Живой дефект (2026-08-07): к только что выполненной задаче
`list_task_attachments` и `get_attachment_download_url` отвечали
«Open task <id> not found» — файл (квитанция, счёт, договор) выглядел
безвозвратно потерянным, хотя дело было лишь в пути поиска: обе функции
клиента искали задачу ТОЛЬКО в `get_open_tasks()`.

Тесты идут через РЕАЛЬНЫЙ путь резолвинга (настоящий TickTickV2Client с
застабленным `_request`, никакой сети), а НЕ через подмену
`_merged_task_attachments` — именно подмена в tests/test_attachment_links.py
и позволила дефекту дожить до прода.
"""
import time

import pytest

import ticktick_mcp.src.server as s
from ticktick_mcp.src.ticktick_v2_client import TickTickV2Client

TASK_ID = "6a74a971bf08801000f175cb"          # id из живой приёмки
PROJECT_ID = "proj-finance"
ATT_ID = "0ae67755c0e5411ab297476a"
CONTENT_WITH_FILE = f"Квитанция во вложении\n![file]({ATT_ID}/receipt.pdf)\n"


def _client(*, open_tasks=(), completed=(), trash=()):
    """Настоящий клиент, у которого подменён ТОЛЬКО транспорт `_request`:
    вся логика резолвинга задачи — боевая."""
    c = TickTickV2Client(token="tok")
    c._state_cache = {"syncTaskBean": {"update": list(open_tasks)},
                      "projectProfiles": [{"id": PROJECT_ID, "name": "Финансы"}],
                      "inboxId": "inbox1"}
    c._state_ts = time.monotonic()
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/project/all/completed":
            return list(completed)
        if path == "/project/all/trash/pagination":
            return {"tasks": list(trash)}
        if path == "/batch/check/0":
            return c._state_cache
        raise AssertionError(f"unexpected request {method} {path}")

    c._request = fake_request
    c.requests_made = calls
    return c


COMPLETED_TASK = {
    "id": TASK_ID, "projectId": PROJECT_ID, "title": "Оплатить счёт",
    "status": 2,
    "attachments": [{"fileName": "receipt.pdf", "id": ATT_ID, "size": 2048}],
    "content": CONTENT_WITH_FILE,
}
TRASHED_TASK = {
    "id": TASK_ID, "projectId": PROJECT_ID, "title": "Оплатить счёт",
    "content": CONTENT_WITH_FILE,
}


# ---- уровень клиента -------------------------------------------------------

def test_client_finds_attachments_of_a_completed_task():
    c = _client(open_tasks=[{"id": "other", "projectId": PROJECT_ID}],
                completed=[COMPLETED_TASK])
    assert c.get_task_attachments(TASK_ID) == COMPLETED_TASK["attachments"]


def test_client_finds_content_refs_of_a_trashed_task():
    c = _client(open_tasks=[], completed=[], trash=[TRASHED_TASK])
    assert c.get_content_attachment_refs(TASK_ID) == [
        {"id": ATT_ID, "fileName": "receipt.pdf"}]


def test_client_error_names_completed_and_trash_when_task_is_truly_gone():
    """Задачи нет НИГДЕ — ошибка обязана честно назвать, где искали, а не
    врать «Open task not found» (человек прочитает это как «файла нет»)."""
    c = _client(open_tasks=[{"id": "other"}], completed=[], trash=[])
    with pytest.raises(ValueError) as e:
        c.get_task_attachments(TASK_ID)
    msg = str(e.value).lower()
    assert TASK_ID in str(e.value)
    assert "completed" in msg and "trash" in msg


def test_client_does_not_pay_for_fallback_when_the_task_is_open():
    """Цена почина = 0 запросов на happy path: открытая задача по-прежнему
    резолвится из уже закэшированного sync-состояния."""
    open_task = dict(COMPLETED_TASK)
    c = _client(open_tasks=[open_task])
    c.get_task_attachments(TASK_ID)
    c.get_content_attachment_refs(TASK_ID)
    assert c.requests_made == []


def test_client_resolves_completed_task_once_for_both_sources():
    """Оба источника вложений (структурный массив + разбор content) не должны
    стоить по отдельному сетевому запросу за один и тот же промах."""
    c = _client(open_tasks=[], completed=[COMPLETED_TASK])
    c.get_task_attachments(TASK_ID)
    c.get_content_attachment_refs(TASK_ID)
    completed_calls = [x for x in c.requests_made if x[1] == "/project/all/completed"]
    assert len(completed_calls) == 1, c.requests_made


# ---- уровень MCP-инструментов (полный боевой путь) -------------------------

@pytest.fixture
def completed_task_server(monkeypatch):
    c = _client(open_tasks=[], completed=[COMPLETED_TASK])
    monkeypatch.setattr(s, "ticktick_v2", c)
    monkeypatch.setattr(s, "ticktick", object())
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    return c


async def test_list_task_attachments_works_for_a_completed_task(completed_task_server):
    out = await s.list_task_attachments(task_id=TASK_ID)
    assert "receipt.pdf" in out
    assert ATT_ID in out
    assert "not found" not in out.lower()


async def test_download_url_works_for_a_completed_task(completed_task_server):
    """project_id не передан намеренно: для завершённой задачи его тоже надо
    уметь достать, иначе ссылка не строится и файл всё равно недостижим."""
    out = await s.get_attachment_download_url(task_id=TASK_ID, index=1)
    assert "https://tt.example.com/dl/" in out
    token = out.split("/dl/")[1].split()[0]
    payload = s._verify_attachment_token(token, "dl")
    assert payload["t"] == TASK_ID
    assert payload["p"] == PROJECT_ID
    assert payload["a"] == ATT_ID


async def test_list_attachments_message_is_honest_when_task_is_gone(monkeypatch):
    c = _client(open_tasks=[], completed=[], trash=[])
    monkeypatch.setattr(s, "ticktick_v2", c)
    monkeypatch.setattr(s, "ticktick", object())
    out = await s.list_task_attachments(task_id=TASK_ID)
    low = out.lower()
    # старая формулировка врала про причину: «Open task <id> not found»
    assert f"open task {TASK_ID.lower()} not found" not in low, out
    assert "completed" in low and "trash" in low, out
