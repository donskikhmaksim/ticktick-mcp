"""Temporary attachment transfer links: the signed stateless token, the /dl
streaming relay, the /ul upload relay, and the two tools that hand the links
out. No real network anywhere — the v2 client is faked, so what is asserted is
the token contract, the HTTP behaviour, and the tools' own guard rails."""
import json
import re
import time

import pytest
from starlette.testclient import TestClient

import ticktick_mcp.src.server as s

TASK_ID = "task1"
PROJECT_ID = "proj1"
ATT_ID = "0ae67755c0e5411ab297476a"


@pytest.fixture
def client():
    return TestClient(s.mcp.streamable_http_app())


@pytest.fixture(autouse=True)
def public_base(monkeypatch):
    """Every test runs as if the server knows its public address."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tt.example.com")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)


class FakeV2:
    """Stand-in for the v2 client: records calls, returns canned bytes."""

    def __init__(self, chunks=(b"hello ", b"world"), headers=None, raises=None,
                 attachments=None, content_refs=None):
        self.chunks = list(chunks)
        self.headers = headers or {}
        self.raises = raises
        self.uploaded = None
        self.stream_args = None
        self.closed = False
        # Метаданные вложений отдаёт ДВОЙНИК КЛИЕНТА — это настоящие методы
        # TickTickV2Client. Раньше здесь подменялась внутренняя функция
        # сервера `_merged_task_attachments`, то есть ровно тот путь, где жил
        # дефект: слияние двух источников и выбор по id/имени/индексу не
        # исполнялись никогда, и семь «покрывающих» тестов оставались
        # зелёными при любой его поломке.
        self.attachments = list(attachments if attachments is not None
                                else [{"id": ATT_ID, "fileName": "Отчёт.pdf"}])
        self.content_refs = list(content_refs or [])

    # ---- attachment metadata
    def get_task_attachments(self, task_id):
        return [dict(a) for a in self.attachments]

    def get_content_attachment_refs(self, task_id):
        return [dict(r) for r in self.content_refs]

    # ---- download side
    def open_attachment_stream(self, project_id, task_id, attachment_id):
        self.stream_args = (project_id, task_id, attachment_id)
        if self.raises:
            raise self.raises
        outer = self

        class FakeResp:
            headers = outer.headers

            def iter_content(self, chunk_size=None):
                yield from outer.chunks

            def close(self):
                outer.closed = True

        return FakeResp()

    # ---- upload side
    def upload_attachment_bytes(self, project_id, task_id, attachment_id, data,
                                filename=None):
        if self.raises:
            raise self.raises
        self.uploaded = (project_id, task_id, attachment_id, data, filename)
        return {"fileName": filename, "size": len(data)}


@pytest.fixture
def fake_v2(monkeypatch):
    fake = FakeV2()
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


def _dl_token(ttl_minutes=15, name="report.pdf", kind="dl"):
    return s._sign_attachment_token(kind, PROJECT_ID, TASK_ID, ATT_ID,
                                    name, ttl_minutes)


# ===========================================================================
# Token: sign / verify
# ===========================================================================

def test_signed_token_round_trips():
    payload = s._verify_attachment_token(_dl_token(), "dl")
    assert payload["p"] == PROJECT_ID
    assert payload["t"] == TASK_ID
    assert payload["a"] == ATT_ID
    assert payload["n"] == "report.pdf"
    assert payload["e"] > time.time()


def test_token_of_the_other_kind_is_rejected():
    """A download token must not work as an upload token (and vice versa) —
    otherwise a read-only link would become a write capability."""
    assert s._verify_attachment_token(_dl_token(kind="dl"), "ul") is None
    assert s._verify_attachment_token(_dl_token(kind="ul"), "dl") is None


def test_expired_token_is_rejected():
    """A negative TTL mints a token that was already dead when it was signed —
    same code path as one that simply sat around too long."""
    assert s._verify_attachment_token(_dl_token(ttl_minutes=-1), "dl") is None


def test_tampered_payload_is_rejected():
    """Rewriting the payload (here: pointing at another task) invalidates the
    signature — the token is not just an opaque blob, it is authenticated."""
    body, sig = _dl_token().split(".")
    payload = json.loads(s._b64u_decode(body))
    payload["t"] = "someone-elses-task"
    forged_body = s._b64u_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    assert s._verify_attachment_token(f"{forged_body}.{sig}", "dl") is None


def test_token_signed_with_a_different_secret_is_rejected(monkeypatch):
    foreign = _dl_token()
    monkeypatch.setattr(s, "SECRET", "some-other-secret")
    assert s._verify_attachment_token(foreign, "dl") is None


def test_malformed_tokens_are_rejected():
    for bad in ["", "nodot", "a.b.c", "!!!.???", "."]:
        assert s._verify_attachment_token(bad, "dl") is None


def test_no_secret_means_no_links(monkeypatch):
    """MCP_SECRET is the root of the signing key: without it links can neither
    be minted nor accepted (fail closed)."""
    token = _dl_token()
    monkeypatch.setattr(s, "SECRET", "")
    assert s._link_key() is None
    assert s._sign_attachment_token("dl", PROJECT_ID, TASK_ID, ATT_ID, "x", 15) is None
    assert s._verify_attachment_token(token, "dl") is None


def test_signing_key_is_not_the_raw_mcp_secret():
    """A leaked link must never expose the secret that guards /mcp/<secret>."""
    assert s._link_key() != s.SECRET.encode("utf-8")
    assert s.SECRET not in _dl_token()


# ===========================================================================
# _public_base_url / _content_disposition
# ===========================================================================

def test_public_base_url_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://custom.example.com/")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "ignored.up.railway.app")
    assert s._public_base_url() == "https://custom.example.com"


def test_public_base_url_falls_back_to_railway_domain(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "tt.up.railway.app")
    assert s._public_base_url() == "https://tt.up.railway.app"


def test_public_base_url_unknown_returns_none(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    assert s._public_base_url() is None


def test_content_disposition_handles_cyrillic_name():
    cd = s._content_disposition("Отчёт за июль.pdf")
    # ASCII fallback keeps a usable name + extension, real name goes in filename*
    assert 'filename="attachment.pdf"' in cd
    assert "filename*=UTF-8''%D0%9E%D1%82%D1%87" in cd
    assert cd.startswith("attachment; ")


def test_content_disposition_keeps_plain_ascii_name():
    assert s._content_disposition("invoice.pdf") == (
        "attachment; filename=\"invoice.pdf\"; filename*=UTF-8''invoice.pdf")


def test_content_disposition_strips_quotes_and_newlines():
    """Header injection guard: a filename can't break out of the header."""
    cd = s._content_disposition('ev"il\nname.txt')
    assert "\n" not in cd
    assert cd.count('"') == 2


# ===========================================================================
# GET /dl/{token}
# ===========================================================================

def test_dl_streams_the_file(client, fake_v2):
    fake_v2.headers = {"Content-Type": "application/pdf"}
    r = client.get(f"/dl/{_dl_token()}")
    assert r.status_code == 200
    assert r.content == b"hello world"
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["cache-control"] == "private, no-store"
    assert 'filename="report.pdf"' in r.headers["content-disposition"]
    assert fake_v2.stream_args == (PROJECT_ID, TASK_ID, ATT_ID)
    assert fake_v2.closed is True


def test_dl_sets_cyrillic_content_disposition(client, fake_v2):
    r = client.get(f"/dl/{_dl_token(name='Отчёт.pdf')}")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert 'filename="attachment.pdf"' in cd
    assert "filename*=UTF-8''%D0%9E%D1%82%D1%87" in cd


def test_dl_guesses_mime_from_the_filename(client, fake_v2):
    """TickTick often answers application/octet-stream; the stored name is the
    better signal so a browser opens the PDF instead of saving a blob."""
    fake_v2.headers = {"Content-Type": "application/octet-stream"}
    r = client.get(f"/dl/{_dl_token()}")
    assert r.headers["content-type"].startswith("application/pdf")


def test_dl_passes_through_content_length(client, fake_v2):
    fake_v2.headers = {"Content-Length": "11"}
    r = client.get(f"/dl/{_dl_token()}")
    assert r.headers.get("content-length") == "11"


def test_dl_drops_content_length_for_encoded_bodies(client, fake_v2):
    """requests decompresses while streaming, so a gzip Content-Length would
    promise fewer bytes than we actually send — better to omit it and chunk."""
    fake_v2.headers = {"Content-Length": "5", "Content-Encoding": "gzip"}
    r = client.get(f"/dl/{_dl_token()}")
    assert r.status_code == 200
    assert r.content == b"hello world"
    assert r.headers.get("content-length") != "5"


def test_dl_expired_token_is_404(client, fake_v2):
    r = client.get(f"/dl/{_dl_token(ttl_minutes=-1)}")
    assert r.status_code == 404
    assert "устарела" in r.text
    assert fake_v2.stream_args is None  # never reached TickTick


def test_dl_foreign_signature_is_404(client, fake_v2, monkeypatch):
    token = _dl_token()
    monkeypatch.setattr(s, "SECRET", "attacker-secret")
    r = client.get(f"/dl/{token}")
    assert r.status_code == 404
    assert fake_v2.stream_args is None


def test_dl_garbage_token_is_404(client, fake_v2):
    r = client.get("/dl/not-a-real-token")
    assert r.status_code == 404
    assert fake_v2.stream_args is None


def test_dl_upload_token_is_404(client, fake_v2):
    """An upload link replayed against /dl gets nothing."""
    r = client.get(f"/dl/{_dl_token(kind='ul')}")
    assert r.status_code == 404
    assert fake_v2.stream_args is None


def test_dl_missing_attachment_upstream_is_404(client, monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(raises=ValueError("not found")))
    r = client.get(f"/dl/{_dl_token()}")
    assert r.status_code == 404


def test_dl_upstream_auth_failure_is_502(client, monkeypatch):
    from ticktick_mcp.src.ticktick_v2_client import TickTickAuthError
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(raises=TickTickAuthError("expired")))
    r = client.get(f"/dl/{_dl_token()}")
    assert r.status_code == 502


def test_dl_without_v2_is_503(client, monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "initialize_client", lambda: False)
    r = client.get(f"/dl/{_dl_token()}")
    assert r.status_code == 503


# ===========================================================================
# PUT /ul/{token}
# ===========================================================================

def test_ul_relays_the_body_into_ticktick(client, fake_v2):
    token = _dl_token(kind="ul", name="scan.png")
    r = client.put(f"/ul/{token}", content=b"\x89PNG fake bytes")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["fileName"] == "scan.png"
    assert body["size_bytes"] == len(b"\x89PNG fake bytes")
    pid, tid, att_id, data, name = fake_v2.uploaded
    assert (pid, tid, att_id) == (PROJECT_ID, TASK_ID, ATT_ID)
    assert data == b"\x89PNG fake bytes"
    assert name == "scan.png"


def test_ul_rejects_a_download_token(client, fake_v2):
    r = client.put(f"/ul/{_dl_token(kind='dl')}", content=b"x")
    assert r.status_code == 404
    assert fake_v2.uploaded is None


def test_ul_expired_token_is_404(client, fake_v2):
    r = client.put(f"/ul/{_dl_token(kind='ul', ttl_minutes=-1)}", content=b"x")
    assert r.status_code == 404
    assert fake_v2.uploaded is None


def test_ul_empty_body_is_400(client, fake_v2):
    r = client.put(f"/ul/{_dl_token(kind='ul')}", content=b"")
    assert r.status_code == 400
    assert fake_v2.uploaded is None


def test_ul_oversized_body_is_413(client, fake_v2, monkeypatch):
    monkeypatch.setattr(s, "ATTACHMENT_MAX_BYTES", 64)
    r = client.put(f"/ul/{_dl_token(kind='ul')}", content=b"x" * 200)
    assert r.status_code == 413
    assert fake_v2.uploaded is None


def test_ul_upstream_failure_is_502(client, monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", FakeV2(raises=RuntimeError("HTTP 500")))
    r = client.put(f"/ul/{_dl_token(kind='ul')}", content=b"x")
    assert r.status_code == 502


# ===========================================================================
# Tool: get_attachment_download_url
# ===========================================================================

async def test_download_url_tool_returns_a_working_link(fake_v2):
    out = await s.get_attachment_download_url(task_id=TASK_ID, project_id=PROJECT_ID,
                                              attachment_id=ATT_ID)
    assert "https://tt.example.com/dl/" in out
    token = out.split("/dl/")[1].split()[0]
    payload = s._verify_attachment_token(token, "dl")
    assert payload["a"] == ATT_ID
    assert payload["n"] == "Отчёт.pdf"
    assert "15 мин" in out


async def test_download_url_tool_requires_a_selector(fake_v2):
    out = await s.get_attachment_download_url(task_id=TASK_ID, project_id=PROJECT_ID)
    assert "attachment_id" in out and "/dl/" not in out


async def test_download_url_tool_ttl_is_clamped(fake_v2):
    out = await s.get_attachment_download_url(task_id=TASK_ID, project_id=PROJECT_ID,
                                              attachment_id=ATT_ID, ttl_minutes=99999)
    token = out.split("/dl/")[1].split()[0]
    payload = s._verify_attachment_token(token, "dl")
    assert payload["e"] <= time.time() + s.ATTACHMENT_LINK_TTL_MAX_MIN * 60 + 5


async def test_download_url_tool_says_so_when_domain_is_unknown(
        fake_v2, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    out = await s.get_attachment_download_url(task_id=TASK_ID, project_id=PROJECT_ID,
                                              attachment_id=ATT_ID)
    assert "PUBLIC_BASE_URL" in out
    assert "/dl/" not in out


async def test_download_url_tool_says_so_without_secret(
        fake_v2, monkeypatch):
    monkeypatch.setattr(s, "SECRET", "")
    out = await s.get_attachment_download_url(task_id=TASK_ID, project_id=PROJECT_ID,
                                              attachment_id=ATT_ID)
    assert "MCP_SECRET" in out and "/dl/" not in out


async def test_download_url_tool_reports_unknown_filename(fake_v2):
    """Имя, которого на задаче нет, — отказ с подсказкой, а не тихий выбор
    первого попавшегося файла. Список вложений приходит от двойника КЛИЕНТА,
    поэтому боевой путь резолвинга исполняется целиком."""
    fake_v2.attachments = [{"id": ATT_ID, "fileName": "a.pdf"}]
    out = await s.get_attachment_download_url(task_id=TASK_ID, project_id=PROJECT_ID,
                                              filename="ghost.pdf")
    assert "No attachment named" in out


# ===========================================================================
# Tool: create_attachment_upload_url
# ===========================================================================
# Тул гейтован (аудит 2026-08-06): ссылка — предъявительский пропуск на запись
# в аккаунт, поэтому она выдаётся только вызовом #2, после согласия. Тесты
# ниже проверяют содержимое ссылки, а не гейт (гейт — в
# tests/test_gate_attachment_url.py), поэтому ходят через этот хелпер.

async def _upload_url(**kwargs) -> str:
    """Полный двухфазный вызов: план → честное «да» → сама ссылка."""
    preview = await s.create_attachment_upload_url(**kwargs)
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"вызов #1 не вернул план: {preview!r}"
    assert "/ul/" not in preview, "план не должен содержать саму ссылку"
    return await s.create_attachment_upload_url(
        **kwargs, manifest_id=m.group(1), user_reply="да")


async def test_upload_url_tool_returns_a_working_link(fake_v2):
    out = await _upload_url(task_id=TASK_ID, project_id=PROJECT_ID,
                            filename="scan.png")
    assert "https://tt.example.com/ul/" in out
    token = out.split("/ul/")[1].split()[0].strip('"')
    payload = s._verify_attachment_token(token, "ul")
    assert payload["t"] == TASK_ID
    assert payload["n"] == "scan.png"
    assert len(payload["a"]) == 24  # client-minted TickTick attachment id


async def test_upload_url_tool_is_honest_that_claude_cannot_put(fake_v2):
    out = await _upload_url(task_id=TASK_ID, project_id=PROJECT_ID,
                            filename="scan.png")
    assert "curl -X PUT" in out
    assert "не могу" in out


async def test_upload_url_tool_rejects_oversized_file_upfront(fake_v2):
    out = await s.create_attachment_upload_url(
        task_id=TASK_ID, project_id=PROJECT_ID, filename="huge.zip",
        size_bytes=s.ATTACHMENT_MAX_BYTES + 1)
    assert "/ul/" not in out
    assert "МБ" in out


async def test_upload_url_tool_says_so_when_domain_is_unknown(fake_v2, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    out = await s.create_attachment_upload_url(task_id=TASK_ID, project_id=PROJECT_ID,
                                               filename="scan.png")
    assert "PUBLIC_BASE_URL" in out and "/ul/" not in out


async def test_upload_link_round_trip_reaches_ticktick(client, fake_v2):
    """End to end over HTTP: the link the tool prints is the link the endpoint
    accepts, and the bytes land in the v2 upload call."""
    out = await _upload_url(task_id=TASK_ID, project_id=PROJECT_ID,
                            filename="note.txt")
    url = out.split("https://tt.example.com")[1].split()[0]
    r = client.put(url, content=b"hi there")
    assert r.status_code == 200
    assert fake_v2.uploaded[3] == b"hi there"
    assert fake_v2.uploaded[4] == "note.txt"
