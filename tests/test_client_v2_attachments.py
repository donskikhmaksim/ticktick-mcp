"""Attachment download (and its supporting metadata reads). No real network:
`_request`/`session.get` are stubbed so we can assert the endpoint shape,
content-parsing fallback, and size/auth error handling."""
import time

import pytest

from ticktick_mcp.src.ticktick_v2_client import (
    ATTACHMENT_MAX_BYTES,
    TickTickAuthError,
    TickTickV2Client,
)

TASK_ID = "task1"
PROJECT_ID = "proj1"


@pytest.fixture
def client():
    c = TickTickV2Client(token="tok")
    return c


def _seed_open_task(client, task):
    """Prime the cached sync state with a single open task, bypassing the
    real /batch/check/0 network call."""
    client._state_cache = {"syncTaskBean": {"update": [task]}}
    client._state_ts = time.monotonic()


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "ignore") if content else ""
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code not in (401, 403, 404):
            raise RuntimeError(f"HTTP {self.status_code}")


# ---- get_task_attachments / get_content_attachment_refs -------------------

def test_get_task_attachments_returns_raw_dicts(client):
    _seed_open_task(client, {
        "id": TASK_ID, "projectId": PROJECT_ID,
        "attachments": [{"fileName": "invoice.pdf"}],
    })
    atts = client.get_task_attachments(TASK_ID)
    assert atts == [{"fileName": "invoice.pdf"}]


def test_get_task_attachments_missing_everywhere_raises_honest_error(client, monkeypatch):
    """Раньше этот тест назывался «missing task», а проверял «задача не среди
    ОТКРЫТЫХ» — то есть закреплял дефект: у завершённой задачи вложения
    объявлялись отсутствующими. Теперь «не найдена» означает «нет ни среди
    открытых, ни среди завершённых, ни в корзине», и сообщение обязано это
    честно называть."""
    _seed_open_task(client, {"id": "other", "projectId": PROJECT_ID})
    # сигнатуры явные, как у настоящего клиента: заглушка не должна
    # проглатывать произвольные аргументы (tests/test_doubles_do_not_cheat.py)
    monkeypatch.setattr(client, "get_completed_tasks",
                        lambda limit=50, from_str="", to_str=None: [])
    monkeypatch.setattr(client, "get_trash", lambda limit=50: [])

    with pytest.raises(ValueError, match=TASK_ID) as e:
        client.get_task_attachments(TASK_ID)

    msg = str(e.value).lower()
    assert "completed" in msg and "trash" in msg
    assert f"open task {TASK_ID} not found" not in msg  # старая враньё-формулировка


def test_lookup_failure_is_not_reported_as_no_such_task(client, monkeypatch):
    """Протухший токен/сеть — это «не смог проверить», а не «задачи нет»:
    ошибку надо поднять, иначе человек прочитает «файла нет»."""
    _seed_open_task(client, {"id": "other", "projectId": PROJECT_ID})

    def boom(limit=50, from_str="", to_str=None):
        raise TickTickAuthError("token expired")

    monkeypatch.setattr(client, "get_completed_tasks", boom)
    monkeypatch.setattr(client, "get_trash", boom)

    with pytest.raises(TickTickAuthError):
        client.get_task_attachments(TASK_ID)


def test_content_attachment_refs_parsed_from_markdown(client):
    content = (
        "Some notes.\n"
        "![file](0ae67755c0e5411ab297476a/Lulu%20Donskikh%20%2321.pdf)\n"
        "![file](7d4d280fc84845bcb9b5e254/plain.pdf)\n"
    )
    _seed_open_task(client, {"id": TASK_ID, "projectId": PROJECT_ID, "content": content})
    refs = client.get_content_attachment_refs(TASK_ID)
    assert refs == [
        {"id": "0ae67755c0e5411ab297476a", "fileName": "Lulu Donskikh #21.pdf"},
        {"id": "7d4d280fc84845bcb9b5e254", "fileName": "plain.pdf"},
    ]


def test_content_attachment_refs_ignores_non_matching_text(client):
    _seed_open_task(client, {"id": TASK_ID, "projectId": PROJECT_ID,
                             "content": "no attachments here, just ![alt](short)"})
    assert client.get_content_attachment_refs(TASK_ID) == []


# ---- download_attachment ---------------------------------------------------

def test_download_attachment_hits_v1_path_without_upload_segment(client, monkeypatch):
    calls = {}

    def fake_get(url, timeout=None):
        calls["url"] = url
        return FakeResponse(200, content=b"%PDF-1.4 fake pdf bytes",
                            headers={"Content-Type": "application/pdf"})

    monkeypatch.setattr(client.session, "get", fake_get)
    name, data, mime = client.download_attachment(PROJECT_ID, TASK_ID, "att123")

    assert calls["url"] == (
        "https://api.ticktick.com/api/v1/attachment/proj1/task1/att123")
    assert "upload" not in calls["url"]
    assert data == b"%PDF-1.4 fake pdf bytes"
    assert mime == "application/pdf"
    assert name == "attachment_att123"  # no filename given, none in headers


def test_download_attachment_uses_given_filename(client, monkeypatch):
    monkeypatch.setattr(client.session, "get",
                        lambda url, timeout=None: FakeResponse(200, content=b"data"))
    name, _, mime = client.download_attachment(
        PROJECT_ID, TASK_ID, "att123", filename="receipt.pdf")
    assert name == "receipt.pdf"
    assert mime == "application/pdf"  # guessed from the .pdf extension


def test_download_attachment_parses_content_disposition_filename(client, monkeypatch):
    monkeypatch.setattr(client.session, "get", lambda url, timeout=None: FakeResponse(
        200, content=b"data",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''report%20final.pdf"}))
    name, _, _ = client.download_attachment(PROJECT_ID, TASK_ID, "att123")
    assert name == "report final.pdf"


def test_download_attachment_401_raises_auth_error(client, monkeypatch):
    monkeypatch.setattr(client.session, "get",
                        lambda url, timeout=None: FakeResponse(401))
    with pytest.raises(TickTickAuthError):
        client.download_attachment(PROJECT_ID, TASK_ID, "att123")


def test_download_attachment_404_raises_value_error(client, monkeypatch):
    monkeypatch.setattr(client.session, "get",
                        lambda url, timeout=None: FakeResponse(404))
    with pytest.raises(ValueError, match="not found"):
        client.download_attachment(PROJECT_ID, TASK_ID, "att123")


def test_download_attachment_over_size_cap_raises(client, monkeypatch):
    big = b"x" * (ATTACHMENT_MAX_BYTES + 1)
    monkeypatch.setattr(client.session, "get",
                        lambda url, timeout=None: FakeResponse(200, content=big))
    with pytest.raises(ValueError, match="MB"):
        client.download_attachment(PROJECT_ID, TASK_ID, "att123")
