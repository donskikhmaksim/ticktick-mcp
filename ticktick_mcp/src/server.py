import asyncio
import base64
import collections
import contextvars
import functools
import hashlib
import hmac
import json
import mimetypes
import os
import re
import time
import unicodedata
import urllib.parse
import uuid
import logging
from datetime import date, datetime, timezone, timedelta
from typing import Dict, List, Any, Literal, Optional, Tuple
from zoneinfo import ZoneInfo

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from dotenv import load_dotenv
from starlette.requests import Request
from starlette.responses import (JSONResponse, PlainTextResponse, Response,
                                 StreamingResponse)

from .ticktick_client import TickTickClient, _normalize_date
from .ticktick_v2_client import (ATTACHMENT_MAX_BYTES, COMPLETED_MAX_LIMIT,
                                 TRASH_MAX_LIMIT, TickTickAuthError,
                                 TickTickV2Client, id2error_failures,
                                 new_attachment_id)
from . import declutter_sheet
from . import log_redaction
from . import manifest_store
from . import tg_approval

# Set up logging
# П8 (2026-08-09): без формата logging.basicConfig печатал ГОЛОЕ сообщение —
# ни времени, ни уровня, ни модуля. Вперемешку с logger.exception (см. ниже)
# это делало логи Railway практически бесполезными при разборе инцидента:
# нельзя было понять, КОГДА упало и в каком модуле, только текст сообщения.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Опциональный внеполосный Telegram-фактор (см. tg_approval.py doc-comment).
# Загружается всегда (даже выключенным) — так call-сайты не ветвятся на
# "а вдруг конфига нет", `enabled_for()` просто всегда False в этом случае.
_TG_CFG = tg_approval.load_tg_approval_config()

# --- Transport / deployment config (read from environment) ---
# Local default is stdio; on Railway set MCP_TRANSPORT=streamable-http.
load_dotenv()
TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").strip()
HOST = os.getenv("MCP_HOST", "0.0.0.0").strip()
# Railway injects PORT; fall back to MCP_PORT then 8000.
PORT = int(os.getenv("PORT", os.getenv("MCP_PORT", "8000")))
# Unguessable secret embedded in the URL path = lightweight auth for the
# public Railway endpoint. Without it the path is the default "/mcp".
SECRET = os.getenv("MCP_SECRET", "").strip()
STREAMABLE_PATH = f"/mcp/{SECRET}" if SECRET else "/mcp"


def _redact_for_user(text: Any) -> str:
    """Прогнать текст, который уйдёт МОДЕЛИ (не только логам), через тот же
    редактор секретов пути, что и `log_redaction.install()` для логов
    (П7, 2026-08-09).

    ПОЧЕМУ. До этой правки `except Exception as e: return f"...: {str(e)}"`
    отдавал текст исключения HTTP-клиента как есть. Такое исключение вполне
    может содержать URL с нашим же секретом (`/mcp/<secret>`) или подписанную
    ссылку вложения (`/dl/<token>`, `/ul/<token>`) — например если клиент
    ретраит запрос к самому себе или логирует полный запрос внутри текста
    ошибки. `log_redaction.redact()` для логов такое уже чистит; для строк,
    возвращаемых из тула модели, чистки не было — секрет уезжал в переписку
    навсегда. Здесь та же функция, тот же список масок, просто применяется
    ко второму каналу вывода.
    """
    return log_redaction.redact(str(text), secret=SECRET or None)


def _tool_error(context: str, exc: Any) -> str:
    """Build the standard failure string returned to the model by a tool.

    Every read/write tool below funnels its `except Exception as e` (or an
    inline `{"error": ...}` payload from the low-level client) through this
    single function instead of hand-writing `f"Error ...: {str(e)}"` at each
    of the ~80 call sites. Two reasons: exception text is redacted the same
    way log lines are (see `_redact_for_user` above — no secret URL leaks
    into the chat), and the wording is uniform instead of a mix of
    "fetching"/"retrieving"/etc. for the same kind of failure.

    Args:
        context: short present-participle phrase naming the failed
            operation, e.g. "fetching projects" — becomes "Error fetching
            projects: ...".
        exc: the caught exception, or an already-stringified error payload
            (e.g. a dict's `['error']` value coming from the client).
    """
    return f"Error {context}: {_redact_for_user(exc)}"


def _automation_key_matches(provided: str) -> bool:
    """Постоянное по времени сравнение `automation_key` с MCP_SECRET, которое
    НЕ падает на не-ASCII (2026-08-06).

    Было: `hmac.compare_digest(automation_key, SECRET)` с ДВУМЯ `str`. В этой
    форме CPython требует, чтобы обе строки были ASCII-only, иначе бросает
    `TypeError: comparing strings with non-ASCII characters is not supported`.
    То есть достаточно одной кириллической буквы или эмодзи в присланном ключе
    (модель прислала «ключ», клиент подставил осмысленную строку) — и вместо
    честного «ключ не подошёл» из гейта вылетало исключение: у `create_tasks`
    оно рвало вызов наружу, а внутри `_require_consent` — рушило гейт целиком,
    вместо того чтобы принять по нему решение. Если не-ASCII оказывался в САМОМ
    MCP_SECRET, автоматика ложилась вся и навсегда, при внешне корректном
    ключе.

    Сравниваем sha256-дайджесты utf-8-байтов: байты `compare_digest` принимает
    любые, дайджест всегда 32 байта — значит не утекает и длина секрета
    (у сравнения сырых байтов разной длины она видна по времени)."""
    if not (SECRET and provided):
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(SECRET.encode("utf-8")).digest(),
    )

# Create FastMCP server
mcp = FastMCP("ticktick", host=HOST, port=PORT, streamable_http_path=STREAMABLE_PATH)

# Read-only tools carry this annotation so MCP clients (Claude) can skip the
# confirmation dialog / offer "always allow" for them.
READONLY = ToolAnnotations(readOnlyHint=True)

# Create TickTick clients
ticktick = None       # official Open API (OAuth)
ticktick_v2 = None    # unofficial v2 API (email/password), optional
# `ticktick` (v1) is documented/stable but single-item-only: no listing,
# filtering, batch ops, get_changes, habits, trash, or project groups.
# `ticktick_v2` is undocumented/reverse-engineered but is what actually
# powers almost every listing/filtering/batch/dedup/audit tool below — several
# tools gate readiness on `ticktick` yet run on `ticktick_v2` when it's
# configured, falling back to `ticktick` only when it's not. See the README
# section "Two TickTick APIs, and what breaks if the unofficial one goes
# down" for the full capability split and what a v2 outage does and doesn't
# take down.

def initialize_client():
    global ticktick, ticktick_v2
    try:
        # Credentials come from the durable volume file first (freshest after a
        # token refresh on a previous container), then env vars.
        load_dotenv()

        from .ticktick_client import load_token_file
        if not os.getenv("TICKTICK_ACCESS_TOKEN") and not load_token_file().get("access_token"):
            logger.error("No TICKTICK_ACCESS_TOKEN set (env or volume). "
                         "Run the local `auth` flow (uv run -m ticktick_mcp.cli auth) "
                         "and set TICKTICK_ACCESS_TOKEN.")
            return False

        # Initialize the official Open API client into a LOCAL first. Only
        # commit it to the module global after validation succeeds — otherwise a
        # failed init leaves `ticktick` truthy and the lazy-retry guard
        # `if not ticktick` never retries.
        local_ticktick = TickTickClient()
        logger.info("TickTick Open API client initialized")

        # Test API connectivity
        projects = local_ticktick.get_projects()
        if 'error' in projects:
            logger.error(f"Failed to access TickTick API: {projects['error']}")
            logger.error("Your access token may have expired. Re-run 'uv run -m ticktick_mcp.cli auth'.")
            return False
        logger.info(f"Connected to TickTick Open API with {len(projects)} projects")

        # Optionally initialize the unofficial v2 client (tags, completed,
        # inbox, move). Preferred auth is the browser `t` cookie via
        # TICKTICK_V2_TOKEN; username/password is a deprecated fallback.
        # Failure here is non-fatal — the Open API still works.
        local_v2 = None
        candidate = TickTickV2Client()
        if candidate.enabled:
            try:
                candidate.authenticate()
                local_v2 = candidate
                logger.info("TickTick v2 API enabled (tags/completed/inbox/move)")
            except Exception as e:
                local_v2 = None
                logger.warning(f"v2 API unavailable, continuing with Open API only: {e}")
        else:
            logger.info("v2 API disabled (set TICKTICK_V2_TOKEN to enable)")

        # Commit the validated clients to the module globals only now.
        ticktick = local_ticktick
        ticktick_v2 = local_v2

        # Вынесенный модуль кнопки (1.2.4, 2026-08-09) держит СВОЮ ссылку на
        # официальный клиент: строки перенесённого куска не менялись, а `global
        # ticktick` выше пишет прямо в словарь этого модуля, минуя __setattr__
        # (то есть мимо проброса в конце файла). Без явной строки ниже удаление
        # ПРОЕКТА по кнопке молча отвечало бы отказом «не смог прочитать
        # содержимое проекта» — клиент там остался бы None навсегда.
        tg_auto_execute.ticktick = ticktick

        # Official-API writes must drop the v2 sync cache so v2 reads stay
        # consistent (e.g. create a task via the official API, then move it).
        TickTickClient.write_hook = lambda: (
            ticktick_v2.invalidate_cache() if ticktick_v2 else None)

        return True
    except Exception:
        logger.exception("Failed to initialize TickTick client")
        return False


# --- HTTP routes ------------------------------------------------------------
# Single-tenant: this instance serves ONE person's TickTick account. Auth is
# established out-of-band (the local `auth` flow writes TICKTICK_ACCESS_TOKEN,
# or it is set as a Railway variable / durable volume file) — there is no
# in-server browser OAuth flow. Only /health is exposed here.


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    return JSONResponse({"status": "ok", "ticktick_connected": ticktick is not None})


# --- own_bot: собственный Telegram-вебхук (TG_BOT_TOKEN_OVERRIDE) ------------
# По умолчанию (own_bot=False) ticktick-mcp НЕ владеет вебхуком: решение по
# кнопке доходит через общий вебхук gmail-mcp и таблицу tg_approvals (см.
# tg_approval.py's докстринг). Этот роут существует ТОЛЬКО ради
# TG_BOT_TOKEN_OVERRIDE — сервер с собственным ботом получает апдейты
# исключительно СВОЕГО токена, поэтому здесь нет коллизии с общим ботом,
# которую решает TG_WEBHOOK_OWNER на TS-стороне.
#
# Роут смонтирован БЕЗУСЛОВНО (та же дисциплина, что у /health, /dl, /ul выше
# и ниже — все они всегда в маршрутной таблице, а поведение решает состояние
# модуля на момент запроса, не факт регистрации): переключение
# TG_BOT_TOKEN_OVERRIDE никогда не требует передеплоя ради изменения роутинга,
# и снаружи выключенный own_bot неотличим от отсутствия роута вовсе — тот же
# 404, что и для любого несуществующего пути. `_TG_CFG`/`TRANSPORT`
# читаются здесь как атрибуты МОДУЛЯ (а не замороженные на момент декорации
# значения), поэтому monkeypatch в тестах (`monkeypatch.setattr(s, "_TG_CFG",
# ...)`) реально меняет поведение уже смонтированного роута — тот же паттерн,
# которым `health_check` выше читает `ticktick`.
@mcp.custom_route("/tg/webhook", methods=["POST"])
async def tg_own_bot_webhook(request: Request) -> Response:
    """Принимает апдейты Telegram для СВОЕГО бота (own_bot). Порт http.ts's
    `app.post("/tg/webhook", ...)` — тот же порядок проверок:

    1. own_bot выключен (или транспорт не streamable-http, где этот роут
       физически не мог бы получать трафик) → 404, ничего не раскрываем;
    2. секрет из заголовка не совпал с `TG_APPROVAL_WEBHOOK_SECRET` → 401;
    3. тело разбирается и уходит в `tg_approval.handle_webhook` (в поток —
       та же причина, что у `_run_blocking` везде в этом файле: внутри
       синхронный `requests` к Telegram API и к Postgres, держать event loop
       на время этого — то же самое зависание /health, что уже чинили для
       notify_plan/reap);
    4. ВСЕГДА отвечает 200, что бы ни случилось внутри — Telegram ретраит
       не-2xx, а каждый «отказ» внутри `handle_webhook` (чужой отправитель,
       повтор, неизвестный callback_data) — намеренный no-op, не ошибка,
       которую стоит повторять."""
    if not (_TG_CFG.enabled and _TG_CFG.own_bot and TRANSPORT == "streamable-http"):
        return PlainTextResponse("Not Found", status_code=404)
    provided = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not tg_approval.secret_token_matches(provided, _TG_CFG.webhook_secret):
        return PlainTextResponse("Unauthorized", status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        await _run_blocking(tg_approval.handle_webhook, _TG_CFG, body)
    except Exception:  # noqa: BLE001 — вебхук обязан ответить Telegram'у,
        # что бы ни случилось внутри обработки; Telegram ретраит не-2xx, а
        # повторная доставка того же апдейта на сломанном коде ничего не чинит
        logger.exception("TG own_bot webhook: handle_webhook упал")
    return PlainTextResponse("", status_code=200)


# --- Attachment transfer links (/dl, /ul) -----------------------------------
# Big files must not travel through the MCP response body (download_task_
# attachment base64-encodes into the answer and caps at 256 KB). Instead a tool
# hands out a short-lived URL on THIS server, and the bytes are streamed
# straight between the client (phone/browser/script) and TickTick.
#
# The link is STATELESS: everything the endpoint needs (project/task/attachment
# ids, filename, expiry) is inside a signed token. There is no database and
# nothing on disk, so nothing to clean up and nothing to leak on restart. The
# signing key is DERIVED from MCP_SECRET rather than being MCP_SECRET itself,
# so a leaked download link can never be replayed as the MCP endpoint's secret.
# The TickTick `t` cookie NEVER leaves this server: the endpoints relay bytes.

ATTACHMENT_LINK_TTL_DEFAULT_MIN = 15
ATTACHMENT_LINK_TTL_MAX_MIN = 120


def _link_key() -> Optional[bytes]:
    """Key used to sign attachment links: HMAC(MCP_SECRET, "attachment-link").
    None when MCP_SECRET is unset — in that case links cannot be signed and
    every /dl and /ul request is rejected (fail closed)."""
    if not SECRET:
        return None
    return hmac.new(SECRET.encode("utf-8"), b"attachment-link",
                    hashlib.sha256).digest()


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign_attachment_token(kind: str, project_id: str, task_id: str,
                           attachment_id: str, filename: str,
                           ttl_minutes: int) -> Optional[str]:
    """Build '<payload>.<signature>' for a /dl (kind='dl') or /ul (kind='ul')
    link. Returns None when there is no MCP_SECRET to derive a key from."""
    key = _link_key()
    if not key:
        return None
    payload = {"k": kind, "p": project_id, "t": task_id, "a": attachment_id,
               "n": filename or "", "e": int(time.time() + ttl_minutes * 60)}
    body = _b64u_encode(json.dumps(payload, separators=(",", ":"),
                                   sort_keys=True,
                                   ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64u_encode(sig)}"


def _verify_attachment_token(token: str, kind: str) -> Optional[Dict]:
    """Decoded payload for a valid, unexpired token of the expected kind, else
    None. Any failure (bad shape, wrong signature, wrong kind, expired) returns
    None — the caller must answer identically for all of them so the endpoint
    never becomes an oracle."""
    key = _link_key()
    if not key or not token or token.count(".") != 1:
        return None
    body, sig_b64 = token.split(".")
    try:
        sig = _b64u_decode(sig_b64)
        expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64u_decode(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("k") != kind:
        return None
    exp = payload.get("e")
    if not isinstance(exp, (int, float)) or time.time() > exp:
        return None
    if not (payload.get("p") and payload.get("t") and payload.get("a")):
        return None
    return payload


def _public_base_url() -> Optional[str]:
    """Base URL this server is reachable at from the outside, or None if it
    cannot be known. PUBLIC_BASE_URL wins (set it when a custom domain or a
    proxy is in front); otherwise Railway's injected RAILWAY_PUBLIC_DOMAIN."""
    base = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not base:
        domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if not domain:
            return None
        base = domain if domain.startswith("http") else f"https://{domain}"
    return base.rstrip("/")


_NO_PUBLIC_URL_MSG = (
    "Не могу собрать ссылку: сервер не знает свой публичный адрес. "
    "Задай переменную окружения PUBLIC_BASE_URL "
    "(например https://ticktick-mcp.up.railway.app) — на Railway обычно хватает "
    "уже готовой RAILWAY_PUBLIC_DOMAIN, но её здесь нет.")
_NO_SECRET_MSG = (
    "Не могу подписать ссылку: не задан MCP_SECRET, а ключ подписи выводится "
    "из него. Без него временные ссылки отключены.")
# Deliberately vague: an expired, forged, or malformed token all look the same
# from outside — no hints about which part was wrong.
_BAD_LINK_MSG = ("Ссылка недействительна или устарела. Попроси новую — "
                 "они живут недолго.\n")


def _content_disposition(filename: str) -> str:
    """Content-Disposition value that survives non-ASCII names: a stripped
    ASCII fallback for ancient clients plus RFC 5987 filename* with the real
    (percent-encoded UTF-8) name, which every current browser prefers."""
    name = (filename or "").replace("\r", " ").replace("\n", " ").strip() or "attachment"
    ascii_name = name.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace('"', "").replace("\\", "").strip()
    # A fully non-ASCII name ("Отчёт.pdf") leaves only the extension — keep the
    # extension but give the fallback an actual stem.
    stem, dot, ext = ascii_name.rpartition(".")
    if not stem.strip():
        ascii_name = f"attachment{dot}{ext}" if dot else "attachment"
    quoted = urllib.parse.quote(name, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _stream_and_close(resp):
    """Yield the relayed body in chunks and always release the upstream
    connection, including when the client hangs up mid-download."""
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk
    finally:
        resp.close()


@mcp.custom_route("/dl/{token}", methods=["GET"])
async def attachment_download_link(request: Request) -> Response:
    """Stream a TickTick attachment to whoever holds a valid, unexpired token.
    Nothing is buffered: bytes go TickTick -> here -> client."""
    payload = _verify_attachment_token(request.path_params.get("token", ""), "dl")
    if not payload:
        return PlainTextResponse(_BAD_LINK_MSG, status_code=404)
    # _ensure_ready can lazily (re-)initialize the clients, which does network
    # I/O — keep it off the event loop like every other blocking client call.
    err = await _run_blocking(_ensure_ready)
    if err:
        return PlainTextResponse(f"{err}\n", status_code=503)
    try:
        upstream = await _run_blocking(
            lambda: ticktick_v2.open_attachment_stream(
                payload["p"], payload["t"], payload["a"]))
    except TickTickAuthError:
        logger.exception("/dl auth error")
        return PlainTextResponse("Сервер не может обратиться к TickTick "
                                 "(сессия истекла).\n", status_code=502)
    except ValueError:
        # Attachment genuinely gone upstream — same 404 wording as a bad link.
        return PlainTextResponse(_BAD_LINK_MSG, status_code=404)
    except Exception:
        logger.exception("/dl upstream error")
        return PlainTextResponse("Не удалось получить файл из TickTick.\n",
                                 status_code=502)

    name = payload.get("n") or f"attachment_{payload['a']}"
    mime = upstream.headers.get("Content-Type")
    if not mime or mime == "application/octet-stream":
        mime = mimetypes.guess_type(name)[0] or mime or "application/octet-stream"
    headers = {"Content-Disposition": _content_disposition(name),
               "Cache-Control": "private, no-store"}
    # Pass the length through only when the upstream body is NOT encoded:
    # iter_content() transparently decompresses, so a gzip Content-Length would
    # describe the compressed size and leave the client waiting for bytes that
    # never come. Without the header the response is simply chunked.
    length = upstream.headers.get("Content-Length")
    if length and not upstream.headers.get("Content-Encoding"):
        headers["Content-Length"] = length
    return StreamingResponse(_stream_and_close(upstream), media_type=mime,
                             headers=headers)


@mcp.custom_route("/ul/{token}", methods=["PUT"])
async def attachment_upload_link(request: Request) -> Response:
    """Accept a raw request body and relay it into TickTick as the multipart
    upload it expects. The 20 MB cap is TickTick's own; the body is read in
    chunks so an oversized upload is rejected without swallowing it whole."""
    payload = _verify_attachment_token(request.path_params.get("token", ""), "ul")
    if not payload:
        return PlainTextResponse(_BAD_LINK_MSG, status_code=404)
    # _ensure_ready can lazily (re-)initialize the clients, which does network
    # I/O — keep it off the event loop like every other blocking client call.
    err = await _run_blocking(_ensure_ready)
    if err:
        return PlainTextResponse(f"{err}\n", status_code=503)

    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > ATTACHMENT_MAX_BYTES:
            return PlainTextResponse(
                f"Файл больше {ATTACHMENT_MAX_BYTES // (1024*1024)} МБ — "
                "TickTick столько не принимает.\n", status_code=413)
    if not buf:
        return PlainTextResponse("Пустое тело запроса — нечего загружать.\n",
                                 status_code=400)

    name = payload.get("n") or f"attachment_{payload['a']}"
    try:
        await _run_blocking(lambda: ticktick_v2.upload_attachment_bytes(
            payload["p"], payload["t"], payload["a"], bytes(buf), filename=name))
    except TickTickAuthError:
        logger.exception("/ul auth error")
        return PlainTextResponse("Сервер не может обратиться к TickTick "
                                 "(сессия истекла).\n", status_code=502)
    except Exception as e:
        logger.exception("/ul upstream error")
        return PlainTextResponse(
            f"TickTick не принял файл: {_redact_for_user(e)}\n",
            status_code=502)
    return JSONResponse({"status": "ok", "fileName": name,
                         "size_bytes": len(buf), "task_id": payload["t"],
                         "attachment_id": payload["a"]})


# Single source of truth for TickTick's priority levels (0/1/3/5).
PRIORITY_MAP = {0: "None", 1: "Low", 3: "Medium", 5: "High"}

# ...and for its task statuses. -1 ("won't do" / abandoned) is absent from the
# official API docs but very much present in the data — get_task_info's v2
# branch already mapped it. format_task used to test `status == 2` and call
# EVERYTHING else "Active", so an abandoned task was reported as active. Any
# status this map doesn't know must stay visible as unknown rather than fall
# into the same silent "Active" bucket.
STATUS_MAP = {0: "Active", 2: "Completed", -1: "Won't do (abandoned)"}


# ---------------------------------------------------------------------------
# ЗАДАЧА БЕЗ НАЗВАНИЯ — НЕ ПУСТОЕ МЕСТО В СПИСКЕ (П15, 2026-08-09).
#
# Случай: из плана на удаление в 14 задач исполнилось 11, а три не прошли — и
# все три были с ПУСТЫМ названием. В списках они выглядели одинаково: пустой
# строкой, неотличимо от мусора. При этом две из трёх были содержательными —
# в Inbox лежала ФОТОГРАФИЯ ЧЕКА HOME DEPOT на возврат $374.92, в Vibe Coding
# Notes скриншот с описанием дефекта. Владелец включил их в удаление именно
# потому, что по строке списка нельзя было отличить документ от мусора;
# спасло только то, что удаление не прошло по другой причине.
#
# ПРАВИЛО: там, где у задачи нет имени, печатается не «(no title)» и не
# пустота, а то, ЧТО В НЕЙ ЕСТЬ: «(без названия: 📎 1 файл)» ≠ «(без
# названия: пусто)». Разница между этими двумя строками — разница между
# документом и мусором, и решение об удалении принимается по ней.
#
# Считается ТОЛЬКО по уже прочитанному объекту задачи, без единого
# дополнительного запроса: эта функция зовётся на каждую строку каждого
# списка (сотни задач за вызов).
_ATTACH_INLINE_RE = re.compile(r"!\[file\]\(([0-9a-fA-F]{24})/([^)]*)\)")


def _files_word(n: int) -> str:
    """«файл» / «файла» / «файлов» — по-русски число обязано согласоваться."""
    if n % 100 in (11, 12, 13, 14):
        return "файлов"
    if n % 10 == 1:
        return "файл"
    if n % 10 in (2, 3, 4):
        return "файла"
    return "файлов"


def _task_attachment_count(task: Dict) -> Optional[int]:
    """Сколько файлов приложено к задаче, по ДВУМ источникам того же объекта:
    структурный массив `attachments` и inline-токены ![file](id/имя) в
    content/desc (те же два источника, что сводит `_merged_task_attachments`,
    только без сетевого чтения). Структурный массив главный; токены считаются
    лишь когда его нет вовсе — у части аккаунтов он приходит пустым, и тогда
    ссылка в тексте единственное свидетельство файла. Складывать их нельзя:
    один и тот же файл присутствует обычно в обоих, и сумма соврала бы «2
    файла» про один чек.

    None — «СКАЗАТЬ НЕ МОГУ», и это НЕ то же самое, что 0 (аудит 2026-08-09).
    Источник, не перечисляющий вложения вовсе — официальный v1 API, чей ответ
    попадает в `format_task`, — не даёт права утверждать «пусто»: карточка
    задачи с фотографией чека Home Depot внутри сказала бы «(без названия:
    пусто)», то есть ровно то, из-за чего документ и попадает под удаление.
    Ноль возвращается ТОЛЬКО когда список вложений реально был перечислен и
    оказался пуст."""
    try:
        atts = task.get("attachments")
        listed = atts is not None
        if listed:
            if not isinstance(atts, (list, tuple)):
                return None           # источник ответил чем-то неожиданным
            structured = [a for a in atts if a]
            if structured:
                return len(structured)
        text = str(task.get("content") or "") + "\n" + str(task.get("desc") or "")
        inline = len({m.group(1) for m in _ATTACH_INLINE_RE.finditer(text)})
        if inline:
            return inline
        return 0 if listed else None
    except Exception as e:                # pragma: no cover — форма ответа
        logger.warning(f"вложения задачи посчитать не удалось: {e}")
        return None


def _task_has_text(task: Dict) -> bool:
    """Есть ли в задаче собственный текст — заметка, описание или пункты
    чеклиста. Inline-ссылки на вложения из текста вычитаются: задача, у
    которой в content ровно `![file](…)` и больше ничего, содержит ФАЙЛ, а не
    текст, и называть её «есть текст» значит прятать файл."""
    for field in ("content", "desc"):
        text = _ATTACH_INLINE_RE.sub("", str(task.get(field) or ""))
        if text.strip():
            return True
    return any((i or {}).get("title") for i in (task.get("items") or []))


def _untitled_label(task: Optional[Dict] = None) -> str:
    """Чем задача без названия называется в любом человеческом выводе.

    ПОРЯДОК ВАЖЕН И ОБРАТНОМУ НЕ ПОДЛЕЖИТ: файл впереди текста. Задача, где
    есть и файл, и подпись к нему, — это ДОКУМЕНТ; сказать про неё «есть
    текст» значит спрятать вложение ровно там, где владелец решает, удалять
    ли. «пусто» — самое сильное утверждение здесь, и оно делается только
    когда источник действительно перечислил вложения (см.
    `_task_attachment_count`); иначе строка просто не утверждает ничего.

    ВНУТРИ ЗАМЕНИТЕЛЯ — ДВОЕТОЧИЕ, А НЕ «·» (Д3, 2026-08-09). Компактная
    строка (`format_task_line`) делит мета-поля тем же символом «·»
    (U+00B7) с теми же пробелами вокруг — внешний контракт заморожен,
    его читает `tg-ai-assistant` регулярками. У безымянной задачи с
    вложением и любым мета-полем в строке «·» встретился бы ДВАЖДЫ, и
    регулярка, делящая по первому разделителю, разъехалась бы: «название»
    получило бы «(без названия», а «мета» — «📎 1 файл) · due …». Двоеточие
    с внешним разделителем не пересекается никогда — заодно это делает
    заменитель узнаваемым на входе (см. `_is_untitled_placeholder`)."""
    task = task or {}
    n = _task_attachment_count(task)
    if n:
        return f"(без названия: 📎 {n} {_files_word(n)})"
    if _task_has_text(task):
        return "(без названия: есть текст)"
    if n == 0:
        return "(без названия: пусто)"
    return "(без названия)"           # содержимое источник не показал


def _is_untitled(title: Optional[str]) -> bool:
    """Пустое ли название — вопрос ПРЕДОХРАНИТЕЛЯ: «нечего ли сверять».
    Одна точка на весь сервер: и None, и "", и строка из одних пробелов, их
    нельзя разводить по разным веткам, иначе сверка в одном месте признаёт
    задачу безымянной, а в соседнем — нет.

    ЗДЕСЬ НАМЕРЕННО НЕ ВЫЧИЩАЮТСЯ НУЛЕВОЙ ШИРИНЫ СИМВОЛЫ (аудит 2026-08-09).
    ТОЧНАЯ ГРАНИЦА, иначе докстринг обещает больше, чем делает `strip()`
    (уточнено 2026-08-09): пробельные по Unicode невидимки — неразрывный
    пробел U+00A0 и родня — `strip()`-ом СНИМАЮТСЯ, и имя из одного NBSP
    считается здесь отсутствующим. Не снимаются только символы нулевой ширины
    (U+200B и соседи, `_INVISIBLE`) — они не пробельные.
    Название из одного zero-width space — не пустота для сверки: оно
    ОТЛИЧАЕТСЯ от пустого живого названия, и признать его пустым значило бы
    пропустить в удаление объект, чьё имя разошлось с планом. Показу нужен
    другой вопрос — «выглядит ли строка пустой глазами», — и на него отвечает
    `_looks_untitled`. Обе ошибаются в безопасную сторону: показ щедрее (чаще
    подставляет заменитель), предохранитель строже (реже считает имя
    отсутствующим)."""
    return not str(title or "").strip()


def _looks_untitled(title: Optional[str]) -> bool:
    """Выглядит ли название пустым МЕСТОМ в списке — вопрос ПОКАЗА.

    Всё, что `_is_untitled`, плюс строки из одних невидимых символов
    (zero-width space и родня, `_INVISIBLE`): человек видит пустую строку
    независимо от того, что там за кодовые точки, и решение об удалении
    принимает по увиденному. Показ по truthiness («title or заменитель») этот
    класс пропускал целиком — задача с названием из пробелов и фотографией
    чека внутри печаталась пустотой (аудит 2026-08-09)."""
    s = str(title or "")
    for z in _INVISIBLE:
        s = s.replace(z, "")
    return not s.strip()


def _is_untitled_placeholder(expected: str, live: Dict) -> bool:
    """Узнаёт заменитель `_untitled_label`, ВОЗВРАЩЁННЫЙ ОБРАТНО как «название»
    (Д2, 2026-08-09).

    Модель читает список, видит у безымянной задачи заменитель
    («(без названия: 📎 1 файл)») и — как требуют описания инструментов
    («название сверяется с живым списком») — подставляет УВИДЕННОЕ обратно
    как `title` в вызов мутатора. Без этой проверки `_names_agree` сравнивает
    непустой заменитель с пустым живым `title` и отказывает «id указывает на
    «», а НЕ «(без названия: …)»» — то есть штрафует модель ровно за то, что
    она в точности повторила собственный вывод сервера. Чем аккуратнее
    воспроизведён список, тем вернее отказ.

    Распознаётся ЯВНО, не подстрокой («в expected есть „без названия"» —
    НЕТ): заменитель признаётся только когда ЖИВОЕ название пусто (иначе
    заменителю просто неоткуда взяться — у задачи есть своё имя) И
    переданная строка совпадает СИМВОЛ В СИМВОЛ с тем, что `_untitled_label`
    вычисляет для ЭТОГО ЖЕ живого объекта прямо сейчас (актуальные вложения/
    текст). Двоеточие внутри заменителя (см. `_untitled_label`, правка Д3)
    делает его узнаваемым побочно — оно не встречается ни во внешнем
    разделителе компактной строки, ни в обычных названиях-с-разделителями —
    но проверка всё равно идёт полным совпадением, а не по наличию символа."""
    if not _looks_untitled((live or {}).get("title")):
        return False
    return expected.strip() == _untitled_label(live).strip()


# Format a task object from TickTick for better display
def format_task(task: Dict, trash_state: Optional[bool] = None) -> str:
    """Format a task into a human-readable string (title first, ids at the end).

    trash_state: True = caller VERIFIED the task sits in the trash, False =
    verified it does not, None = not checked (the default — the official v1
    API carries no deletion flag at all, so most callers simply cannot know).
    """
    # «No title» здесь было ровно тем слепым пятном, что и пустая строка в
    # списке (П15, 2026-08-09): карточка задачи с чеком Home Depot выглядела
    # так же, как карточка пустышки. Заглушка заменена на заменитель, который
    # говорит, ЧТО в задаче есть.
    formatted = ("Title: " + (_untitled_label(task)
                              if _looks_untitled(task.get("title"))
                              else task["title"]) + "\n")

    # Dates are printed in the OWNER's zone (_local_datetime_str), never as the
    # raw UTC instant TickTick stores. A deadline near local midnight otherwise
    # renders as the wrong calendar day: 23:59 America/Los_Angeles is 06:59 UTC
    # the NEXT day, so the raw value contradicted the very filter that selected
    # the task (an "overdue" list showing today's date).
    if task.get('startDate'):
        formatted += f"Start Date: {_local_datetime_str(task, 'startDate')}\n"
    if task.get('dueDate'):
        formatted += f"Due Date: {_local_datetime_str(task, 'dueDate')}\n"

    # Add priority if available
    priority = task.get('priority', 0)
    formatted += f"Priority: {PRIORITY_MAP.get(priority, str(priority))}\n"
    
    # Status. A trashed task keeps whatever status it had before deletion, so
    # printing that field alone ("Active") is a lie about a task in the bin —
    # the deletion has to lead, with the raw field kept for reference.
    raw_status = task.get('status', 0)
    status = STATUS_MAP.get(raw_status, f"Unknown (raw: {raw_status})")
    if trash_state is True:
        formatted += f"Status: IN TRASH (deleted) — status field says «{status}»\n"
    else:
        formatted += f"Status: {status}\n"

    # Add content if available
    if task.get('content'):
        formatted += f"\nContent:\n{task.get('content')}\n"

    # Add subtasks if available
    items = task.get('items', [])
    if items:
        formatted += f"\nSubtasks ({len(items)}):\n"
        for i, item in enumerate(items, 1):
            mark = "✓" if item.get('status') == 1 else "□"
            formatted += f"{i}. [{mark}] {item.get('title', 'No title')}\n"

    # Ids last — needed for follow-up calls, but not the headline.
    formatted += f"(id: {task.get('id', '?')} | project: {task.get('projectId', '?')})\n"
    return formatted

def _identity_or_refusal(obj: Optional[Dict], wanted_id: str, kind: str,
                         missing: str = "не найдена", hint: str = "") -> Optional[str]:
    """None if `obj` really is the object with id `wanted_id`; otherwise the
    refusal text to return to the caller INSTEAD of a formatted card.

    Why this exists: the official v1 API answers an unknown id with an EMPTY
    body, and `_make_request` turns an empty body into `{}` (its normal
    convention for 204/empty). `{}` carries no `error` key, so a plain
    "if 'error' in resp" check let it through into format_task()/
    format_project(), where every field fell back to a placeholder — «No
    title», «No name», «(id: ?)» — and format_task's default status 0 printed
    a cheerful «Status: Active». The result was a plausible card for an object
    that does not exist, i.e. an answer from which «exists» and «does not
    exist» are indistinguishable.

    The id check (not just emptiness) is the same invariant
    `_official_task_snapshot()` already enforces: an object whose id isn't the
    requested one cannot be printed as an answer ABOUT the requested one.
    """
    if not isinstance(obj, dict) or not obj:
        return (f"🛑 {kind} с id «{wanted_id}» {missing}: TickTick вернул "
                f"пустой ответ. Такого объекта нет, либо id неверный.\n"
                f"Карточку не печатаю — выдумывать поля не буду." +
                (f"\n{hint}" if hint else ""))
    got = obj.get("id")
    if got != wanted_id:
        return (f"🛑 Не подтверждаю: по id «{wanted_id}» TickTick вернул объект "
                f"с ДРУГИМ id «{got}». Печатать его как запрошенный объект "
                f"нельзя." + (f"\n{hint}" if hint else ""))
    return None


def _format_folder_line(project: Dict, group_names: Optional[Dict] = None) -> str:
    """The "Folder:" line of format_project().

    Every read tool used to drop `groupId` on the floor, so a project's folder
    was invisible even though both clients return the field (official v1
    GET /project, v2 projectProfiles) and the server itself relies on it
    internally (move_project_to_group's post-verify). The four states are kept
    distinct on purpose — "no folder" must never look the same as "couldn't
    look the name up":

      Folder: Личное (id: g1)          — in a folder, name resolved
      Folder: (none)                   — genuinely not in any folder
      Folder: (unknown group) (id: g9) — has a groupId no live folder matches
      Folder: (name unavailable) (id: g9) — folder list couldn't be read at all

    `group_names` lets a caller formatting many projects resolve the folder
    map once instead of per project; None means "look it up yourself".
    """
    gid = project.get('groupId')
    # TickTick uses the literal string "NONE" to mean "ungroup" on writes and
    # may echo it back; treat it as "no folder", same as null/missing.
    if not gid or gid == "NONE":
        return "Folder: (none)\n"
    names = _v2_group_names() if group_names is None else group_names
    if names is None:
        return f"Folder: (name unavailable) (id: {gid})\n"
    name = names.get(gid)
    if not name:
        return f"Folder: (unknown group) (id: {gid})\n"
    return f"Folder: {name} (id: {gid})\n"


# Format a project object from TickTick for better display
def format_project(project: Dict, group_names: Optional[Dict] = None) -> str:
    """Format a project into a human-readable string (name first, id at the end).

    group_names: optional pre-resolved {groupId: name} map (see
    _format_folder_line) so a caller looping over many projects resolves the
    folder list once; omit it and the folder name is looked up per call.
    """
    formatted = f"Name: {project.get('name', 'No name')}\n"

    # Add color if available
    if project.get('color'):
        formatted += f"Color: {project.get('color')}\n"

    # Add view mode if available
    if project.get('viewMode'):
        formatted += f"View Mode: {project.get('viewMode')}\n"

    # Add closed status if available
    if 'closed' in project:
        formatted += f"Closed: {'Yes' if project.get('closed') else 'No'}\n"

    # Add kind if available
    if project.get('kind'):
        formatted += f"Kind: {project.get('kind')}\n"

    # Which folder (project group) the project sits in — always printed, so
    # "not in any folder" is visible instead of being an absent line.
    formatted += _format_folder_line(project, group_names)

    # Id last — needed for follow-up calls, but not the headline.
    formatted += f"(id: {project.get('id', '?')})\n"
    return formatted

_PRIO_SHORT = {0: "", 1: "P-Low", 3: "P-Med", 5: "P-High"}


def format_task_line(task: Dict, project_name: str = None) -> str:
    """One compact line per task — keeps tool outputs small so the model
    isn't forced to read multi-KB dumps for every list call."""
    bits = []
    if project_name:
        bits.append(f"[{project_name}]")
    # П15 (2026-08-09): «(no title)» не отличало документ от мусора — см.
    # _untitled_label. Три задачи владельца выглядели в списке одинаково, и
    # две из них были содержательными (чек Home Depot, скриншот дефекта).
    bits.append(_untitled_label(task) if _looks_untitled(task.get("title"))
                else task["title"])
    meta = []
    if task.get("dueDate"):
        # Owner's calendar day, not dueDate[:10] of the raw UTC instant — see
        # _local_calendar_date(). The old slice made a task due 23:59 local
        # print as tomorrow, inside a list titled "overdue".
        meta.append("due " + _local_date_str(task, "dueDate"))
    pr = _PRIO_SHORT.get(task.get("priority", 0))
    if pr:
        meta.append(pr)
    if task.get("tags"):
        meta.append(" ".join("#" + t for t in task["tags"]))
    # def-D5: статус не печатался НИКОГДА, поэтому в выводе
    # search_all_tasks(include_completed=True) сделанная задача выглядела
    # ровно как активная. Метка ставится только для НЕ-активных статусов,
    # чтобы обычные списки не раздувались.
    _status = task.get("status", 0)
    if _status == 2:
        meta.append("✅ completed")
    elif _status == -1:
        meta.append("✖ won't do")
    line = "- " + " ".join(bits)
    if meta:
        line += " · " + ", ".join(meta)
    return line + f"  (id:{task.get('id')} proj:{task.get('projectId')})"


def _v2_project_names_or_none() -> Optional[Dict]:
    """Like _v2_project_names(), but returns None — never {} — when every
    fetch path raised. _v2_project_names()'s plain {} return conflates "no
    projects" with "couldn't check"; callers that treat an empty result as
    CONFIRMED ABSENCE (delete_project's post-verify, _verify_item's
    "delete_project" branch) must not read a failed refetch as a successful
    deletion. Use this instead of _v2_project_names() anywhere the caller's
    next move depends on "did this really disappear" rather than just
    wanting a human-readable name for display."""
    if ticktick_v2:
        try:
            st = ticktick_v2.get_state()
            names = {p["id"]: p.get("name") for p in (st.get("projectProfiles") or [])}
            if st.get("inboxId"):
                names[st["inboxId"]] = "Inbox"
            return names
        except Exception:
            pass
    if ticktick:
        try:
            return {p.get("id"): p.get("name") for p in (ticktick.get_projects() or [])}
        except Exception:
            pass
    return None


def _v2_habits_or_none() -> Optional[List[Dict]]:
    """def-119 (2026-08-07): свежий список привычек для post-verify, или
    None (никогда []) когда чтение упало — тот же контракт «не путать пусто
    с недоступно», что у _v2_project_names_or_none чуть выше (см. её
    докстринг): нужен _verify_item's "delete_habit" branch, чтобы пустой
    список честно означал «привычки нет», а не «чтение не состоялось».
    Привычки есть только в v2 API — фолбэка на v1 нет."""
    if not ticktick_v2:
        return None
    try:
        return ticktick_v2.get_habits()
    except Exception:
        return None


def _v2_project_names() -> Dict:
    """Map projectId -> name (incl. Inbox) from the cached v2 state,
    falling back to the official v1 API so results stay human-readable
    even when v2 is unavailable."""
    if ticktick_v2:
        try:
            st = ticktick_v2.get_state()
            names = {p["id"]: p.get("name") for p in (st.get("projectProfiles") or [])}
            if st.get("inboxId"):
                names[st["inboxId"]] = "Inbox"
            if names:
                return names
        except Exception:
            pass
    # v1 fallback: one get_projects call — names instead of raw ids.
    if ticktick:
        try:
            return {p.get("id"): p.get("name") for p in (ticktick.get_projects() or [])}
        except Exception:
            pass
    return {}


def _group_names_from_state(st: Dict) -> Dict:
    """{groupId: folder name} out of an ALREADY-FETCHED v2 state snapshot.
    Split out of _v2_group_names() so a caller that needs several things from
    the state (get_projects: folders AND the Inbox) reads it once."""
    return {g["id"]: g.get("name")
            for g in (st.get("projectGroups") or [])
            if g.get("id") and not g.get("deleted")}


def _v2_group_names() -> Optional[Dict]:
    """Map groupId -> folder name from the cached v2 state, or None when the
    folder list could not be read at all.

    There is no v1 fallback on purpose: the official Open API exposes a
    project's `groupId` but has no endpoint listing the groups themselves, so
    without v2 the name is genuinely unknowable — and None ("couldn't check")
    must stay distinguishable from {} ("there are no folders"), exactly like
    _v2_project_names_or_none() vs _v2_project_names(). Deleted groups are
    dropped so a project pointing at one reads as "unknown group", not as a
    live folder.
    """
    if not ticktick_v2:
        return None
    try:
        return _group_names_from_state(ticktick_v2.get_state())
    except Exception:
        return None


def _inbox_from_state(st: Dict) -> Optional[Dict]:
    """The built-in Inbox as an ordinary project record, from an
    ALREADY-FETCHED v2 state snapshot (None when the state carries no
    inboxId).

    The official Open API has no concept of the Inbox at all: GET /project
    omits it, so a listing built from that endpoint alone reads as "there is
    no Inbox / nothing in it" while tasks quietly sit in it.

    The id is the v2 `inboxId` ("inbox<userId>") — deliberately NOT a made-up
    label: it is the very same id the rest of this server addresses the Inbox
    by (_v2_project_names, the `projectId` every Inbox task carries,
    get_inbox_tasks' filter, _guard_project's liveness check), so an id taken
    out of the listing actually works in the follow-up calls.
    """
    iid = st.get("inboxId")
    return {"id": iid, "name": "Inbox", "kind": "TASK"} if iid else None


def _inbox_project() -> Optional[Dict]:
    """_inbox_from_state() against the live (cached) v2 state; None when v2 is
    unavailable or the state can't be read."""
    if not ticktick_v2:
        return None
    try:
        return _inbox_from_state(ticktick_v2.get_state())
    except Exception:
        return None


def _v2_groups_and_inbox() -> tuple:
    """(folder map, Inbox record) from ONE read of the v2 state — the project
    listing needs both, and neither is worth a second state fetch. Either
    element is None when it isn't knowable (no v2 / unreadable state / no
    inboxId), exactly as the single-purpose helpers above return."""
    if not ticktick_v2:
        return None, None
    try:
        st = ticktick_v2.get_state()
    except Exception:
        return None, None
    return _group_names_from_state(st), _inbox_from_state(st)


def _lookup_task_title(task_id: str) -> str:
    """Return the task's title from the v2 cache, or a fallback string.

    ЗАДАЧА НАЙДЕНА, А ИМЕНИ У НЕЁ НЕТ — это НЕ «не нашли» (П15, 2026-08-09):
    раньше обе ветки давали `[task 6a757123…]`, то есть в позицию имени
    попадал идентификатор, и безымянная задача с фотографией чека Home Depot
    выглядела в каждом сообщении так же, как задача, которой вообще нет.
    Найденная безымянная называется тем, ЧТО в ней лежит (_untitled_label);
    `[task …]` остаётся только для по-настоящему ненайденной."""
    if ticktick_v2:
        try:
            t = next((x for x in ticktick_v2.get_open_tasks()
                      if x.get("id") == task_id), None)
            if t:
                return (_untitled_label(t) if _looks_untitled(t.get("title"))
                        else t["title"])
        except Exception:
            pass
    return f"[task {task_id[:8]}…]"


def _resolve_project_id(task_id: str, given: str) -> str:
    """Return the task's CURRENT projectId. After a move_task the caller often
    still holds the old projectId, and the official API silently no-ops an
    update/complete/delete with a mismatched projectId. Look up the real one
    from the (cache-fresh) v2 state when available; fall back to `given`."""
    if ticktick_v2:
        try:
            t = next((x for x in ticktick_v2.get_open_tasks()
                      if x.get("id") == task_id), None)
            if t and t.get("projectId"):
                return t["projectId"]
        except Exception:
            pass
    return given


# Returned by guards / shown by post-verify when the v2 state can't be read:
# a failed fetch must NEVER be confused with "the task is gone" (fail CLOSED).
_STATE_UNAVAILABLE_MSG = (
    "🛑 Не могу сверить — состояние TickTick недоступно (v2 не отвечает или "
    "не настроен). Ничего не тронул.")
_UNVERIFIED_MSG = ("⚠️ Исход НЕ ПОДТВЕРЖДЁН — состояние TickTick недоступно, "
                   "проверь вручную.")


def _open_by_id(fresh: bool = False) -> Optional[Dict[str, Dict]]:
    """{taskId: task} of the v2 OPEN-task state, or None when the state is
    UNAVAILABLE (v2 not configured, or the fetch failed). None ≠ {}: an empty
    dict means «no open tasks», None means «cannot know» — mutation guards must
    fail CLOSED on None, and post-verify must say UNVERIFIED instead of
    treating absence-from-nothing as success.
    fresh=True forces an uncached refetch (get_state(force=True)), so a
    just-renamed/moved/completed task is seen — used by the write guard so it
    never checks against a stale title, and by post-verify so a concurrent
    reader can't repopulate the cache with a pre-write snapshot."""
    if not ticktick_v2:
        return None
    try:
        if fresh:
            ticktick_v2.get_state(force=True)
        return {t.get("id"): t for t in ticktick_v2.get_open_tasks() if t.get("id")}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Identity-guard fallback: point-read via the OFFICIAL Open API.
#
# ПОЧЕМУ ЭТО ЗДЕСЬ (живой прогон 2026-08-07 00:03 PT, move_tasks, задача
# «__AUTOTEST__dup-src», id 6a7571238f0854e347f51407 — обычная задача, НЕ
# дубликат): в 23:38 PT она была штатно перемещена в проект X (перемещение
# реально состоялось). В 00:03 PT — через 25 МИНУТ — попытка переместить её
# обратно была отвергнута identity-guard'ом («не найдена среди открытых»),
# хотя та же задача читалась МГНОВЕННО и напрямую через get_project_tasks и
# get_task (оба — официальный Open API) сразу после отказа. Это НЕ та же
# гонка, что чинит _POSTVERIFY_RETRY_DELAYS_S/_reread_open_until (пара
# секунд лага сразу после мутации, до ~9с ретраев) — 25 минут не влезают ни
# в какое разумное окно ретраев одного и того же источника. Причина глубже:
# _open_by_id/_guard_task читают ТОЛЬКО через неофициальный v2 web-API
# (`ticktick_v2`, /batch/check/0) — та же задача может надолго выпасть из
# ЭТОЙ выборки «открытых задач» и никаким количеством повторных чтений ЕЁ ЖЕ
# это не исправить (см. также test_identity_guard_lookup.py). Официальный
# Open API — СОВСЕМ ДРУГОЙ backend (OAuth, /project/{id}/task/{id}) — этой
# проблеме не подвержен: он и увидел задачу мгновенно в живом прогоне.
#
# Поэтому guard, прежде чем объявить задачу отсутствующей СРЕДИ ОТКРЫТЫХ,
# пробует один точечный запрос к официальному API — но ТОЛЬКО когда v2-
# выборка уже не нашла задачу (нулевая цена на счастливом пути, который
# остаётся таким же, как был) и только как ДОПОЛНИТЕЛЬНЫЙ источник данных:
# сама сверка id/названия/контейнера (_guard_task ниже) не меняется — меняется
# только СПОСОБ добыть объект для этой сверки.
def _official_task_read(project_id: str, task_id: str) -> Optional[Dict]:
    """Single-task point read via the OFFICIAL Open API (`GET
    /project/{projectId}/task/{taskId}`), WHATEVER the task's status —
    the raw "does this id name a real task, and which one" question.

    Requires the CURRENT project_id (the official endpoint 404s on a stale
    one). Returns a dict shaped like a v2 task ({id, title, projectId,
    status, …}) on success, None on ANY failure: no official client
    configured, no project_id given, a network/HTTP error, a 404 (wrong
    container or truly gone), or an id that doesn't match what was asked
    for (the same invariant `_identity_or_refusal` enforces: an object
    whose id isn't the requested one can never answer FOR it).

    NOTE the deliberate absence of a status filter here. Two DIFFERENT
    questions used to share one function, and that was defect №1 (live
    acceptance 2026-08-07): "may I touch this?" (identity guard — completed
    tasks are out of the OPEN pool by policy, see _official_task_snapshot
    right below) versus "what is this called?" (display — a completed
    task's name is perfectly well known and refusing to print it protects
    nobody). For a guard an empty answer means «I won't risk it»; for a
    display path it means «I don't know». One function cannot serve both,
    so it no longer does: guards go through _official_task_snapshot, which
    keeps the OPEN restriction; display goes through this one."""
    if not ticktick or not project_id or not task_id:
        return None
    try:
        t = ticktick.get_task(project_id, task_id)
    except Exception:
        return None
    if not isinstance(t, dict) or "error" in t:
        return None
    if t.get("id") != task_id:
        return None
    return t


def _official_task_snapshot(project_id: str, task_id: str) -> Optional[Dict]:
    """Single-task fallback read via the OFFICIAL Open API — used by the
    identity guard ONLY when the v2 open-task snapshot didn't have the task.
    Thin OPEN-only wrapper over `_official_task_read` above.

    Returns a dict shaped like a v2 task on success, and None on everything
    `_official_task_read` rejects PLUS one more case: a task that exists but
    is no longer OPEN (completed/won't-do). That OPEN restriction mirrors
    what _open_by_id already enforces via get_open_tasks(), so this fallback
    can't accidentally make the guard MORE permissive than the primary path,
    only more resilient to the primary path's staleness. It is a guard
    policy, NOT a fact about the data — anything that merely needs to NAME
    the task must call `_official_task_read` instead."""
    t = _official_task_read(project_id, task_id)
    if t is None:
        return None
    if t.get("status", 0) != 0:
        return None  # completed / won't-do — not part of the OPEN pool
    return t


def _official_task_scan(task_id: str, *, open_only: bool = True) -> Optional[Dict]:
    """Last-resort identity-guard fallback for callers that don't have a
    project_id at all (e.g. abandon_task/duplicate_task take no project_id
    argument) — scans every official project for the task, same rationale
    as _official_task_snapshot above just without a container to target
    directly. Reuses the same "iterate official projects" pattern already
    used by search_tasks'/get_recurring_tasks' no-v2 fallback. Only ever
    reached after the v2 snapshot already came up empty, so the extra HTTP
    calls are paid exclusively on that already-degraded path, never on the
    common happy path. Returns None (never raises) on any failure.

    `open_only=False` drops the OPEN restriction — for the callers whose
    object of interest is a task that may legitimately be COMPLETED (see
    _closed_task_snapshot). Guards keep the default."""
    if not ticktick:
        return None
    try:
        projects = ticktick.get_projects()
    except Exception:
        return None
    if not isinstance(projects, list):
        return None
    read = _official_task_snapshot if open_only else _official_task_read
    for p in projects:
        pid = p.get("id") if isinstance(p, dict) else None
        if not pid:
            continue
        t = read(pid, task_id)
        if t:
            return t
    return None


def _closed_task_snapshot(task_id: str, project_id: str = "") -> Optional[Dict]:
    """The live record of a task that is NOT among the open ones — completed,
    won't-do, or in the trash. Returns None only when no source knows it.

    Source picked to fit the OBJECT of the operation, not out of habit — the
    pattern `restore_tasks` already follows in this repo by checking the
    TRASH rather than the open pool. For a class of operations that are
    legitimate ON A COMPLETED TASK (attach the receipt to the finished job,
    append the outcome, duplicate it as a template), the open-task snapshot
    is simply the wrong place to look:
      1. the official point read (`GET /project/{pid}/task/{tid}`) — knows a
         task of ANY age and ANY status, but needs the CURRENT project_id;
      2. v2's `find_task_any_state` — completed feed, then trash; these are
         PAGES with a ceiling (100/500), so they cover the recent past, not
         all history, which is why they come after the point read;
      3. an official all-projects scan, but ONLY when no project_id was given
         at all (duplicate_task/abandon_task take none) — it costs one request
         per project, so it is never paid when step 1 could have run.

    LIMIT worth stating where callers phrase their refusals: none of these
    sources is an index. A task that is neither open, nor readable at its
    given project_id, nor in the recent completed/trash pages comes back as
    None — which honestly means "not found in any source we have", not
    "provably does not exist"."""
    if project_id:
        t = _official_task_read(project_id, task_id)
        if t:
            return t
    if ticktick_v2:
        try:
            found, _where = ticktick_v2.find_task_any_state(task_id)
            if found:
                return found
        except Exception:
            pass
    if not project_id:
        return _official_task_scan(task_id, open_only=False)
    return None


def _live_task_title(task_id: str, project_id: str = "") -> Optional[str]:
    """Живое НАЗВАНИЕ задачи по её id — для КАРТОЧКИ подтверждения, чтобы
    человек читал «в задачу «Купить молоко»», а не 24 hex-символа, которые
    глазами не сверяет никто.

    Возвращает None, когда имя установить не удалось. Это НЕ отказ и не
    identity-guard: вызывающий обязан сказать про неудачу вслух в тексте
    карточки (молчаливый показ сырого id и был дефектом), но продолжить —
    защиту от «не той задачи» здесь строить не на чем, вызывающему не
    передают ожидаемое название, сверять нечего с чем.

    Цена намеренно минимальная: сначала УЖЕ закэшированный v2-снапшот
    открытых задач (fresh=False — карточке хватает состояния возрастом
    ≤20 c, force-refetch ради отображаемого имени не оправдан); если задачи
    там нет — одно точечное чтение официального API, когда известен
    project_id; и последним — ленты завершённых/корзины v2
    (`find_task_any_state`, до двух запросов, результат кэшируется на тот же
    TTL). Полного скана аккаунта (_official_task_scan, запрос на КАЖДЫЙ
    проект) здесь НЕТ намеренно: он оправдан для guard'а, решающего
    «трогать или нет», а не для строчки в превью.

    Последний шаг добавлен 2026-08-07 по аудиту фикса №1: без него дефект
    был закрыт только для случая «project_id передан», а
    create_attachment_upload_url его не требует — и завершённая задача без
    project_id снова печаталась как «⚠️ НАЗВАНИЕ ЗАДАЧИ УСТАНОВИТЬ НЕ
    УДАЛОСЬ» в единственной карточке, выдающей право ЗАПИСИ в аккаунт.

    Точечное чтение идёт через `_official_task_read` — БЕЗ фильтра «только
    открытые». Дефект №1 (живая приёмка 2026-08-07) был ровно в том, что
    здесь звался `_official_task_snapshot`, у которого этот фильтр есть по
    guard-политике: две карточки, отличавшиеся ТОЛЬКО статусом задачи,
    давали «в задачу «__AUTOTEST__upd-B1» (id …)» для открытой и «в задачу
    id 6a7571238f0854e347f51407 — ⚠️ НАЗВАНИЕ ЗАДАЧИ УСТАНОВИТЬ НЕ УДАЛОСЬ»
    для завершённой. Имя завершённой задачи известно, и отказ его печатать
    не защищал ничего — он лишь прятал от подтверждающего, КУДА ляжет файл.
    Ветка «имя не установить» осталась на месте для настоящих случаев:
    задачи нет вовсе, или чтение недоступно.

    ЗАДАЧА НАЙДЕНА, А ИМЕНИ У НЕЁ НЕТ — это НЕ «не установили» (П15,
    2026-08-09): такая задача возвращает заменитель по содержимому («(без
    названия: 📎 1 файл)»), а None остаётся ровно за двумя настоящими
    случаями выше. Иначе карточка, ВРУЧАЮЩАЯ право записи в аккаунт, говорила
    про существующую задачу «её нет в живом состоянии аккаунта»."""
    try:
        by_id = _open_by_id(fresh=False)
    except Exception:
        by_id = None
    live = (by_id or {}).get(task_id)
    if not live and project_id:
        live = _official_task_read(project_id, task_id)
    if not live and ticktick_v2:
        try:
            live, _where = ticktick_v2.find_task_any_state(task_id)
        except Exception:
            live = None
    if not live:
        return None
    return (_untitled_label(live) if _looks_untitled(live.get("title"))
            else live["title"].strip())


def _locate_task_any_state(task_id: str) -> Tuple[Optional[Dict], Optional[str], bool]:
    """ГДЕ задача сейчас — по всем трём лентам v2: (задача, где, прочиталось).

    `где` ∈ {"open", "completed", "trash", None}; третий элемент — УДАЛОСЬ ЛИ
    ВООБЩЕ ПРОЧИТАТЬ. Это разные вещи, и путать их нельзя: (None, None, True)
    значит «искали везде и не нашли», а (None, None, False) — «спросить не
    получилось». Первое имеет право стать ❌, второе — только ⚠️ «не
    подтверждено».

    Нужно там, где «нет среди открытых» НЕ равно «операция провалилась»:
    задача, завершённая ДО удаления, возвращается из корзины ЗАВЕРШЁННОЙ и в
    снимок открытых (/batch/check/0) не попадает никогда — пост-проверка,
    которая смотрит только туда, называла успешное восстановление
    «❌ НЕ восстановлено» (живая приёмка 2026-08-07)."""
    if not ticktick_v2:
        return None, None, False
    try:
        found, where = ticktick_v2.find_task_any_state(task_id)
        return found, where, True
    except Exception as e:
        logger.warning(f"не удалось определить состояние задачи "
                       f"{str(task_id)[:8]}…: {e}")
        return None, None, False


# ---------------------------------------------------------------------------
# НАЗЫВАТЬ ОБЪЕКТ, А НЕ ПОКАЗЫВАТЬ ЕГО id.
#
# Класс дефекта, добитый 2026-08-07 живой приёмкой. Describe-функции гейта
# печатали `t.get('title') or taskId` — то есть при не переданном названии в
# позицию НАЗВАНИЯ, внутрь кавычек, попадали 24 hex-символа: «**«6a7571238f
# 0854e347f51407»**». Человек читает это как имя и не может сверить, тот ли
# объект; id глазами не сверяет никто. То же было с проектом назначения
# (`to_project_name or to_project_id`) и с родительской задачей
# (`parent_task_title or parent_task_id`).
#
# ПРАВИЛО: идентификатор РЯДОМ с именем — нормально; идентификатор ВМЕСТО
# имени — дефект. Не удалось установить имя — сказать это ВСЛУХ, а не
# показывать id молча (молчание читается как «имя такое и есть»).
_NO_NAME_TASK = ("⚠️ НАЗВАНИЕ ЗАДАЧИ УСТАНОВИТЬ НЕ УДАЛОСЬ (её нет в живом "
                 "состоянии аккаунта или оно недоступно) — сверить глазами, "
                 "та ли это задача, нельзя")
_NO_NAME_PROJECT = ("⚠️ НАЗВАНИЕ СПИСКА УСТАНОВИТЬ НЕ УДАЛОСЬ (его нет в "
                    "живом состоянии аккаунта или оно недоступно) — сверить "
                    "глазами, тот ли это список, нельзя")
_NO_NAME_PERSON = ("⚠️ ИМЯ УЧАСТНИКА УСТАНОВИТЬ НЕ УДАЛОСЬ (его нет среди "
                   "участников проекта или список недоступен)")


def _plan_task_titles(tasks: Optional[List[Dict]], *,
                      source: str = "open") -> Dict[str, Dict[str, Any]]:
    """{taskId: {"label": как назвать, "untitled": имени у задачи НЕТ}} для
    строк карточки плана.

    ДВА РАЗНЫХ ИСХОДА, КОТОРЫЕ РАНЬШЕ БЫЛИ ОДНИМ (П15, 2026-08-09). Функция
    возвращала только непустые имена, поэтому «задачи не нашли» и «задачу
    нашли, но названия у неё нет» приходили к `_plan_task_name` одинаково —
    отсутствием ключа, — и карточка плана в обоих случаях писала «⚠️ НАЗВАНИЕ
    ЗАДАЧИ УСТАНОВИТЬ НЕ УДАЛОСЬ (её нет в живом состоянии аккаунта или оно
    недоступно)». Про безымянную задачу это неправда: она есть, её карточка
    читается, из неё скачивается вложение (случай трёх задач владельца, среди
    них фотография чека Home Depot на возврат $374.92). Теперь второй исход
    называет себя сам — заменителем из `_untitled_label` и признаком
    `untitled`, по которому строка плана говорит про опознание по id.

    Платит ТОЛЬКО за то, чего не хватает: если вызывающий дал название
    каждому элементу (обычный случай — этого требуют докстринги тулов),
    возвращается пустой словарь и не делается ни одного запроса. Иначе —
    ОДНО чтение на весь батч, а не по чтению на строку.

    `source="open"` — снимок открытых задач (кэш ≤20 c: карточке хватает, а
    force-refetch ради отображаемого имени не оправдан). `source="trash"` —
    корзина, для restore_tasks: его задачи по определению не открыты, и
    искать их в открытых бессмысленно.

    BEST-EFFORT: недоступное чтение не имеет права ронять фазу плана — тогда
    пустой словарь, и строка честно скажет, что имя неизвестно (см.
    `_plan_task_name`), а не выдаст незнание за имя."""
    need = {str(t.get("taskId") or t.get("task_id") or "")
            for t in (tasks or []) if _looks_untitled(t.get("title"))}
    need.discard("")
    if not need:
        return {}
    try:
        if source == "trash":
            rows = ticktick_v2.get_trash(_TRASH_SCAN_LIMIT) if ticktick_v2 else []
            live = {r.get("id"): r for r in (rows or [])}
        else:
            live = _open_by_id(fresh=False) or {}
    except Exception as e:
        logger.warning(f"карточка плана: живые названия ({source}) прочитать "
                       f"не удалось ({e}) — строки скажут об этом вслух")
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for tid in need:
        row = live.get(tid)
        if row is None:
            continue                      # задачи нет — это другой исход
        out[tid] = ({"label": _untitled_label(row), "untitled": True}
                    if _looks_untitled(row.get("title")) else
                    {"label": row["title"].strip(), "untitled": False})
    return out


# Пометка для строки плана, объекту которой имени НЕ ПОЛОЖЕНО (П15,
# 2026-08-09). Опознание по id — законный случай, но молчать о нём нельзя:
# человек, читающий план, обязан видеть, ЧЕМ сверялась личность объекта, и
# ⚠️ здесь не годится (это не сомнение в проверке, а факт о состоянии
# объекта — см. правило ⚠️/ℹ️ у _COMPLETED_TASK_NOTE).
_BY_ID_NOTE = ("ℹ️ опознана ПО id: названия у задачи нет, сверять нечего — "
               "возможно, стоит сначала её назвать")


def _plan_task_name(t: Dict[str, Any],
                    titles: Optional[Dict[str, Any]] = None) -> str:
    """Кусок строки карточки, НАЗЫВАЮЩИЙ задачу: «Купить молоко»; для задачи
    БЕЗ названия — заменитель по её содержимому плюс признание, что личность
    сверена по id; и только если про задачу не известно ничего — id вместе с
    прямым признанием, что имя неизвестно."""
    tid = str(t.get("taskId") or t.get("task_id") or "")
    # `_looks_untitled`, а не truthiness: переданное имя из пробелов/невидимых
    # символов напечаталось бы пустотой внутри кавычек (аудит 2026-08-09).
    if not _looks_untitled(t.get("title")):
        return f"«{t['title'].strip()}»"
    entry = (titles or {}).get(tid)
    if isinstance(entry, str):            # старый вид значения — только имя
        entry = {"label": entry, "untitled": False}
    if not entry:
        return f"id {tid} — {_NO_NAME_TASK}"
    if entry.get("untitled"):
        return f"«{entry['label']}» — {_BY_ID_NOTE}"
    return f"«{entry['label']}»"


def _plan_project_name(project_id: str, given: str = "") -> str:
    """То же для проекта/списка. Живое написание предпочтительнее
    переданного даже когда переданное есть: карточка обязана показывать
    состояние аккаунта, а не пересказ вызывающего (тот же довод, что в
    `_describe_create_project_column`)."""
    live = (_v2_project_names_or_none() or {}).get(project_id) if project_id else None
    name = ((live or "") or (given or "")).strip()
    return f"«{name}»" if name else f"id {project_id} — {_NO_NAME_PROJECT}"


def _member_names(project_id: str) -> Dict[str, str]:
    """{userId: отображаемое имя} участников проекта — best-effort (пустой
    словарь, когда проект не расшарен или список недоступен)."""
    if not project_id or not ticktick_v2:
        return {}
    out: Dict[str, str] = {}
    try:
        for m in (ticktick_v2.get_project_members(project_id) or []):
            uid = str(m.get("userId") or m.get("userCode") or "")
            nm = m.get("displayName") or m.get("username")
            if uid and nm:
                out[uid] = nm
    except Exception as e:
        logger.warning(f"участники проекта {project_id} недоступны: {e}")
    return out


def _column_names(project_id: str) -> Dict[str, str]:
    """{columnId: имя раздела} проекта — best-effort, тем же правилом, что
    `_member_names` выше: недоступность списка не должна ронять чтение."""
    if not project_id or not ticktick_v2:
        return {}
    try:
        return {c.get("id"): (c.get("name") or c.get("title") or "")
                for c in (ticktick_v2.get_project_columns(project_id) or [])
                if c.get("id")}
    except Exception as e:
        logger.warning(f"разделы проекта {project_id} недоступны: {e}")
        return {}


def _person_label(user_id, names: Dict[str, str]) -> str:
    """«Ирина (userId: 333444)» — имя, а рядом id (он реально нужен: именно
    его кладут в поле assignee). Имя неизвестно — сказать это вслух."""
    uid = str(user_id or "")
    name = (names.get(uid) or "").strip()
    return f"{name} (userId: {uid})" if name else f"userId {uid} — {_NO_NAME_PERSON}"


# Сколько раз и с какой паузой повторно перечитывать живое состояние ПОСЛЕ
# identity-changing мутации (меняет проект/родителя/группу задачи —
# move_tasks, set_task_parent, unset_task_parent, move_project_to_group,
# restore_tasks), прежде чем окончательно признать расхождение.
#
# ПОЧЕМУ ЭТО ЗДЕСЬ (дефект №4, живой прогон 2026-08-06, move_tasks, манифест
# 58afe2beeea7): задача «__AUTOTEST__dup-src (copy)» РЕАЛЬНО переместилась —
# независимое чтение через get_project_tasks (ОФИЦИАЛЬНЫЙ Open API,
# ticktick.get_project_with_data) сразу после нажатия ✅ увидело её в новом
# проекте, поле projectId на самой задаче обновилось. Но и собственный
# post-verify _move_tasks_impl, и независимая перепроверка operation_report
# читают состояние через СОВСЕМ ДРУГОЙ backend — неофициальный v2 web-API
# (`ticktick_v2`, /batch/check/0), ТЕМ ЖЕ, которым сам move и был сделан —
# и оба, прочитав его СРАЗУ после мутации, ещё видели задачу как «не среди
# открытых вовсе» (ни в старом, ни в новом проекте — а не «нашлась, но не
# там»). Сравнение против места назначения (`projectId == to_project_id` /
# `parentId == parent_task_id` / `groupId == group_id`) уже было и остаётся
# ПРАВИЛЬНЫМ — это подтверждено и чтением кода, и прогоном unit-тестов с
# управляемым фейковым состоянием (see tests/test_move_tasks_postverify.py).
# Расхождение — не в логике сравнения, а в том, что моментальное чтение
# СРАЗУ после записи в /batch/check/0 может на секунду-две отставать от уже
# состоявшейся мутации, сделанной через тот же API мгновением раньше.
#
# Ретрай НИКОГДА не ослабляет проверку: критерий успеха («там, куда
# перемещали») остаётся тем же самым строгим сравнением на КАЖДОЙ попытке —
# он просто даёт TickTick время «долиться», прежде чем звать расхождение
# окончательным. Цена в общем случае — НОЛЬ: если состояние уже свежее (как
# почти всегда), первое же чтение проходит проверку и цикл завершается без
# единой лишней паузы. Настоящий провал (мутация правда не применилась)
# по-прежнему остаётся ❌ — просто после исчерпания расписания, а не после
# одной попытки.
#
# ОБНОВЛЕНО (дефект №4, ПОВТОРНЫЙ живой прогон 2026-08-06 23:38 PT, УЖЕ на
# фиксе выше, манифест b87428f1b426): окно в 2 попытки по 0.7с (макс. 1.4с
# ожидания, 3 чтения) оказалось СНОВА недостаточным — задача
# «__AUTOTEST__dup-src» реально переместилась (независимо подтверждено
# немедленным чтением через get_project_tasks, ОФИЦИАЛЬНЫЙ Open API), но обе
# v2-перепроверки (_move_tasks_impl И independent operation_report) отчитались
# ❌ «не найдена среди открытых» — то есть реальное отставание v2-синка
# TickTick иногда превышает 1.4с. Переход на официальный Open API вместо
# расширения окна РАССМОТРЕН и ОТКЛОНЁН как основной путь: официальный клиент
# (ticktick_client.py) физически не знает о project groups/папках (нет
# groupId — move_project_to_group немыслим без него) и о корзине/restore
# (get_trash/restore_task есть только в ticktick_v2_client.py) — то есть
# Open API не покрывает 2 из 6 точек ретрая ВООБЩЕ, а собирать пост-проверку
# из двух разных backend'ов (Open API для part точек, v2 для остальных) —
# больше риска (два разных пути дают два разных класса багов) при частичной
# выгоде. Вместо этого — то же самое решение, но с бОльшим и НЕРАВНОМЕРНЫМ
# бюджетом ожидания: список пауз вместо одного числа попыток×паузы,
# экспоненциально нарастающий (короткие первые попытки покрывают типичную
# долю секунды лага почти без задержки отчёта; редкий длинный хвост лага —
# длинными последними). Суммарно ~9с в худшем случае (задача реально не
# такая уж редкая по докладу Максима — второй раз подряд на одном и том же
# инструменте), но эта пауза срабатывает ТОЛЬКО когда чтение всё ещё
# расходится с ожиданием; счастливый путь как был, так и остаётся без единой
# лишней паузы.
_POSTVERIFY_RETRY_DELAYS_S: Tuple[float, ...] = (0.5, 1.0, 1.5, 2.5, 3.5)
# Бэккомпат-алиас для мест/тестов, которым нужно только число попыток
# (например верхнеуровневый цикл _build_operation_report ниже).
_POSTVERIFY_RETRY_ATTEMPTS = len(_POSTVERIFY_RETRY_DELAYS_S)


def _reread_open_until(check, delays: Tuple[float, ...] = _POSTVERIFY_RETRY_DELAYS_S,
                       ) -> Optional[Dict[str, Dict]]:
    """_open_by_id(fresh=True), retried on a bounded, growing-pause schedule
    while `check(live_map) is False` — see the block comment above for why
    this exists. `check` receives the freshly re-fetched {taskId: task} map
    and returns True once it confirms whatever the caller is waiting to see
    (e.g. every moved task's projectId now equals the destination).

    `delays` is the pause taken BEFORE each retry read, in order — e.g.
    (0.5, 1.0) means: read, and if `check` is still False sleep 0.5s and
    read again, and if STILL False sleep 1.0s and read a third and final
    time. len(delays) is the max number of retries (reads = 1 + len(delays)
    in the worst case); an empty tuple disables retrying entirely (single
    read, exactly the old no-retry behaviour — used by tests to prove a
    narrower window would have failed where the real schedule succeeds).

    Returns the LAST fetched map regardless of outcome — None only when a
    fetch itself failed (v2 unavailable), exactly like a plain
    `_open_by_id(fresh=True)` call; a caller's existing None-handling (fail
    UNVERIFIED, never confused with "check still false") is untouched. The
    caller re-runs its OWN strict comparison against whatever this returns —
    this helper only decides how many times to look, never what counts as
    success."""
    fresh = _open_by_id(fresh=True)
    for delay in delays:
        if fresh is None or check(fresh):
            break
        time.sleep(delay)
        fresh = _open_by_id(fresh=True)
    return fresh


def _reread_projects_until(check, delays: Tuple[float, ...] = _POSTVERIFY_RETRY_DELAYS_S,
                           ) -> List[Dict]:
    """Project-list analogue of _reread_open_until — used only by
    move_project_to_group's post-verify, which checks a PROJECT's live
    groupId rather than a task's projectId/parentId, so it can't share the
    task-keyed map _open_by_id builds. Same contract: `check` gets the fresh
    project list and returns True once it confirms the expected groupId;
    retried on the same growing-pause schedule and for the same reason (a v2
    /batch/check/0 re-read can lag behind a write just made through the same
    v2 API — see the block comment above _POSTVERIFY_RETRY_DELAYS_S)."""
    ticktick_v2.get_state(force=True)
    projs = ticktick_v2.list_projects()
    for delay in delays:
        if check(projs):
            break
        time.sleep(delay)
        ticktick_v2.get_state(force=True)
        projs = ticktick_v2.list_projects()
    return projs


# TickTick's own apps hard-cap nested subtasks at 5 total levels (root task =
# level 1, subtask = level 2, sub-subtask = level 3, … down to level 5) —
# confirmed 2026-08-06 against TickTick's official help center, "Multilevel
# Tasks" article, FAQ "Does multi-level task support unlimited splitting?":
# «At present, we only allow up to 5 levels of nested tasks. If the limit is
# exceeded, you cannot continue to add.» That cap lives in TickTick's own
# apps' UI, NOT in the v2 API this server calls directly — so a tree built
# here could silently exceed what TickTick's own apps can display or let you
# edit further. We enforce the same real cap here instead, fail-closed,
# always from LIVE parentId chains (never from what a caller merely claims).
MAX_TASK_NEST_LEVELS = 5


def _task_level(task_id: str, by_id: Dict[str, Dict]) -> int:
    """1-based nesting level of an EXISTING task from its live parentId chain
    (a root task with no parent = level 1). Cycle-safe: a corrupt/circular
    chain stops counting instead of looping forever."""
    level = 1
    seen = {task_id}
    cur = (by_id.get(task_id) or {}).get("parentId")
    while cur and cur not in seen:
        level += 1
        seen.add(cur)
        cur = (by_id.get(cur) or {}).get("parentId")
    return level


def _children_index(by_id: Dict[str, Dict]) -> Dict[str, List[str]]:
    """{parentId: [childId, …]} built once from the live open-task map, for
    walking a task's DOWNWARD subtree (depth-adding checks)."""
    idx: Dict[str, List[str]] = {}
    for tid, t in by_id.items():
        pid = t.get("parentId")
        if pid:
            idx.setdefault(pid, []).append(tid)
    return idx


def _subtree_height(task_id: str, children_of: Dict[str, List[str]],
                    _seen: Optional[set] = None) -> int:
    """How many levels task_id's OWN live subtree already spans (a leaf task
    counts as 1). A task being re-parented may already have descendants of
    its own — moving it deeper drags them along, so the depth check must be
    against what it would ADD below the new parent, not just its own level."""
    _seen = (_seen or set()) | {task_id}
    kids = [k for k in (children_of.get(task_id) or []) if k not in _seen]
    if not kids:
        return 1
    return 1 + max(_subtree_height(k, children_of, _seen) for k in kids)


# Zero-width / variation-selector chars that can silently differ between two
# otherwise-identical titles (emoji VS16, ZWJ, ZWSP, BOM).
_INVISIBLE = ("️", "‍", "​", "﻿", "‎", "‏")


def _norm_name(s: str) -> str:
    """Comparison key for a title/project name. NFKC-normalise, drop invisible
    joiners, lowercase, collapse inner whitespace, strip leading/trailing
    non-word chars — so a control marker («👁 »), emoji, surrounding punctuation,
    case and spacing never cause a false mismatch, while the meaningful text is
    preserved exactly."""
    s = unicodedata.normalize("NFKC", s or "")
    for z in _INVISIBLE:
        s = s.replace(z, "")
    s = re.sub(r"\s+", " ", s.lower()).strip()
    return re.sub(r"^[\W_]+|[\W_]+$", "", s, flags=re.UNICODE)


def _norm_loose(s: str) -> str:
    """Looser comparison key: NFKC + drop invisibles + lowercase + collapse
    whitespace, but KEEP emoji/punctuation. Used when a title consists ONLY of
    symbols («🔥», «???») — stripping \\W would erase the whole claim and
    silently disarm the guard."""
    s = unicodedata.normalize("NFKC", s or "")
    for z in _INVISIBLE:
        s = s.replace(z, "")
    return re.sub(r"\s+", " ", s.lower()).strip()


def _names_agree(expected: str, actual: str) -> bool:
    """True if a caller-supplied name matches the live one. Empty expected → no
    claim to verify (True). Otherwise EXACT match after normalisation — NOT a
    loose substring: «Позвонить» must NOT match «Позвонить Пете», and different
    numbers/amounts ($10 000 vs $11 000) fail. Marker/case/space differences pass.
    An emoji/punctuation-only claim («🔥») does NOT disarm the check: it is
    compared loosely (case/space-insensitive) against the raw actual title."""
    if not (expected or "").strip():
        return True
    a = _norm_name(expected)
    if not a:
        # The claim normalises to nothing (emoji/punct-only) — compare the
        # raw strings loosely instead of returning True.
        return _norm_loose(expected) == _norm_loose(actual)
    return a == _norm_name(actual)


def _task_is_in_trash(task_id: str) -> bool:
    """Лежит ли задача в КОРЗИНЕ. False и когда её там нет, и когда спросить
    не удалось.

    Источник один — v2 (`_locate_task_any_state` → `find_task_any_state`,
    результат кэшируется по id): официальный Open API про удаление не знает
    вообще, он отдаёт корзинную задачу как живую (см. `_trash_state`).

    ПОЧЕМУ НЕ FAIL-CLOSED. Сбой чтения корзины здесь возвращает False, то
    есть поведение остаётся ровно тем, каким было до появления этой
    проверки, — не хуже. Обратный выбор («не смогли проверить → считаем
    удалённой») превратил бы разовую сетевую ошибку в отказ по ЛЮБОЙ задаче,
    которой нет в снимке открытых, — цена, несоизмеримая с риском: на этой
    ветке за спиной уже стоят и сверка названия, и пост-проверка исполнителя.

    ЧЕСТНОЕ ОГРАНИЧЕНИЕ: лента корзины — страница на `TRASH_MAX_LIMIT`
    записей, а не индекс. Задача, удалённая давно, из неё выпадает, и тогда
    ответ здесь — False, как и для неудалённой. Это ограничение источника,
    единственного, который вообще знает про удаление.

    ЦЕНА: до двух запросов (лента завершённых, затем корзина), результат
    кэшируется по id задачи на TTL клиента. В батче они платятся за КАЖДУЮ
    строку, которой нет в снимке открытых, — но именно на этом пути уже
    стоит официальный фолбэк, который без project_id делает запрос на каждый
    проект аккаунта; двойка сверху его не меняет по порядку величины."""
    try:
        _found, where, readable = _locate_task_any_state(task_id)
    except Exception as e:            # pragma: no cover — оно уже ловит своё
        logger.warning(f"проверка корзины для {str(task_id)[:8]}… не удалась: {e}")
        return False
    return bool(readable) and where == "trash"


class _Guard:
    """Result of the identity guard for one task.
    status ∈ {ok, mismatch, missing, unavailable} — plus 'completed', which
    ONLY `_guard_task_incl_completed` ever returns (the task exists and its
    title checks out, it is simply no longer open). `ok` stays strictly
    "open and verified", so nothing that switches on `.ok` is affected."""
    __slots__ = ("status", "project_id", "title", "message", "title_known")

    def __init__(self, status, project_id="", title="", message="",
                 title_known=False):
        self.status = status
        self.project_id = project_id   # the task's CURRENT projectId (corrected)
        self.title = title             # the live title
        self.message = message
        # ПРОЧИТАНО ЛИ живое название вообще (2026-08-09). Пустая строка в
        # `title` отвечает сразу на два разных вопроса — «у задачи нет имени» и
        # «объект до нас не доехал / в ответе нет такого поля», — и вызывающий,
        # строящий на ней проверку, разоружается на втором случае, думая, что
        # видит первый. Кто различать не обязан, тот просто не читает это поле:
        # добавление ничего не ломает.
        self.title_known = title_known

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _guard_task(
    task_id: str,
    expected_title: str = "",
    project_id: str = "",
    expected_project: str = "",
    *,
    fresh: bool = True,
    by_id: Optional[Dict[str, Dict]] = None,
) -> "_Guard":
    """Identity guard for a SINGLE-task mutation: confirm the id points at the
    task the caller means BEFORE touching it, using fresh live state.

    - v2 state can't be read at all        → status 'unavailable' (REFUSE — fail closed)
    - id not among open tasks              → status 'missing' (can't verify)
    - id resolves to a DIFFERENT title     → status 'mismatch' (REFUSE — wrong task)
    - id in a different project than asked  → status 'mismatch'
    - otherwise                            → status 'ok', project_id corrected

    Title check is armed only when `expected_title` is given (back-compatible).

    When the v2 open-task snapshot (`by_id`) doesn't have the task, this
    does NOT immediately conclude 'missing': that snapshot can lag a
    mutation made through the same v2 API by far longer than any bounded
    retry covers (minutes, not seconds — see _official_task_snapshot's
    docstring for the live incident this fixes). Before refusing, it takes
    one point-read against the OFFICIAL Open API — a different backend not
    subject to the same staleness — using `project_id` when given, or a
    full-project scan when it isn't (see _official_task_scan). Only THEN,
    if that also fails to confirm the task, is it reported missing. The
    identity check itself (id/title/container below) is unchanged either
    way — only how the live object is obtained changes."""
    if by_id is None:
        by_id = _open_by_id(fresh=fresh)
    if by_id is None:
        return _Guard("unavailable", project_id, expected_title,
                      _STATE_UNAVAILABLE_MSG)
    live = by_id.get(task_id)
    if not live:
        live = (_official_task_snapshot(project_id, task_id) if project_id
                else _official_task_scan(task_id))
        # ЗАДАЧА В КОРЗИНЕ ВЫГЛЯДИТ ОТКРЫТОЙ (дефект живой приёмки
        # 2026-08-07). TickTick НЕ меняет `status` при удалении — он остаётся
        # 0, — а официальный Open API отдаёт удалённую задачу точечным
        # чтением как ни в чём не бывало (то же свойство описано в
        # `_trash_state`). Фильтр `_official_task_snapshot` отсеивает только
        # `status != 0`, поэтому корзинная задача проходила через этот фолбэк
        # КАК ОТКРЫТАЯ: план строился на удалённый объект, а батч
        # `update_tasks` не помечал такую строку ⛔ и обещал применить
        # больше, чем мог.
        #
        # Спрашиваем лишь у того источника, который знает про удаление
        # (v2-лента корзины), и ТОЛЬКО на этой ветке: задача, найденная в
        # снимке открытых, в корзине лежать не может по определению, поэтому
        # счастливый путь не платит за проверку ни одним запросом.
        #
        # Ответ здесь — "missing" (а не отдельный статус): `_guard_task`
        # зовут десятки мутаторов, и новый статус, которого никто из них не
        # ждёт, провалился бы у них в ветку «можно менять». Класс, которому
        # корзину надо отличать от «просто не найдено», делает это ниже —
        # `_guard_task_incl_completed`. Сообщение при этом называет причину
        # своими словами: «в корзине», а не «завершена/удалена/неверный id».
        if live and _task_is_in_trash(task_id):
            return _Guard("missing", live.get("projectId") or project_id,
                          live.get("title") or "",
                          f"задача «{live.get('title') or task_id}» лежит В "
                          "КОРЗИНЕ (удалена) — верните её через restore_tasks, "
                          "прежде чем работать с ней")
    if not live:
        return _Guard("missing", project_id, expected_title,
                      f"id {str(task_id)[:8]}… не среди открытых задач "
                      "(завершена/удалена/неверный id)")
    real_pid = live.get("projectId") or project_id
    real_title = live.get("title") or ""
    # Ключа `title` в ответе может не быть вовсе (урезанный снимок) — это НЕ
    # «задача без названия», и склеивать их нельзя: см. `_Guard.title_known`.
    title_known = "title" in live
    names = _v2_project_names()
    # Д2 (2026-08-09): переданное название — это заменитель, который сервер
    # сам напечатал для ЭТОЙ ЖЕ безымянной задачи (см.
    # _is_untitled_placeholder) — не подлог, а точное эхо вывода. Сверка по
    # имени тут неприменима (сравнивать нечего), но отказом это быть не
    # должно.
    if (not _names_agree(expected_title, real_title)
            and not _is_untitled_placeholder(expected_title, live)):
        return _Guard("mismatch", real_pid, real_title,
                      f"id указывает на «{real_title}», а НЕ «{expected_title}»",
                      title_known)
    if expected_project and not _names_agree(expected_project, names.get(real_pid, "")):
        return _Guard("mismatch", real_pid, real_title,
                      f"id в проекте «{names.get(real_pid, '')}», а НЕ «{expected_project}»",
                      title_known)
    return _Guard("ok", real_pid, real_title, title_known=title_known)


# Единая пометка класса «операции над завершённой задачей» — один текст на
# все пять инструментов, чтобы подтверждающий читал одно и то же, у какого бы
# из них он ни оказался.
#
# ЗНАЧОК ℹ️, А НЕ ⚠️ — и это не косметика (2026-08-07, дефект «вердикт не
# различает успех и провал»). До этой правки ОДИН символ ⚠️ означал две
# РАЗНЫЕ вещи: «часть операции не подтверждена» (оценка ПРОВЕРКИ) и «задача
# завершена» (ФАКТ о состоянии объекта). Пока символ один, любой честный
# факт о состоянии автоматически понижает вердикт: `_EXEC_WARN_MARKERS`
# ищет ⚠️ где угодно в самоотчёте исполнителя, поэтому пометка, вклеенная
# сюда, ЛИЧНО стоила исполнителю его собственного «✅» — успешный
# комментарий к завершённой задаче приходил владельцу как
# «❓ НЕ подтверждено», неотличимо от упавшей операции.
#
# ПРАВИЛО, закрывающее класс: ⚠️ — сомнение В ПРОВЕРКЕ («исход не
# подтверждён», «проверить не удалось»); ℹ️ — факт о состоянии объекта или
# о продукте операции, который проверке не противоречит. Вердикт понижают
# только первые. Симметрично помечен и другой такой факт — оговорка
# `duplicate_task` про поля, которые в копию не переносятся.
_COMPLETED_TASK_NOTE = ("задача ЗАВЕРШЕНА (не среди открытых) — операция над "
                        "ней допустима, название сверено с живым состоянием")

# Единый текст отказа для второго состояния того же класса — задача в
# КОРЗИНЕ. Тоже один на все пять инструментов: подтверждающий обязан читать
# одно и то же, у какого бы из них он ни оказался, и обязан узнать, ЧТО
# случилось с задачей и КАК её вернуть (без этого «операция не выполнена»
# читается как сбой сервера, а не как состояние объекта).
_TRASHED_TASK_NOTE = ("задача «{title}» лежит В КОРЗИНЕ (удалена): операция "
                      "над удалённым объектом не выполняется — верните её "
                      "через restore_tasks, и тогда повторите.")


# ─────────────────────────────────────────────────────────────────────────
# ОБЩИЕ АБЗАЦЫ ОПИСАНИЙ КОМАНД (2026-08-09, ZAHOD1.md 1.2.2, П10 часть 2).
#
# До этой правки один и тот же текст был скопирован в докстринги 23 команд
# (абзац про кнопку в Telegram) и 30 команд (абзац про automation_key).
# Копии уже разъехались в ТРЁХ местах — это измеренный факт, не гипотеза:
# `manual_triage` пересказала абзац про кнопку своими словами, `create_tasks`
# лишилась запрета подставлять ключ автоматики, `execute_task_creation`
# описала аргумент `user_reply` отдельным текстом. Пока копий больше одной,
# следующее расхождение — вопрос времени, а не аккуратности: кто-то поправит
# текст в N местах и забудет в N+1-м.
#
# ПОЧЕМУ ПОДСТАНОВКА ПО МЕСТУ, А НЕ ПРИКЛЕИВАНИЕ В КОНЕЦ ОПИСАНИЯ. Абзацы
# лежат не в хвосте: у 10 команд после абзаца про кнопку идёт ещё абзац про
# двойную сверку личности объекта, у 6 — то же после строк Args. Приклеивание
# в конец переставило бы абзацы у 16 команд, то есть изменило бы текст,
# который читает модель, у 16 команд вместо трёх расходящихся. Поэтому в
# докстринге на месте абзаца стоит МАРКЕР, а декоратор `_shared_notes`
# (см. ниже) разворачивает его в константу, сохраняя и отступ, и порядок
# абзацев: модель читает описание сверху вниз, «правило впереди исключения»
# здесь работает буквально.

# Абзац про Telegram-approval-слой. Хранится БЕЗ отступа — отступ берётся из
# строки маркера при подстановке, поэтому одна и та же константа годится и
# для абзаца (отступ 4), и для строки внутри Args: (отступ 8).
_TG_APPROVAL_NOTE = (
    "Telegram approval layer (when it is enabled on this server): the plan\n"
    "built by call #1 is also sent to the owner as a Telegram message with\n"
    "✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —\n"
    "this tool is NOT called a second time, and the result is written back\n"
    "into that same message. While that is in effect the TEXT path is CLOSED:\n"
    "call #2 is refused whatever `user_reply` says — before the press (wait for\n"
    "it) and after it too (the server is already running the operation). Do not\n"
    "retry it; just tell the user to tap the button."
)

# Абзац про automation_key. Последние две строки — запрет интерактивным
# ассистентам подставлять ключ; именно его не хватало в `create_tasks`.
_AUTOMATION_KEY_NOTE = (
    "automation_key is ONLY for headless automation clients (bots/pipelines):\n"
    "they pass their own connection secret to prove they are automation. A\n"
    "VALID key runs the operation IMMEDIATELY on the FIRST call — no plan, no\n"
    "user_reply, and no Telegram button is sent to the owner. A wrong, empty\n"
    "or missing key changes nothing: the ordinary plan → approval flow applies.\n"
    "⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't\n"
    "know it and guessing is a protocol violation."
)

# Строки описания аргументов в Args:. По отдельности — потому что
# `execute_task_creation` объявляет `user_reply`, но не объявляет
# `automation_key`: у него нет такого параметра в сигнатуре.
_ARG_USER_REPLY = (
    "user_reply: the user's literal reply approving the plan — required on call #2"
)
_ARG_AUTOMATION_KEY = (
    "automation_key: headless-automation only — a VALID key executes on the "
    "FIRST call (no plan, no button, no user_reply); interactive assistants "
    "leave this empty"
)
# Обе строки подряд — то, что стоит в Args: у 29 гейтованных команд.
_GATE_ARGS_TAIL = _ARG_USER_REPLY + "\n" + _ARG_AUTOMATION_KEY

# Маркер (слот) -> текст, который в него разворачивается. Ключ словаря
# совпадает с именем флага `_shared_notes`.
_SHARED_NOTE_SLOTS = {
    "tg": ("{{TG_APPROVAL_NOTE}}", _TG_APPROVAL_NOTE),
    "automation": ("{{AUTOMATION_KEY_NOTE}}", _AUTOMATION_KEY_NOTE),
    "gate_args": ("{{GATE_ARGS_TAIL}}", _GATE_ARGS_TAIL),
    "user_reply_arg": ("{{ARG_USER_REPLY}}", _ARG_USER_REPLY),
}

# Признак «в докстринге остался неразвёрнутый маркер» — по нему в конце
# модуля проверяется, что ни одна команда не осталась без декоратора.
_SHARED_NOTE_SLOT_MARK = "{{"


def _expand_note_slot(doc: str, slot: str, text: str, where: str) -> str:
    """Развернуть маркер `slot` в `text`, выровняв текст по отступу той
    строки, где маркер стоял. Маркер обязан занимать свою строку целиком —
    иначе непонятно, какой отступ считать общим для абзаца."""
    if doc.count(slot) != 1:
        raise RuntimeError(
            f"{where}: маркер {slot} встречается {doc.count(slot)} раз(а), "
            "ожидался ровно один"
        )
    start = doc.find(slot)
    line_start = doc.rfind("\n", 0, start) + 1
    line_end = doc.find("\n", start)
    if line_end == -1:
        line_end = len(doc)
    line = doc[line_start:line_end]
    if line.strip() != slot:
        raise RuntimeError(
            f"{where}: маркер {slot} обязан занимать строку целиком, "
            f"а строка такая: {line!r}"
        )
    indent = line[:len(line) - len(line.lstrip(" "))]
    body = "\n".join(indent + ln if ln else ln for ln in text.split("\n"))
    return doc[:line_start] + body + doc[line_end:]


def _shared_notes(*, tg: bool = False, automation: bool = False,
                  gate_args: bool = False, user_reply_arg: bool = False):
    """Развернуть общие абзацы в докстринге команды ДО её регистрации.

    Ставится ПОД `@mcp.tool(...)`: декораторы применяются снизу вверх, так
    что FastMCP получает уже готовый текст, а строка `@mcp.tool(...)` со
    своими `annotations=` остаётся ровно там, где была (её читают и AST-скан
    tests/test_gated_tool_schemas.py, и tests/test_tool_registry.py).

    ФЛАГИ ДУБЛИРУЮТ МАРКЕРЫ НАМЕРЕННО и сверяются с ними в обе стороны.
    Главный риск этой правки — не изменённый текст, а ПОТЕРЯННОЕ указание:
    команда, из которой абзац вырезали, но обёртку не навесили, внешне
    неотличима от команды, которой этот абзац не нужен. Ни тест, ни журнал
    об этом не скажут — скажет только живой сервер, где модель перестанет
    видеть запрет на подстановку ключа автоматики или указание про кнопку.
    Флаг делает намерение явным: чтобы указание пропало, придётся снять И
    маркер, И флаг — то есть сделать это осознанно и видимо в diff.
    """
    flags = {"tg": tg, "automation": automation,
             "gate_args": gate_args, "user_reply_arg": user_reply_arg}

    def decorator(fn):
        doc = fn.__doc__ or ""
        where = getattr(fn, "__name__", "<?>")
        for key, (slot, text) in _SHARED_NOTE_SLOTS.items():
            wanted = flags[key]
            present = slot in doc
            if wanted and not present:
                raise RuntimeError(
                    f"{where}: объявлено {key}=True, но маркера {slot} в "
                    "докстринге нет — указание было бы потеряно молча"
                )
            if present and not wanted:
                raise RuntimeError(
                    f"{where}: в докстринге есть маркер {slot}, но {key}=True "
                    "не объявлено — маркер утёк бы в описание как текст"
                )
            if wanted:
                doc = _expand_note_slot(doc, slot, text, where)
        fn.__doc__ = doc
        return fn

    return decorator


def _guard_task_incl_completed(
    task_id: str,
    expected_title: str = "",
    project_id: str = "",
    *,
    fresh: bool = True,
    by_id: Optional[Dict[str, Dict]] = None,
) -> "_Guard":
    """Identity guard for the class of single-task operations that are
    LEGITIMATE on a completed task: add_task_comment, attach_file_to_task,
    update_task_comment, delete_task_comment, duplicate_task.

    Same guard as `_guard_task`, two statuses richer:

      - task is open                        → whatever _guard_task says
      - task is NOT open but a source knows
        it, and the title agrees            → status 'completed'
      - …and the title does NOT agree       → status 'mismatch' (as always)
      - task is IN THE TRASH                → status 'trashed' (REFUSE)
      - no source knows it at all           → status 'missing'

    ПОЧЕМУ КОРЗИНА — ОТДЕЛЬНЫЙ СТАТУС, А НЕ РАЗНОВИДНОСТЬ «completed»
    (политика, 2026-08-07). Оба состояния означают «задачи нет среди
    открытых», но требуют РАЗНОГО решения, и слить их — значит либо
    запретить законное, либо разрешить ошибочное:
      * ЗАВЕРШЁННАЯ задача — нормальный конечный объект. Приложить к ней
        чек, дописать вывод, продублировать как шаблон — обычные сценарии,
        и результат такой операции владелец увидит там же, где саму задачу.
        Операция ДОПУСТИМА.
      * КОРЗИНА — заявленное «я это убрал». Комментарий, дописанный в
        корзину, не увидит никто; файл, приложенный туда, уедет вместе с
        задачей, когда корзина очистится; «дубликат удалённого» почти
        всегда означает, что человек хотел restore_tasks, а не копию.
        Операция над удалённым объектом — почти всегда ошибка, поэтому
        ОТКАЗ с подсказкой, как вернуть задачу.
    Статус назван состоянием объекта, а не решением («refused»), чтобы
    политику можно было поменять в вызывающих, не переписывая guard.

    ПОЧЕМУ ЭТО ОТДЕЛЬНАЯ ФУНКЦИЯ, а не правка `_guard_task` (дефект №2,
    живая приёмка 2026-08-07). На завершённой задаче `_guard_task` возвращал
    `missing` — и это ЧЕСТНО для него: «среди открытых нет». Расходились
    ВЫЗЫВАЮЩИЕ: четверо смягчали `missing` до ⚠️ «название НЕ проверено» и
    работали, а `duplicate_task` тот же самый ответ превращал в 🛑 ОТКАЗ.
    Строгость выходила обратной риску: дублирование создаёт КОПИЮ и ничего
    не портит, но отказывало, — а изменяющие комментарии шли. Чинить guard
    было нельзя: «только открытые» — осознанная политика для мутаций,
    которые на завершённой задаче бессмысленны (complete_tasks, move_tasks,
    set_task_parent, abandon_task). Поэтому решение принято ОДИН РАЗ ДЛЯ
    ВСЕГО КЛАССА, здесь, а инструменты класса зовут эту функцию вместо
    `_guard_task`.

    Побочно это УСИЛИВАЕТ проверку, а не ослабляет: раньше на завершённой
    задаче название не сверялось вовсе («НЕ проверено» — и подлог проходил),
    теперь оно сверяется с живой записью, и `mismatch` ловится. А `missing`
    после этого означает гораздо более сильное «не нашли НИ В ОДНОМ
    источнике», поэтому вызывающим уместно на нём отказывать."""
    g = _guard_task(task_id, expected_title, project_id, fresh=fresh, by_id=by_id)
    if g.status != "missing":
        return g
    # ГДЕ задача сейчас, спрошено У ТОГО, КТО ЗНАЕТ ПРО УДАЛЕНИЕ. Это первым
    # шагом, а не после `_closed_task_snapshot`: тот начинает с официального
    # точечного чтения, которое флага удаления не несёт вовсе и вернуло бы
    # корзинную задачу как обычную завершённую. `find_task_any_state` уже
    # знает место находки — «trash» здесь просто перестал выбрасываться.
    # Кэш у неё общий с `_task_is_in_trash` выше, так что повторный вопрос
    # внутри одного хода бесплатен.
    found, where, _readable = _locate_task_any_state(task_id)
    live = found or _closed_task_snapshot(task_id, project_id)
    if not live:
        return _Guard("missing", project_id, expected_title,
                      f"id {str(task_id)[:8]}… не найден ни среди открытых "
                      "задач, ни среди завершённых/удалённых (неверный id "
                      "или задача слишком старая для этих выборок)")
    real_title = live.get("title") or ""
    real_pid = live.get("projectId") or project_id
    # Сверка названия — РАНЬШЕ решения о состоянии: защита от «не той задачи»
    # сильнее всего остального, и подлог обязан читаться как подлог, что бы
    # ни было с самим объектом.
    # Д2 (2026-08-09): то же исключение для заменителя, что и в _guard_task —
    # см. _is_untitled_placeholder.
    if (not _names_agree(expected_title, real_title)
            and not _is_untitled_placeholder(expected_title, live)):
        return _Guard("mismatch", real_pid, real_title,
                      f"id указывает на «{real_title}», а НЕ «{expected_title}»")
    if where == "trash":
        return _Guard("trashed", real_pid, real_title, _TRASHED_TASK_NOTE.format(
            title=real_title or str(task_id)[:8] + "…"))
    return _Guard("completed", real_pid, real_title, _COMPLETED_TASK_NOTE)


# ЕДИНЫЙ ОТВЕТ ОХРАННИКА ЛИЧНОСТИ ОБЪЕКТА (2026-08-09, ZAHOD1.md 1.2.3, П11).
# Цепочка `if g.status == …` была написана в файле ДВАДЦАТЬ ПЯТЬ раз (в ТЗ
# значилось девятнадцать — за одну ночную работу стало на шесть больше). Цена
# копий не эстетическая: сервер удаляет задачи по кнопке, и «поправили в
# двадцати четырёх местах, забыли в двадцать пятом» означает операцию не над
# тем объектом.
#
# ПОЧЕМУ ПАРАМЕТРЫ, А НЕ ОДИН ОБЩИЙ ТЕКСТ. Формулировки площадок разные не по
# недосмотру: `add_task_comment` говорит «id это «X», а НЕ «Y»» (свой текст),
# `attach_file_to_task` — «id указывает на «X», а НЕ «Y»» (из `.message`).
# Вариант через `.message` встречается чаще, но «привести к большинству»
# значит ИЗМЕНИТЬ ОТВЕТ СЕРВЕРА у живых команд, а ответы разбирают регулярками
# телеграм-бот и агент в n8n. Поэтому свёрнута СТРУКТУРА (порядок веток,
# префикс, хвост, политика корзины), а слова вынесены в параметры и сохранены
# посимвольно — это доказывает `tests/test_refusal_texts_frozen.py`, чьи
# ожидания сняты ДО свёртки.
#
# `_split_tasks_by_state` (ниже) НЕ переведён на помощник осознанно: он не
# возвращает текст отказа, а раскладывает строки пакета по спискам
# found/mismatch/missing, и подгонка под общую форму изменила бы поведение
# пакетных команд, где отказ не возвращается, а копится.

# Четыре текста «сверить не удалось» — по одному на класс площадок; раньше
# каждый был написан столько раз, сколько площадок его использует.
_UNVERIFIED_PARENT_TITLE = ("⚠️ Название родительской задачи НЕ удалось "
                            "сверить с живым состоянием (чтение не удалось) — "
                            "сверка повторится при подтверждении, и "
                            "расхождение остановит исполнение.")
_UNVERIFIED_TASK_TITLE = ("⚠️ Название задачи НЕ удалось сверить с живым "
                          "состоянием (чтение не удалось) — сверка "
                          "повторится при подтверждении, и расхождение "
                          "остановит исполнение.")
_UNVERIFIED_TASK = ("⚠️ Задачу НЕ удалось сверить с живым состоянием (чтение "
                    "не удалось) — сверка повторится при подтверждении, и "
                    "расхождение остановит исполнение.")
_UNVERIFIED_PARENT_NAME = ("⚠️ Имя родителя НЕ удалось сверить с живым "
                           "состоянием (чтение не удалось) — сверка "
                           "повторится при подтверждении.")
# Родителя нет среди открытых — для `unset_task_parent` это НЕ повод
# отказать (отцепляют как раз от завершённого/удалённого), но сказать вслух
# обязательно.
_PARENT_GONE_NOTE = ("⚠️ Родитель не среди открытых задач (возможно "
                     "завершён/удалён) — имя не сверено; связь "
                     "перепроверится при подтверждении.")

# Хвост отказа: чем фаза кончает фразу. «План НЕ построен» ничего не менял,
# исполнение — не трогал, пакетная строка хвоста не имеет вовсе (она едет в
# общий список результатов, где хвост у каждой строки читался бы как шум).
_STAGE_TAIL = {"план": "Ничего не изменено.",
               "исполнение": "Ничего не тронул.",
               "пакет-строка": "",
               "пакет-родитель": ""}


def _guard_or_refuse(g: "_Guard", *, stage: str, verb: str = "",
                     verb_mismatch: str = "", expected: str = "",
                     parent: bool = False, parent_id: str = "",
                     says: str = "own", missing_says: str = "own",
                     missing_name: str = "", missing_extra: str = "",
                     missing_note: str = "", shield: bool = False,
                     unavailable_note: str = "") -> Tuple[str, str]:
    """Ответ площадки на вердикт охранника: (текст отказа, текст предупреждения).

    Пустой отказ означает «можно работать дальше» — предупреждение при этом
    может быть непустым (завершённая задача, нечитаемое живое состояние).

    stage — фаза, она же задаёт префикс и хвост:
      «план»        → «🛑 План НЕ построен — …  Ничего не изменено.»
      «исполнение»  → «🛑 {verb} — …  Ничего не тронул.»
      «пакет-строка»→ «🛑 {verb} — …» (хвоста нет, строка едет в общий список)
      «пакет-родитель» → голая нота про внешнего родителя: значок и номер
                         строки приклеивает сама площадка.
    verb — глагол площадки («НЕ добавил комментарий»), verb_mismatch — он же,
      если на ветке mismatch площадка называет задачу другим именем (так в
      `update_tasks`/`complete_tasks`: там mismatch печатает ЗАЯВЛЕННОЕ имя, а
      остальные ветки — то, что удалось найти).
    says / missing_says — «own» (текст собирает помощник) или «message» (текст
      приходит готовым из `_Guard.message`); «skip» у missing означает, что
      площадка обрабатывает эту ветку сама (в `complete_tasks` пропуск строки
      не отказ).
    parent — речь о РОДИТЕЛЕ, а не о самой задаче (меняет и подлежащее, и род
      причастий в «завершён/завершена»).
    shield — приписка «(защита от «не той задачи»)»; на плане она стоит
      всегда, на исполнении — только там, где стояла раньше.
    """
    # Пакетное создание отвечает ГОЛОЙ нотой про внешнего родителя: значок и
    # «#3 «Купить бумагу»: » приклеивает площадка (у неё на плане свой
    # заголовок «🛑 Исключены N»), а тексты уже свёрнуты в
    # _wrong_parent_note / _dead_parent_note. Ветки при этом ТЕ ЖЕ — иначе
    # политика снова разъедется на два места.
    batch_parent = stage == "пакет-родитель"
    plan = stage == "план"
    head = "🛑 План НЕ построен" if plan else f"🛑 {verb}"
    tail = _STAGE_TAIL[stage]
    # Тело, которое кончается точкой (`{tail}` уже с ней), и тело, которое
    # НЕ кончается: _TRASHED_TASK_NOTE несёт свою точку, вторая читалась бы
    # как опечатка.
    dotted = f". {tail}" if tail else ""
    plain = f" {tail}" if tail else ""

    if g.status == "unavailable":
        # На плане разовый сбой чтения — не отказ (иначе он блокирует всякую
        # работу), но карточка обязана сказать об этом вслух; на исполнении
        # это отказ, и последнее слово именно за ним. Пакетная СТРОКА при
        # этом называет себя («🛑 НЕ обновил «Отчёт» — …»): в общем списке
        # результатов голое сообщение было бы неизвестно про какую задачу.
        if plan:
            return "", unavailable_note
        if batch_parent:
            # Сверить не удалось — решает площадка: на плане строка остаётся
            # с пометкой «НЕ сверено», на исполнении отбраковывается.
            return "", ""
        return (g.message if tail else f"{head} — {g.message}"), ""
    if g.status == "mismatch":
        if batch_parent:
            return _wrong_parent_note(g.title, expected), ""
        if says == "message":
            body = g.message
        else:
            body = (f"{'родитель по id это' if parent else 'id это'} "
                    f"«{g.title}», а НЕ «{expected}»")
        if plan or shield:
            body += " (защита от «не той задачи»)"
        prefix = f"🛑 {verb_mismatch}" if verb_mismatch else head
        return f"{prefix} — {body}{dotted}", ""
    if g.status == "trashed":
        # Политика класса для УДАЛЁННОЙ задачи — отказ, до всякого согласия
        # (см. _guard_task_incl_completed). Раньше такая задача проходила как
        # открытая: `status` в корзине остаётся 0.
        return f"{head} — {g.message}{plain}", ""
    if g.status == "missing":
        if batch_parent:
            return _dead_parent_note(expected, parent_id), ""
        if missing_says == "skip":
            return "", ""
        if missing_note:
            return "", missing_note
        if missing_says == "message":
            return f"{head} — {g.message}{dotted}", ""
        who = ("родитель " if parent else "") + f"«{missing_name or expected}»"
        gone = ("завершён/удалён/неверный id" if parent
                else "завершена/удалена/неверный id")
        return (f"{head} — {who} не среди открытых задач "
                f"({gone}){missing_extra}{dotted}", "")
    if g.status == "completed":
        # ℹ️, а не ⚠️: это ФАКТ о состоянии объекта, а не сомнение в проверке
        # — см. _COMPLETED_TASK_NOTE.
        return "", f"ℹ️ {_COMPLETED_TASK_NOTE}."
    return "", ""


def _split_tasks_by_state(
    tasks: List[Dict], by_id: Optional[Dict[str, Dict]] = None, fresh: bool = True
) -> tuple:
    """Split requested task dicts against FRESH open-task state so a batch
    mutating tool acts only on the RIGHT task and reports REAL results.

    Returns (found, mismatch, missing) — see _guard_task for the per-item rules.
      found    — [{taskId, projectId, title, armed}] id open AND name agrees (or
                 no name given — then armed=False: the id↔title check never ran);
                 projectId corrected to the CURRENT one.
      mismatch — [{taskId, expected, actual, project}] id resolves to a DIFFERENT
                 task/project — REFUSED, never touched.
      missing  — [{taskId, projectId, title}] id not among open tasks.

    Raises RuntimeError when the live state is UNAVAILABLE — callers must
    check _open_by_id() themselves first and refuse (fail closed)."""
    if by_id is None:
        by_id = _open_by_id(fresh=fresh)
    if by_id is None:
        raise RuntimeError(_STATE_UNAVAILABLE_MSG)
    names = _v2_project_names()
    found, mismatch, missing = [], [], []
    for row, t in enumerate(tasks):
        tid = t.get("taskId") or t.get("task_id")
        given_pid = t.get("projectId") or t.get("project_id") or ""
        exp_title = t.get("title") or ""
        exp_proj = t.get("projectName") or ""
        g = _guard_task(tid, exp_title, given_pid, exp_proj, by_id=by_id)
        if g.status == "missing":
            # `reason` — СОБСТВЕННЫЕ слова guard'а о том, ПОЧЕМУ строка не
            # прошла: «лежит В КОРЗИНЕ … верните через restore_tasks» против
            # «не среди открытых (завершена/удалена/неверный id)». Вызывающие
            # раньше выдумывали одну общую формулировку на оба случая, и
            # человек у карточки не мог отличить удалённую задачу от опечатки
            # в id — то есть не знал ни что случилось, ни как это починить.
            missing.append({"taskId": tid, "projectId": given_pid,
                            "title": exp_title or f"[task {str(tid)[:8]}…]",
                            "reason": g.message})
        elif g.status == "mismatch":
            mismatch.append({"taskId": tid, "expected": exp_title or "(без названия)",
                             "actual": g.title or "(без названия)",
                             "project": names.get(g.project_id, "")})
        else:
            # У БЕЗЫМЯННОЙ задачи сверка не «не состоялась», а прошла ПО id
            # (П15, 2026-08-09): имени нет ни у вызывающего, ни в живом
            # состоянии — сравнивать нечего, объект опознан идентификатором.
            # Это отдельный признак, а не `armed`: armed по-прежнему значит
            # «имя реально сверено», и раздувать его смыслом нельзя.
            #
            # ЗАМЕНИТЕЛЬ ЖИВЁТ В ОТДЕЛЬНОМ ПОЛЕ `label`, а `title` остаётся
            # ПУСТЫМ, каким он и есть у задачи. Иначе «(без названия: 📎 1
            # файл)» уехало бы в манифест плана как настоящее название, и на
            # исполнении сверка сравнивала бы выдуманное имя с пустым живым —
            # то есть отказывала бы ровно в том случае, ради которого правка
            # и делалась.
            by_id_only = _is_untitled(exp_title) and _is_untitled(g.title)
            shown = exp_title if not _looks_untitled(exp_title) else (
                g.title if not _looks_untitled(g.title)
                else _untitled_label(by_id.get(tid) or {}))
            # `live_title` и `row` — 2026-08-09, по следам живой поломки.
            # ЖИВОЕ ИМЯ БЕРЁТСЯ У GUARD'А, а не из `by_id`: снимок открытых
            # задач отстаёт (см. `_official_task_snapshot` — инцидент
            # 2026-08-07, задача выпадала из v2-выборки на 25 минут), и guard
            # ради этого ходит в официальный запасной канал. Вызывающий,
            # который потом сам полезет в `by_id`, получит пустоту по живой
            # задаче — а пустота в поле имени читается как «имени нет» и
            # разоружает любую проверку, построенную на ней. Отдаём то, на чём
            # guard ПРИНИМАЛ РЕШЕНИЕ, чтобы второго источника правды не было.
            #
            # `row` — номер ИСХОДНОЙ строки запроса: id может повторяться в
            # одном вызове, и сопоставление по taskId склеивает разные строки
            # в одну (жалоба по законной строке пропадала вместе с отказом по
            # соседней).
            found.append({"taskId": tid, "title": exp_title or g.title,
                          "label": shown,
                          "projectId": g.project_id,
                          "live_title": g.title if g.title_known else None,
                          "row": row,
                          "armed": bool((exp_title or "").strip()),
                          "by_id": by_id_only})
    return found, mismatch, missing


def _sync_point_date(live: Optional[Dict], start_date, due_date) -> tuple:
    """Bug fix for date updates turning into unintended ranges.

    The official update endpoint writes startDate/dueDate INDEPENDENTLY — a
    request that supplies only ONE of them leaves the OTHER holding its OLD
    value untouched. If the task used to be a single fixed date (startDate ==
    dueDate), changing just due_date therefore produces startDate=<old date>,
    dueDate=<new date>, which TickTick renders as a multi-day RANGE
    («с 23-го по <новое>») instead of the intended plain move to a new date.

    Takes the RAW start_date/due_date strings as supplied by the caller
    (either may be None/empty — not yet run through _normalize_date) and the
    task's CURRENT live v2 state (or None if unknown/new task). Returns
    (start_date, due_date, warn) — warn is a Russian status line to surface,
    or None.

    Rule:
      - Caller supplies BOTH fields → explicit, deliberate choice (a real
        range, or two identical dates) — passed through unchanged, no sync.
      - Caller supplies exactly ONE field, and the task's live state is a
        single point (startDate == dueDate, both set) → sync the untouched
        field to the SAME new value, so the task stays single-date, just
        moved. This is the default/expected behaviour and the case that was
        reported as a bug.
      - Caller supplies exactly ONE field, and the task was ALREADY a
        deliberate-looking range (startDate != dueDate) → leave values as
        given (don't clobber a real range the user may be intentionally
        shifting one edge of) but return a warning so the span change isn't
        silent.
      - `live` is None (state unknown, or a brand-new task with no prior
        dates) → nothing to sync against, pass through unchanged — matches
        create_tasks, where a single supplied field with no prior state is
        just "no date on the other side", not a range.
    """
    has_due = due_date not in (None, "")
    has_start = start_date not in (None, "")
    warn = None
    if has_due != has_start and live:
        live_start = live.get("startDate")
        live_due = live.get("dueDate")
        was_point = bool(live_start) and bool(live_due) and \
            str(live_start)[:10] == str(live_due)[:10]
        if was_point:
            if has_due:
                start_date = due_date
            else:
                due_date = start_date
        elif live_start and live_due:
            warn = (f"у задачи был диапазон {str(live_start)[:10]}–"
                    f"{str(live_due)[:10]}, теперь изменился только один "
                    "край — проверьте, что диапазон остался корректным")
    return start_date, due_date, warn


def _unarmed_note(found: List[Dict]) -> str:
    """Warning line when some items were mutated WITHOUT the id↔title check
    (the caller sent no title, so the guard had nothing to verify). Makes the
    over-claim visible instead of silently pretending the guard ran."""
    # Безымянные задачи из этой жалобы вынуты (П15, 2026-08-09): у них сверять
    # было НЕЧЕГО не по недосмотру вызывающего, а потому что имени нет ни с
    # одной стороны, и объект опознан по id. Смешивать это с «title не
    # передан» — значит пугать владельца ⚠️ там, где всё в порядке, и заодно
    # прятать настоящие непроверенные строки среди ложных.
    loose = [f for f in found
             if not f.get("armed", True) and not f.get("by_id")]
    by_id_rows = [f for f in found if f.get("by_id")]
    notes = []
    if loose:
        notes.append(f"⚠️ {len(loose)} выполнено БЕЗ сверки названия (title не "
                     "передан): "
                     + ", ".join(f"«{f.get('label') or f['title']}»"
                                 for f in loose))
    if by_id_rows:
        notes.append(f"ℹ️ {len(by_id_rows)} опознано ПО id — названия у этих "
                     "задач нет, сверять нечего (возможно, стоит сначала их "
                     "назвать): "
                     + ", ".join(f"«{f.get('label') or f['title']}»"
                                 for f in by_id_rows))
    return "\n".join(notes)


# ─── Н9 (2026-08-09): взведённость сверки как УСЛОВИЕ записи в поле названия ──
#
# Дыра, которую это закрывает. `_names_agree` первой строкой возвращает True на
# пустое ожидаемое имя — и это её ЧЕСТНЫЙ контракт («претензии нет, сверять
# нечего»), её зовут 24 места, половина законно передаёт пустоту. Менять её
# нельзя. Но у одного вызывающего пустота означала другое: `update_tasks`
# принимал строку {"taskId": X, "new_title": "ЗАТЁРТО"} БЕЗ поля title, guard
# при этом был разоружён, имя живой задачи затиралось, а жалоба «выполнено БЕЗ
# сверки названия» печаталась ПОСЛЕ записи. Восстановить затёртое нечем:
# журнальная запись хранит НОВОЕ имя.
#
# Развести надо два внешне одинаковых случая:
#   «имени нет, потому что менять его не собираемся» — законно и массово
#       (срок, приоритет, теги, проект, завершение) — требовать имя там значит
#       издеваться над вызывающим;
#   «имени нет, но пишем В ПОЛЕ НАЗВАНИЯ» — единственный случай, когда
#       отсутствие имени означает затирание старого без единой сверки.
#
# Поэтому правило записано ОДНОЙ функцией, а не рассуждением по месту, и один
# текст отказа обслуживает все точки: подтверждающий обязан читать одно и то
# же, откуда бы отказ ни пришёл.
_RENAME_UNARMED_REFUSAL = (
    "🛑 НЕ переименовал «{label}» — в строке есть new_title, но НЕТ текущего "
    "названия задачи (поле title), то есть сверка личности не состоялась бы "
    "вовсе: id из устаревшего списка затёр бы имя ЖИВОЙ задачи безвозвратно "
    "(в журнал пишется НОВОЕ имя, восстанавливать не из чего). Добавь в эту "
    "строку \"title\": \"<точное текущее название>\"; если названия у задачи "
    "ДЕЙСТВИТЕЛЬНО нет — \"untitled\": true вместо него. Эта строка НЕ "
    "применена, название не тронуто.")

# Живое имя ПРОЧИТАТЬ НЕ УДАЛОСЬ — это не то же самое, что «имени нет»
# (2026-08-09, найдено скептиком на пакетном пути). Разница наблюдаемая:
# «прочитали, там пусто» разрешает переименование по признаку «объект опознан
# идентификатором»; «не читали» не разрешает НИЧЕГО, потому что незнание — не
# основание разоружаться.
_RENAME_UNKNOWN_LIVE_REFUSAL = (
    "🛑 НЕ переименовал «{label}» — прочитать текущее название живой задачи не "
    "удалось, а в строке нет поля title, с которым его можно было бы сверить. "
    "Незнание не разрешает запись в поле названия: именно так одно "
    "переименование по устаревшему id и стирает имя живой задачи. Передай "
    "\"title\": \"<точное текущее название>\" и повтори. Эта строка НЕ "
    "применена, название не тронуто.")

# Переименование В ПУСТОТУ. Сверка при этом может быть честно взведена (имя
# передано и совпало) — предикат ниже судит о том, ЧТО ПЕРЕДАНО для сверки, а
# это отдельный вопрос: что ПИШЕТСЯ в поле названия. Пути расходились:
# официальный клиент фильтрует `if title:` (ticktick_client.py:560) и пустое
# имя не отправлял, а пакетный канал отправлял и стирал название, после чего
# отчёт рапортовал успех именем, которого уже нет.
_RENAME_TO_NOTHING_REFUSAL = (
    "🛑 НЕ переименовал «{label}» — new_title пустой, а это стёрло бы название "
    "задачи в пустоту (в журнал ушло бы пустое значение, восстанавливать было "
    "бы не из чего). Если название надо изменить — передай новое; если "
    "трогать его не нужно — убери поле new_title из строки. Эта строка НЕ "
    "применена, название не тронуто.")


def _title_check_armed(expected_title: str, untitled_claim: bool,
                       live_title: Optional[str]) -> bool:
    """Взведена ли сверка названия для ОДНОЙ строки изменения (Н9).

        сверка взведена := передано ВИДИМОЕ title
                        ИЛИ структурный маркер untitled=true, ПОДТВЕРЖДЁННЫЙ
                            прочитанным живым состоянием
                        ИЛИ имени нет ни у вызывающего, ни у ПРОЧИТАННОЙ живой
                            задачи (тот же признак, что `by_id` в
                            `_split_tasks_by_state`)

    `live_title is None` означает «живое имя НЕ ПРОЧИТАНО» и не разрешает
    ничего. Пустая строка означает «прочитали, там пусто» — это разные вещи, и
    различать их обязан код, а не вызывающий: на пакетном пути живое имя
    добывалось из снимка открытых задач, которого у живой задачи могло не
    оказаться (её находил официальный запасной канал), и пустота «я не смотрел»
    читалась предикатом как «имени нет» — сверка разоружалась ровно на том
    классе задач, ради которого фолбэк и появился.

    Пустоту РЕШАЕТ `_looks_untitled` (вопрос показа: «выглядит ли пустым
    местом»), а не `_is_untitled`. Причина конкретная: название из одного
    невидимого символа фаза плана уже признаёт безымянным, и строгий ответ
    здесь означал бы два разных ответа на вопрос «пусто ли это» в одной
    цепочке — план принимает, исполнение отказывает, и переименовать такую
    задачу становится нельзя вообще. Заявленное имя из одних невидимых
    символов именем тоже не считается: сверять по нему нечего, а сходство с
    живым уже проверил `_names_agree` (иначе строка сюда не дошла бы)."""
    if live_title is None:
        return False
    # Нестроковый вход не роняет предикат: сюда доходят словари, собранные
    # моделью, и `{"title": 123}` обязан получить отказ, а не AttributeError
    # изнутри проверки безопасности.
    expected_title = "" if expected_title is None else str(expected_title)
    live_title = str(live_title)
    if not _looks_untitled(expected_title):
        return True
    if untitled_claim and _looks_untitled(live_title):
        return True
    return _looks_untitled(expected_title) and _looks_untitled(live_title)


def _rename_guard_refusal(t: Dict[str, Any], live_title: Optional[str],
                          label: str) -> Optional[str]:
    """Текст отказа для строки изменения — или None, если писать можно.

    Зовётся ДО обращения к TickTick в обоих путях `_update_tasks_impl`.
    Предупреждение после записи проверкой не считается: данные к этому моменту
    уже испорчены.

    `live_title` обязан приходить ОТ GUARD'А (`_Guard.title` / поле
    `live_title` строки `found`), а не из снимка открытых задач: снимок
    отстаёт, и живая задача выпадает из него на минуты. `None` — «не
    прочитали», и это отказ, а не поблажка.

    Ни один отказ здесь не говорит «ничего не изменено»: в одном вызове строк
    несколько, соседние могли примениться законно, и обещание про ВЕСЬ объект
    было бы ложью. Речь всегда про ЭТУ строку.

    Маркер `untitled` читается ЕДИНСТВЕННОЙ реализацией — `_triage_untitled_claim`
    (строгий тип: "true" строкой и 1 числом НЕ проходят). Две его беды —
    неверный тип и заявление, разошедшееся с живым состоянием, — отказывают
    ВСЕГДА, а не только при переименовании: маркер это утверждение о ЛИЧНОСТИ
    объекта, и ложное утверждение о личности не становится безобидным оттого,
    что в этой же строке меняют всего лишь срок."""
    untitled, bad_flag = _triage_untitled_claim(t, "untitled")
    if bad_flag:
        return f"🛑 НЕ обновил «{label}» — {bad_flag} Эта строка НЕ применена."
    if untitled and live_title is None:
        return (f"🛑 НЕ обновил «{label}» — передан маркер untitled=true, но "
                "прочитать живое название задачи не удалось, значит проверить "
                "это утверждение нечем. Маркер, который никто не сверил, — то "
                "же самое, что отсутствие сверки. Передай \"title\": "
                "\"<точное текущее название>\". Эта строка НЕ применена.")
    if untitled and not _looks_untitled(live_title):
        return (f"🛑 НЕ обновил «{label}» — передан маркер untitled=true, но у "
                f"живой задачи название ЕСТЬ: «{live_title}». Маркер — "
                "утверждение «у этой задачи имени нет», и оно разошлось с "
                "живым состоянием: значит id указывает не на ту задачу, "
                "которую ты имеешь в виду. Передай точное текущее название в "
                "поле title. Эта строка НЕ применена.")
    new_title = t.get("new_title")
    if new_title is None:
        return None                      # в поле названия не пишем — правило молчит
    if not str(new_title).strip():
        return _RENAME_TO_NOTHING_REFUSAL.format(label=label)
    if live_title is None:
        return _RENAME_UNKNOWN_LIVE_REFUSAL.format(label=label)
    if _title_check_armed(t.get("title") or "", untitled, live_title):
        return None
    return _RENAME_UNARMED_REFUSAL.format(label=label)


def _guard_project(project_id: str, expected_name: str = "", *,
                   fresh: bool = False, require_known: bool = False) -> Optional[str]:
    """Identity guard for a PROJECT mutation: if the caller supplied the project
    name, verify project_id still resolves to it. Returns an error string to
    return to the caller (refusal), or None when it's safe to proceed.

    fresh=True drops the v2 cache first so the comparison never runs on a
    ≤20s-stale name (an in-app rename would otherwise slip through) — use it
    for destructive callers.
    require_known=True FAILS CLOSED when the id resolves to no live name
    (unknown id, or the names fetch failed): a destructive op must never
    proceed at exactly the moment identity can't be verified."""
    if not expected_name and not require_known:
        return None
    if fresh and ticktick_v2:
        try:
            ticktick_v2.invalidate_cache()
        except Exception:
            pass
    real = _v2_project_names().get(project_id, "")
    if not real:
        if require_known:
            return (f"🛑 Отказ — проект по id {str(project_id)[:12]}… не найден "
                    "среди живых проектов (или имена недоступны) — сверить "
                    "личность проекта нельзя. Ничего не тронул.")
        return None
    if expected_name and not _names_agree(expected_name, real):
        return (f"🛑 Отказ — project_id указывает на «{real}», а НЕ "
                f"«{expected_name}» (защита от «не того проекта»). Ничего не тронул.")
    return None


def _guard_project_or_refuse(project_id: str, expected_name: str = "", *,
                             fresh: bool = False, require_known: bool = False,
                             prefix: str = "") -> Optional[str]:
    """Обёртка отказа по папке: сам отказ плюс ПРЕФИКС фазы (2026-08-09,
    ZAHOD1.md 1.2.3, П11).

    Пятнадцать площадок писали это вручную, и пять из них ещё и подменяли
    префикс собственной парой `.replace(...)`, скопированной слово в слово.
    Префикс — ПАРАМЕТР одной обёртки, а не её вторая версия: на плане
    «🛑 План НЕ построен — … Ничего не изменено.», на исполнении — то, что
    печатает сам `_guard_project`. Пустой `prefix` ничего не подменяет."""
    refusal = _guard_project(project_id, expected_name, fresh=fresh,
                             require_known=require_known)
    if refusal and prefix:
        refusal = (refusal.replace("🛑 Отказ —", prefix, 1)
                          .replace("Ничего не тронул.", "Ничего не изменено."))
    return refusal


# Префикс плановой фазы для обёртки выше — один литерал вместо пяти копий.
_PLAN_REFUSAL_PREFIX = "🛑 План НЕ построен —"


def _mismatch_report(mismatch: List[Dict], verb: str) -> str:
    """Human line for the identity guard: ids whose live task didn't match the
    name the caller expected, so we refused to touch them."""
    parts = [f"«{m['expected']}» → по id это «{m['actual']}»"
             + (f" в «{m['project']}»" if m.get("project") else "")
             for m in mismatch]
    return (f"🛑 НЕ {verb} {len(mismatch)} — id НЕ совпал с названием "
            "(защита от «не той задачи»): " + "; ".join(parts))


def format_task_list(tasks: List[Dict], limit: int = 100) -> str:
    """Compact, one-line-per-task rendering with project names resolved once."""
    names = _v2_project_names()
    lines = [format_task_line(t, names.get(t.get("projectId"))) for t in tasks[:limit]]
    out = "\n".join(lines)
    if len(tasks) > limit:
        out += f"\n... and {len(tasks) - limit} more."
    return out


def format_task_tree(tasks: List[Dict], limit: int = 200) -> str:
    """Render tasks as a hierarchy: subtasks indented under their parent,
    recursing to ARBITRARY depth (grandchildren, great-grandchildren, …).
    If a subtask's parent is not in this list, it appears at the top level."""
    names = _v2_project_names()
    task_ids = {t.get("id") for t in tasks if t.get("id")}
    top = [t for t in tasks if not t.get("parentId") or t.get("parentId") not in task_ids]
    children: Dict[str, List] = {}
    for t in tasks:
        pid = t.get("parentId")
        if pid and pid in task_ids:
            children.setdefault(pid, []).append(t)

    lines: List[str] = []
    seen = set()  # guard against cyclic parentId references

    def walk(task: Dict, depth: int) -> None:
        if len(lines) >= limit:
            return
        tid = task.get("id")
        if tid in seen:
            return
        seen.add(tid)
        if depth == 0:
            lines.append(format_task_line(task, names.get(task.get("projectId"))))
        else:
            lines.append("  " * depth + "↳ " + format_task_line(task))
        for kid in children.get(tid or "", []):
            if len(lines) >= limit:
                return
            walk(kid, depth + 1)

    for t in top:
        if len(lines) >= limit:
            break
        walk(t, 0)

    out = "\n".join(lines)
    if len(tasks) > limit:
        out += f"\n... and {len(tasks) - limit} more."
    return out


# Default page sizes for the two whole-account readers (def-E1). Calibrated on
# the owner's live account, where BOTH tools returned an over-limit error
# instead of data: get_all_tasks printed 164 871 chars for 1481 tasks (≈111
# chars per compact line) and get_project_tasks 153 436 chars for a single
# 310-task project (≈495 chars per full task card). At the measured ~1.9 chars
# per token that is ~87k and ~81k tokens — far past any tool-output budget.
# A default page costs ≈22 KB (~12k tokens) and ≈25 KB (~13k tokens): an order
# of magnitude under the old dumps, with the tail reachable one call at a time.
# 200 also matches the neighbouring get_tasks_by_priority page size.
_ALL_TASKS_PAGE = 200
_PROJECT_TASKS_PAGE = 50

# Page size for the other two compact-tree readers (run_filter,
# get_inbox_tasks). They print the SAME compact line as get_all_tasks
# (format_task_tree), measured at 135 chars per task on a realistic task
# (title + due date + priority + tag + ids): a 200-task page is ~27 KB (~14k
# tokens), the whole 1436-match filter would be 194 185 chars (~102k tokens).
# 200 is also the ceiling format_task_tree already applied on its own — so the
# default page stays exactly the size it was; what changes is that the tail is
# now reachable instead of being announced as "... and 1236 more." and dropped.
_TREE_PAGE = 200


def _page_task_forest(tasks: List[Dict], limit: int, offset: int):
    """One page of a task FOREST: whole subtrees only, never a torn-off child.

    Slicing the flat list instead would tear a subtask off its parent exactly
    at the page boundary — and format_task_tree() renders such an orphan at the
    TOP level (its parent is "not in this list"), i.e. a subtask would silently
    read as a standalone task. So a page is filled with top-level tasks TOGETHER
    with their descendants, and stops once `limit` tasks are collected: the
    boundary always falls between trees, never inside one.

    `limit` counts TASKS (that's what the output size is made of, so the page
    stays bounded even if one parent carries a hundred subtasks); it is a soft
    ceiling — the tree that crosses it is finished, not cut. `offset` counts
    TOP-LEVEL tasks, which is what the returned `shown_to` (and the caller's
    footer) hands back for the next call.

    Returns (page_tasks, total_roots, offset, shown_to) where page_tasks is a
    flat list — roots each followed by their descendants — ready for the
    existing grouping/formatting code.
    """
    ids = {t.get("id") for t in tasks if t.get("id")}
    children: Dict[str, List] = {}
    roots: List[Dict] = []
    for t in tasks:
        pid = t.get("parentId")
        if pid and pid in ids:
            children.setdefault(pid, []).append(t)
        else:
            # Same rule as format_task_tree: a subtask whose parent isn't in
            # this list is a root here, so it can't fall out of the paging.
            roots.append(t)

    total_roots = len(roots)
    offset = max(0, offset)
    limit = max(1, limit)

    page: List[Dict] = []
    seen = set()  # guard against cyclic parentId references, as in the tree
    roots_taken = 0

    def walk(task: Dict) -> None:
        tid = task.get("id")
        if tid in seen:
            return
        seen.add(tid)
        page.append(task)
        for kid in children.get(tid or "", []):
            walk(kid)

    for root in roots[offset:]:
        if page and len(page) >= limit:
            break
        walk(root)
        roots_taken += 1

    return page, total_roots, offset, offset + roots_taken


def _last_page_offset(total: int, limit: int) -> int:
    """Offset of the last non-empty page — what to tell a caller who overshot."""
    limit = max(1, limit)
    return max(0, (max(total, 1) - 1) // limit * limit)


def _valid_offset_range(total_roots: int) -> str:
    """Готовый хвост про диапазон допустимых offset — для читателей, режущих
    ДЕРЕВЬЯ (2026-08-09, ZAHOD1.md 1.2.3, П11).

    Отдельный помощник от `_last_page_offset`: у `_page_task_forest` страницы
    плавающие (поддерево дописывается целиком), поэтому назвать «начало
    последней страницы» нельзя — врать про неё хуже, чем назвать диапазон.
    Отдаётся только ЧИСЛО в готовом хвосте, предложение по-прежнему собирает
    вызывающий: у трёх площадок вокруг него разные слова, и сведение их к
    одному тексту меняло бы ответ сервера."""
    return f"valid offsets are 0-{total_roots - 1}"


# --- Readiness helpers ------------------------------------------------------

_INIT_FAIL_MSG = "Failed to initialize TickTick client. Please check your API credentials."


def _ensure_official() -> Optional[str]:
    """Return an error string if the official-API client isn't ready, else None.
    Lazily (re-)initializes it on first use. Analogous to _ensure_ready (v2)."""
    if not ticktick:
        if not initialize_client():
            return _INIT_FAIL_MSG
    return None


async def _run_blocking(func, *args, **kwargs):
    """Run a synchronous (requests-based) client call off the event loop so it
    doesn't block /health and other streamable-http sessions. Uniform wrapper
    used by tools that touch the blocking clients."""
    return await asyncio.to_thread(func, *args, **kwargs)


# MCP Tools

@mcp.tool(annotations=READONLY)
async def get_projects() -> str:
    """Get all projects from TickTick — including the built-in Inbox — each
    with the folder (project group) it sits in ("Folder: (none)" when it sits
    in none). The Inbox is listed first and its id works like any other
    project id here (read it with get_project_tasks, target it on create)."""
    err = _ensure_official()
    if err:
        return err

    try:
        projects = await _run_blocking(lambda: ticktick.get_projects())
        if 'error' in projects:
            return _tool_error("fetching projects", projects['error'])

        # One v2 state read for the whole listing: the folder map (resolved
        # once, not per project) and the built-in Inbox.
        group_names, inbox = await _run_blocking(_v2_groups_and_inbox)

        # The official /project endpoint omits the Inbox entirely, so without
        # this the listing reads as "you have no Inbox" while tasks sit in it.
        # Matched by id, so if TickTick ever starts returning the Inbox
        # itself, it is NOT listed twice.
        if inbox and not any(p.get("id") == inbox["id"] for p in projects):
            projects = [inbox] + projects

        if not projects:
            return "No projects found."

        result = f"Found {len(projects)} projects:\n\n"
        for i, project in enumerate(projects, 1):
            result += f"Project {i}:\n" + format_project(project, group_names) + "\n"

        return result
    except Exception as e:
        logger.exception("Error in get_projects")
        return _tool_error("fetching projects", e)

@mcp.tool(annotations=READONLY)
async def get_project(project_id: str) -> str:
    """
    Get details about a specific project, including the folder (project
    group) it sits in — "Folder: (none)" when it sits in none.

    Args:
        project_id: ID of the project (the Inbox id from get_projects works too)
    """
    err = _ensure_official()
    if err:
        return err

    # The Inbox is not a project for the official API — asking it about the
    # Inbox id is an error, not a project card. Serve it from the v2 state so
    # an id taken out of get_projects doesn't dead-end here.
    inbox = await _run_blocking(_inbox_project)
    if inbox and project_id == inbox["id"]:
        return format_project(inbox)

    try:
        project = await _run_blocking(lambda: ticktick.get_project(project_id))
        if isinstance(project, dict) and 'error' in project:
            return _tool_error("fetching project", project['error'])

        # Тот же отказ, что и в get_task: пустой ответ печатался как проект
        # «No name / (id: ?)», то есть как существующий.
        refusal = _identity_or_refusal(
            project, project_id, "Проект", "не найден",
            hint="Список живых проектов и их id — get_projects().")
        if refusal:
            return refusal

        # Folder name comes from the v2 state — resolve it off the event loop
        # instead of letting format_project() do it inline.
        group_names = await _run_blocking(_v2_group_names)
        return format_project(project, group_names)
    except Exception as e:
        logger.exception("Error in get_project")
        return _tool_error("fetching project", e)

@mcp.tool(annotations=READONLY)
async def get_project_tasks(project_id: str, limit: int = _PROJECT_TASKS_PAGE,
                            offset: int = 0) -> str:
    """
    Get all tasks in a specific project.

    The output is paged: the header always states the TOTAL number of tasks in
    the project and which range is shown, and when it doesn't fit, the last
    line says how many are left and which offset continues the list.

    The Inbox id is served by get_inbox_tasks (the official API knows nothing
    about the Inbox): limit/offset are passed through unchanged, but that
    listing is a compact tree, so `offset` counts TOP-LEVEL tasks there.

    Args:
        project_id: ID of the project (the Inbox id from get_projects works too)
        limit: Maximum tasks to show in one call (default 50 — a full task card
            is ~0.5 KB, so a page is ~25 KB)
        offset: Skip this many tasks — use it to read the tail past `limit`
    """
    err = _ensure_official()
    if err:
        return err

    # Same as get_project: the official /project/<id>/data endpoint knows
    # nothing about the Inbox, so route the Inbox id to the v2-backed reader
    # instead of returning its "project not found" to the caller.
    inbox = await _run_blocking(_inbox_project)
    if inbox and project_id == inbox["id"]:
        # The caller's limit/offset go THROUGH to get_inbox_tasks, which takes
        # the same pair. Until 2026-08-07 they were dropped here with a warning
        # that pointed at get_inbox_tasks "which pages on its own" — a method
        # that had no parameters at all, i.e. the tail of a big Inbox was
        # unreachable down either path. `offset` counts top-level tasks there
        # (the Inbox is printed as a compact tree, not as task cards).
        return await get_inbox_tasks(limit=limit, offset=offset)

    try:
        project_data = await _run_blocking(lambda: ticktick.get_project_with_data(project_id))
        if 'error' in project_data:
            return _tool_error("fetching project data", project_data['error'])
        
        tasks = project_data.get('tasks', [])
        pname = project_data.get('project', {}).get('name', project_id)
        if not tasks:
            return f"No tasks found in project '{pname}'."

        # def-E1: раньше печатались ВСЕ задачи проекта — на боевом проекте из
        # 310 задач это 153 436 символов, то есть ответ не влезал в лимит и
        # инструмент возвращал ошибку вместо данных. Теперь страница
        # ограничена, а хвост достижим через offset.
        total = len(tasks)
        offset = max(0, offset)
        limit = max(1, limit)
        page = tasks[offset:offset + limit]
        if not page:
            # Пустая страница за концом списка — это НЕ «задач нет».
            return (f"Project '{pname}' has {total} tasks, but offset={offset} is past "
                    f"the end (last page starts at offset={_last_page_offset(total, limit)}).")
        shown_to = offset + len(page)

        if offset or shown_to < total:
            result = (f"Found {total} tasks in project '{pname}' "
                      f"(showing {offset + 1}-{shown_to}):\n\n")
        else:
            result = f"Found {len(tasks)} tasks in project '{pname}':\n\n"
        # Нумерация сквозная: иначе вторая страница выглядит как первая.
        for i, task in enumerate(page, offset + 1):
            result += f"Task {i}:\n" + format_task(task) + "\n"
        if shown_to < total:
            result += (f"... and {total - shown_to} more — call again with "
                       f"offset={shown_to}.\n")

        return result
    except Exception as e:
        logger.exception("Error in get_project_tasks")
        return _tool_error("fetching project tasks", e)

# v2's trash endpoint caps a page at 500; ask for the whole page so "not in the
# trash" is as close to a full answer as one request can be.
_TRASH_SCAN_LIMIT = 500


async def _trash_state(task_id: str) -> tuple:
    """Is this task in the trash — and could that even be checked?

    The official v1 API does not know. GET /project/{pid}/task/{tid} happily
    returns a DELETED task's object with no deletion flag anywhere in it, which
    is exactly how get_task came to print "Status: Active" about two tasks
    sitting in the bin. The only source of truth is the v2 trash listing, and it
    costs ONE extra GET /project/all/trash/pagination per get_task call.

    That price is paid on purpose: get_task is the point-lookup tool people use
    as PROOF about a single task (not a loop over a list — format_task's other
    callers get trash_state=None and pay nothing), and staying silent here reads
    as "definitely not deleted", a claim the source never made.

    Returns (in_trash, note):
      (True,  "")     found in the trash;
      (False, "")     trash read in full, not there;
      (False, note)   only the most recent page was read — older trash unchecked;
      (None,  note)   could not check at all (v2 unavailable / read failed).
    """
    unknown = ("Trash: NOT CHECKED — the official API carries no deletion flag, "
               "so this task could be in the trash and still read as above.\n")
    if not ticktick_v2:
        return None, unknown
    try:
        trashed = await _run_blocking(lambda: ticktick_v2.get_trash(_TRASH_SCAN_LIMIT))
    except Exception as e:
        logger.warning(f"get_task: trash check failed: {e}")
        return None, unknown
    if any(t.get("id") == task_id for t in (trashed or [])):
        return True, ""
    if len(trashed or []) >= _TRASH_SCAN_LIMIT:
        return False, (f"Trash: not among the {_TRASH_SCAN_LIMIT} most recently "
                       "deleted tasks; older trash was not checked.\n")
    return False, ""


@mcp.tool(annotations=READONLY)
async def get_task(project_id: str, task_id: str) -> str:
    """
    Get details about a specific task. The Status line is cross-checked against
    the trash (v2), because the official API reports a deleted task as if it
    were still live; when that check is impossible the output says so.

    Args:
        project_id: ID of the project
        task_id: ID of the task
    """
    err = _ensure_official()
    if err:
        return err

    try:
        task = await _run_blocking(lambda: ticktick.get_task(project_id, task_id))
        if isinstance(task, dict) and 'error' in task:
            return _tool_error("fetching task", task['error'])

        # Пустой/чужой ответ — это ОТКАЗ, а не карточка «No title / Active».
        refusal = _identity_or_refusal(
            task, task_id, "Задача", "не найдена",
            hint=(f"Проверял в проекте «{project_id}» — задача может лежать в "
                  "другом списке. Попробуй get_task_info(task_id) (ищет по "
                  "всем спискам, включая завершённые и корзину)."))
        if refusal:
            return refusal

        in_trash, note = await _trash_state(task_id)
        return format_task(task, trash_state=in_trash) + note
    except Exception as e:
        logger.exception("Error in get_task")
        return _tool_error("fetching task", e)

def _build_v2_task_obj(node: Dict, project_id: str, task_id: str,
                       parent_id: str = None) -> Dict:
    """Convert a task definition dict into a v2 batch task object."""
    obj: Dict[str, Any] = {
        "id": task_id,
        "title": node.get("title", ""),
        "projectId": project_id,
        "status": 0,
        "priority": node.get("priority", 0),
    }
    if parent_id:
        obj["parentId"] = parent_id
    for src, dst in (("due_date", "dueDate"), ("start_date", "startDate")):
        if node.get(src):
            val, all_day = _normalize_date(node[src])
            obj[dst] = val
            if all_day:
                obj["isAllDay"] = True
    if node.get("content"):
        obj["content"] = node["content"]
    if node.get("tags"):
        obj["tags"] = node["tags"]
    if node.get("assignee") is not None:
        obj["assignee"] = node["assignee"]
    return obj


def _requested_tree_depth(node: Dict) -> int:
    """Depth (in levels, the node itself counted as 1) of a task/subtask dict
    tree AS REQUESTED by the caller — a pure function over the payload, no
    I/O. String subtasks are leaves (they can't carry their own `subtasks`
    field); dict subtasks recurse. Used to fail-closed BEFORE building
    anything when a request would exceed MAX_TASK_NEST_LEVELS, instead of
    silently truncating deep branches after the fact."""
    subtasks = node.get("subtasks") or []
    if not subtasks:
        return 1
    return 1 + max(
        (_requested_tree_depth(s) if isinstance(s, dict) else 1) for s in subtasks
    )


def _flatten_task_tree(node: Dict, project_id: str, parent_id: str = None,
                       level: int = 0, max_level: int = MAX_TASK_NEST_LEVELS - 1):
    """Recursively flatten a nested task tree.
    Returns (tasks, relations) where:
      tasks     — list of v2 task objects WITHOUT parentId (TickTick ignores it in batch/task)
      relations — list of {"parentId","taskId","projectId"} for batch/taskParent call
    IDs are pre-generated so both calls can be built before any HTTP request.
    max_level (default MAX_TASK_NEST_LEVELS-1=4) means task + 4 levels of
    nesting (5 levels total — TickTick's own real cap, see
    MAX_TASK_NEST_LEVELS above). This is a defense-in-depth backstop only:
    callers are expected to refuse oversized requests via
    _requested_tree_depth() BEFORE reaching this function, so in practice the
    cutoff below should never actually trigger and silently drop a branch."""
    import uuid as _uuid
    task_id = _uuid.uuid4().hex[:24]
    obj = _build_v2_task_obj(node, project_id, task_id, parent_id=None)
    tasks = [obj]
    relations = []
    if parent_id:
        relations.append({"parentId": parent_id, "taskId": task_id,
                          "projectId": project_id})
    if level < max_level:
        for child in (node.get("subtasks") or []):
            if isinstance(child, str):
                child = {"title": child}
            child_tasks, child_rels = _flatten_task_tree(
                child, project_id, task_id, level + 1, max_level)
            tasks.extend(child_tasks)
            relations.extend(child_rels)
    return tasks, relations


# П19 (2026-08-09): «создаётся подзадачей — жив ли родитель и он ли это».
# Отказы печатаются ОДНИМ текстом на обоих входах — на фазе плана
# (plan_task_creation) и в ядре мутации (_create_tasks_impl), — чтобы
# владелец читал одно и то же, каким бы путём запрос ни пришёл. Слова взяты
# у _create_subtask_impl, который эту же сверку делает с самого начала; там
# же и причина, почему «не среди открытых» — это отказ, а не пометка: без
# живого родителя TickTick кладёт новую задачу отдельной строкой В КОРЕНЬ
# списка, и «✓ создано» оказывается правдой про запись и ложью про место.
def _dead_parent_note(parent_label: str, parent_id: str) -> str:
    who = (f"родитель «{parent_label}» ({str(parent_id)[:8]}…)"
           if parent_label else f"родитель {str(parent_id)[:8]}…")
    return (f"{who} не среди открытых задач (завершён/удалён/в корзине) — "
            "подзадача НЕ создаётся, иначе она легла бы отдельной задачей "
            "в корне")


def _wrong_parent_note(real_title: str, expected_title: str) -> str:
    return (f"родитель по id это «{real_title}», а НЕ «{expected_title}» "
            "(защита от «не той задачи») — подзадача НЕ создаётся")


# Единственная формулировка «родителя сверить не удалось». На ПЛАНЕ это не
# отказ (разовый сбой чтения не имеет права заблокировать всякое создание —
# решение уже принято в create_subtask), но сказать об этом владельцу вслух
# обязательно: он подтверждает вложение, которого сервер не читал.
_PARENT_UNVERIFIED_NOTE = (
    "⚠️ Родителя сверить с живым состоянием НЕ удалось (чтение не удалось) — "
    "сверка повторится при исполнении, и расхождение его остановит.")


@mcp.tool()
@_shared_notes(automation=True)
async def create_tasks(
    summary: str = "",
    tasks: List[Dict[str, Any]] = None,
    automation_key: str = ""
) -> str:
    """
    Create one or more tasks in TickTick with full nested subtask support
    (up to 5 levels: task → subtask → sub-subtask → sub-sub-subtask →
    sub-sub-sub-subtask — TickTick's own real cap; requests that would nest
    deeper are refused outright, nothing partial gets created).

    ⛔ INTERACTIVE ASSISTANTS: this tool will REFUSE your call. Use
    plan_task_creation (read-only) → reprint its echo VERBATIM → get the
    user's explicit «да/ок» → execute_task_creation(manifest_id, user_reply=...)
    → operation_report. Do NOT try to fill automation_key — you don't know it
    and guessing is a protocol violation.

    {{AUTOMATION_KEY_NOTE}}

    summary (FIRST arg): one-line human sentence IN THE USER'S LANGUAGE shown
    at the TOP of the summary you show the user (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's), e.g.
    «Создаю задачу „Позвонить маме" в „Личное", срок 2026-07-01, приоритет высокий»
    or «Создаю 3 задачи в „Работа"». Include date and priority when set.

    For a single task, pass a one-element list. For multiple tasks, pass all items
    at once — do NOT call this tool in a loop.

    ── Supported fields per task/subtask object ──
      title (required at root), project_id (required at root only — inherited by subtasks),
      content, start_date, due_date,
      priority (0=None / 1=Low / 3=Medium / 5=High, default 0),
      tags (list of tag names; requires v2),
      assignee (user ID; requires shared project + v2),
      column_id (kanban section ID; root task only; use list_project_columns),
      parent_id (existing task ID to attach root as a subtask; requires v2),
      parent_title (the parent's CURRENT title — optional but strongly
        advised whenever parent_id is set: the server checks the parent is
        still alive either way, and this additionally proves the id names
        the task you mean. A mismatch refuses the row BEFORE anything is
        created, instead of quietly attaching your subtask to whatever that
        id now points at),
      repeat_flag (RRULE; root task only via official API; use build_recurrence_rule),
      reminders (list of triggers; root task only via official API; use build_reminder),
      subtasks (list of strings OR list of full task objects — recursive, up to 4 levels deep)

    Dates: use "YYYY-MM-DD" for all-day; full ISO "YYYY-MM-DDThh:mm:ss+0000"
    only when the user specified an exact time. Do NOT invent a time.
    For "today"/"tomorrow"/"yesterday" or a bare weekday name (RU too:
    "сегодня"/"завтра"/"вчера", "понедельник".."воскресенье"/"пн".."вс") pass
    that WORD literally instead of computing the date yourself — the server
    resolves it off its own real clock (a weekday name resolves to the
    nearest such day from today, today included). This matters in long
    conversations: if your own sense of "today" has drifted (stale context),
    a self-computed date silently lands on the wrong day; the literal word
    never can.

    ── Examples ──

    Simple batch:
      [{"title": "A", "project_id": "x"},
       {"title": "B", "project_id": "x", "priority": 5, "due_date": "2026-07-10"}]

    Nested structure (strings):
      [{"title": "Epic", "project_id": "x",
        "subtasks": ["Step 1", "Step 2", "Step 3"]}]

    Nested structure with full params (up to 5 levels):
      [{"title": "Q3 Launch", "project_id": "x", "priority": 5,
        "subtasks": [
          {"title": "Design", "due_date": "2026-07-15", "priority": 3,
           "subtasks": [
             {"title": "Mockups", "due_date": "2026-07-10",
              "subtasks": [{"title": "Mobile screens",
                            "subtasks": [{"title": "Icon set"}]}]}
           ]},
          {"title": "Dev", "due_date": "2026-07-20",
           "subtasks": [{"title": "Backend"}, {"title": "Frontend"}]}
        ]}]

    Attach as subtask of existing task:
      [{"title": "New step", "project_id": "x", "parent_id": "<existing_task_id>"}]

    Args:
        summary: Human-readable confirmation line (see above). NOT required by
            the schema: an interactive call is refused whatever it says, so
            omitting it still gets you the refusal (with the correct route)
            instead of a validation error. Headless automation should still
            pass it — it is what the operation journal records.
        tasks: List of task definition objects — one item for a single task

    Returns:
        A formatted summary. Each successfully-created root task line ends with
        the created task's id as `(id:<id>)` so callers can link it without a
        follow-up title search.

        A created task is NOT the same claim as a created TREE. Nesting, tags
        and assignee each travel over the v2 batch endpoints, which answer
        HTTP 200 with per-item rejections INSIDE the body — every one of those
        answers is read, and anything TickTick refused is named in the report
        («связи родитель-подзадача не применились», «теги НЕ применились»,
        «исполнитель НЕ назначен») instead of being smoothed into the ✓ line.
        The post-operation block additionally re-reads live state and checks
        the LINKS themselves, so subtasks that exist but sit at top level are
        reported («подзадачи созданы, но НЕ вложены под родителя»); the same
        expectations go into the operation journal, so operation_report
        re-checks them independently. New tags are registered in the account
        tag list first — a tag set here shows up in list_tags and is deletable
        with delete_tag, never an orphan label.

    TELEGRAM CONFIRMATION LAYER (optional, off by default): "create_tasks" is
    also the NAME this server announces creation plans under in Telegram —
    plan_task_creation sends the owner a message with [✅ Подтвердить]/
    [🛑 Отклонить] buttons itself, and pressing "Подтвердить" makes the server
    perform the creation on its own (background poller), reporting back into
    that same message. It is the server doing this, not some external relay
    on top of MCP. This direct entry point stays automation-only and is NOT
    part of that flow: `automation_key` bypasses the interactive gate
    entirely, so no button and no `user_reply` are involved here.
    """
    if not _automation_key_matches(automation_key):
        return ("🛑 Прямое создание — только для автоматики. Интерактивный флоу: "
                "plan_task_creation (покажи эхо пользователю дословно) → явное "
                "«да» → execute_task_creation(manifest_id, user_reply=<реплика>) "
                "→ operation_report. Ничего не создано.")
    # `summary`/`tasks` необязательны В СХЕМЕ (2026-08-07): интерактивный
    # вызывающий всё равно получает отказ выше, и требовать от него поля,
    # которые ни на что не влияют, значило отдавать ему `1 validation error`
    # вместо маршрута. Для автоматики, дошедшей сюда, summary по-прежнему
    # нужен — он идёт в журнал операций, поэтому пустой заменяется явной
    # подписью, а не уезжает в журнал безымянной строкой.
    return await _create_tasks_impl(summary or "Создание задач (автоматика)",
                                    tasks or [])


async def _create_tasks_impl(summary: str, tasks: List[Dict[str, Any]]) -> str:
    """Shared creation engine behind create_tasks (direct/headless) and
    execute_task_creation (approved manifest)."""
    err = _ensure_official()
    if err:
        return err

    if not tasks:
        return "No tasks provided."

    created = []
    failed = []

    # (title, id, expected_pid, expected_col, expected_parent, expected_tags)
    # — checked at the end. expected_parent/expected_tags появились 2026-08-09
    # (д5/д6): раньше привязка и теги не проверялись после операции вообще.
    to_verify = []
    # (title, id, project_id, expected_parent) созданных ПОДЗАДАЧ — сверяется
    # и существование, и то, что подзадача реально лежит под родителем.
    sub_verify = []
    _depth_by_id = None  # lazily-fetched live state, only if some task attaches to an existing parent

    # Один сброс кэша на весь вызов (не на каждую задачу): официальный API
    # создания молча перекладывает задачу в Inbox, если переданный projectId
    # не резолвится в ЖИВОЙ проект (битый/устаревший/удалённый id) — при
    # этом приходит 200 с id задачи, то есть внешне это выглядит успехом,
    # если не поймать это ЗДЕСЬ, до записи. Один invalidate + первый вызов
    # _guard_project ниже форсируют один реальный перезапрос; остальные
    # элементы того же батча переиспользуют это свежее состояние (оно ещё
    # в пределах своего короткого TTL) вместо перезапроса на каждую задачу.
    if ticktick_v2:
        try:
            ticktick_v2.invalidate_cache()
        except Exception:
            pass

    for i, t in enumerate(tasks):
        # Idempotent: a no-op if plan_task_creation already resolved these
        # (defends direct/headless callers that skip the plan phase).
        _resolve_dates_in_task_tree(t)
        title = t.get("title")
        project_id = t.get("project_id") or t.get("projectId")
        if not title or not project_id:
            failed.append(f"#{i+1}: missing title or project_id")
            continue
        # Guard назначения, FAIL-CLOSED: project_id ОБЯЗАН резолвиться в
        # живой проект — require_known=True отказывает, даже если вызывающий
        # не передал project_name для сверки (обычный случай). Без этого
        # нерезолвящийся id уходил прямиком в вызов создания, и бэкенд
        # TickTick молча сбрасывал задачу в Inbox — баг доставки, который
        # читается как чистый «✓ создано».
        exp_proj = t.get("project_name") or t.get("projectName") or ""
        refusal = _guard_project_or_refuse(project_id, exp_proj,
                                           require_known=True)
        if refusal:
            failed.append(f"#{i+1} «{title}»: {refusal}")
            continue
        priority = t.get("priority", 0)
        if priority not in [0, 1, 3, 5]:
            failed.append(f"#{i+1} «{title}»: неверный приоритет")
            continue

        # Depth guard: TickTick supports at most MAX_TASK_NEST_LEVELS total
        # levels counting from the root task — see MAX_TASK_NEST_LEVELS
        # above. The requested tree's own depth comes straight from the
        # payload (nothing is live yet); if it attaches under an EXISTING
        # task (parent_id), that task's LIVE depth is added on top — a
        # caller's own claim about how deep it already sits is never
        # trusted. Refused up front, fail-closed — nothing in this task is
        # created, not even the levels that would have fit.
        ext_parent_id = t.get("parent_id")
        base_level = 0
        if ext_parent_id:
            if _depth_by_id is None:
                _depth_by_id = _open_by_id(fresh=True)
            if _depth_by_id is None:
                failed.append(f"#{i+1} «{title}»: {_STATE_UNAVAILABLE_MSG}")
                continue
            # П19 (2026-08-09): ЖИВ ЛИ РОДИТЕЛЬ — до записи, а не после.
            # Раньше единственным обращением к живому состоянию здесь был
            # расчёт глубины, а он на неизвестном id молча отвечает «уровень
            # 1»: мёртвый родитель читался как живой корневой, задача
            # создавалась и ложилась ОТДЕЛЬНОЙ строкой в корень, а отчёт
            # говорил «создано» (привязку разбирал только пост-verify —
            # после факта).
            #
            # Тот же снимок, что уже загружен ради глубины (by_id=), — ни
            # одного лишнего запроса. Порядок важен: сперва существование,
            # потом глубина, иначе остаётся ветка, где «уровень 1» получен
            # из пустоты.
            #
            # Различие «объекта нет» и «я не смотрел» держится тем, что оба
            # состояния приходят РАЗНЫМИ ответами и оба закрыты: снимок
            # прочитать не удалось → выход выше (_STATE_UNAVAILABLE_MSG);
            # снимок прочитан и родителя в нём нет → _guard_task сам
            # доспрашивает официальный Open API (штатный запасной канал —
            # v2-лента отстаёт, см. _official_task_snapshot) и лишь потом
            # отвечает 'missing'. Пустота как разрешение не читается ни в
            # одной из веток.
            pg = _guard_task(ext_parent_id, t.get("parent_title") or "",
                             project_id, by_id=_depth_by_id)
            if pg.status == "unavailable":
                failed.append(f"#{i+1} «{title}»: {pg.message}")
                continue
            # 🛑 приклеивается ЗДЕСЬ, а не внутрь общей формулировки: на фазе
            # плана тот же текст уже стоит под шапкой «🛑 Исключены N», и
            # второй значок в строке читался бы как вторая беда.
            note, _warn = _guard_or_refuse(
                pg, stage="пакет-родитель",
                expected=t.get("parent_title") or "", parent_id=ext_parent_id)
            if note:
                failed.append(f"#{i+1} «{title}»: 🛑 {note}. Ничего не изменено.")
                continue
            base_level = _task_level(ext_parent_id, _depth_by_id)
        total_depth = base_level + _requested_tree_depth(t)
        if total_depth > MAX_TASK_NEST_LEVELS:
            failed.append(
                f"#{i+1} «{title}»: 🛑 запрошенная вложенность даёт "
                f"{total_depth} уровней вместо {MAX_TASK_NEST_LEVELS} "
                "поддерживаемых TickTick (считая от корневой задачи) — "
                "задача НЕ создана целиком.")
            continue

        has_nested = any(
            isinstance(s, dict) for s in (t.get("subtasks") or [])
        )
        has_advanced = t.get("repeat_flag") or t.get("reminders")

        try:
            # ── PATH A: nested dict subtasks → tree via two v2 calls ──
            if ticktick_v2 and has_nested and not has_advanced:
                # д6 (2026-08-09): теги и исполнитель КОРНЯ убраны из полезной
                # нагрузки создания — раньше _build_v2_task_obj клал их прямо
                # в тело /batch/task вместе с самим созданием, и это МИМО
                # проверенного ядра простановки тегов (_apply_tags_verified):
                # без регистрации тега в аккаунте, без живой сверки «что
                # просили = что стало». Оба поля проставляются НИЖЕ, отдельно,
                # тем же путём, что и в соседней ветке (не дерево). Подзадачи
                # дерева не тронуты — их теги/исполнитель по-прежнему едут
                # внутри payload, это вне этой правки.
                _root_for_tree = dict(t)
                _root_for_tree.pop("tags", None)
                _root_for_tree.pop("assignee", None)
                tasks_flat, relations = _flatten_task_tree(
                    _root_for_tree, project_id, parent_id=t.get("parent_id"))
                sub_notes = []
                resp = await _run_blocking(
                    lambda: ticktick_v2.batch_create_tasks(tasks_flat))
                tree_fail = id2error_failures(
                    resp, [x["id"] for x in tasks_flat])
                rel_fail = {}
                if relations:
                    rel_resp = await _run_blocking(lambda: ticktick_v2._request(
                        "POST", "/batch/taskParent", json=relations))
                    rel_fail = id2error_failures(
                        rel_resp, [r.get("taskId") for r in relations])
                    if rel_fail:
                        sub_notes.append(
                            f"⚠️ связи родитель-подзадача не применились у "
                            f"{len(rel_fail)}: "
                            + "; ".join(f"{k[:8]}…: {v}" for k, v in rel_fail.items()))
                await _run_blocking(lambda: ticktick_v2.invalidate_cache())
                root_id = tasks_flat[0]["id"]
                if tree_fail:
                    sub_notes.append(
                        f"⚠️ TickTick отклонил {len(tree_fail)} из {len(tasks_flat)} "
                        "задач дерева: "
                        + "; ".join(f"{k[:8]}…: {v}" for k, v in tree_fail.items()))
                if t.get("column_id"):
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_column(root_id, t["column_id"]))
                    except Exception as e:
                        logger.warning(f"Column failed: {e}")
                        sub_notes.append(f"⚠️ раздел (column) не применился: {_redact_for_user(e)}")
                # Д11 (2026-08-09): число в ГЛАВНОЙ строке считается по
                # СОЗДАННОМУ, а не по запрошенному. Раньше здесь стояло
                # `len(tasks_flat)` целиком — то есть строка «✓ «Ремонт» + 7
                # подзадач» печаталась и тогда, когда канал только что
                # отклонил половину дерева, прямо противореча собственному
                # предупреждению двумя строками выше. Соседняя ветка того же
                # метода (PATH B, `sub_count = len(all_sub_tasks) -
                # len(sub_fail)`) вычитала отказы всегда — правило берётся
                # оттуда. Корень считается отдельно: когда отклонён он сам,
                # «✓ «title»» было бы ложью про саму задачу, а не про счёт.
                total = len(tasks_flat)
                made = total - len(tree_fail)
                root_ok = tasks_flat[0]["id"] not in tree_fail
                kids_made = made - (1 if root_ok else 0)
                if root_ok:
                    line = f"✓ «{title}» + {kids_made} подзадач (дерево, "
                    line += (f"{made} из {total})" if tree_fail
                             else f"{total} всего)")
                else:
                    line = (f"⚠️ «{title}» — САМА задача НЕ создана, TickTick "
                            f"отклонил её (из дерева создано {made} из "
                            f"{total})")
                # Родитель КОРНЯ дерева (когда всё дерево вешают под уже
                # существующую задачу) — такое же ожидание, как у подзадач:
                # ожидается только если канал не сообщил об отказе по этой
                # связи (д5, 2026-08-09).
                root_rel = next((r for r in relations
                                 if r.get("taskId") == tasks_flat[0]["id"]), None)
                root_parent = (root_rel or {}).get("parentId") \
                    if root_rel and root_rel.get("taskId") not in rel_fail else None
                # Теги и исполнитель КОРНЯ (д6, 2026-08-09) — тем же ядром
                # (`_apply_tags_verified`) и тем же приёмом чтения id2error,
                # что в соседней ветке (не дерево), см. PATH B выше. Только
                # когда сам корень реально создан: назначать поля
                # несуществующей задаче бессмысленно.
                tags_expect_root = None
                assignee_expect_root = None
                if root_ok:
                    if t.get("tags"):
                        try:
                            tag_out = await _apply_tags_verified(
                                [{"taskId": root_id, "title": title,
                                  "projectId": project_id, "tags": t["tags"]}])
                            sub_notes.extend(_tag_notes_for_create(tag_out))
                            tags_expect_root = tag_out.tags_by_id.get(root_id)
                        except Exception as e:
                            logger.warning(f"Tagging failed (tree root): {e}")
                            sub_notes.append(f"⚠️ теги не применились: {_redact_for_user(e)}")
                    if t.get("assignee") is not None:
                        try:
                            a_resp = await _run_blocking(lambda: ticktick_v2.batch_update_tasks(
                                [{"taskId": root_id, "assignee": t["assignee"]}]))
                            a_err = id2error_failures(a_resp, [root_id]).get(root_id)
                            if a_err:
                                sub_notes.append(
                                    f"⚠️ исполнитель НЕ назначен — TickTick "
                                    f"отклонил: {a_err}")
                            else:
                                assignee_expect_root = t["assignee"]
                        except Exception as e:
                            logger.warning(f"Assignee failed (tree root): {e}")
                            sub_notes.append(f"⚠️ исполнитель не назначен: {_redact_for_user(e)}")
                if root_id:
                    line += f" (id:{root_id})"
                    to_verify.append((title, root_id, project_id,
                                      t.get("column_id"), root_parent,
                                      tags_expect_root, assignee_expect_root))
                want_parent = {r.get("taskId"): r.get("parentId") for r in relations}
                for x in tasks_flat[1:]:
                    if x["id"] not in tree_fail:
                        sub_verify.append((x.get("title") or "?", x["id"],
                                           project_id, want_parent.get(x["id"])))
                if sub_notes:
                    line += "\n  " + "\n  ".join(sub_notes)
                created.append(line)
                continue

            # ── PATH B: official API for root + v2 batch for flat subtasks ──
            task = await _run_blocking(
                ticktick.create_task,
                title=title,
                project_id=project_id,
                content=t.get("content"),
                start_date=t.get("start_date"),
                due_date=t.get("due_date"),
                priority=priority,
                is_all_day=t.get("is_all_day", False),
                repeat_flag=t.get("repeat_flag"),
                reminders=t.get("reminders"),
            )
            if 'error' in task:
                failed.append(f"#{i+1} «{title}»: {_redact_for_user(task['error'])}")
                continue
            task_id = task.get("id")

            sub_notes = []
            tags_expect = None   # набор тегов, который ОТПРАВЛЕН (для журнала)
            parent_expect = None  # родитель, привязка к которому подтверждена
            assignee_expect = None  # исполнитель, назначение которого подтверждено
            if ticktick_v2 and task_id:
                if t.get("tags"):
                    # д6 (2026-08-09). Здесь звался голый
                    # `ticktick_v2.set_task_tags`: ответ канала не читался
                    # (а /batch/task отвечает 200 с отказами внутри id2error),
                    # живой сверки не было, и новый тег уезжал на задачу БЕЗ
                    # регистрации в аккаунте — «тег-сирота», невидимый в
                    # list_tags и неудаляемый delete_tag. Все три вещи уже
                    # умеет отдельная команда простановки тегов, поэтому
                    # зовётся ЕЁ ядро (`_apply_tags_verified`), а не пишется
                    # похожий код второй раз.
                    try:
                        tag_out = await _apply_tags_verified(
                            [{"taskId": task_id, "title": title,
                              "projectId": project_id, "tags": t["tags"]}])
                        sub_notes.extend(_tag_notes_for_create(tag_out))
                        tags_expect = tag_out.tags_by_id.get(task_id)
                    except Exception as e:
                        logger.warning(f"Tagging failed: {e}")
                        sub_notes.append(f"⚠️ теги не применились: {_redact_for_user(e)}")
                if t.get("assignee") is not None:
                    try:
                        # д6 (2026-08-09): ответ /batch/task читается на
                        # предмет отказа именно по ЭТОЙ задаче — раньше
                        # отклонённое назначение исполнителя не давало ни
                        # исключения, ни строки в отчёте. Это ПЕРВЫЙ слой.
                        a_resp = await _run_blocking(lambda: ticktick_v2.batch_update_tasks(
                            [{"taskId": task_id, "assignee": t["assignee"]}]))
                        a_err = id2error_failures(a_resp, [task_id]).get(task_id)
                        if a_err:
                            sub_notes.append(
                                f"⚠️ исполнитель НЕ назначен — TickTick "
                                f"отклонил: {a_err}")
                        else:
                            # ВТОРОЙ слой (2026-08-09, д6): ожидание идёт в
                            # to_verify/журнал ТОЛЬКО когда канал не сообщил
                            # об отказе — по образцу parent_expect ниже,
                            # иначе один и тот же провал был бы назван дважды
                            # разными словами (отказ канала здесь, и «не
                            # применилось» независимой перепроверкой позже).
                            assignee_expect = t["assignee"]
                    except Exception as e:
                        logger.warning(f"Assignee failed: {e}")
                        sub_notes.append(f"⚠️ исполнитель не назначен: {_redact_for_user(e)}")
                if t.get("column_id"):
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_column(task_id, t["column_id"]))
                    except Exception as e:
                        logger.warning(f"Column failed: {e}")
                        sub_notes.append(f"⚠️ раздел (column) не применился: {_redact_for_user(e)}")
                if t.get("parent_id"):
                    try:
                        # д5 (2026-08-09): /batch/taskParent отвечает 200 и
                        # кладёт отказ по конкретной задаче в id2error —
                        # непрочитанный ответ означал «✓ создано» на задаче,
                        # которая осталась лежать КОРНЕВОЙ, а не под
                        # родителем. Родитель попадает в ожидания пост-проверки
                        # только когда канал не сообщил об отказе.
                        p_resp = await _run_blocking(lambda: ticktick_v2.batch_set_task_parent(
                            [task_id], t["parent_id"], project_id))
                        p_err = id2error_failures(p_resp, [task_id]).get(task_id)
                        if p_err:
                            sub_notes.append(
                                "⚠️ привязка к родителю НЕ применилась — "
                                f"TickTick отклонил: {p_err}")
                        else:
                            parent_expect = t["parent_id"]
                    except Exception as e:
                        logger.warning(f"Parent link failed: {e}")
                        sub_notes.append(f"⚠️ привязка к родителю не применилась: {_redact_for_user(e)}")
            elif task_id and not ticktick_v2 and (
                    t.get("tags") or t.get("assignee") is not None
                    or t.get("parent_id")):
                sub_notes.append("⚠️ теги/исполнитель/родитель требуют v2 API — "
                                 "v2 недоступен, эти поля НЕ применены")

            # Subtasks (flat strings or dicts without deeper nesting)
            sub_items = t.get("subtasks") or []
            sub_count = 0
            if sub_items and task_id and ticktick_v2:
                all_sub_tasks = []
                all_sub_rels = []
                for s in sub_items:
                    if isinstance(s, str):
                        s = {"title": s}
                    st_tasks, st_rels = _flatten_task_tree(
                        s, project_id, parent_id=task_id)
                    all_sub_tasks.extend(st_tasks)
                    all_sub_rels.extend(st_rels)
                try:
                    resp = await _run_blocking(
                        lambda: ticktick_v2.batch_create_tasks(all_sub_tasks))
                    sub_fail = id2error_failures(
                        resp, [x["id"] for x in all_sub_tasks])
                    rel_fail = {}
                    if all_sub_rels:
                        # д5 (2026-08-09). Здесь ответ на /batch/taskParent
                        # выбрасывался целиком — а именно в нём канал сообщает
                        # (внутри 200) о непринятых связях. Отчёт печатал
                        # «✓ «X» + N подзадач», подзадачи существовали, но
                        # лежали КОРНЕВЫМИ: дерева не было, и по тексту это
                        # было неотличимо от успеха. Соседняя ветка того же
                        # метода (PATH A, выше) читала этот ответ уже давно —
                        # здесь ровно та же проверка.
                        rel_resp = await _run_blocking(lambda: ticktick_v2._request(
                            "POST", "/batch/taskParent", json=all_sub_rels))
                        rel_fail = id2error_failures(
                            rel_resp, [r.get("taskId") for r in all_sub_rels])
                        if rel_fail:
                            sub_notes.append(
                                f"⚠️ связи родитель-подзадача не применились у "
                                f"{len(rel_fail)} (подзадачи созданы, но НЕ "
                                "вложены): "
                                + "; ".join(f"{k[:8]}…: {v}"
                                            for k, v in rel_fail.items()))
                    await _run_blocking(lambda: ticktick_v2.invalidate_cache())
                    sub_count = len(all_sub_tasks) - len(sub_fail)
                    if sub_fail:
                        sub_notes.append(
                            f"⚠️ TickTick отклонил {len(sub_fail)} подзадач: "
                            + "; ".join(f"{k[:8]}…: {v}" for k, v in sub_fail.items()))
                    # Ожидаемый родитель едет вместе с подзадачей в
                    # пост-проверку и в журнал: до этой правки проверялось
                    # только СУЩЕСТВОВАНИЕ подзадачи, то есть развалившееся
                    # дерево не ловил ни блок проверки, ни независимый отчёт.
                    want_parent = {r.get("taskId"): r.get("parentId")
                                   for r in all_sub_rels}
                    for x in all_sub_tasks:
                        if x["id"] not in sub_fail:
                            sub_verify.append((x.get("title") or "?", x["id"],
                                               project_id,
                                               want_parent.get(x["id"])))
                except Exception as e:
                    logger.warning(f"Batch subtasks failed: {e}")
                    sub_notes.append(
                        f"⚠️ подзадачи НЕ созданы ({len(all_sub_tasks)} шт.): {_redact_for_user(e)}")
            elif sub_items and task_id and not ticktick_v2:
                sub_notes.append(
                    f"⚠️ запрошено {len(sub_items)} подзадач, но они требуют "
                    "v2 API — v2 недоступен, подзадачи НЕ созданы")

            line = f"✓ «{title}»"
            if sub_count:
                line += f" + {sub_count} подзадач"
            if task_id:
                line += f" (id:{task_id})"
                to_verify.append((title, task_id, project_id,
                                  t.get("column_id"), parent_expect, tags_expect,
                                  assignee_expect))
            if sub_notes:
                line += "\n  " + "\n  ".join(sub_notes)
            created.append(line)

        except Exception as e:
            failed.append(f"#{i+1} «{title}»: {_redact_for_user(e)}")

    # Post-verify DESTINATION against fresh state: each created task must
    # actually sit in the requested project (and column, when one was asked).
    # A creation that landed elsewhere is reported, not silently celebrated.
    warnings = []
    if (to_verify or sub_verify) and ticktick_v2:
        fresh = _open_by_id(fresh=True)
        if fresh is None:
            warnings.append(f"{_UNVERIFIED_MSG} (созданное не перепроверено)")
            fresh = {}
            skip_verify = True
        else:
            skip_verify = False
        names = _v2_project_names()
        if not skip_verify:
            for v_title, v_id, v_pid, v_col, v_parent, _v_tags, _v_assignee in to_verify:
                live = fresh.get(v_id)
                if not live:
                    warnings.append(f"⚠️ «{v_title}»: создание НЕ подтвердилось "
                                    "(нет среди открытых) — проверь")
                    continue
                real_pid = live.get("projectId")
                if real_pid and real_pid != v_pid:
                    warnings.append(
                        f"⚠️ «{v_title}»: попала в «{names.get(real_pid, real_pid)}», "
                        f"а НЕ в запрошенный «{names.get(v_pid, v_pid)}»")
                if v_col and live.get("columnId") != v_col:
                    warnings.append(f"⚠️ «{v_title}»: раздел (column) не применился")
                # ПРИВЯЗКА — такой же предмет проверки, как проект и раздел
                # (д5, 2026-08-09): раньше её тут не было вовсе, и задача,
                # оставшаяся корневой вместо подзадачи, проходила блок
                # проверки без единого замечания.
                if v_parent and live.get("parentId") != v_parent:
                    warnings.append(
                        f"⚠️ «{v_title}»: НЕ вложена под родителя "
                        f"{str(v_parent)[:8]}… (parentId="
                        f"{live.get('parentId')!r}) — лежит отдельно")
            # Subtasks: existence check (a rejected subtask must not survive
            # as a phantom «+ N подзадач» claim).
            lost_subs = [s_title for s_title, s_id, _s_pid, _s_par in sub_verify
                         if s_id not in fresh]
            if lost_subs:
                warnings.append(
                    f"⚠️ подзадачи НЕ подтвердились ({len(lost_subs)}): "
                    + ", ".join(f"«{t}»" for t in lost_subs))
            # …и вторая, независимая от неё проверка: подзадача существует, но
            # лежит ли она ПОД родителем. Существование без привязки — ровно
            # тот случай, когда отчёт хвалился деревом, которого нет.
            orphan_subs = [s_title for s_title, s_id, _s_pid, s_par in sub_verify
                           if s_par and s_id in fresh
                           and (fresh.get(s_id) or {}).get("parentId") != s_par]
            if orphan_subs:
                warnings.append(
                    f"⚠️ подзадачи созданы, но НЕ вложены под родителя "
                    f"({len(orphan_subs)}): "
                    + ", ".join(f"«{t}»" for t in orphan_subs))

    parts = []
    if created:
        parts.append(f"Создано {len(created)}:\n" + "\n".join(created))
    if warnings:
        parts.append("Проверка назначения:\n" + "\n".join(warnings))
    if failed:
        parts.append(f"Ошибки ({len(failed)}):\n" + "\n".join(failed))
    if to_verify or sub_verify:
        # В журнал (и, значит, в независимый operation_report) едут и
        # ПОДЗАДАЧИ, и ожидаемая привязка — до этой правки журнал знал только
        # корневые задачи и только их проект/раздел, поэтому развалившееся
        # дерево независимая проверка не увидела бы никогда (д5, 2026-08-09).
        rid = _op_journal("create", [
            {"taskId": v_id, "title": v_title,
             "expect": {"projectId": v_pid,
                        **({"columnId": v_col} if v_col else {}),
                        **({"parentId": v_parent} if v_parent else {}),
                        **({"tags": v_tags} if v_tags is not None else {}),
                        **({"assignee": v_assignee} if v_assignee is not None else {})}}
            for v_title, v_id, v_pid, v_col, v_parent, v_tags, v_assignee in to_verify
        ] + [
            {"taskId": s_id, "title": s_title,
             "expect": {"projectId": s_pid,
                        **({"parentId": s_par} if s_par else {})}}
            for s_title, s_id, s_pid, s_par in sub_verify
        ], summary)
        parts.append(_report_line(rid))
    return "\n\n".join(parts)


def _suggest_destinations(titles: List[str], names: Dict[str, str]) -> List[Dict]:
    """Ask the Claude shim to propose a destination project PER TASK.

    Returns aligned [{project_id, project, confidence: sure|unsure, reason}]
    (empty list on any failure — caller then asks the user instead of guessing).
    Uses CLAUDE_CLI_URL/CLAUDE_CLI_TOKEN/CLAUDE_CLI_MODEL env (the same
    claude-p-shim the bot uses)."""
    url = os.environ.get("CLAUDE_CLI_URL")
    token = os.environ.get("CLAUDE_CLI_TOKEN")
    if not url or not titles:
        return []
    import requests as _rq
    proj_list = "\n".join(f"- {n}" for n in names.values())
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    prompt = (
        "Разложи задачи по проектам владельца. Список проектов:\n"
        f"{proj_list}\n\nЗадачи:\n{numbered}\n\n"
        "Для КАЖДОЙ задачи выбери самый подходящий проект. confidence='sure' "
        "только когда назначение очевидно; иначе 'unsure' + короткий уточняющий "
        "вопрос в reason (например «какой банк — личное или бизнес?»). Никогда "
        "не выбирай проекты типа «Тест». Ответ СТРОГО JSON-массивом:\n"
        '[{"i": 0, "project": "<имя из списка>", "confidence": "sure|unsure", '
        '"reason": "<кратко>"}]'
    )
    try:
        r = _rq.post(url, json={
            "system": "Ты раскладываешь задачи по проектам. Отвечай только JSON.",
            "prompt": prompt,
            "model": os.environ.get("CLAUDE_CLI_MODEL", "sonnet"),
        }, headers={"Authorization": f"Bearer {token}"}, timeout=90)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return []
        text = data.get("result") or ""
        a, b = text.find("["), text.rfind("]")
        arr = json.loads(text[a:b + 1])
        # Normalised-name index. Projects whose names differ only in emoji/
        # punctuation collapse to one key — such a resolution can point at the
        # WRONG twin, so collisions are demoted to 'unsure' (ask the user).
        # The '' key (emoji-only project name) is never registered.
        by_name: Dict[str, str] = {}
        collisions: Dict[str, List[str]] = {}
        for k, v in names.items():
            key = _norm_name(v)
            if not key:
                continue
            if key in by_name:
                collisions.setdefault(key, [names.get(by_name[key], "")]).append(v)
            else:
                by_name[key] = k
        out = [{} for _ in titles]
        for it in arr:
            idx = int(it.get("i", -1))
            if not (0 <= idx < len(titles)):
                continue
            key = _norm_name(it.get("project") or "")
            if not key:
                continue
            pid = by_name.get(key)
            if not pid:
                continue
            sug = {"project_id": pid, "project": names.get(pid, ""),
                   "confidence": it.get("confidence") or "unsure",
                   "reason": (it.get("reason") or "").strip()}
            if key in collisions:
                twins = ", ".join(f"«{n}»" for n in collisions[key] if n)
                sug["confidence"] = "unsure"
                sug["reason"] = (f"несколько проектов с похожим названием "
                                 f"({twins}) — какой именно?")
            out[idx] = sug
        return out
    except Exception as e:
        logger.warning(f"destination suggester failed: {e}")
        return []


def _create_object_hash(raw: List[Dict[str, Any]]) -> str:
    """Binding-хэш для манифеста СОЗДАНИЯ (kind="create"). У остальных
    манифестов (delete/declutter) объекты уже существуют, и `object_hash`
    считается по их id — у создаваемых задач id ещё нет по определению,
    поэтому хэшируется само НОРМАЛИЗОВАННОЕ содержимое каждой задачи
    (json.dumps с sort_keys — порядок ключей в dict не должен влиять).

    Одна-единственная формула на оба места, где хэш нужен: фаза плана
    (plan_task_creation) и пересчёт при авто-исполнении по кнопке
    (_rehash_create_manifest). Не «две одинаковые реализации», а буквально
    один вызов — иначе побайтовое совпадение держалось бы на честном слове,
    а разошедшийся хэш беззвучно превратил бы кнопку в «ничего не делает»."""
    return _manifest_object_hash(
        "create",
        [json.dumps(t, sort_keys=True, ensure_ascii=False, default=str)
         for t in raw])


@mcp.tool(annotations=READONLY)
async def plan_task_creation(summary: str, tasks: List[Dict[str, Any]],
                             max_items: int = 50) -> str:
    """
    Phase 1 of confirmed creation — THE way to create tasks in an interactive
    chat: build a creation MANIFEST without creating anything. Read-only.

    Accepts the same task objects as create_tasks (title, project_id, content,
    due_date, priority, tags, column_id, subtasks, …). project_id is OPTIONAL:
    when the user didn't name a project, OMIT it — the server itself looks at
    the owner's project list and proposes a destination PER TASK (sure /
    ❓-unsure with a clarifying question). Do NOT guess a project yourself and
    NEVER default to a sandbox like «Тест». The echo also flags title
    duplicates already open in the destination. The user answers per item
    («2 — в Fix&Roll»); re-plan with explicit project_id for corrections.

    Attaching under an EXISTING task: pass parent_id AND parent_title (the
    parent's current title). The plan checks the parent against live state
    BEFORE it is shown — a parent that is completed, deleted or in the trash,
    or an id whose real title is a different one, drops that row from the
    plan entirely (it is never a confirmable line), because such a "subtask"
    would land as a separate top-level task instead. The card names the
    attachment («→ подзадача «…»») so the owner confirms nesting he can read.

    IMPORTANT: reprint the returned text VERBATIM and IN FULL to the user, ask
    for explicit confirmation («ок?»), and only after their real reply call
    execute_task_creation(manifest_id, user_reply=<their literal message>).
    Afterwards run operation_report and reprint it — the flow is: спроси →
    сделай → докажи. execute_task_creation IS hard-gated on user_reply, same
    as every other mutating tool on this server (Maksim, 2026-08-05: no more
    tier exemption for "it's just a create/reversible edit" — ALL write tools
    go through plan→execute, without exception).

    TELEGRAM CONFIRMATION LAYER (optional, off by default): when it is on,
    THIS SERVER itself also sends the plan to the owner as a Telegram message
    carrying [✅ Подтвердить]/[🛑 Отклонить] buttons — this is not an external
    relay bolted on top of MCP, it is the server's own out-of-band second
    factor. Pressing "Подтвердить" makes the server execute the creation
    ITSELF (a background poller does it) and rewrites the report into that
    same Telegram message — no second tool call is needed for it to happen.
    In that mode the text path is CLOSED: execute_task_creation refuses ANY
    `user_reply`, before the press and after it alike. Do not call it — show
    the plan and let the owner tap the button.

    Args:
        summary: one-line human sentence describing the batch
        tasks: same objects create_tasks takes
        max_items: refuse to plan more than this many creations
    """
    err = _ensure_official()
    if err:
        return err
    _prune_manifests()
    if not tasks:
        return "Пустой список — планировать нечего."
    if len(tasks) > max_items:
        return (f"🛑 Отказ: {len(tasks)} создани(й) — больше капа {max_items}. "
                "Разбей на части или подними max_items осознанно.")
    # Resolve "today"/"tomorrow"/etc. off the real clock BEFORE building the
    # preview, so what the user approves is already the real calendar date —
    # not a word the calling model computed (possibly with stale context).
    for t in tasks:
        _resolve_dates_in_task_tree(t)
    names = _v2_project_names()
    good, refused, pending = [], [], []  # pending: no project given → suggest
    for i, t in enumerate(tasks, 1):
        title = t.get("title")
        pid = t.get("project_id") or t.get("projectId") or ""
        if not title:
            refused.append(f"#{i}: нет title")
            continue
        if not pid:
            pending.append((i, t))
            continue
        pname = names.get(pid)
        if pname is None:
            # FAIL-CLOSED: отказ всегда, когда id не резолвится в живой
            # проект — в том числе когда сама карта `names` пустая (v2
            # недоступен И v1-фолбэк тоже не сработал). Старое условие
            # `if names and pname is None` пропускало эту проверку целиком
            # на пустой карте — битый/устаревший project_id проходил план
            # непроверенным и на execute уходил в Inbox без единого отказа.
            reason = ("проект по id не найден" if names else
                      "список проектов сейчас недоступен — сверить id нельзя")
            refused.append(f"#{i} «{title}»: {pid} — {reason}")
            continue
        exp_name = t.get("project_name") or t.get("projectName") or ""
        if exp_name and pname and not _names_agree(exp_name, pname):
            refused.append(f"#{i} «{title}»: project_id это «{pname}», а НЕ "
                           f"«{exp_name}»")
            continue
        good.append((t, pname or pid, None, i))

    # No project named → the SERVER thinks: per-task destination suggestions
    # via the Claude shim (sure/unsure + a clarifying question when unsure).
    if pending:
        sugs = await _run_blocking(lambda: _suggest_destinations(
            [t.get("title") for _, t in pending], names))
        for (i, t), sug in zip(pending, sugs or [{}] * len(pending)):
            if sug.get("project_id"):
                t = dict(t)
                t["project_id"] = sug["project_id"]
                good.append((t, sug["project"], sug, i))
            else:
                refused.append(f"#{i} «{t.get('title')}»: проект не указан "
                               "(подсказчик недоступен) — назови проект")

    # П19 (2026-08-09): ЖИВ ЛИ РОДИТЕЛЬ — на фазе плана, до того как владелец
    # увидел карточку. Слово parent_id раньше не встречалось в этой функции
    # ни разу: план на подзадачу удалённого родителя показывался как обычный
    # план, и узнавал владелец из предупреждения ПОСЛЕ создания. Не прошло
    # сверку → строка не помечается, а вообще не попадает в манифест: пометка
    # оставила бы кнопку, нажатие на которую даёт ровно то, от чего защищаемся.
    #
    # Место цикла выбрано осознанно — ПОСЛЕ ветки `pending`, когда проект
    # известен у каждой строки, включая те, которым его предложил подсказчик.
    # Стой сверка выше, вызов без project_id обходил бы её целиком.
    #
    # `plan_state_read` (флаг «живое состояние уже читали») отделён от самого
    # снимка НЕ ради красоты: `_open_by_id` возвращает None и когда читать
    # нечем, и это единственный способ не спутать «ещё не смотрел» с
    # «посмотрел, состояние недоступно». Читаем лениво — план без единого
    # parent_id не платит за эту проверку ни одним запросом.
    checked = []
    plan_by_id = None
    plan_state_read = False
    for (t, pname, sug, i) in good:
        parent_id = t.get("parent_id")
        if not parent_id:
            checked.append((t, pname, sug, ""))
            continue
        exp_parent = t.get("parent_title") or t.get("parentTitle") or ""
        if not plan_state_read:
            plan_by_id = _open_by_id(fresh=True)
            plan_state_read = True
        if plan_by_id is None:
            # Fail-OPEN здесь и только здесь: разовый сбой чтения не имеет
            # права заблокировать всякое создание (так же решено в
            # create_subtask). Строка остаётся, но карточка говорит вслух,
            # что имя родителя не сверено, а исполнение переспросит — там
            # недоступное чтение уже отказ, и это последняя линия.
            checked.append((t, pname, sug,
                            (exp_parent or str(parent_id)[:8] + "…")
                            + " — НЕ сверено"))
            continue
        g = _guard_task(parent_id, exp_parent,
                        t.get("project_id") or t.get("projectId") or "",
                        by_id=plan_by_id)
        note, _warn = _guard_or_refuse(g, stage="пакет-родитель",
                                       expected=exp_parent, parent_id=parent_id)
        if note:
            refused.append(f"#{i} «{t.get('title')}»: {note}")
            continue
        if g.status == "unavailable":
            checked.append((t, pname, sug,
                            (exp_parent or str(parent_id)[:8] + "…")
                            + " — НЕ сверено"))
            continue
        # Имя для карточки берётся у ТОГО, КТО ЕГО ПРОЧИТАЛ (живое `g.title`),
        # а не у вызывающего: владелец должен видеть, куда задача ляжет на
        # самом деле, а не что про это думает модель.
        checked.append((t, pname, sug, g.title or exp_parent))
    good = checked

    # Duplicate radar: same-normalised title already open in the destination.
    open_titles: Dict[str, set] = {}
    for lt in (_open_by_id() or {}).values():
        open_titles.setdefault(lt.get("projectId") or "", set()).add(
            _norm_name(lt.get("title") or ""))

    # Не осталось ни одной прошедшей строки — плана нет (2026-08-09, П19).
    # Пустой манифест был бы кнопкой, которая ничего не создаёт, и его же
    # уносил бы в Telegram второй фактор; отказ обязан прийти ответом здесь.
    if not good:
        return ("### 📋 План создания — 0\n"
                "🛑 Плана нет — ни одна строка сверку не прошла, манифест НЕ "
                "создан, подтверждать нечего.\n"
                f"🛑 **Исключены {len(refused)}:** " + "; ".join(refused)
                + "\nНичего не изменено.")

    mid = uuid.uuid4().hex[:12]
    now = time.monotonic()
    raw_items = [t for t, _, _, _ in good]
    _MANIFESTS[mid] = {"kind": "create", "raw": raw_items,
                       "created": now, "plan_shown_at": now,
                       "summary": summary, "consumed": False,
                       # `tool` — под каким именем этот план анонсируется в
                       # Telegram (оно же ключ в TG_APPROVAL_TOOLS и в
                       # _AUTO_EXECUTORS); `_gate` — метка формы распаковки
                       # для авто-исполнителя, чтобы он не принял за «свой»
                       # чужой манифест с таким же kind.
                       "tool": "create_tasks", "_gate": "create",
                       # Binding: до 2026-08-06 манифест создания вообще не
                       # имел object_hash — между планом и нажатием кнопки
                       # содержимое `raw` можно было подменить, и ни
                       # _require_consent, ни try_auto_execute этого не
                       # заметили бы (оба сверяют хэш только `if stored_hash`).
                       "object_hash": _create_object_hash(raw_items)}
    lines = [f"### 📋 План создания — {len(good)}",
             _plan_id_line(mid, "ничего ещё не создано"), ""]
    for i, (t, pname, sug, parent_label) in enumerate(good, 1):
        bits = [f"{i}. **«{t.get('title')}»** → **{pname}**"]
        # Вложение названо В КАРТОЧКЕ (2026-08-09, П19): до этого из плана
        # не было видно вовсе, что задача уйдёт под другую — владелец
        # подтверждал вложение, которого не читал.
        if parent_label:
            bits.append(f"→ подзадача «{parent_label}»")
        if sug:
            if (sug.get("confidence") or "unsure") == "sure":
                bits.append(f"(моё предложение: {sug.get('reason') or 'подходит по смыслу'})")
            else:
                bits.append(f"❓ НЕ уверен — {sug.get('reason') or 'уточни проект'}")
        if t.get("due_date"):
            bits.append(f"срок {t['due_date']}")
        if t.get("priority"):
            bits.append(f"приоритет {PRIORITY_MAP.get(t.get('priority'), t.get('priority'))}")
        subs = t.get("subtasks") or []
        if subs:
            bits.append(f"+{len(subs)} подзадач")
        if _norm_name(t.get("title") or "") in open_titles.get(t.get("project_id") or t.get("projectId") or "", set()):
            bits.append("⚠️ задача с таким названием УЖЕ есть в этом проекте")
        lines.append(", ".join(bits))
    if refused:
        lines.append("")
        lines.append(f"🛑 **Исключены {len(refused)}:** " + "; ".join(refused))
    if any(lbl.endswith("НЕ сверено") for _, _, _, lbl in good):
        lines.append("")
        lines.append(_PARENT_UNVERIFIED_NOTE)
    if any(s and (s.get("confidence") or "unsure") != "sure"
           for _, _, s, _ in good):
        lines.append("")
        lines.append("❗ _По задачам с ❓ уточни проект — можно ответить пунктами "
                     "(«2 — в Fix&Roll»), тогда план пересоберётся с явными "
                     "адресами._")
    lines.append("")
    # Инструкция для модели («вызови execute_task_creation…») — ОТДЕЛЬНО от
    # человеческой части `lines` (2026-08-06, дефект №2): `_maybe_tg_notify_plan`
    # приклеивает её только к ответу модели, в Telegram-карточку плана она не
    # уходит.
    agent_tail = ("После явного «да» вызови "
                 f"`execute_task_creation(manifest_id=\"{mid}\", "
                 "user_reply=\"<дословная реплика пользователя>\")` · "
                 f"действует {_manifest_ttl_phrase()}, одноразово.")
    # Опциональный ТГ-фактор. При выключенном слое (дефолт) возвращает текст
    # плана БЕЗ единого изменения; при включённом — шлёт план кнопкой и
    # помечает манифест `tg_notified`, из-за чего execute-фаза начинает
    # требовать нажатие. Fail-closed: не смогли отправить — манифест гаснет,
    # наружу уходит текст ошибки вместо плана.
    return await _maybe_tg_notify_plan("create_tasks", mid, "\n".join(lines),
                                       agent_tail)


@mcp.tool()
@_shared_notes(user_reply_arg=True)
async def execute_task_creation(manifest_id: str, user_reply: str = "") -> str:
    """
    Phase 2: create exactly what plan_task_creation planned and the user
    approved. Runs the normal creation engine (id echo, destination
    post-verify, operation_report record). One-shot. Gated 🟡
    (docs/DESIGN_approval_gate.md): user_reply is HARD-enforced — refused, and
    nothing is created, when the reply is empty, a negation (anywhere in the
    sentence), an echo of the server's own manifest jargon, a partial "yes"
    carrying a caveat/exclusion («ок, кроме последней» — the manifest can only
    be applied whole), or a paraphrase of the user rather than their words
    («пользователь: да»). The server CANNOT tell a genuine «да» from one a
    model made up — the only real out-of-band factor is the Telegram button
    confirmation, when that layer is enabled (TG_APPROVAL_ENABLED).

    TELEGRAM CONFIRMATION LAYER (optional, off by default): when plan_task_
    creation announced this plan in Telegram, THIS SERVER sent the owner a
    message with [✅ Подтвердить]/[🛑 Отклонить] buttons itself. Pressing
    "Подтвердить" makes the server run the creation on its own (background
    poller) and write the report back into that same message — you do not
    have to call this tool again for it to happen. For such a plan the text
    path is CLOSED: this call is refused whatever `user_reply` says, both
    before the press (plan stays alive; refusal message tells the user to
    press the button) and after it (refusal message tells the user the
    server is already executing). Calling it again changes nothing.

    Args:
        manifest_id: id from plan_task_creation
        {{ARG_USER_REPLY}}
            (this tool IS call #2 — the reply must be a genuine affirmative
            («да»/«ok»/…), quoted verbatim, not paraphrased and not made up)
    """
    err = _ensure_official()
    if err:
        return err
    _prune_manifests()
    # План мог быть построен ДРУГИМ процессом (перезапуск между планом и
    # исполнением). Если этот его не знает — поднимаем из базы (#91).
    await _rehydrate_manifest(manifest_id)
    m = _MANIFESTS.get(manifest_id)
    if not m or m.get("kind") != "create":
        return _manifest_gone_msg(
            manifest_id,
            f"🛑 Манифест создания {manifest_id} не найден/истёк/уже "
            "исполнен. Сначала plan_task_creation.")
    # `tool=`/`manifest_id=` передаются ОБЯЗАТЕЛЬНО (2026-08-06): именно их
    # отсутствие в execute_task_deletion делало ТГ-гейт недетерминированным
    # (см. tests/test_gate_tg_determinism.py). Имя тула здесь — то же, под
    # которым план анонсируется в Telegram («create_tasks»), оно же ключ в
    # TG_APPROVAL_TOOLS и в _AUTO_EXECUTORS.
    cr = _require_consent(action="create", tier=1, manifest=m,
                          user_reply=user_reply, tool="create_tasks",
                          manifest_id=manifest_id)
    if not cr.ok:
        return cr.reason
    _mark_manifest_consumed(m, manifest_id)
    result = await _create_tasks_impl(m.get("summary") or "Создание по манифесту",
                                      m["raw"])
    # Independent verification is NOT optional: append the server-built report
    # right here, so it reaches the user even if the model never asks for it.
    rid_m = re.search(r'operation_report\(record_id="([\w-]+)"\)', result)
    if rid_m:
        result += "\n\n" + _build_operation_report(rid_m.group(1))
    return result


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def update_tasks(
    summary: str,
    tasks: List[Dict[str, Any]] = None,
    manifest_id: str = "",
    user_reply: str = "",
    automation_key: str = ""
) -> str:
    """
    Update one or more tasks in TickTick. Gated 🟡 (docs/DESIGN_approval_gate.md):
    two calls, same tool name — nothing is changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest from `tasks` and
    returns a preview of exactly what would change — nothing is updated yet.
    Call #2 (after the user actually replied): repeat the call with
    manifest_id=<id from call #1> and user_reply=<the user's literal last
    message> — `tasks` is ignored on this call (the manifest's own stored
    items are used, so the set can't be swapped between the two calls). Do
    NOT make call #2 in the same turn as call #1.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user, e.g. «Меняю задачу
    „Оплатить аренду": срок 2026-07-01, приоритет высокий» or «Меняю срок у
    3 задач на 2026-07-05». Mention only what actually changes.

    Each item identifies a task and carries the fields to update. For a single
    task, use a one-element list. For multiple tasks, all items are processed in
    one call via v2 batch (limited fields). For a single task with advanced fields
    (repeat_flag, reminders, column_id), the official API is used.

    RULE, ENFORCED IN CODE — not a wish: every item MUST carry the task's
    CURRENT title as "title". An item that sets "new_title" WITHOUT "title" is
    REFUSED before anything is sent to TickTick (the reply starts with 🛑 and
    names the missing field). The price of the old behaviour is why: renaming
    with no name to verify let one stale id overwrite a LIVE task's name, and
    the journal keeps only the NEW name — the old one cannot be restored from
    anywhere. If the task GENUINELY has no name (the list shows it as
    «(без названия …)»), pass "untitled": true INSTEAD of "title"; it must be
    boolean true — the string "true" and the number 1 are refused, and so is
    the marker on a task that does have a name.

    Supported fields per item:
      taskId (required), projectId (required for single/advanced),
      title (current title — required whenever new_title is set),
      untitled (boolean true; use INSTEAD of title for a task with no name),
      new_title, content,
      start_date ("YYYY-MM-DD" = all-day; full ISO only if time given; or the
        literal word "today"/"tomorrow"/"yesterday"/a weekday name — RU too —
        resolved by the server off its own clock, immune to your own date
        drifting),
      due_date (same rule), priority (0/1/3/5),
      repeat_flag (single task only; use build_recurrence_rule),
      reminders (single task only; use build_reminder),
      tags (replaces existing), column_id (single task only),
      assignee (user ID to assign; requires shared project and v2 API)

    Example (single): [{"title": "Pay rent", "taskId": "abc",
                         "projectId": "xyz", "due_date": "2026-07-01",
                         "priority": 5}]
    Example (batch):  [{"title": "A", "taskId": "1", "priority": 3},
                       {"title": "B", "taskId": "2", "due_date": "2026-07-05"}]

    {{AUTOMATION_KEY_NOTE}}

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of task change objects — required on call #1, ignored on call #2
        manifest_id: from call #1's response — pass on call #2 to actually update
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_official()
    if err:
        return err
    # Resolve "today"/"tomorrow"/etc. off the real clock BEFORE the manifest
    # is built (call #1) — the preview the user approves and what actually
    # gets written must be the same date, not a word the caller computed.
    if tasks:
        for t in tasks:
            for key in ("due_date", "start_date"):
                if key in t:
                    t[key] = _resolve_relative_date(t[key])
    # Сверка с живым состоянием на фазе ПЛАНА: строка, которую исполнитель
    # заведомо отвергнет, обязана быть видна КАК ТАКАЯ до подтверждения, а не
    # только в отчёте после нажатия (см. _unapplicable_update_rows).
    describe, notes = _describe_update_item, None
    if not manifest_id and tasks:
        # Живые названия для строк, которым вызывающий их не дал: карточка
        # обязана называть задачу, а не показывать её id (_plan_task_name).
        titles = _plan_task_titles(tasks)
        describe = functools.partial(_describe_update_item, titles=titles)
        # Н9 (2026-08-09): фаза плана обязана знать то же правило, что и ядро.
        # До этого она печатала «название → «Затёрто»» без пометки, человек
        # соглашался, и всё согласие уходило в 🛑 на исполнении. Правило не
        # копируется сюда второй реализацией: план зовёт ТУ ЖЕ функцию отказа,
        # что и мутатор, на уже прочитанном живом состоянии.
        describe, notes, refusal = _plan_live_check(
            tasks, describe, row_refusal=_rename_guard_refusal)
        if refusal:
            return refusal
    outcome = await _gate_batch("update", "update_tasks", tasks, summary,
                                manifest_id, user_reply, describe,
                                notes=notes, automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _update_tasks_impl(outcome.summary, outcome.tasks)


def _update_change_bits(t: Dict[str, Any], sep: str = "; ") -> str:
    """Человеческий перечень изменений ОДНОЙ задачи («название → «X»; срок →
    2026-08-10»), без обёртки с названием. Вынесено из _describe_update_item,
    чтобы manual_triage печатал те же формулировки тем же кодом, а не своей
    копией, которая со временем разойдётся."""
    bits = []
    if t.get("new_title"):
        bits.append(f"название → «{t['new_title']}»")
    if t.get("content") is not None:
        bits.append("содержимое меняется")
    if t.get("due_date"):
        bits.append(f"срок → {t['due_date']}")
    if t.get("start_date"):
        bits.append(f"начало → {t['start_date']}")
    if t.get("priority") is not None:
        bits.append(f"приоритет → {PRIORITY_MAP.get(t.get('priority'), t.get('priority'))}")
    if t.get("tags") is not None:
        bits.append(f"теги → {', '.join(t['tags'])}")
    if t.get("column_id"):
        bits.append("колонка меняется")
    if t.get("assignee") is not None:
        bits.append("исполнитель меняется")
    # repeat_flag/reminders РЕАЛЬНО применяются _update_tasks_impl, но до
    # 2026-08-06 здесь не печатались вовсе: план с одним только напоминанием
    # показывал человеку «(поля изменений не распознаны)» и просил «да» на
    # непонятно что. Строка должна называть КАЖДОЕ поле, которое исполнитель
    # действительно тронет.
    if t.get("repeat_flag"):
        bits.append("повтор меняется")
    if t.get("reminders") is not None:
        bits.append("напоминания меняются")
    return sep.join(bits) or "(поля изменений не распознаны)"


def _describe_update_item(t: Dict[str, Any],
                          titles: Optional[Dict[str, str]] = None) -> str:
    return f"**{_plan_task_name(t, titles)}** — {_update_change_bits(t)}"


# ---------------------------------------------------------------------------
# СВЕРКА СТРОК ПЛАНА С ЖИВЫМ СОСТОЯНИЕМ — одна на все батч-мутаторы
# (update_tasks / complete_tasks / move_tasks / set_task_tags).
#
# Зачем она вообще (2026-08-07). Раньше батч сверял задачи с живым состоянием
# ТОЛЬКО в исполнителе. Живой прогон: батч из пяти задач, одна из которых
# лежала в корзине ещё до построения плана, — карточка перечислила все пять
# одинаково, и человек подтверждал операцию, часть которой была обречена
# заранее. Отчёт потом честно говорил «НЕ применилось 1», но постфактум:
# согласие уже получено на то, чего не будет.
#
# ПОЧЕМУ ОДНА ФУНКЦИЯ НА ЧЕТЫРЕ ТУЛА (круг 8). Сверку сначала завели только у
# `update_tasks`, и живой прогон по кнопкам тут же нашёл цену такого решения:
# ТОТ ЖЕ корзинный вход у `complete_tasks`, `move_tasks` и `set_task_tags`
# строил план как на живую задачу — без ⛔, без слова «корзина», в одном
# экране рядом с честной карточкой `update_tasks`. Отличать «мне откажут» от
# «всё применится» человек должен по состоянию объекта, а не по тому, какой
# из четырёх похожих тулов позвала модель, поэтому решение принято ОДИН РАЗ
# ДЛЯ ВСЕГО КЛАССА — здесь.
#
# ТРИ ИСХОДА, И ОНИ РАЗНЫЕ (это ядро круга 8):
#   * все строки исполнимы          → план как был, ничего не дорисовано;
#   * часть строк обречена          → ⛔ на этих строках + ⚠️-сводка под
#                                     списком; остальные исполнимы, решает
#                                     человек;
#   * исполнимых строк не осталось  → плана НЕТ вовсе (🛑): подтверждать
#                                     нечего, а карточка «согласны?» на ноль
#                                     исполнимых строк — это просьба нажать
#                                     «да» впустую. Одиночный корзинный вход
#                                     (ровно то, что видел живой прогон)
#                                     попадает сюда и получает тот же отказ
#                                     со словом «корзина» и подсказкой
#                                     restore_tasks, что и класс из пяти
#                                     одиночных тулов, — политика круга 7
#                                     наконец звучит одинаково у всех;
#   * СВЕРКА НЕ УДАЛАСЬ             → см. `_PLAN_UNVERIFIED_NOTE` ниже.
_PLAN_UNAPPLICABLE_NOTE = ("⚠️ Не применится строк: {n} из {total} — они "
                           "помечены ⛔ выше. Подтверждение их не оживит; "
                           "остальные будут выполнены.")

# СБОЙ САМОЙ СВЕРКИ — ГЛАВНАЯ ПРАВКА КРУГА 8.
#
# Дефект. Сверка молча возвращала «помечать нечего» в ДВУХ разных случаях:
# когда все строки исполнимы и когда проверить их не удалось (живое состояние
# недоступно, чтение упало, батчу не хватило запросов). Внешне это
# неотличимо: план выглядел полностью исполнимым, а предупреждение уходило
# только в серверный лог — человек у кнопки его не видит вообще. Живой прогон
# поймал это дважды на ОДНОМ И ТОМ ЖЕ батче из 4 задач: первый раз карточка
# пришла без ⛔, повтор — с ⛔ и сводкой «1 из 4». Это ровно тот класс, ради
# которого шли семь кругов: «сомнение проверки неотличимо от факта», — и в
# превью ДО операции он опаснее, чем в вердикте ПОСЛЕ неё.
#
# ПОЧЕМУ ПРЕДУПРЕЖДЕНИЕ, А НЕ ОТКАЗ СТРОИТЬ ПЛАН. Невозможность ПРОВЕРИТЬ —
# это не то же самое, что невозможность ВЫПОЛНИТЬ: разовый сбой чтения
# состояния превратился бы в отказ обслуживания по всем задачам подряд, в том
# числе полностью исправным (тот же довод, по которому `_task_is_in_trash` не
# fail-closed). Риск при этом закрыт с другой стороны и без отказа: исполнитель
# перечитывает состояние сам и на недоступном отказывает по КАЖДОЙ строке
# (`_guard_task` → status 'unavailable' → 🛑), то есть «подтвердил, а
# применилось не то» здесь физически не выходит. Отбирать у человека рабочий
# план ради риска, которого нет, — плохая сделка.
#
# ЗНАЧОК ⚠️, А НЕ ⛔ И НЕ ℹ️ — по каналам, разведённым в круге 7: ⛔/ℹ️ —
# заявление о ФАКТЕ («эта строка не применится», «задача завершена»), а здесь
# сервер как раз факта не знает. ⚠️ — сомнение В ПРОВЕРКЕ, ровно этот случай.
_PLAN_UNVERIFIED_NOTE = (
    "⚠️ ПРОВЕРИТЬ ИСПОЛНИМОСТЬ СТРОК НЕ УДАЛОСЬ ({why}). Отсутствие пометок "
    "⛔ в списке выше НЕ означает, что применится всё: сверить план с живым "
    "состоянием аккаунта сейчас не получилось. Часть строк может не "
    "примениться — при исполнении состояние перечитывается заново, и по "
    "непригодной строке будет отказ.")


class _PlanCheck:
    """Результат сверки строк плана: пометки и БЫЛА ЛИ СВЕРКА ВООБЩЕ.

    `checked=False` (сверка не удалась) — не то же самое, что пустой `marks`
    (сверка прошла, помечать нечего). Слить их в одно «пометок нет» и значит
    выдать незнание за «всё в порядке»; ровно это и чинится."""

    __slots__ = ("marks", "checked", "why", "row_marks")

    def __init__(self, marks: Dict[str, str], checked: bool = True, why: str = "",
                 row_marks: Optional[Dict[int, str]] = None):
        self.marks = marks
        self.checked = checked
        self.why = why
        # Пометки, привязанные к НОМЕРУ СТРОКИ, а не к taskId (2026-08-09).
        # `marks` по id верны для своих причин: «задачи нет среди открытых» и
        # «id указывает на другую задачу» — свойства САМОГО id, и все строки с
        # этим id обречены одинаково. Причина «в этой строке пишут в поле
        # названия без сверки» — свойство ПОЛЕЙ СТРОКИ: один и тот же id
        # может стоять в законной строке (срок) и в обречённой
        # (переименование), и пометка по id либо приписала бы ⛔ законной
        # строке, либо (что хуже) отменила бы весь план из-за соседней.
        self.row_marks: Dict[int, str] = row_marks or {}


def _row_refusal_reason(refusal: str, label: str) -> str:
    """Текст отказа исполнения → причина для пометки ⛔ в плане.

    Снимает ведущее «🛑 НЕ <глагол> «label» — », и только его: тело причины
    остаётся ДОСЛОВНО тем же, что человек прочитает на исполнении, — иначе у
    плана и у исполнителя завелись бы два расходящихся объяснения одного
    отказа.

    Сравнение идёт по ПОЛНОМУ префиксу с подставленным label, а не по первому
    « — » в строке: у настоящих задач разделитель встречается прямо в имени
    («Договор аренды — подписать до 15-го»), и резать по нему значило бы
    показывать человеку обрубок его же названия вместо причины."""
    for verb in ("переименовал", "обновил"):
        head = f"🛑 НЕ {verb} «{label}» — "
        if refusal.startswith(head):
            return refusal[len(head):]
    return refusal


def _check_plan_rows(tasks: List[Dict[str, Any]], row_refusal=None) -> _PlanCheck:
    """Сверяет строки плана с живым состоянием. Причины ровно те, по которым
    откажет исполнитель: задачи нет среди открытых (завершена / удалена / в
    корзине) или id указывает на ДРУГУЮ задачу. Формулировку причины даёт сам
    guard (`missing[...]["reason"]`), поэтому корзина называется корзиной и
    несёт подсказку `restore_tasks`, а не растворяется в общем «нет среди
    открытых».

    `row_refusal(t, live_title, label) -> Optional[str]` — ПОСТРОЧНАЯ проверка
    ядра того тула, чей план сейчас строится (2026-08-09). Её передаёт сам
    тул, и это не украшение сигнатуры: правило «пишешь в поле названия —
    предъяви текущее» живёт в ядре `update_tasks`, а `complete_tasks`,
    `move_tasks` и `set_task_tags` про него ничего не знают. Позвать её
    безусловно значило бы напечатать ⛔ у тула, который эту строку исполнит
    как ни в чём не бывало, — то есть соврать в другую сторону.

    Причина при этом НЕ ДОСПРАШИВАЕТ состояние: живое имя берётся из строки
    `found`, которую `_split_tasks_by_state` уже вернул (поле `live_title` —
    то самое, на котором guard принимал решение, при необходимости прочитанное
    официальным запасным каналом). `None` там значит «не читали» и разрешением
    не является ни здесь, ни на исполнении."""
    try:
        by_id = _open_by_id(fresh=True)
        if by_id is None:
            return _PlanCheck({}, checked=False,
                              why="живое состояние аккаунта недоступно")
        found, mismatch, missing = _split_tasks_by_state(tasks, by_id=by_id)
    except Exception as e:
        logger.warning(f"сверка плана с живым состоянием не удалась ({e}) — "
                       f"план скажет об этом вслух")
        return _PlanCheck({}, checked=False,
                          why=f"чтение живого состояния не удалось: {_redact_for_user(e)}")
    out: Dict[str, str] = {}
    for m in missing:
        out[str(m.get("taskId"))] = (m.get("reason")
                                     or "этой задачи нет среди открытых "
                                        "(закрыта, удалена или в корзине)")
    for m in mismatch:
        out[str(m.get("taskId"))] = (f"id указывает на другую задачу — "
                                     f"живое название «{m.get('actual')}»")
    rows: Dict[int, str] = {}
    if row_refusal is not None:
        for f in found:
            row = f.get("row")
            if row is None or not (0 <= row < len(tasks)):
                continue
            label = f.get("label") or f.get("title") or ""
            refusal = row_refusal(tasks[row], f.get("live_title"), label)
            if refusal:
                rows[row] = _row_refusal_reason(refusal, label)
    return _PlanCheck(out, row_marks=rows)


def _plan_row_why(i: int, t: Dict[str, Any], check: _PlanCheck) -> str:
    """Почему ЭТА строка плана обречена — по обоим видам пометок, или ""."""
    return (check.marks.get(str(t.get("taskId") or t.get("task_id") or ""))
            or check.row_marks.get(i, ""))


def _mark_unapplicable_rows(describe_item, marks: Dict[str, str],
                            tasks: Optional[List[Dict[str, Any]]] = None,
                            row_marks: Optional[Dict[int, str]] = None):
    """describe-функция тула + пометка обречённой строки. Пометка живёт ТОЛЬКО
    в тексте превью и не попадает в сами элементы манифеста: на исполнении
    состояние перечитывается заново, и вчерашняя пометка не должна ни отменять
    строку, ни выдавать себя за проверку.

    Построчные пометки (`row_marks`) находятся по ТОМУ ЖЕ объекту строки
    (`x is t`), а не по taskId и не по счётчику вызовов: describe зовут по
    элементам того же списка `tasks`, который сверялся, а id в этом списке
    может повторяться."""
    def describe(t: Dict[str, Any]) -> str:
        line = describe_item(t)
        why = marks.get(str(t.get("taskId") or t.get("task_id") or ""))
        if not why and row_marks:
            for i, x in enumerate(tasks or ()):
                if x is t:
                    why = row_marks.get(i)
                    break
        return f"{line}\n   ⛔ НЕ БУДЕТ ПРИМЕНЕНО: {why}" if why else line
    return describe


def _plan_live_check(tasks: List[Dict[str, Any]], describe_item, row_refusal=None):
    """(describe, notes, refusal) для фазы ПЛАНА батч-мутатора.

    `refusal` не None — вернуть его вызывающему как есть: план не строится,
    манифест не создаётся, ничего не изменено.

    `row_refusal` — построчная проверка ядра ЭТОГО тула, см. `_check_plan_rows`.
    Без неё план молчал про строки, которые исполнитель заведомо отвергнет по
    их собственным полям: карточка печатала «название → «Затёрто»» без единой
    пометки, человек жал «да», и всё согласие уходило в отказ. Правда о СВОЕЙ
    осведомлённости — обязанность фазы плана: причина известна из уже
    прочитанного состояния, доспрашивать для неё нечего."""
    check = _check_plan_rows(tasks, row_refusal=row_refusal)
    if not check.checked:
        return describe_item, [_PLAN_UNVERIFIED_NOTE.format(why=check.why)], None
    if not check.marks and not check.row_marks:
        return describe_item, None, None
    doomed = [i for i, t in enumerate(tasks) if _plan_row_why(i, t, check)]
    if len(doomed) == len(tasks):
        reasons = "\n".join(
            f"- «{(t.get('title') or '').strip() or str(t.get('taskId') or t.get('task_id') or '')}»: "
            f"{_plan_row_why(i, t, check)}"
            for i, t in enumerate(tasks))
        return describe_item, None, (
            "🛑 План НЕ построен — исполнить нечего: ни одна строка не пройдёт "
            f"проверку на исполнении.\n{reasons}\nНичего не изменено.")
    return (_mark_unapplicable_rows(describe_item, check.marks, tasks,
                                    check.row_marks),
            [_PLAN_UNAPPLICABLE_NOTE.format(n=len(doomed), total=len(tasks))],
            None)


async def _update_tasks_impl(
    summary: str,
    tasks: List[Dict[str, Any]]
) -> str:
    """Pure mutation logic for update_tasks — no consent gate. Called by
    the public gated update_tasks() below AND directly by
    execute_declutter/resume_declutter (an already-approved declutter
    manifest must not be asked to confirm twice)."""
    err = _ensure_official()
    if err:
        return err

    has_advanced = any(t.get("repeat_flag") or t.get("reminders") or t.get("column_id")
                       for t in tasks)

    if len(tasks) == 1 or has_advanced:
        results = []
        _single_updates = []
        # Fetched once and reused for both the identity guard and the
        # point-date sync below (see _sync_point_date) — avoids re-fetching
        # fresh v2 state per task in the loop.
        _by_id = _open_by_id(fresh=True)
        for t in tasks:
            tid = t.get("taskId") or t.get("task_id")
            pid = t.get("projectId") or t.get("project_id") or ""
            shown_title = t.get("title") or _lookup_task_title(tid)
            new_title = t.get("new_title")
            priority = t.get("priority")
            if priority is not None and priority not in [0, 1, 3, 5]:
                results.append(f"✗ «{shown_title}»: неверный приоритет (допустимо 0/1/3/5)")
                continue
            # Identity guard: refuse to edit a DIFFERENT task if the id is stale.
            g = _guard_task(tid, t.get("title") or "", pid, by_id=_by_id)
            # `missing` здесь — тоже отказ: официальный API молча превратил бы
            # правку со старым projectId в пустую операцию. На ветке mismatch
            # строка называет ЗАЯВЛЕННОЕ имя (о нём и спор), на остальных —
            # то, которое удалось найти.
            refusal, _warn = _guard_or_refuse(
                g, stage="пакет-строка", verb=f"НЕ обновил «{shown_title}»",
                verb_mismatch=f"НЕ обновил «{t.get('title')}»",
                says="message", missing_says="message")
            if refusal:
                results.append(refusal)
                continue
            # Н9 (2026-08-09): запись В ПОЛЕ НАЗВАНИЯ при невзведённой сверке —
            # отказ ЗДЕСЬ, до единого обращения к TickTick. Живое имя берётся у
            # guard'а (`g.title`), который его только что прочитал, — второго
            # чтения не нужно.
            refusal = _rename_guard_refusal(
                t, g.title if g.title_known else None, shown_title)
            if refusal:
                results.append(refusal)
                continue
            # Fix for the "changing one date turns the task into a range" bug:
            # sync the untouched start/due field when the caller only supplied
            # one of them and the task was previously a single fixed date.
            live = (_by_id or {}).get(tid)
            sync_start, sync_due, date_warn = _sync_point_date(
                live, t.get("start_date"), t.get("due_date"))
            try:
                pid = g.project_id or _resolve_project_id(tid, pid)
                task = await _run_blocking(
                    ticktick.update_task,
                    task_id=tid,
                    project_id=pid,
                    title=new_title,
                    content=t.get("content"),
                    start_date=sync_start,
                    due_date=sync_due,
                    priority=priority,
                    repeat_flag=t.get("repeat_flag"),
                    reminders=t.get("reminders"),
                )
                if 'error' in task:
                    results.append(f"✗ «{shown_title}»: {_redact_for_user(task['error'])}")
                    continue
                # Sub-steps (tags/column/assignee) — failures go into the RESULT
                # text, not only the log: «обновлено» must not hide a lost tag.
                sub_fails = []
                if t.get("tags") is not None and ticktick_v2:
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_tags(tid, t["tags"]))
                    except Exception as e:
                        logger.warning(f"Updated but tagging failed: {e}")
                        sub_fails.append(f"теги не применились ({_redact_for_user(e)})")
                if t.get("column_id") and ticktick_v2:
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_column(tid, t["column_id"]))
                    except Exception as e:
                        logger.warning(f"Updated but column assignment failed: {e}")
                        sub_fails.append(f"раздел (column) не применился ({_redact_for_user(e)})")
                assignee_expect = None  # исполнитель, назначение которого подтверждено
                if t.get("assignee") is not None and ticktick_v2:
                    try:
                        # д6 (2026-08-09), та же дыра, что в создании: ответ
                        # /batch/task приходит 200 даже когда назначение
                        # отклонено — отказ лежит внутри, в id2error, и до
                        # этой правки не читался ничем. Это ПЕРВЫЙ слой.
                        a_resp = await _run_blocking(lambda: ticktick_v2.batch_update_tasks([{"taskId": tid, "assignee": t["assignee"]}]))
                        a_err = id2error_failures(a_resp, [tid]).get(tid)
                        if a_err:
                            sub_fails.append(
                                f"исполнитель НЕ назначен — TickTick отклонил: {a_err}")
                        else:
                            # ВТОРОЙ слой (2026-08-09, д6): в `changes` (а
                            # значит и в живую сверку `_verify_item`, и в
                            # журнал) исполнитель попадает ТОЛЬКО когда канал
                            # не отказал — иначе один и тот же провал был бы
                            # назван дважды разными словами.
                            assignee_expect = t["assignee"]
                    except Exception as e:
                        logger.warning(f"Updated but assignee failed: {e}")
                        sub_fails.append(f"исполнитель не назначен ({_redact_for_user(e)})")
                if (t.get("tags") is not None or t.get("assignee") is not None) \
                        and not ticktick_v2:
                    sub_fails.append("теги/исполнитель требуют v2 API — v2 "
                                     "недоступен, эти поля НЕ применены")
                changes = {}
                if new_title is not None:
                    changes["title"] = new_title
                if t.get("content") is not None:
                    changes["content"] = t["content"]
                if priority is not None:
                    changes["priority"] = priority
                if t.get("tags") is not None:
                    changes["tags"] = [x.lstrip("#").lower() for x in t["tags"]]
                if assignee_expect is not None:
                    changes["assignee"] = assignee_expect
                for src, dst, val_raw in (("due_date", "dueDate", sync_due),
                                           ("start_date", "startDate", sync_start)):
                    if val_raw:
                        val, all_day = _normalize_date(val_raw)
                        changes[dst] = val
                        # Preserve the all-day flag on update — dropping it turned
                        # an edited all-day date into a timed midnight task, which
                        # a negative-offset account then rendered a day early (#36).
                        if all_day:
                            changes["isAllDay"] = True
                # Post-verify: re-read fresh state and diff the requested
                # fields — the official API can 200-no-op, so «обновлено» is
                # only printed when the change is VISIBLE in live data.
                item = {"taskId": tid, "title": new_title or shown_title,
                        "expect": {"changes": changes}}
                fresh = _open_by_id(fresh=True)
                if fresh is None:
                    line = f"✏️ «{shown_title}» отправлено, но {_UNVERIFIED_MSG}"
                else:
                    status, verdict = _verify_item("update", item, fresh,
                                                   _v2_project_names())
                    if status == "ok":
                        line = f"✏️ «{shown_title}» обновлено (проверено)"
                    else:
                        line = (f"❌ «{shown_title}» — изменения НЕ видны в "
                                f"живом состоянии: {verdict.lstrip('- ')}")
                if date_warn:
                    line += f"\n  ⚠️ {date_warn}"
                # Н9 (2026-08-09): жалоба «выполнено БЕЗ сверки названия» здесь
                # УБРАНА. Опасный случай — запись в поле названия без сверки —
                # теперь не доходит до записи вовсе (отказ выше), а на
                # безымянной задаче эта строка пугала владельца там, где всё в
                # порядке: имени нет ни у вызывающего, ни у задачи, и объект
                # опознан идентификатором (тот же разбор, что в `_unarmed_note`).
                if sub_fails:
                    line += "\n  ⚠️ " + "; ".join(sub_fails)
                results.append(line)
                _single_updates.append(item)
            except Exception as e:
                results.append(f"✗ «{shown_title}»: {_redact_for_user(e)}")
        if _single_updates:
            rid = _op_journal("update", _single_updates, summary)
            results.append(_report_line(rid))
        return "\n".join(results)

    # Multiple tasks, no advanced fields — use v2 batch
    err = _ensure_ready()
    if err:
        return err
    try:
        # Identity guard first: only edit ids that resolve to the RIGHT task.
        by_id = _open_by_id(fresh=True)
        if by_id is None:
            return _STATE_UNAVAILABLE_MSG
        found, mismatch, missing = _split_tasks_by_state(tasks, by_id=by_id)
        label_of = {}
        changes = []
        date_warns = {}
        # Н9 (2026-08-09): ЧЕТВЁРТАЯ корзина. Строка с записью в поле названия
        # при невзведённой сверке не попадает в `changes` вообще — то есть
        # отказ случается ДО `batch_update_tasks`, а не после. Корзина печатается
        # рядом с `_mismatch_report`, иначе «Обновлено N» перестало бы сходиться
        # с длиной запроса — ровно тот класс ошибок, который здесь и чинится.
        refused = []
        kept = []
        # Идём по СТРОКАМ, прошедшим guard (`found`), а не по исходному списку:
        # живое имя обязано прийти оттуда же, откуда его взял guard, — он его
        # уже прочитал, при необходимости через официальный запасной канал, и
        # положил рядом. Прежний код доставал имя из `by_id`, то есть из
        # снимка открытых задач, которого у живой задачи могло не оказаться
        # (инцидент 2026-08-07: выпадала на 25 минут). Тогда «имени нет»
        # означало на самом деле «я не смотрел», предикат разоружался, и
        # переименование без сверки проходило в канал — ровно та дыра, которую
        # правка закрывает в одиночном пути.
        for f in found:
            t = tasks[f["row"]]
            tid = f["taskId"]
            # Ярлык берётся и из строки guard'а (`label`) тоже: у задачи вне
            # v2-снимка `_lookup_task_title` имени не найдёт, и отказ назвал бы
            # её «[task X…]» — то есть человек прочитал бы отказ, не поняв, о
            # какой задаче речь.
            label_of[tid] = (t.get("title") or f.get("label")
                             or _lookup_task_title(tid))
            refusal = _rename_guard_refusal(t, f.get("live_title"),
                                            label_of[tid])
            if refusal:
                refused.append((f, refusal))
                continue
            kept.append(f)
            ch = {"taskId": tid}
            if t.get("new_title") is not None:
                ch["title"] = t["new_title"]
            if t.get("content") is not None:
                ch["content"] = t["content"]
            if t.get("priority") is not None:
                ch["priority"] = t["priority"]
            if t.get("tags") is not None:
                ch["tags"] = [x.lstrip("#").lower() for x in t["tags"]]
            if t.get("assignee") is not None:
                ch["assignee"] = t["assignee"]
            # Same fix as the single-task path: sync the untouched date field
            # when only one is supplied and the task was previously a single
            # fixed date, so it doesn't silently turn into a range.
            live = (by_id or {}).get(tid)
            sync_start, sync_due, warn = _sync_point_date(
                live, t.get("start_date"), t.get("due_date"))
            if warn:
                date_warns[tid] = warn
            for src, dst, val_raw in (("due_date", "dueDate", sync_due),
                                       ("start_date", "startDate", sync_start)):
                if val_raw:
                    val, all_day = _normalize_date(val_raw)
                    ch[dst] = val
                    if all_day:
                        ch["isAllDay"] = True
            changes.append(ch)
        api_fail = {}
        if changes:
            resp = await _run_blocking(
                lambda: ticktick_v2.batch_update_tasks(changes))
            api_fail = id2error_failures(resp, [c["taskId"] for c in changes])
        # Post-verify inline (like complete/move): fresh re-read + field diff —
        # «Обновлено N» must describe live state, not the request.
        items = [{"taskId": ch["taskId"],
                  "title": ch.get("title") or label_of.get(ch["taskId"], ""),
                  "expect": {"changes": {k: v for k, v in ch.items()
                                         if k != "taskId"}}}
                 for ch in changes]
        updated, not_applied = [], []
        unverified = False
        if changes:
            fresh = _open_by_id(fresh=True)
            if fresh is None:
                unverified = True
            else:
                names = _v2_project_names()
                for it in items:
                    if it["taskId"] in api_fail:
                        not_applied.append(
                            f"«{label_of.get(it['taskId'], it['title'])}» — "
                            f"TickTick отклонил: {api_fail[it['taskId']]}")
                        continue
                    status, verdict = _verify_item("update", it, fresh, names)
                    if status == "ok":
                        updated.append(label_of.get(it["taskId"], it["title"]))
                    else:
                        not_applied.append(verdict.lstrip("- "))
        lines = []
        if updated:
            lines.append(f"✏️ Обновлено {len(updated)} (проверено): "
                         + ", ".join(f"«{lbl}»" for lbl in updated))
        if date_warns:
            for tid, w in date_warns.items():
                lines.append(f"  ⚠️ «{label_of.get(tid, tid)}»: {w}")
        if unverified:
            lines.append(f"✏️ Отправлено {len(changes)}, но {_UNVERIFIED_MSG}")
        if not_applied:
            lines.append(f"❌ НЕ применилось {len(not_applied)}:\n  - "
                         + "\n  - ".join(not_applied))
        # Отказанные строки из жалобы «выполнено БЕЗ сверки» вынуты: они НЕ
        # выполнены. Сама ветка `loose` в `_unarmed_note` остаётся — она нужна
        # тем вызывающим, где имя не пишется вовсе (завершение, перемещение).
        # Вычитаются именно СТРОКИ (`kept`), а не идентификаторы: один и тот же
        # id может стоять в вызове дважды, и вычитание по id глушило честное
        # предупреждение по законной строке заодно с отказом по соседней.
        note = _unarmed_note(kept)
        if note:
            lines.append(note)
        for _f, refusal in refused:
            lines.append(refusal)
        if mismatch:
            lines.append(_mismatch_report(mismatch, "обновил"))
        if missing:
            lines.append(f"↷ Не найдены среди открытых {len(missing)} "
                         "(неверный id/завершены): "
                         + ", ".join(f"«{m['title']}»" for m in missing))
        if changes:
            rid = _op_journal("update", items, summary)
            lines.append(_report_line(rid))
        return "\n".join(lines) if lines else "Ничего не обновлено."
    except Exception as e:
        logger.exception("Error in update_tasks")
        return _tool_error("updating tasks", e)
@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def complete_tasks(summary: str, tasks: List[Dict[str, str]] = None,
                         manifest_id: str = "", user_reply: str = "",
                         automation_key: str = "") -> str:
    """
    Mark one or more tasks as complete in one call. Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest from `tasks`
    and returns a preview of what would be completed — nothing is completed
    yet. Call #2 (after the user actually replied): repeat the call with
    manifest_id=<id from call #1> and user_reply=<the user's literal last
    message> — `tasks` is ignored on this call (the manifest's own stored
    items are used). Do NOT make call #2 in the same turn as call #1.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user, e.g. «Завершаю задачу
    „Купить молоко" в проекте „Покупки"» or «Завершаю 4 задачи».

    Put the human title inside each task object so the dialog shows what's
    being completed: [{"title": "Buy milk", "taskId": "abc", "projectId": "xyz"}].
    project_name is optional but nice to have for a single task.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title","taskId","projectId"} objects — required on
            call #1, ignored on call #2
        manifest_id: from call #1's response — pass on call #2 to actually complete
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_official()
    if err:
        return err
    # Живые названия для строк, которым вызывающий их не дал: карточка
    # обязана называть задачу, а не показывать её id (_plan_task_name).
    titles = _plan_task_titles(tasks) if not manifest_id else {}

    def _describe(t: Dict[str, Any]) -> str:
        return f"**{_plan_task_name(t, titles)}**"

    # Сверка с живым состоянием на фазе ПЛАНА — та же, что у update_tasks
    # (см. `_plan_live_check`). До круга 8 её здесь не было ВОВСЕ: корзинная
    # задача попадала в карточку неотличимо от живой, хотя исполнитель её
    # заведомо пропустит.
    notes = None
    if not manifest_id and tasks:
        _describe, notes, refusal = _plan_live_check(tasks, _describe)
        if refusal:
            return refusal
    outcome = await _gate_batch(
        "complete", "complete_tasks", tasks, summary, manifest_id, user_reply,
        _describe, notes=notes, automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _complete_tasks_impl(outcome.summary, outcome.tasks)


async def _complete_tasks_impl(summary: str, tasks: List[Dict[str, str]]) -> str:
    """Pure mutation logic for complete_tasks — no consent gate. Called
    only by the public gated complete_tasks() below."""
    err = _ensure_official()
    if err:
        return err
    try:
        if ticktick_v2 and len(tasks) > 1:
            # Verify against live state: batch_complete silently skips ids that
            # aren't open, so reporting by request count would over-claim. Ids
            # whose title/project disagree with the caller are refused (guard).
            by_id = _open_by_id(fresh=True)
            if by_id is None:
                return _STATE_UNAVAILABLE_MSG
            found, mismatch, missing = _split_tasks_by_state(tasks, by_id=by_id)
            done, failed = [], []
            api_fail = {}
            unverified = False
            if found:
                resp = await _run_blocking(lambda: ticktick_v2.batch_complete_tasks(
                    [f["taskId"] for f in found]))
                api_fail = id2error_failures(resp, [f["taskId"] for f in found])
                still_open = _open_by_id(fresh=True)  # completed ⇒ leaves the open pool
                if still_open is None:
                    unverified = True
                else:
                    done = [f["title"] for f in found
                            if f["taskId"] not in still_open
                            and f["taskId"] not in api_fail]
                    failed = [f["title"] for f in found
                              if f["taskId"] in still_open
                              or f["taskId"] in api_fail]
            lines = []
            if done:
                lines.append(f"✓ Завершено {len(done)}: "
                             + ", ".join(f"«{t}»" for t in done))
            if unverified:
                lines.append(f"Отправлено на завершение {len(found)}, но "
                             f"{_UNVERIFIED_MSG}")
            note = _unarmed_note(found)
            if note:
                lines.append(note)
            if mismatch:
                lines.append(_mismatch_report(mismatch, "завершил"))
            if missing:
                lines.append(
                    f"↷ Не найдены среди открытых {len(missing)} "
                    "(возможно уже завершены/неверный id): "
                    + ", ".join(f"«{t['title']}»" for t in missing))
            if failed:
                details = [f"«{t}»" for t in failed]
                extra = "; ".join(f"{k[:8]}…: {v}" for k, v in api_fail.items())
                lines.append(f"❌ НЕ завершены {len(failed)} (всё ещё открыты"
                             + (f"; TickTick сообщил: {extra}" if extra else "")
                             + "): " + ", ".join(details))
            if found:
                rid = _op_journal("complete", [
                    {"taskId": f["taskId"], "title": f["title"]} for f in found], summary)
                lines.append(_report_line(rid))
            return "\n".join(lines) if lines else "Ничего не завершено."
        else:
            results = []
            _done_items = []
            for t in tasks:
                tid = t.get("taskId") or t.get("task_id")
                pid = t.get("projectId") or t.get("project_id") or ""
                title = t.get("title") or _lookup_task_title(tid)
                # Identity guard for the single-completion path too.
                g = _guard_task(tid, t.get("title") or "", pid)
                refusal, _warn = _guard_or_refuse(
                    g, stage="пакет-строка", verb=f"НЕ завершил «{title}»",
                    verb_mismatch=f"НЕ завершил «{t.get('title')}»",
                    says="message", missing_says="skip")
                if refusal:
                    results.append(refusal)
                    continue
                if g.status == "missing":
                    # Ветка своя, а не общая: «не среди открытых» для
                    # ЗАВЕРШЕНИЯ — не отказ, а пропуск (задача и так закрыта
                    # либо удалена; выполнять нечего, врать не о чем).
                    results.append(f"↷ «{title}» — не среди открытых "
                                   "(уже завершена/удалена/неверный id), "
                                   "пропущено")
                    continue
                pid = g.project_id or _resolve_project_id(tid, pid)
                pname = t.get("projectName") or _v2_project_names().get(pid, "")
                res = await _run_blocking(lambda: ticktick.complete_task(pid, tid))
                if 'error' in res:
                    results.append(f"✗ «{title}»: {_redact_for_user(res['error'])}")
                    continue
                # Post-verify: the official API can silently no-op a complete
                # with a mismatched projectId — «✓» only after the task is
                # SEEN gone from the fresh open pool.
                fresh = _open_by_id(fresh=True)
                where = f" в «{pname}»" if pname else ""
                if fresh is None:
                    results.append(f"«{title}»{where} — отправлено, но "
                                   f"{_UNVERIFIED_MSG}")
                elif tid in fresh:
                    results.append(f"❌ «{title}»{where} — complete НЕ сработал "
                                   "(задача всё ещё среди открытых)")
                    continue
                else:
                    line = f"✓ «{title}»{where}"
                    if not (t.get("title") or "").strip():
                        line += (" ⚠️ выполнено БЕЗ сверки названия "
                                 "(title не передан)")
                    results.append(line)
                _done_items.append({"taskId": tid, "title": title})
            if _done_items:
                rid = _op_journal("complete", _done_items, summary)
                results.append(_report_line(rid))
            return "\n".join(results)
    except Exception as e:
        logger.exception("Error in complete_tasks")
        return _tool_error("completing tasks", e)
@mcp.tool()
@_shared_notes(automation=True, gate_args=True)
async def delete_tasks(summary: str, tasks: Optional[List[Dict[str, str]]] = None,
                       manifest_id: str = "", user_reply: str = "",
                       automation_key: str = "") -> str:
    """
    ⚠️ Delete one or more tasks (removed to TickTick's trash — recoverable
    via restore_tasks, NOT permanent; def-114, 2026-08-07 — was wrongly
    documented here as "permanently"). Gated (🔴 — even a SINGLE deletion):
    this is now a two-call plan → user says yes → execute flow, same shape
    as plan_task_deletion/execute_task_deletion.

    Call #1 (manifest_id omitted): resolves `tasks` against live state and
    returns a one-shot manifest — nothing is deleted yet. Show that manifest
    to the user VERBATIM and wait for their real reply.

    Call #2 (after the user actually replied): repeat the call with
    manifest_id=<id from call #1> and user_reply=<the user's literal last
    message> — `tasks` is ignored (the manifest's own stored items are used,
    so the set can't be swapped between the two calls). Do NOT make call #2
    in the same turn as call #1.

    summary (FIRST arg): one-line human sentence in the user's language,
    Destructive — START WITH ⚠️, e.g.
    «⚠️ Удаляю задачу „Купить молоко" из „Покупки"» or
    «⚠️ Удаляю 5 задач из „Inbox"». It is echoed VERBATIM as the plan card's
    header (call #1's response) — this IS what the user reads, not
    decoration, so make it accurate and specific.

    Put the human title and project name INSIDE each task object (call #1)
    so the manifest shows what's being deleted:
    [{"title": "Buy milk", "projectName": "Groceries", "taskId": "abc",
      "projectId": "xyz"}]

    BULK (more than DIRECT_DELETE_CAP tasks) is refused here outright — use
    plan_task_deletion → execute_task_deletion instead.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        summary: Human-readable line starting with ⚠️ (see above)
        tasks: List of {"title","projectName","taskId","projectId"} objects
            — required on call #1, ignored on call #2
        manifest_id: from call #1's response — pass on call #2 to actually delete
        {{GATE_ARGS_TAIL}}
    """
    err = _ensure_ready()
    if err:
        return err
    _prune_manifests()

    if manifest_id:
    # План мог быть построен ДРУГИМ процессом (перезапуск между планом и
    # исполнением). Если этот его не знает — поднимаем из базы (#91).
        await _rehydrate_manifest(manifest_id)
        m = _MANIFESTS.get(manifest_id)
        if not m or m.get("kind") != "delete":
            return _manifest_gone_msg(
                manifest_id,
                f"🛑 Манифест удаления {manifest_id} не найден/истёк/уже "
                "исполнен. Начни заново: delete_tasks(summary, tasks).")
        # `automation_key` (#118): headless-клиенту с ВЕРНЫМ ключом второй
        # вызов вообще не нужен — первый уже исполняет. Но если план всё-таки
        # был построен (ретрай, старый двухшаговый клиент, план от прошлого
        # интерактивного круга), ключ обязан провести его до конца, а не
        # оставить висеть: `_require_consent` пропускает по ключу первой же
        # своей строкой.
        cr = _require_consent(action="delete", tier=2, manifest=m,
                              user_reply=user_reply,
                              automation_key=automation_key,
                              object_ids=[it["taskId"] for it in m["items"]],
                              tool="delete_tasks", manifest_id=manifest_id)
        if not cr.ok:
            return cr.reason
        return await _execute_task_deletion_impl(manifest_id, m)

    if not tasks:
        return "Нечего удалять: список пуст."
    # SINGLE task → direct delete allowed, but only fully armed: the title is
    # REQUIRED (identity guard always on), the manifest is one-shot and needs
    # explicit user consent, the snapshot is journaled once executed, and
    # operation_report works for it. BULK (>cap) → two-phase manifest only
    # (plan → text approval → execute → independent report).
    direct_cap = int(os.environ.get("DIRECT_DELETE_CAP", "1"))
    if len(tasks) > direct_cap:
        return (f"🛑 Пакетное удаление ({len(tasks)} задач) — только через "
                "манифест: plan_task_deletion → (аппрув) → execute_task_deletion "
                "→ operation_report. Напрямую можно удалить только "
                f"{direct_cap} задачу за вызов.")
    if any(not (t.get("title") or "").strip() for t in tasks):
        return ("🛑 Для прямого удаления обязателен title каждой задачи — "
                "сверка id↔название должна быть взведена. Добавь title "
                "(или используй plan_task_deletion).")
    try:
        # Resolve every task against live state FIRST: correct the projectId for
        # open tasks (a wrong one makes TickTick silently no-op the delete),
        # REFUSE ids whose title/project don't match the caller's (guards against
        # deleting the wrong task by a stale id), and separate ids that aren't
        # among open tasks (already gone, or completed).
        by_id = _open_by_id(fresh=True)
        if by_id is None:
            return _STATE_UNAVAILABLE_MSG
        found, mismatch, missing = _split_tasks_by_state(tasks, by_id=by_id)
        lines = []
        if mismatch:
            lines.append(_mismatch_report(mismatch, "буду удалять"))
        if missing:
            lines.append(
                f"↷ Не среди открытых {len(missing)} — пропущено (сверить "
                "название нельзя, значит удалять нельзя). Если это завершённая "
                "задача — используй plan_task_deletion: "
                + ", ".join(f"«{m['title']}»" for m in missing))
        if not found:
            lines.insert(0, "Нечего удалять — среди открытых задач не нашёл "
                            "ни одной из указанных.")
            return "\n".join(lines)

        names = _v2_project_names()
        items = []
        for f in found:
            live = by_id.get(f["taskId"]) or {}
            items.append({
                "taskId": f["taskId"], "projectId": f["projectId"],
                "title": live.get("title") or f["title"],
                "project": names.get(f["projectId"], ""),
                "snapshot": _snapshot_of(live),
            })
        mid = uuid.uuid4().hex[:12]
        now = time.monotonic()
        obj_hash = _manifest_object_hash("delete", [it["taskId"] for it in items])
        _MANIFESTS[mid] = {"kind": "delete", "items": items,
                           "created": now, "plan_shown_at": now,
                           "object_hash": obj_hash,
                           "summary": summary, "consumed": False}
        # ─── ОБХОД ПО КЛЮЧУ — ДО показа плана и ДО отправки (#118) ───
        # У delete_tasks гейт СОБСТВЕННЫЙ (свой вид манифеста с items и
        # снимками задач), поэтому общий блок из `_gate_batch` сюда не
        # подставить — но контракт ровно тот же: верный ключ исполняет с
        # первого вызова, кнопка владельцу не уходит.
        #
        # Что НЕ пропускается по ключу: всё, что выше по этой же ветке —
        # обязательный title каждой задачи, сверка id↔название↔проект против
        # живого состояния, потолок DIRECT_DELETE_CAP. Ключ снимает вопрос
        # «человек согласен?», а не «та ли это задача»: identity guard в
        # `_execute_task_deletion_impl` отрабатывает ещё раз, снимок пишется
        # в журнал, эффект перепроверяется свежим чтением — как и по кнопке.
        if _automation_key_matches(automation_key):
            done = await _execute_task_deletion_impl(mid, _MANIFESTS[mid])
            # `lines` — предупреждения про пропущенные/несовпавшие задачи.
            # В интерактивном пути они видны человеку в превью; headless-путь
            # превью не печатает, поэтому они едут прямо в ответ, иначе
            # автоматика молча не узнала бы, что часть списка не тронута.
            return "\n".join([done] + lines) if lines else done
        # def-114 (2026-08-07): заголовок раньше был обезличен («Готов
        # удалить — N») и полностью отбрасывал `summary`, переданный
        # вызывающим — а докстринг ТРЕБУЕТ от вызывающего начинать summary с
        # ⚠️ и называть объект(ы) («⚠️ Удаляю задачу «Купить молоко» из
        # «Покупки»»). У безопасных операций заголовок называет объект — у
        # необратимой удаления был хуже, чем у безобидной. Теперь заголовок
        # — сам `summary` (несёт и ⚠️, и названия — это ответственность
        # вызывающего, как и раньше, только теперь она не выбрасывается), а
        # следом честная строка про обратимость: удалённые задачи реально
        # уходят в корзину TickTick (проверено живьём) и восстановимы через
        # restore_tasks — молчание об этом раньше не давало Максиму понять,
        # что удаление не окончательное.
        preview = [f"### 📋 {summary} — {len(items)}",
                  _plan_id_line(mid, "ничего ещё не удалено"),
                  "- задачи уходят в корзину TickTick — можно вернуть через "
                  "restore_tasks", ""]
        for i, it in enumerate(items, 1):
            preview.append(f"{i}. **«{it['title']}»** — {it['project']} (`{it['taskId']}`)")
        preview.extend(lines)
        preview.append("")
        # Инструкция для модели — ОТДЕЛЬНО от `preview` (2026-08-06, дефект
        # №2): раньше уходила дословно в Telegram-карточку плана.
        agent_tail = ("Покажи это пользователю дословно и ДОЖДИСЬ его "
                     "отдельного ответа (не отвечай за него). Когда он явно "
                     "согласится, вызови "
                     f"`delete_tasks(summary=\"{summary}\", manifest_id=\"{mid}\", "
                     "user_reply=\"<дословная реплика пользователя>\")` — "
                     "НЕ в этом же ходе. Манифест одноразовый, "
                     f"действует {_manifest_ttl_phrase()}.")
        # Хук — корутина: сетевую часть (синхронный requests, паузы между
        # кусками, сон на 429 до _MAX_SEND_WAIT_S на КАЖДЫЙ кусок) он уводит в
        # поток сам, одинаково для всех 22 гейтованных тулов. Раньше поток
        # заказывал каждый вызывающий вручную, и сделали это ровно три места
        # из двадцати двух — см. `_maybe_tg_notify_plan`.
        return await _maybe_tg_notify_plan("delete_tasks", mid,
                                           "\n".join(preview), agent_tail)
    except Exception as e:
        logger.exception("Error in delete_tasks")
        return _tool_error("deleting tasks", e)


# Гейт согласия, манифесты и классификатор ответа вынесены в отдельный
# модуль (пункт 1.2.4 захода 1, 2026-08-09). Импорт стоит РОВНО на месте
# вырезанного куска: здесь уже определены logger, _TG_CFG, _run_blocking,
# _automation_key_matches и _redact_for_user, которые кусок берёт снаружи.
# Список имён ЯВНЫЙ, а не `import *`: видно ровно то, что видно.
from . import consent  # noqa: E402
from .consent import (  # noqa: E402,F401
ConsentResult, _CONSENT_MAX_TOKENS, _JOURNAL_DIR, _MANIFESTS,
    _MANIFEST_TOMBSTONES, _MANIFEST_TTL, _TG_AUTO_EXECUTE_MANIFEST,
    _TOMBSTONE_CLAIMED, _TOMBSTONE_EXECUTED, _TOMBSTONE_FAILED,
    _TOMBSTONE_UNCONFIRMED, _classify_consent_reply,
    _consent_refusal_reason, _durable_payload, _duration_ru, _gate_batch,
    _gate_single, _is_affirmative_reply, _is_negative_reply,
    _journal_write, _manifest_from_payload, _manifest_gone_msg,
    _manifest_object_hash, _manifest_params_hash, _manifest_ttl_phrase,
    _mark_manifest_consumed, _maybe_tg_notify_plan, _op_journal,
    _plan_id_line, _prune_manifests, _rehydrate_manifest, _report_line,
    _require_consent, _restore_manifests_from_db, _ru_plural,
    _snapshot_of, _tg_button_only, _tombstone_manifest,
    _tombstone_reason_for_verdict)
consent.bind_server_hooks(  # noqa: E402
    tg_cfg=_TG_CFG, automation_key_matches=_automation_key_matches,
    redact_for_user=_redact_for_user, run_blocking=_run_blocking)
@mcp.tool(annotations=READONLY)
async def plan_task_deletion(summary: str, tasks: List[Dict[str, str]],
                             max_items: int = 50) -> str:
    """
    Phase 1 of SAFE deletion — the two-phase (plan → user says yes →
    execute) path, REQUIRED for bulk deletes and for parent+subtree deletes.
    (A single task MAY still be removed directly via delete_tasks when its
    title is supplied, up to DIRECT_DELETE_CAP=1 — that path is ALSO gated,
    not disabled; anything larger than the cap is refused there and routed
    here.) Builds a deletion MANIFEST without deleting anything. Read-only —
    safe to call without confirmation.

    Each requested {taskId, title?, projectId?, with_subtasks?} is resolved
    against LIVE state: ids that don't exist or whose live title doesn't match
    the given one are EXCLUDED and reported. with_subtasks=true expands the
    item's open subtasks into the manifest (server-side, from live state). The
    returned manifest lists exactly what WOULD be deleted — as the SERVER sees
    it, not as the caller claims.

    IMPORTANT: reprint the returned manifest text VERBATIM and IN FULL in your
    own reply to the user (tool-result blocks may be collapsed in some UIs —
    your message is always fully visible), then STOP and wait for their real
    reply — do NOT call execute in this same turn. Only once the human has
    actually answered, call execute_task_deletion(manifest_id,
    user_reply=<their literal last message, verbatim — do not paraphrase or
    invent it>), and afterwards operation_report(record_id) for the
    independent outcome check.

    Nothing is deleted by this tool. Manifests are one-shot and expire in 1 h.

    TELEGRAM CONFIRMATION LAYER (optional, off by default): when it is on this
    plan ALSO goes to the owner as a message with ✅/🛑 buttons, and ✅ makes
    the SERVER delete on its own (background poller), reporting into that same
    message. Then do NOT call execute_task_deletion at all — for such a plan
    the text path is closed and every call is refused.

    Args:
        summary: one-line human sentence (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        tasks: List of {"taskId","title","projectId","with_subtasks"} — title recommended
        max_items: refuse to plan more than this many deletions (blast cap)
    """
    err = _ensure_ready()
    if err:
        return err
    _prune_manifests()
    if not tasks:
        return "Пустой список — планировать нечего."
    by_id = _open_by_id(fresh=True)
    if by_id is None:
        return _STATE_UNAVAILABLE_MSG
    found, mismatch, missing = _split_tasks_by_state(tasks, by_id=by_id)
    names = _v2_project_names()
    mid = uuid.uuid4().hex[:12]

    def _mk_item(tid, pid, live):
        return {
            "taskId": tid, "projectId": pid,
            "title": (live or {}).get("title") or "",
            "project": names.get(pid, ""),
            # `attachments` — та же причина, что в `_snapshot_of` (аудит
            # 2026-08-09): по снимку строится имя безымянной задачи в отчёте,
            # и по нему же видно, что вместе с задачей удалён файл.
            "snapshot": {k: (live or {}).get(k) for k in
                         ("title", "content", "desc", "dueDate", "startDate",
                          "priority", "tags", "projectId", "parentId",
                          "isAllDay", "attachments")
                         if (live or {}).get(k) is not None},
        }

    want_subs = {(t.get("taskId") or t.get("task_id"))
                 for t in tasks if t.get("with_subtasks")}
    # Children index for FULL subtree expansion (grandchildren included) —
    # one-level expansion would delete parent+child and orphan the grandchild.
    kids: Dict[str, List[Dict]] = {}
    for sub in by_id.values():
        p = sub.get("parentId")
        if p:
            kids.setdefault(p, []).append(sub)
    items, seen = [], set()
    for f in found:
        if f["taskId"] in seen:
            continue
        seen.add(f["taskId"])
        live = by_id.get(f["taskId"]) or {}
        it = _mk_item(f["taskId"], f["projectId"], live)
        it["title"] = it["title"] or f["title"]
        items.append(it)
        if f["taskId"] in want_subs:
            # Server-side expansion: the ENTIRE open subtree of this parent
            # (BFS over parentId, any depth) joins the manifest with its live
            # title — nothing hand-typed by the caller, no orphans left.
            queue = list(kids.get(f["taskId"], []))
            depth_of = {f["taskId"]: 0}
            while queue:
                sub = queue.pop(0)
                sid = sub.get("id")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                d = depth_of.get(sub.get("parentId"), 0) + 1
                depth_of[sid] = d
                si = _mk_item(sid, sub.get("projectId") or f["projectId"], sub)
                si["title"] = si["title"] or f"[task {str(sid)[:8]}…]"
                si["depth"] = d  # render-only; title stays clean for re-verify
                items.append(si)
                queue.extend(kids.get(sid, []))
    if len(items) > max_items:
        return (f"🛑 Отказ: после разворачивания подзадач в плане {len(items)} "
                f"удалений — больше капа {max_items}. Разбей на части или "
                "подними max_items осознанно.")
    now = time.monotonic()
    obj_hash = _manifest_object_hash("delete", [it["taskId"] for it in items])
    _MANIFESTS[mid] = {"kind": "delete", "items": items,
                       "created": now, "plan_shown_at": now,
                       "object_hash": obj_hash,
                       "summary": summary, "consumed": False}
    lines = [f"### 📋 План удаления — {len(items)}",
             _plan_id_line(mid, "ничего ещё не удалено"), ""]
    for i, it in enumerate(items, 1):
        mark = "↳ " * it.get("depth", 0)
        # Строка про безымянную задачу говорит, ЧТО в ней лежит, и что её
        # личность сверена по id (П15, 2026-08-09). До этого здесь печаталось
        # `**«»**` — пустое место, по которому владелец и включил в удаление
        # чек Home Depot вместе с настоящим мусором. Название в манифесте
        # при этом остаётся ПУСТЫМ: по нему сверяется исполнение, и подменять
        # его заменителем значило бы сверять выдуманное имя.
        shown = (f"**«{it['title']}»**" if not _looks_untitled(it["title"]) else
                 f"**«{_untitled_label(by_id.get(it['taskId']) or {})}»** "
                 f"— {_BY_ID_NOTE}")
        lines.append(f"{i}. {mark}{shown} — {it['project']} (`{it['taskId']}`)")
    if mismatch:
        lines.append(_mismatch_report(mismatch, "включил в план"))
    if missing:
        lines.append(f"↷ Исключены (не среди открытых) {len(missing)}: "
                     + ", ".join(f"«{m['title']}»" for m in missing))
    lines.append("")
    # Инструкция для модели — ОТДЕЛЬНО от `lines` (2026-08-06, дефект №2):
    # раньше уходила дословно в Telegram-карточку плана.
    agent_tail = ("Покажи этот план пользователю дословно и ДОЖДИСЬ его "
                 "отдельного ответа (не отвечай за него). Когда он явно "
                 "согласится, вызови "
                 f"`execute_task_deletion(manifest_id=\"{mid}\", "
                 "user_reply=\"<дословная реплика пользователя>\")` — НЕ в "
                 "этом же ходе. Манифест одноразовый, "
                 f"действует {_manifest_ttl_phrase()}.")
    # Сетевую часть отправки уводит в поток сам хук (см. delete_tasks выше и
    # докстринг `_maybe_tg_notify_plan`) — здесь просто await.
    return await _maybe_tg_notify_plan("delete_tasks", mid, "\n".join(lines),
                                       agent_tail)


@mcp.tool()
async def execute_task_deletion(manifest_id: str, user_reply: str = "") -> str:
    """
    Phase 2: execute a deletion manifest created by plan_task_deletion.

    Deletes EXACTLY the manifest's items — the caller cannot add or swap tasks
    here. Gated (🔴 docs/DESIGN_approval_gate.md): `user_reply` must be the
    user's VERBATIM last chat message, given ONLY after they actually saw the
    plan and replied — do not paraphrase, summarize, or invent it, and do not
    call this in the same turn where you printed the plan. The server checks
    the reply looks like a human affirmative — not empty, no negation anywhere
    in the sentence, not manifest jargon echoed back, not a partial "yes" with
    a caveat («ок, кроме последней»: the manifest is applied whole or not at
    all), not a paraphrase of the user («пользователь: да») — enforces a
    minimum gap since the plan was shown, and consumes the manifest once. It
    cannot tell a genuine «да» from a made-up one; the only out-of-band factor
    is the Telegram button, when enabled. Every item is also re-verified
    against live state (renamed since planning → skipped); full task
    snapshots are appended to the deletion journal before the delete; the
    effect is post-verified against fresh state.

    TELEGRAM CONFIRMATION LAYER (optional, off by default): when the plan was
    announced there, the text path is CLOSED for it — this call is refused
    whatever `user_reply` says. Pressing ✅ makes the SERVER delete on its own
    (background poller) and write the report into that same message; there is
    nothing for you to do afterwards.

    Args:
        manifest_id: id returned by plan_task_deletion
        user_reply: the user's literal last message approving the plan
    """
    err = _ensure_ready()
    if err:
        return err
    _prune_manifests()
    # План мог быть построен ДРУГИМ процессом (перезапуск между планом и
    # исполнением). Если этот его не знает — поднимаем из базы (#91).
    await _rehydrate_manifest(manifest_id)
    m = _MANIFESTS.get(manifest_id)
    if not m or m.get("kind") != "delete":
        return _manifest_gone_msg(
            manifest_id,
            f"🛑 Манифест удаления {manifest_id} не найден/истёк/уже "
            "исполнен. Сначала plan_task_deletion.")
    # tool=/manifest_id= ОБЯЗАТЕЛЬНЫ здесь (2026-08-06): без них
    # `_require_consent` молча пропускал ТГ-фактор целиком, и то же самое
    # удаление, что через `delete_tasks` требовало кнопку, здесь исполнялось
    # по одному лишь chat-«да». Имя тула — то, под которым план анонсирован в
    # Telegram (`plan_task_deletion` шлёт его как "delete_tasks", тот же ключ
    # в `_AUTO_EXECUTORS`/`TG_APPROVAL_TOOLS`), а не имя этой функции.
    cr = _require_consent(action="delete", tier=2, manifest=m,
                          user_reply=user_reply,
                          object_ids=[it["taskId"] for it in m["items"]],
                          tool="delete_tasks", manifest_id=manifest_id)
    if not cr.ok:
        return cr.reason
    return await _execute_task_deletion_impl(manifest_id, m)


async def _execute_task_deletion_impl(manifest_id: str, m: Optional[Dict] = None) -> str:
    """Shared deletion engine: does the actual TickTick delete for a
    plan_task_deletion manifest, once consent has already been granted (by
    execute_task_deletion) or is inherited from an ALREADY-consented outer
    action (execute_declutter/_execute_declutter_from_sheet build a fresh
    sub-manifest and call straight in here — the human already said yes to
    the outer declutter, so re-asking for this internal step would just be
    the model self-confirming with extra steps)."""
    if m is None:
        m = _MANIFESTS.get(manifest_id)
    if not m or m.get("kind") != "delete":
        return (f"🛑 Манифест удаления {manifest_id} не найден/истёк/уже "
                "исполнен. Сначала plan_task_deletion.")
    try:
        by_id = _open_by_id(fresh=True)
        if by_id is None:
            # Do NOT consume the manifest: nothing was verified or deleted.
            return _STATE_UNAVAILABLE_MSG
        _mark_manifest_consumed(m, manifest_id)
        names = _v2_project_names()
        ready, drifted = [], []
        for it in m["items"]:
            live = by_id.get(it["taskId"])
            planned_title = (it.get("title") or "").strip()
            live_title = (live or {}).get("title") or ""
            live_pid = (live or {}).get("projectId") or it.get("projectId") or ""
            planned_pid = (it.get("projectId") or "").strip()
            planned_proj = (it.get("project") or "").strip()
            # FULL identity binding before an irreversible delete: the id must
            # still resolve to a live task whose TITLE **and** PROJECT are the
            # ones the human saw in the approved plan. Anything unverifiable
            # (task gone, no title stored to check against) is fail-closed —
            # skipped and reported, never "deleted just in case".
            if not live:
                drifted.append((it.get("title") or f"[task {str(it['taskId'])[:8]}…]",
                                "нет среди открытых задач"))
                continue
            # ОДИН новый разрешённый случай: имени НЕТ С ОБЕИХ СТОРОН (П15,
            # 2026-08-09). Сверка «имя из плана == живое имя» защищает от
            # подмены объекта, но у безымянной задачи сравнивать нечего, и
            # прежний безусловный отказ здесь не защищал, а запирал: три
            # задачи владельца (среди них фотография чека Home Depot на
            # возврат $374.92) нельзя было ни удалить, ни переименовать —
            # штатного выхода из состояния не было вовсе.
            #
            # ГРАНИЦА ПОСЛАБЛЕНИЯ, УЖЕ ЕЁ НЕ БЫВАЕТ: пусто И в плане, И в
            # живом состоянии. Пустое имя в плане при НЕ пустом живом (или
            # наоборот) — по-прежнему расхождение и отказ: это ровно случай
            # «id теперь указывает на другую задачу», от которого сверка и
            # стоит. Задачу при этом всё равно опознали — по id, — и об этом
            # сказано вслух в карточке плана (_plan_task_name/_BY_ID_NOTE),
            # а не пропущено молча.
            if _is_untitled(planned_title) and not _is_untitled(live_title):
                drifted.append((live_title,
                                "в плане не было названия, а у живой задачи "
                                "оно есть — сверить id↔задачу нечем"))
                continue
            if not _is_untitled(planned_title) and _is_untitled(live_title):
                drifted.append((planned_title,
                                "у живой задачи по этому id названия НЕТ, а в "
                                "плане оно было — это другой объект"))
                continue
            if not _names_agree(planned_title, live_title):
                drifted.append((planned_title,
                                f"id теперь указывает на «{live_title}»"))
                continue
            # Project check: armed by id when the plan carried one, else by
            # display name (sheet-backed rows store only the name). Both
            # absent → nothing to compare, title check stands alone.
            if planned_pid and live_pid and live_pid != planned_pid:
                drifted.append((planned_title,
                                "задача переехала в другой проект "
                                f"(«{names.get(live_pid, live_pid)}» вместо "
                                f"«{names.get(planned_pid, planned_pid)}»)"))
                continue
            if (not planned_pid) and planned_proj and \
                    not _names_agree(planned_proj, names.get(live_pid, "")):
                drifted.append((planned_title,
                                "задача не в том проекте, что в плане "
                                f"(«{names.get(live_pid, '')}» вместо "
                                f"«{planned_proj}»)"))
                continue
            ready.append({"taskId": it["taskId"],
                          "projectId": live_pid or it.get("projectId", ""),
                          # Отчёт об удалении обязан НАЗЫВАТЬ объект: пустое
                          # `planned_title` печаталось бы как «» — строка, по
                          # которой нельзя понять, что именно исчезло (П15,
                          # 2026-08-09).
                          "title": (planned_title
                                    if not _looks_untitled(planned_title)
                                    else _untitled_label(live)),
                          "snapshot": it["snapshot"]})
        journal = _journal_write({
            "ts": datetime.now(timezone.utc).isoformat(),
            "manifest": manifest_id, "summary": m.get("summary"),
            "deleted": [{**r["snapshot"], "taskId": r["taskId"]} for r in ready],
        }) if ready else ""
        api_fail = {}
        if ready:
            resp = await _run_blocking(lambda: ticktick_v2.batch_delete_tasks(
                [{"taskId": r["taskId"], "projectId": r["projectId"]} for r in ready]))
            api_fail = id2error_failures(resp, [r["taskId"] for r in ready])
        still = _open_by_id(fresh=True) if ready else {}
        lines = []
        if still is None:
            deleted, failed = [], []
            lines.append(f"Отправлено на удаление {len(ready)}, но "
                         f"{_UNVERIFIED_MSG}")
        else:
            deleted = [r["title"] for r in ready
                       if r["taskId"] not in still and r["taskId"] not in api_fail]
            failed = [r["title"] for r in ready
                      if r["taskId"] in still or r["taskId"] in api_fail]
        if api_fail:
            lines.append("❌ TickTick отклонил " + str(len(api_fail)) + ": "
                         + "; ".join(f"{k[:8]}…: {v}" for k, v in api_fail.items()))
        if deleted:
            lines.append(f"🗑 Удалено {len(deleted)}/{len(m['items'])}: "
                         + ", ".join(f"«{t}»" for t in deleted))
        if drifted:
            lines.append(
                f"⏭ Пропущены {len(drifted)} (не совпали с одобренным планом — "
                "перепланируй): "
                + ", ".join(f"«{t}» ({why})" for t, why in drifted))
        if failed:
            lines.append(f"❌ НЕ удалено {len(failed)} (всё ещё в TickTick): "
                         + ", ".join(f"«{t}»" for t in failed))
        if journal:
            lines.append(f"🧾 Снапшоты удалённого — в журнале: {journal} "
                         "(восстановление: restore_tasks из корзины, либо "
                         "пересоздание из снапшота).")
        # Append the server-built independent report — not optional, the model
        # can't skip what's already in the tool result.
        if deleted or failed:
            lines.append("\n" + _build_operation_report(manifest_id))
        return "\n".join(lines) if lines else "Ничего не удалено."
    except Exception as e:
        logger.exception("Error in execute_task_deletion")
        return _tool_error("executing deletion manifest", e)


def _fmt_tag_set(tags) -> str:
    """Человекочитаемое представление набора тегов для отчёта перепроверки:
    «tag1, tag2» вместо сырого Python-репра `['tag1', 'tag2']` — владелец
    читает это в Telegram, квадратные скобки и кавычки ему ни о чём не
    говорят. Пустой набор — «нет», не «[]»."""
    joined = ", ".join(sorted(tags))
    return joined if joined else "нет"


def _verify_item(op: str, item: Dict, live_map: Dict[str, Dict],
                 names: Dict) -> Tuple[str, str]:
    """Один вердикт по одной записи из журнала, по ТЕКУЩЕМУ живому состоянию.

    Возвращает (status, line): status — строго одно из "ok"/"warn"/"bad", и
    ЭТО ЕДИНСТВЕННОЕ, по чему вызывающий код имеет право считать статистику
    (см. _build_operation_report). Статус НИКОГДА не восстанавливается заново
    парсингом эмодзи в начале `line` — именно так раньше терялись расхождения
    с пометкой ⚠️ (баг «расхождений: 0» при 4 напечатанных пунктах). Каждый
    return ниже явно указывает статус рядом со строкой, к которой он относится.
    """
    tid = item.get("taskId")
    # КАК ЗОВУТ ОБЪЕКТ В ВЕРДИКТЕ (аудит 2026-08-09). Раньше безымянная
    # задача падала на последний фолбэк, и отчёт печатал «- ✅ **«[task
    # tB…]»** — удалена» — идентификатор в кавычках, в позиции имени, ровно
    # та форма, которую сервер осуждает везде ещё. Причём строкой выше тот же
    # объект уже был назван по-человечески («🗑 Удалено 1/1: «(без названия:
    # 📎 1 файл)»»), то есть один объект в одном сообщении звался двумя
    # разными способами. Снимок удалённой задачи несёт её содержимое (включая
    # метаданные вложений — см. `_mk_item`), поэтому заменитель строится из
    # него и совпадает с тем, что человек видел в плане.
    snap = item.get("snapshot") or {}
    if not _looks_untitled(item.get("title")):
        title = item["title"]
    elif not _looks_untitled(snap.get("title")):
        title = snap["title"]
    elif snap:
        title = _untitled_label(snap)
    else:
        title = f"[task {str(tid)[:8]}…]"
    live = live_map.get(tid)
    exp = item.get("expect") or {}

    if op == "delete":
        return (("bad", f"- ❌ **«{title}»** — ВСЁ ЕЩЁ существует (удаление не "
                 "состоялось или восстановлена)") if live else
                ("ok", f"- ✅ **«{title}»** — удалена"))
    if op == "restore":
        # Сведение двух правок (fix/qa-c-report + fix/qa-e-restore):
        # C перевёл ВСЕ ветки на явный кортеж (status, line) — статус больше
        # никогда не восстанавливается парсингом эмодзи; E добавил сюда
        # сверку проекта назначения (задача может вернуться из корзины, но
        # не в тот список). Проверка E сохранена целиком, в контракте C:
        # «вернулась, но не туда» — это именно ⚠️/"warn", то самое
        # расхождение, потеря которого и была багом «расхождений: 0».
        if not live:
            # «Нет среди открытых» — ещё не провал. Задача, завершённая ДО
            # удаления, возвращается из корзины ЗАВЕРШЁННОЙ, и в снимке
            # открытых её не будет никогда: прежний безусловный ❌ здесь был
            # красным вердиктом об удавшейся операции (живая приёмка
            # 2026-08-07). Спрашиваем те же три ленты, что и identity-guard
            # самого restore_tasks, — включая КОРЗИНУ, по которой только и
            # видно настоящий провал «так и не восстановилась».
            found, where, readable = _locate_task_any_state(tid)
            if not readable:
                return ("warn", f"- ⚠️ **«{title}»** — среди открытых нет, а "
                        "проверить остальные состояния не удалось (чтение не "
                        "прошло): исход НЕ ПОДТВЕРЖДЁН")
            if where == "trash":
                return ("bad", f"- ❌ **«{title}»** — ВСЁ ЕЩЁ В КОРЗИНЕ "
                        "(восстановление не состоялось)")
            if where != "completed":
                return ("bad", f"- ❌ **«{title}»** — не найдена нигде: ни "
                        "среди открытых, ни среди завершённых, ни в корзине "
                        "(восстановление не подтвердилось)")
            want_pid = exp.get("projectId")
            got_pid = (found or {}).get("projectId")
            if want_pid and got_pid != want_pid:
                return ("warn", f"- ⚠️ **«{title}»** — восстановлена, но "
                        "вернулась ЗАВЕРШЁННОЙ и лежит в «"
                        f"{names.get(got_pid, got_pid)}», а не в «"
                        f"{names.get(want_pid, want_pid)}» (не тот список)")
            return ("ok", f"- ✅ **«{title}»** — восстановлена; вернулась "
                    "ЗАВЕРШЁННОЙ (потому её и нет среди открытых), в нужном "
                    "списке")
        want_pid = exp.get("projectId")
        if want_pid and live.get("projectId") != want_pid:
            return ("warn", f"- ⚠️ **«{title}»** — среди открытых, но в «"
                    f"{names.get(live.get('projectId'), live.get('projectId'))}»"
                    f", а не в «{names.get(want_pid, want_pid)}» (не тот список)")
        return ("ok", f"- ✅ **«{title}»** — снова среди открытых, в нужном списке")
    if op in ("complete", "abandon"):
        verb = "закрыта" if op == "complete" else "отмечена «не буду делать»"
        return (("bad", f"- ❌ **«{title}»** — всё ещё среди открытых") if live
                else ("ok", f"- ✅ **«{title}»** — {verb} (ушла из открытых)"))
    if op == "delete_project":
        # tid here is the PROJECT id, not a task id. Re-fetch fresh via the
        # None-distinguishing helper rather than trusting the `names` dict
        # the caller already had — a stale/failed fetch must never read as
        # "project confirmed deleted".
        fresh_names = _v2_project_names_or_none()
        if fresh_names is None:
            return ("warn", f"- ⚠️ **«{title}»** — проект: проверка не удалась "
                    "(не получилось перечитать список проектов), исход НЕ "
                    "ПОДТВЕРЖДЁН")
        still = fresh_names.get(tid)
        return (("bad", f"- ❌ **«{title}»** — проект ВСЁ ЕЩЁ существует "
                 "(удаление не подтвердилось)") if still else
                ("ok", f"- ✅ **«{title}»** — проект удалён"))
    if op == "delete_habit":
        # def-119 (2026-08-07), часть 2: `tid` — id ПРИВЫЧКИ, не задачи. Тот
        # же приём, что у delete_project чуть выше (CONFIRMED ABSENCE): фреш-
        # чтение через хелпер, различающий «пусто» и «не смогли прочитать»,
        # иначе неудачный фетч читался бы как подтверждённое удаление.
        fresh_habits = _v2_habits_or_none()
        if fresh_habits is None:
            return ("warn", f"- ⚠️ **«{title}»** — привычка: проверка не "
                    "удалась (не получилось перечитать список привычек), "
                    "исход НЕ ПОДТВЕРЖДЁН")
        still = any(h.get("id") == tid for h in fresh_habits)
        return (("bad", f"- ❌ **«{title}»** — привычка ВСЁ ЕЩЁ существует "
                 "(удаление не подтвердилось)") if still else
                ("ok", f"- ✅ **«{title}»** — привычка удалена"))
    if live is None:
        return ("bad", f"- ❌ **«{title}»** — не найдена среди открытых "
                "(ожидалась живой)")
    if op == "create":
        probs = []
        want_pid = exp.get("projectId")
        if want_pid and live.get("projectId") != want_pid:
            probs.append(f"в «{names.get(live.get('projectId'), '?')}», а не "
                         f"«{names.get(want_pid, want_pid)}»")
        if exp.get("columnId") and live.get("columnId") != exp.get("columnId"):
            probs.append("раздел не применился")
        # Привязка и теги (д5/д6, 2026-08-09). Создание умеет и то и другое,
        # но независимая проверка знала только про проект и раздел — задача,
        # оставшаяся корневой вместо подзадачи, и потерянный тег получали
        # от отчёта чистое «✅ создана».
        want_parent = exp.get("parentId")
        if want_parent and live.get("parentId") != want_parent:
            parent_title = (live_map.get(want_parent) or {}).get("title") \
                or f"{str(want_parent)[:8]}…"
            probs.append(f"НЕ вложена под «{parent_title}» "
                         f"(parentId={live.get('parentId')!r})")
        want_tags = exp.get("tags")
        if want_tags is not None \
                and set(live.get("tags") or []) != set(want_tags):
            probs.append(f"теги: {_fmt_tag_set(set(live.get('tags') or []))}, "
                         f"ожидались {_fmt_tag_set(set(want_tags))}")
        # Исполнитель (д6, 2026-08-09), рядом с тегами — та же дыра, тот же
        # адрес. Сравнение СТРОКОВОЕ (`str(got) != str(want)`), а не по
        # равенству значений: TickTick отдаёт assignee числом, вызывающие —
        # то числом, то строкой, и точное сравнение типов даёт ложную
        # тревогу на исправном назначении (отдельный дефект внутри Д6, не
        # косметика). Сверяется только ЗАПРОШЕННОЕ (`is not None`); пустое
        # живое поле трактуется как «никто», как и в _assignee_matches
        # (ticktick_v2_client.py) — личный проект не должен пугать ложной
        # тревогой на каждом созданной задаче без исполнителя.
        want_assignee = exp.get("assignee")
        if want_assignee is not None \
                and str(live.get("assignee")) != str(want_assignee):
            members = _member_names(live.get("projectId") or want_pid or "")
            probs.append(
                f"исполнитель: {_person_label(live.get('assignee'), members)}, "
                f"ожидался {_person_label(want_assignee, members)}")
        if probs:
            return ("warn", f"- ⚠️ **«{title}»** — создана, но: "
                    + "; ".join(probs))
        # State the FACTS, not agreement-with-intent: the reader must SEE where
        # it landed, so a wrong-but-consistent request is still visible.
        facts = [f"в «{names.get(live.get('projectId'), live.get('projectId'))}»"]
        if live.get("columnId"):
            facts.append("раздел применён")
        if live.get("dueDate"):
            # Это текст РЕШЕНИЯ: по нему владелец принимает результат
            # создания, а не просматривает список. Сырой срез [:10] от
            # UTC-строки называл здесь чужой календарный день — тот же
            # дефект, что чинили в списках (f85fc76), но с более дорогой
            # ценой ошибки. _local_date_str() держит и all-day буквально.
            facts.append(f"срок {_local_date_str(live, 'dueDate')}")
        if live.get("priority"):
            facts.append(f"приоритет {PRIORITY_MAP.get(live['priority'], live['priority'])}")
        return ("ok", f"- ✅ **«{title}»** — создана {', '.join(facts)}")
    if op == "move":
        want = exp.get("projectId")
        return (("ok", f"- ✅ **«{title}»** — в **«{names.get(want, want)}»**")
                if live.get("projectId") == want else
                ("bad", f"- ❌ **«{title}»** — осталась в «{names.get(live.get('projectId'), '?')}»"))
    if op == "tags":
        want = set(exp.get("tags") or [])
        got = set(live.get("tags") or [])
        return (("ok", f"- ✅ **«{title}»** — теги: {_fmt_tag_set(got)}") if want == got
                else ("bad", f"- ❌ **«{title}»** — теги: {_fmt_tag_set(got)}, "
                      f"ожидались: {_fmt_tag_set(want)}"))
    if op == "parent":
        want = exp.get("parentId")  # None = detached
        got = live.get("parentId")
        # A parentId "applied" toward a parent that is NOT itself alive among
        # open tasks is an orphaning, not a success — check the parent too.
        if want and want not in live_map:
            return ("bad", f"- ❌ **«{title}»** — родитель {str(want)[:8]}… НЕ "
                    "среди открытых задач (вложение под несуществующего/"
                    "закрытого родителя)")
        ok = (got == want) if want else not got
        if not ok:
            return ("bad", f"- ❌ **«{title}»** — parentId={got!r}, ожидался {want!r}")
        # def-110: «родитель применён» раньше был ОДНИМ литералом на обе
        # противоположные операции (вложить / отцепить) — set_task_parent и
        # unset_task_parent над одной и той же задачей давали посимвольно
        # одинаковую строку успеха, и по отчёту нельзя было понять, что
        # реально произошло. По образцу соседней ветки `tags` чуть выше
        # (которая печатает НАБЛЮДАЕМОЕ состояние поля — `sorted(got)` — а не
        # название операции) строка теперь называет ФАКТ: что сейчас стоит в
        # `parentId`, прочитанное живьём (`got`), а не какая операция
        # предположительно к этому привела. Так строка не может
        # рассинхронизироваться с операцией — она вообще не знает, какая она
        # была, — и сама собой различает любую будущую операцию над
        # `parentId`, не только эти две.
        if got:
            parent_title = (live_map.get(got) or {}).get("title") \
                or f"{str(got)[:8]}…"
            return ("ok", f"- ✅ **«{title}»** — родитель: «{parent_title}»")
        return ("ok", f"- ✅ **«{title}»** — родитель: нет")
    if op == "update":
        changes = exp.get("changes") or {}
        diffs = []
        for field, want in changes.items():
            got = live.get(field)
            if field in ("dueDate", "startDate") and isinstance(got, str) \
                    and isinstance(want, str):
                if got[:10] != want[:10]:
                    diffs.append(f"{field}: {got!r} ≠ {want!r}")
            elif field == "tags":
                if set(got or []) != set(want or []):
                    diffs.append(f"tags: {got} ≠ {want}")
            elif field == "assignee":
                # д6 (2026-08-09): тот же дефект, что в ветке "create" выше —
                # assignee приходит то числом, то строкой, и точное
                # сравнение давало ложную тревогу на исправном назначении.
                if str(got) != str(want):
                    diffs.append(f"assignee: {got!r} ≠ {want!r}")
            elif got != want:
                diffs.append(f"{field}: {got!r} ≠ {want!r}")
        return (("bad", f"- ❌ **«{title}»** — не применилось: " + "; ".join(diffs))
                if diffs else ("ok", f"- ✅ **«{title}»** — все изменения на месте"))
    # Тип операции без выделенного проверятеля: автоматически НЕ проверяется —
    # это предупреждение, а не молчаливый успех, и должно считаться как такое.
    # Также: ASCII-символ "✓" запрещён как статусный маркер замороженной
    # легендой (output-format.md §7.2) — используем ⚠️, как и в остальных
    # случаях «не проверено».
    return ("warn", f"- ⚠️ **«{title}»** — записана в журнал (тип {op} не "
            "проверяется автоматически)")


def _compute_op_verdicts(records: List[Dict], live: Dict[str, Dict],
                         names: Dict) -> List[Tuple[str, str]]:
    """Runs _verify_item over every item of every journal record against ONE
    given live snapshot. Factored out of _build_operation_report so the
    identity-changing-op retry there (_POSTVERIFY_RETRY_ATTEMPTS) can re-run
    the exact same computation against a freshly re-fetched snapshot without
    duplicating the items-extraction logic."""
    verdicts: List[Tuple[str, str]] = []
    for rec in records:
        op = rec.get("op") or "delete"
        if op == "delete_habit":
            # def-119 (2026-08-07), часть 2: журнальная запись delete_habit
            # (см. _delete_habit_impl) несёт ОДИНОЧНЫЙ "snapshot" — не
            # "items"/"deleted" списком, как у остальных операций. Без этой
            # ветки `items` ниже молчаливо получался пустым: цикл
            # `_verify_item` не вызывался НИ РАЗУ, verdicts оставался
            # пустым, и итог читался как «0/0/0» — вакуумная истина (см.
            # часть 1 def-119 чуть выше по файлу).
            snap = rec.get("snapshot") or {}
            items = ([{"taskId": snap.get("id"), "title": snap.get("name")}]
                     if snap.get("id") else [])
        else:
            items = rec.get("items") or [
                {"taskId": s.get("taskId"), "title": s.get("title"), "snapshot": s}
                for s in rec.get("deleted", [])
            ]
        for item in items:
            verdicts.append(_verify_item(op, item, live, names))
    return verdicts


@mcp.tool(annotations=READONLY)
async def operation_report(record_id: str) -> str:
    """
    Independent post-execution report for ANY journaled mutation. Read-only.

    Every mutating tool (create/update/complete/delete/move/tags/parent/abandon)
    returns a record_id like "create-a1b2c3d4". This tool re-reads what was
    RECORDED in the on-disk journal at execution time and re-checks every item
    against the CURRENT live TickTick state: created tasks must exist in the
    requested project/column, updates must show the new field values, deletions
    must be gone, moves must sit in the target project, etc. The verdict is
    built by the server from data — call it after any mutation the user cares
    about and reprint the output VERBATIM, so the outcome they see is the
    server's, not a paraphrase.

    Accepts both "<op>-<hex>" record ids and deletion manifest ids.

    Args:
        record_id: id returned by a mutating tool (or a deletion manifest id)
    """
    err = _ensure_ready()
    if err:
        return err
    return _build_operation_report(record_id)


def _build_operation_report(record_id: str) -> str:
    """Shared engine behind operation_report — also appended by the execute_*
    tools DIRECTLY into their result, so the independent check reaches the user
    even when the calling model never asks for it."""
    try:
        path = os.path.join(_JOURNAL_DIR, "deletion_journal.jsonl")
        records = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    # `tg_manifest` — метка автоисполнения по кнопке (см.
                    # _TG_AUTO_EXECUTE_MANIFEST): по ней отчёт находится и для
                    # тех исполнителей, что журналируют через _op_journal со
                    # своим "<op>-<hex>", а не под id манифеста.
                    if record_id in (rec.get("record"), rec.get("manifest"),
                                     rec.get("tg_manifest")):
                        records.append(rec)
        except FileNotFoundError:
            return (f"🧾 Журнал не найден ({path}) — операция {record_id} не "
                    "записана, отчёт дать не могу.")
        if not records:
            return (f"🧾 В журнале нет записей по {record_id} — операция не "
                    "исполнялась или журнал был недоступен в момент записи.")
        live = _open_by_id(fresh=True)
        if live is None:
            return (f"### 🧾 Отчёт по `{record_id}` невозможен\n"
                    "⚠️ Живое состояние TickTick недоступно — независимая "
                    "проверка не выполнена, исход операции НЕ ПОДТВЕРЖДЁН. "
                    "Повтори operation_report позже.")
        names = _v2_project_names()
        when = records[-1].get("ts", "?")
        try:
            when_dt = datetime.fromisoformat(when)
            when = when_dt.astimezone(_USER_TZ).strftime("%d.%m %H:%M")
        except (ValueError, TypeError):
            pass
        lines = [f"### 🧾 Независимый отчёт — `{record_id}`",
                 f"_{when} · журнал операции ⇄ живое состояние TickTick_", ""]
        # Единый источник истины: каждый вердикт сначала собирается сюда, в
        # виде пары (status, напечатанная_строка). И строки, печатаемые ниже,
        # и итоговый подсчёт дальше — оба выведены из ЭТОГО ЖЕ списка, а не
        # из отдельного счётчика, заново распознающего эмодзи в тексте. Это
        # структурно исключает расхождение между напечатанными пунктами и
        # строкой «Итог» (именно так раньше получалось «0 расхождений» рядом
        # с 4 явными пунктами ⚠️ — эти пункты начинались с ⚠️, а старый
        # счётчик по первым символам строки его не распознавал, поэтому
        # пункт печатался, но никогда не учитывался).
        verdicts = _compute_op_verdicts(records, live, names)
        # Ретрай ТОЛЬКО для identity-changing операций (move/parent/restore —
        # см. _POSTVERIFY_RETRY_DELAYS_S): та же гонка с v2-синком TickTick,
        # найденная живьём 2026-08-06 (дважды — второй раз уже НА фиксе
        # с более узким окном) на move_tasks, бьёт и по НЕЗАВИСИМОЙ
        # перепроверке, не только по post-verify самого исполнителя — «not
        # found among open at all» сразу после мутации был именно этот
        # случай. Не трогает delete/create/tags/complete/abandon — там ❌
        # уже сегодня означает то, что означает, и без подтверждённой гонки
        # незачем добавлять им лишнюю задержку на настоящем провале.
        retryable_ops = {"move", "parent", "restore"}
        if (any(status == "bad" for status, _ in verdicts)
                and any((rec.get("op") or "delete") in retryable_ops
                        for rec in records)):
            for delay in _POSTVERIFY_RETRY_DELAYS_S:
                if not any(status == "bad" for status, _ in verdicts):
                    break
                time.sleep(delay)
                live2 = _open_by_id(fresh=True)
                if live2 is None:
                    break
                live = live2
                verdicts = _compute_op_verdicts(records, live, names)
        lines.extend(line for _, line in verdicts)
        ok = sum(1 for status, _ in verdicts if status == "ok")
        warn = sum(1 for status, _ in verdicts if status == "warn")
        bad = sum(1 for status, _ in verdicts if status == "bad")
        lines.append("")
        lines.append(f"**Итог: ✅ {ok} подтверждено, ⚠️ {warn} не проверено, "
                      f"❌ {bad} расхождений.**")
        # Явный, однозначный общий вердикт для headless/программных
        # потребителей (например, бота tg-ai-assistant) — они не должны
        # прочитать отчёт с хотя бы одним расхождением или непроверенным
        # пунктом как успех.
        #
        # def-119 (2026-08-07), часть 1: ПУСТОЙ список проверок (ok=warn=
        # bad=0 — не было ни одной строки `_verify_item`, например журнальная
        # запись есть, но `_compute_op_verdicts` не смогла извлечь из неё ни
        # одного объекта) раньше читался как «✅ — всё подтверждено»: и bad,
        # и warn были falsy, а условие ниже не отличало «нечего проверять» от
        # «проверили и всё чисто». Это вакуумная истина — «Итог: ✅ 0
        # подтверждено…» само по себе уже говорит, что подтверждать было
        # нечего, а следующая строка утверждала обратное. Живой пример:
        # delete_habit (см. часть 2 ниже) — заголовок «❓ НЕ подтверждено»
        # соседствовал с «Статус операции: ✅ — всё подтверждено» в одном
        # сообщении.
        if bad:
            overall, tail = "❌", "есть расхождения — это НЕ успех."
        elif warn:
            overall, tail = "⚠️", "есть непроверенные пункты — это НЕ полный успех."
        elif ok:
            overall, tail = "✅", "всё подтверждено."
        else:
            overall, tail = "❓", "проверять было нечего — исход НЕ подтверждён."
        lines.append(f"**Статус операции: {overall}** — {tail}")
        lines.append("[агенту: перепечатай этот отчёт пользователю ДОСЛОВНО — "
                     "это серверная проверка, не заменяй её своим пересказом]")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("Error in operation_report")
        return _tool_error("building operation report", e)


@mcp.tool()
async def delete_task_with_subtasks(
    summary: str = "",
    task_id: str = "",
    project_id: str = "",
    task_title: str = None,
    project_name: str = None,
) -> str:
    """
    DEPRECATED / always refuses. Subtree deletion is NOT performed here —
    this tool exists only to catch old callers and redirect them. It always
    returns a refusal pointing to plan_task_deletion with {"taskId": ...,
    "title": ..., "with_subtasks": true}, which expands the ENTIRE open
    subtree into a manifest for approval (already gated 🔴 — plan_task_deletion →
    execute_task_deletion(manifest_id, user_reply=...)). No argument below
    has any effect; nothing is ever deleted by THIS tool, regardless of what
    you pass. None of them is required either (2026-08-07): a tool whose whole
    answer is a refusal must not demand a value to hand that refusal over —
    calling it with no arguments at all returns the same redirect.

    Args:
        summary: unused — has no effect, kept for backward-compatible calls
        task_id: unused — has no effect, kept for backward-compatible calls
        project_id: unused — has no effect, kept for backward-compatible calls
        task_title: unused — has no effect, kept for backward-compatible calls
        project_name: unused — has no effect, kept for backward-compatible calls
    """
    err = _ensure_ready()
    if err:
        return err
    # A parent + its subtasks is inherently a BULK delete → manifest ONLY.
    # (The former ALLOW_DIRECT_SUBTREE_DELETE escape hatch is removed: it had
    # no journal, no post-verify and an unhandled 'missing' guard — one env
    # var away from being the only unguarded destructive path in the cluster.)
    return ("🛑 Удаление дерева — только через манифест. Используй "
            "plan_task_deletion с {\"taskId\": ..., \"title\": ..., "
            "\"with_subtasks\": true} — план сам развернёт ВСЁ поддерево "
            "(включая под-подзадачи), покажет полный список на аппрув, а "
            "operation_report подтвердит результат.")


def _describe_create_project(p: Dict) -> str:
    color = p.get("color") or "по умолчанию"
    view = p.get("view_mode") or "list"
    return f'Создаю проект «{p.get("name")}» (цвет {color}, вид {view})'


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def create_project(
    name: str,
    color: str = "#F18181",
    view_mode: str = "list",
    manifest_id: str = "",
    user_reply: str = "",
    automation_key: str = "",
) -> str:
    """
    Create a new project in TickTick. Gated 🟡 (docs/DESIGN_approval_gate.md):
    two calls, same tool name — nothing is created on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is created yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — name/color/view_mode are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        name: Project name (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        color: Color code (hex format) (optional)
        view_mode: View mode - one of list, kanban, or timeline (optional)
        manifest_id: from call #1's response — pass on call #2 to actually create
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_official()
    if err:
        return err

    # Validate view_mode up front (cheap, applies to both calls).
    if view_mode not in ["list", "kanban", "timeline"]:
        return "Invalid view_mode. Must be one of: list, kanban, timeline."

    params = {"name": name, "color": color, "view_mode": view_mode}
    outcome = await _gate_single("create_project", "create_project",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, _describe_create_project,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _create_project_impl(**outcome.extra)


async def _create_project_impl(name: str, color: str = "#F18181",
                               view_mode: str = "list") -> str:
    """Pure mutation logic for create_project — no consent gate. Called only
    by the gated create_project() above once the plan is approved."""
    try:
        project = await _run_blocking(
            ticktick.create_project,
            name=name,
            color=color,
            view_mode=view_mode
        )

        if 'error' in project:
            return (f"### ❌ Проект «{name}» НЕ создан\n\nTickTick отклонил: "
                    f"{_redact_for_user(project['error'])}")
        pid = project.get('id')
    except Exception as e:
        logger.exception("Error in create_project")
        return f"### ❌ Проект «{name}» НЕ создан\n\nОшибка: {_redact_for_user(e)}"

    # Post-verify: independent fresh re-read (separate GET, not the create
    # response) so a false-positive "success" from the API doesn't slip through.
    try:
        fresh = await _run_blocking(ticktick.get_project, pid)
        if isinstance(fresh, dict) and not fresh.get('error') and fresh.get('id') == pid:
            return (f"### ✅ Создан проект «{fresh.get('name', name)}»\n\n"
                    f"{format_project(fresh)}\n\n"
                    f"🧾 Проверено: подтверждено отдельным живым чтением TickTick "
                    f"(id: {pid}).")
        return (f"### ⚠️ Проект «{name}» создан, но НЕ подтверждён\n\n"
                f"{format_project(project)}\n\n"
                f"⚠️ {_UNVERIFIED_MSG} (id: {pid})")
    except Exception as e:
        return (f"### ⚠️ Проект «{name}» создан, но НЕ подтверждён\n\n"
                f"{format_project(project)}\n\n"
                f"⚠️ {_UNVERIFIED_MSG} ({_redact_for_user(e)})")

_PROJECT_DELETE_SAMPLE_CAP = 20  # preview lines shown before the confirm echo


def _find_live_inline_manifest(kind: str, key: str) -> Tuple[str, Optional[Dict]]:
    """Самый свежий ЖИВОЙ манифест данного `kind` с совпадающим `key`.

    Нужна тулам, которые исторически были ОДНОХОДОВЫМИ (delete_project,
    rename_tag): у них нет параметра `manifest_id`, который можно было бы
    вернуть модели и получить обратно вторым вызовом, — публичная сигнатура
    менялась бы, а старые вызовы ломались. Поэтому «план» и «исполнение»
    здесь по-прежнему различаются наличием `user_reply`, а связь между двумя
    вызовами держится на естественном ключе объекта (id проекта / пара имён
    тега). Возвращает ("", None), когда живого плана нет, — тогда вызывающий
    ведёт себя ровно как до появления манифестов (manifest=None).
    """
    _prune_manifests()
    best_id, best = "", None
    for mid, m in _MANIFESTS.items():
        if m.get("kind") != kind or m.get("consumed") or m.get("key") != key:
            continue
        if best is None or (m.get("created") or 0) > (best.get("created") or 0):
            best_id, best = mid, m
    return best_id, best


@mcp.tool()
async def delete_project(project_name: str, project_id: str, user_reply: str = "") -> str:
    """
    ⚠️ Delete a project permanently — TickTick's own cascade ALSO deletes every
    task the project contains (uncapped blast radius: a project can hold any
    number of tasks). Irreversible → gated 🔴 (docs/DESIGN_approval_gate.md):

    1st call (user_reply omitted): deletes NOTHING. Returns the project's
    CURRENT contained-task count plus a short sample of titles and asks you
    to show that to the user and wait for their real reply.
    2nd call: AFTER the user actually replied, call again with
    user_reply=<their literal message>, verbatim — do not paraphrase or
    invent it, and do not make this 2nd call in the same turn as the 1st.
    The count is re-read fresh on EVERY call (nothing cached from the first
    call), so the project having changed in between is naturally reflected
    rather than deleting against a stale count.

    TELEGRAM CONFIRMATION LAYER (optional, off by default): with it on, the
    1st call ALSO makes this server send the plan to the owner as a Telegram
    message with [✅ Подтвердить]/[🛑 Отклонить] buttons — the server's own
    out-of-band second factor, not an external relay. In that mode the TEXT
    path is CLOSED: the 2nd call is refused however genuine `user_reply` is,
    before the press and after it alike. Pressing ✅ makes the SERVER delete
    the project on its own (background poller) and report into that same
    message; a press of "🛑 Отклонить" kills the plan outright.

    Args:
        project_name: Name of the project (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        project_id: ID of the project
        user_reply: the user's literal reply approving the deletion — omit on
            the 1st call
    """
    err = _ensure_official()
    if err:
        return err
    # Destructive: verify against FRESH names and FAIL CLOSED when the id
    # can't be resolved — never delete what can't be identified.
    refusal = _guard_project_or_refuse(project_id, project_name, fresh=True,
                                       require_known=True)
    if refusal:
        return refusal
    live_name = _v2_project_names().get(project_id, project_name)

    # Blast-radius disclosure: read the project's CURRENT contents fresh on
    # every call (no stored plan/manifest) — the compare below is always
    # against what's live right now, so drift between preview and confirm
    # naturally re-triggers a fresh preview instead of deleting stale counts.
    try:
        data = await _run_blocking(lambda: ticktick.get_project_with_data(project_id))
    except Exception as e:
        logger.exception("Error in delete_project (fetching contents)")
        return (f"🛑 Не смог прочитать содержимое проекта «{live_name}» — отказ "
                f"(не удаляю вслепую): {_redact_for_user(e)}")
    if 'error' in data:
        return (f"🛑 Не смог прочитать содержимое проекта «{live_name}» — отказ "
                f"(не удаляю вслепую): {_redact_for_user(data['error'])}")
    tasks = data.get('tasks') or []
    count = len(tasks)

    # Двухфазность через МАНИФЕСТ (2026-08-06). Раньше здесь стояло
    # `manifest=None` и не передавался `tool=` — то есть удаление ЦЕЛОГО
    # проекта (самая крупная воронка на сервере: каскадом уходят все его
    # задачи) было единственной 🔴-операцией ВНЕ Telegram-контура: план
    # владельцу не уходил, кнопка не требовалась. Просто дописать `tool=`
    # было нельзя — без отправленного плана строки в `tg_approvals` не
    # существует, `check_approval` вернул бы "none", и тул отказывал бы
    # ВСЕГДА при включённом слое. Поэтому фаза плана теперь заводит
    # настоящий манифест и зовёт `_maybe_tg_notify_plan`, а фаза исполнения
    # сверяется с ним; `tool=` намеренно НЕ передаётся — основанием требовать
    # кнопку служит пометка `tg_notified` на самом манифесте, которая
    # появляется ровно тогда, когда план реально ушёл.
    #
    # Публичная сигнатура тула не изменилась: фазы по-прежнему различаются
    # наличием `user_reply`, связь между вызовами — по id проекта.
    mid, m = _find_live_inline_manifest("delete_project", project_id)
    cr = _require_consent(action="delete_project", tier=2, manifest=m,
                          user_reply=user_reply,
                          object_ids=[project_id] if m is not None else None,
                          manifest_id=mid,
                          # min_gap=0 — сознательно: до появления манифеста
                          # этот тул анти-дуплетного таймера не имел, и
                          # включать его заодно значило бы менять поведение
                          # при выключенном ТГ-слое (запрещено).
                          min_gap=0)
    if cr.ok and m is None and tg_approval.enabled_for(_TG_CFG, "delete_project"):
        # Дыра, которую иначе оставила бы связка «нет манифеста → manifest=
        # None → ТГ-фактор не требуется»: модель могла позвать тул СРАЗУ с
        # user_reply="да", без первой фазы, и удалить проект в обход кнопки
        # при формально включённом слое. Лечится не отказом (это был бы тот
        # самый «отказывает всегда»), а откатом к ФАЗЕ ПЛАНА: ниже строится
        # манифест и уходит сообщение с кнопками.
        cr = ConsentResult(False, "")
    if not cr.ok:
        lines = [f"⚠️ Проект «{live_name}» содержит {count} задач(и) — при "
                 "удалении проекта TickTick удалит их ВМЕСТЕ с ним, "
                 "безвозвратно.", ""]
        if tasks:
            for t in tasks[:_PROJECT_DELETE_SAMPLE_CAP]:
                lines.append(format_task_line(t))
            if count > _PROJECT_DELETE_SAMPLE_CAP:
                lines.append(f"... и ещё {count - _PROJECT_DELETE_SAMPLE_CAP}.")
            lines.append("")
        if _is_negative_reply(user_reply) or m is not None:
            # Отказ человека — план и так аннулирован внутри _require_consent.
            # m is not None — план УЖЕ существует (и, возможно, уже отправлен
            # в Telegram): второй раз слать то же самое сообщение нельзя,
            # показываем причину отказа поверх свежего пересчёта.
            if m is not None:
                # Тот же id, что уже висит кнопкой в Telegram: этой веткой
                # отвечает сервер, когда план ЖИВ и ждёт нажатия, — именно
                # здесь клиенту нужнее всего знать, КАКОЙ план он ждёт.
                lines.insert(1, _plan_id_line(mid, "ничего ещё не удалено"))
            lines.append(cr.reason)
            return "\n".join(lines)
        # "Ничего не удалено." — человеку; остальное (2026-08-06, дефект №2)
        # — служебная инструкция ДЛЯ МОДЕЛИ («вызови delete_project снова с
        # user_reply=…»), раньше склеенная В ТУ ЖЕ строку и потому уходившая
        # дословно в Telegram-карточку плана. `_maybe_tg_notify_plan`
        # приклеивает её только к ответу модели.
        lines.append("Ничего не удалено.")
        agent_tail = (
            "Покажи это пользователю дословно и "
            "ДОЖДИСЬ его отдельного ответа (не отвечай за него). Когда "
            "он явно согласится, вызови "
            f'delete_project(project_name="{live_name}", '
            f'project_id="{project_id}", '
            'user_reply="<дословная реплика пользователя>") — НЕ в этом '
            'же ходе.')
        new_mid = uuid.uuid4().hex[:12]
        now = time.monotonic()
        _MANIFESTS[new_mid] = {
            "kind": "delete_project", "key": project_id,
            "project_id": project_id, "project_name": live_name,
            "count": count, "created": now, "plan_shown_at": now,
            "summary": f"Удаление проекта «{live_name}» ({count} задач)",
            "consumed": False, "tool": "delete_project",
            "_gate": "delete_project",
            "object_hash": _manifest_object_hash("delete_project", [project_id])}
        # Идентификатор плана — В ОТВЕТЕ (2026-08-06). До этой строки план
        # `delete_project` был снаружи безымянным: у тула нет параметра
        # `manifest_id`, связь фаз держится на id проекта
        # (`_find_live_inline_manifest`), а в ответе не было ничего, чем план
        # можно назвать. Клиенту (он видит ТОЛЬКО этот текст) оставалось
        # угадывать свой план по разнице списка ожидающих до/после вызова —
        # что и делал агент уборки, получив 19 «планов без идентификатора».
        lines.insert(1, _plan_id_line(new_mid, "ничего ещё не удалено"))
        return await _maybe_tg_notify_plan("delete_project", new_mid,
                                           "\n".join(lines), agent_tail)

    if m is not None:
        # one-shot: план сгорел вместе с исполнением (и в памяти, и в базе)
        _mark_manifest_consumed(m, mid)

    return await _delete_project_impl(project_id, live_name, tasks)


async def _delete_project_impl(project_id: str, project_name: str,
                               tasks: List[Dict[str, Any]]) -> str:
    """Само удаление проекта, БЕЗ гейта — согласие к этому моменту уже
    получено вызывающим (`delete_project` после `_require_consent`, либо
    фоновый поллер по нажатой кнопке через `_auto_execute_delete_project`).

    Вынесена 2026-08-06 вместе с button-only: до этого тело мутации жило
    прямо в теле тула, из-за чего нажатие кнопки на плане удаления ПРОЕКТА
    не приводило ни к чему (поллеру нечего было позвать), и операцию можно
    было завершить только вторым текстовым вызовом. С закрытым текстовым
    путём это означало бы «кнопка есть, а исполнить нечем» — поэтому обе
    половины (исполнитель + регистрация) едут вместе.

    `tasks` — содержимое проекта, прочитанное вызывающим (нужно для журнала
    и для счётчика в отчёте); функция сама в TickTick за ним не ходит, чтобы
    оба пути журналировали ровно то, что было показано человеку/поллеру."""
    live_name = project_name
    count = len(tasks)
    # Journal a pre-delete snapshot of the project AND every contained task
    # BEFORE the actual delete call, same convention as delete_tasks/
    # execute_task_deletion (snapshot first, mutate second).
    record_id = "delete_project-" + uuid.uuid4().hex[:8]
    snap_fields = ("title", "content", "desc", "dueDate", "startDate",
                   "priority", "tags", "parentId", "isAllDay")
    _journal_write({
        "ts": datetime.now(timezone.utc).isoformat(),
        "manifest": record_id, "op": "delete_project",
        "summary": f"Удаление проекта «{live_name}» ({count} задач)",
        "items": [{"taskId": project_id, "title": live_name}],
    })
    if tasks:
        _journal_write({
            "ts": datetime.now(timezone.utc).isoformat(),
            "manifest": record_id,
            "summary": f"Задачи проекта «{live_name}», удаляемые каскадом",
            "deleted": [{**{k: t.get(k) for k in snap_fields
                            if t.get(k) is not None},
                        "taskId": t.get("id"), "title": t.get("title")}
                       for t in tasks],
        })

    try:
        result = await _run_blocking(lambda: ticktick.delete_project(project_id))
        if 'error' in result:
            return (f"❌ TickTick отклонил удаление проекта «{live_name}»: "
                    f"{_redact_for_user(result['error'])}\n{_report_line(record_id)}")

        # Post-verify against FRESH state: the project must no longer resolve.
        if ticktick_v2:
            try:
                ticktick_v2.invalidate_cache()
            except Exception:
                pass
        names_after = _v2_project_names_or_none()
        lines = []
        if names_after is None:
            # Fetch failed outright — a failed refetch must never read as a
            # confirmed deletion (the TickTick call above may have genuinely
            # succeeded; we just couldn't check).
            lines.append(f"⚠️ Проект «{live_name}» отправлен на удаление, но "
                         "проверить результат не удалось (не получилось "
                         "перечитать список проектов) — исход НЕ ПОДТВЕРЖДЁН. "
                         "Повтори operation_report позже.")
        elif names_after.get(project_id):
            lines.append(f"❌ Проект «{live_name}» ВСЁ ЕЩЁ существует — "
                         "удаление не подтвердилось.")
        else:
            lines.append(f"🗑 Проект «{live_name}» удалён вместе с {count} "
                         "задачами.")
        lines.append(_report_line(record_id))
        return "\n".join(lines)
    except Exception as e:
        logger.exception("Error in delete_project")
        return _tool_error("deleting project", e)
    

### Improved Task MCP Tools

# Helper Functions

# User's local timezone. Date comparisons for "today"/"overdue"/"due in N days"
# happen in this zone, not UTC, so an all-day task stored at local-midnight
# isn't off-by-one. Matches USER_TIMEZONE used by the client's date handling.
_USER_TZ = ZoneInfo(os.getenv("USER_TIMEZONE", "UTC"))

# A bare calendar date (no clock time) — an all-day marker on either side.
_DATE_ONLY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _is_all_day_task(task: Dict[str, Any]) -> bool:
    """An all-day / date-only deadline is a ZONE-INDEPENDENT calendar date, not
    a timezone-bearing instant. Detect it from the explicit isAllDay flag or a
    bare YYYY-MM-DD dueDate so its date is read verbatim, never .astimezone()'d
    (which would push a negative-offset zone back to the previous day, #36)."""
    if task.get("isAllDay"):
        return True
    due = task.get("dueDate")
    return bool(due) and isinstance(due, str) and _DATE_ONLY.match(due.strip()) is not None


def _all_day_date(value: str) -> Optional[date]:
    """Take the bare calendar date from an all-day dueDate VERBATIM (dueDate[:10]),
    with no timezone assumption and no conversion."""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _parse_ticktick_datetime(value: str) -> Optional[datetime]:
    """Parse a TickTick date string robustly. TickTick usually emits
    '%Y-%m-%dT%H:%M:%S.%f%z' but not always (missing millis, 'Z' suffix,
    date-only). Try several formats plus fromisoformat; return an
    aware datetime (assume UTC if no tz), or None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    # datetime.fromisoformat handles most ISO variants; normalize a trailing Z.
    candidates = [raw]
    if raw.endswith("Z"):
        candidates.append(raw[:-1] + "+00:00")
    for c in candidates:
        try:
            dt = datetime.fromisoformat(c)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _task_due_local_date(task: Dict[str, Any]):
    """Return the task's due date as a calendar date, or None if there's
    no/unparseable due date.

    All-day / date-only deadlines are zone-independent calendar dates: take
    dueDate[:10] verbatim, never assume UTC and never .astimezone() them (that
    is the #36 off-by-one — a negative-offset zone would read the previous day).
    Only genuinely timed deadlines are converted into the user's local zone."""
    due = task.get('dueDate')
    if _is_all_day_task(task):
        return _all_day_date(due)
    dt = _parse_ticktick_datetime(due)
    if dt is None:
        return None
    return dt.astimezone(_USER_TZ).date()


# ---- date RENDERING (what a human reads) --------------------------------
# The filters above already think in _USER_TZ; the formatters used to print the
# raw stored string instead, so the two disagreed by a whole calendar day for
# any deadline near local midnight (23:59 America/Los_Angeles == 06:59 UTC the
# NEXT day). That produced output that contradicted itself — an "overdue" list
# whose first line showed today's date. Rendering goes through these two
# helpers so display and classification can never drift apart again.
# Used by format_task_line() / format_task(), defined far above: Python resolves
# them at call time, so the ordering is fine.

def _is_all_day_value(task: Dict[str, Any], value: Any) -> bool:
    """All-day for THIS field: the task-wide isAllDay flag, or a bare
    YYYY-MM-DD (a zone-independent calendar date — never .astimezone() it,
    that is the #36 off-by-one)."""
    if task.get("isAllDay"):
        return True
    return isinstance(value, str) and _DATE_ONLY.match(value.strip()) is not None


def _local_calendar_date(task: Dict[str, Any], field: str = "dueDate") -> Optional[date]:
    """Calendar date of `field` (dueDate/startDate) in the OWNER's zone.
    Same rules as _task_due_local_date, but for any date field."""
    value = task.get(field)
    if not value:
        return None
    if _is_all_day_value(task, value):
        return _all_day_date(str(value))
    dt = _parse_ticktick_datetime(str(value))
    return dt.astimezone(_USER_TZ).date() if dt else None


def _local_date_str(task: Dict[str, Any], field: str = "dueDate") -> str:
    """Compact 'YYYY-MM-DD' in the owner's zone for one-line listings. An
    unparseable value falls back to the raw text rather than vanishing."""
    d = _local_calendar_date(task, field)
    return d.isoformat() if d else str(task.get(field))[:10]


def _local_datetime_str(task: Dict[str, Any], field: str = "dueDate") -> str:
    """Full rendering for detail views: local day + clock time + the zone it is
    stated in, so the reader never has to convert an offset in their head.
    All-day values keep their bare date (no fake 00:00) and say so."""
    raw = task.get(field)
    if _is_all_day_value(task, raw):
        d = _all_day_date(str(raw))
        return f"{d.isoformat()} (all-day)" if d else f"{raw} (unparsed)"
    dt = _parse_ticktick_datetime(str(raw))
    if dt is None:
        return f"{raw} (unparsed)"
    return f"{dt.astimezone(_USER_TZ).strftime('%Y-%m-%d %H:%M')} ({_USER_TZ.key})"


def _local_stamp_str(value: Any, with_zone: bool = True,
                     seconds: bool = False) -> str:
    """Render an EVENT INSTANT — createdTime / modifiedTime / completedTime,
    an activity-log `when` — in the owner's zone.

    Deliberately NOT _local_datetime_str: that one asks _is_all_day_value()
    first, and an all-day TASK carries isAllDay=True for the whole record, so
    reusing it here would declare the task's CREATION TIME a zone-independent
    calendar date and re-introduce the very off-by-one this fixes. These
    stamps are always real moments; there is no all-day case for them.

    with_zone=False drops the trailing "(America/Los_Angeles)" for callers
    that state the zone once in a header instead of on every one of N lines
    (the activity log). `seconds` keeps an event log's second-level precision,
    which the old `[:19]` slice did carry.
    """
    if not value:
        return "?"
    dt = _parse_ticktick_datetime(str(value))
    if dt is None:
        return f"{value} (unparsed)"
    fmt = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    out = dt.astimezone(_USER_TZ).strftime(fmt)
    return f"{out} ({_USER_TZ.key})" if with_zone else out


def _today_local():
    return datetime.now(_USER_TZ).date()


# A long-running chat can drift a day behind the wall clock (stale date
# context) — the caller then computes its own "today" wrong and silently
# writes yesterday's date as "today" for the rest of that conversation. Every
# task actually gets that literal wrong date; nothing here can detect it after
# the fact. So relative day-words are resolved SERVER-SIDE, off the real clock
# + USER_TIMEZONE, instead of trusting the caller's own arithmetic — the class
# of bug this closes, not just a one-off case.
_RELATIVE_DATE_WORDS = {
    "today": 0, "сегодня": 0,
    "tomorrow": 1, "завтра": 1,
    "yesterday": -1, "вчера": -1,
}

# Bare weekday name ("Monday", "понедельник", "пн") -> the NEAREST occurrence
# from today (today itself counts, if today already is that weekday) —
# resolved off the real clock, same as the today/tomorrow/yesterday words
# above. A weekday name is just as easy for a caller with stale date context
# to land one day off on (reported case: asked for Monday, got Sunday — a
# plain -1-day drift wearing a different name).
_WEEKDAY_NAMES = {
    "monday": 0, "понедельник": 0, "пн": 0,
    "tuesday": 1, "вторник": 1, "вт": 1,
    "wednesday": 2, "среда": 2, "ср": 2,
    "thursday": 3, "четверг": 3, "чт": 3,
    "friday": 4, "пятница": 4, "пт": 4,
    "saturday": 5, "суббота": 5, "сб": 5,
    "sunday": 6, "воскресенье": 6, "вс": 6,
}


def _resolve_relative_date(value):
    """'today'/'tomorrow'/'yesterday' or a bare weekday name (RU too,
    case-insensitive) -> real 'YYYY-MM-DD' from THIS server's clock.
    Anything else (an already-literal date, a full ISO timestamp, None)
    passes through unchanged — idempotent, safe to call on values that were
    already resolved."""
    if not isinstance(value, str):
        return value
    key = value.strip().lower()
    offset = _RELATIVE_DATE_WORDS.get(key)
    if offset is not None:
        return (_today_local() + timedelta(days=offset)).isoformat()
    weekday = _WEEKDAY_NAMES.get(key)
    if weekday is not None:
        today = _today_local()
        delta = (weekday - today.weekday()) % 7
        return (today + timedelta(days=delta)).isoformat()
    return value


def _resolve_dates_in_task_tree(node: Dict[str, Any]) -> None:
    """Mutates due_date/start_date in place on `node` and recursively through
    node['subtasks'] (root + nested creation tree)."""
    if not isinstance(node, dict):
        return
    for key in ("due_date", "start_date"):
        if key in node:
            node[key] = _resolve_relative_date(node[key])
    for child in (node.get("subtasks") or []):
        if isinstance(child, dict):
            _resolve_dates_in_task_tree(child)


def _is_task_due_today(task: Dict[str, Any]) -> bool:
    """Check if a task is due today (in the user's local timezone)."""
    d = _task_due_local_date(task)
    return d is not None and d == _today_local()

def _is_task_overdue(task: Dict[str, Any]) -> bool:
    """Check if a task is overdue.

    For an all-day / date-only deadline "overdue" is a calendar-date compare in
    the user's local zone (its date is before today) — NOT a UTC-instant compare,
    which would read an all-day task due today as overdue for most of the day."""
    if _is_all_day_task(task):
        d = _all_day_date(task.get('dueDate'))
        return d is not None and d < _today_local()
    dt = _parse_ticktick_datetime(task.get('dueDate'))
    if dt is None:
        return False
    return dt < datetime.now(timezone.utc)

def _is_task_due_in_days(task: Dict[str, Any], days: int) -> bool:
    """Check if a task is due in exactly X days (in the user's local timezone)."""
    d = _task_due_local_date(task)
    return d is not None and d == _today_local() + timedelta(days=days)

def _task_matches_search(task: Dict[str, Any], search_term: str) -> bool:
    """Check if a task matches the search term (case-insensitive).

    Every field is read as `x or ''`, not `.get(k, '')`: TickTick sends an
    explicit `null` for an empty title/content (and `null` for `items`), and
    a default only fires when the KEY IS ABSENT. `None.lower()` then raised,
    and the tool's own `except` turned that into «Error searching tasks:
    'NoneType' object has no attribute 'lower'» — ONE malformed task made the
    whole search unusable instead of just not matching."""
    search_term = search_term.lower()

    # Search in title
    title = (task.get('title') or '').lower()
    if search_term in title:
        return True

    # Search in content
    content = (task.get('content') or '').lower()
    if search_term in content:
        return True

    # Search in subtasks
    items = task.get('items') or []
    for item in items:
        item_title = ((item or {}).get('title') or '').lower()
        if search_term in item_title:
            return True

    return False

def _get_project_tasks_by_filter(filter_func, filter_name: str,
                                 limit: int = 200, offset: int = 0) -> str:
    """
    Helper function to filter tasks across all projects.

    Args:
        filter_func: Function that takes a task and returns True if it matches the filter
        filter_name: Name of the filter for output formatting
        limit: Page size on the v2 path (default 200 — the historical cut-off)
        offset: How many matches to skip, so the tail past `limit` is reachable
            at all instead of being permanently cut off (def-D4)

    Returns:
        Formatted string of filtered tasks

    Fetches projects only on the official-API fallback path; when v2 is
    available no per-project HTTP calls are made at all.
    """
    # Prefer the v2 open-task pool: it includes the Inbox (which the official
    # API leaves out of the project list) and is a single call instead of one
    # request per project. Falls back to official iteration when v2 is off.
    if ticktick_v2:
        try:
            state = ticktick_v2.get_state()
            tasks = state.get("syncTaskBean", {}).get("update", []) or []
            matched = [t for t in tasks if filter_func(t)]
            if not matched:
                return f"No tasks found that are '{filter_name}'."
            total = len(matched)
            offset = max(0, offset)
            limit = max(1, limit)
            page = matched[offset:offset + limit]
            if not page:
                # def-D4: пустая страница за концом списка — это НЕ «задач нет».
                return (f"Tasks that are '{filter_name}': {total} total, but offset={offset} "
                        f"is past the end (last page starts at offset="
                        f"{_last_page_offset(total, limit)}).")
            shown_to = offset + len(page)
            if offset or shown_to < total:
                out = (f"Tasks that are '{filter_name}' ({total} total; "
                       f"showing {offset + 1}-{shown_to}):\n")
            else:
                out = f"Tasks that are '{filter_name}' ({total}):\n"
            body = out + format_task_tree(page, limit)
            if shown_to < total:
                # Раньше здесь была голая пометка «... and N more.» из
                # format_task_tree — она говорила, что список неполон, но не
                # давала способа его дочитать: параметров у инструмента не было.
                body += f"\n... and {total - shown_to} more — call again with offset={shown_to}."
            return body
        except Exception as e:
            logger.warning(f"v2 task pool failed, falling back to official API: {e}")

    # Official-API fallback: fetch the project list only now that we need it.
    projects = ticktick.get_projects()
    if 'error' in projects:
        return _tool_error("fetching projects", projects['error'])
    if not projects:
        return "No projects found."

    result = f"Found {len(projects)} projects:\n\n"
    # Folder map resolved once for the whole listing, not per project.
    group_names = _v2_group_names()

    for i, project in enumerate(projects, 1):
        if project.get('closed'):
            continue

        project_id = project.get('id', 'No ID')
        project_data = ticktick.get_project_with_data(project_id)
        tasks = project_data.get('tasks', [])

        if not tasks:
            result += f"Project {i}:\n{format_project(project, group_names)}"
            result += f"With 0 tasks that are to be '{filter_name}' in this project :\n\n\n"
            continue

        # Filter tasks using the provided function
        filtered_tasks = [(t, task) for t, task in enumerate(tasks, 1) if filter_func(task)]

        result += f"Project {i}:\n{format_project(project, group_names)}"
        result += f"With {len(filtered_tasks)} tasks that are to be '{filter_name}' in this project :\n"
        
        for t, task in filtered_tasks:
            result += f"Task {t}:\n{format_task(task)}\n"
        
        result += "\n\n"
    
    return result

# New MCP Tools for Tasks

@mcp.tool(annotations=READONLY)
async def get_all_tasks(limit: int = _ALL_TASKS_PAGE, offset: int = 0) -> str:
    """
    Get ALL open tasks across every project and the Inbox in one fast call.

    Preferred over get_project_tasks when you need a full picture — this uses
    the v2 sync state (single request, includes Inbox) when available, falling
    back to the official API otherwise.

    The output is paged: the header always states the TOTAL number of tasks,
    and when they don't fit, the last line says how many top-level tasks are
    left and which offset continues the list. Subtasks always travel with their
    parent, so no page ever shows a subtask torn off the task it belongs to.

    Args:
        limit: Maximum tasks per call (default 200, same page size as
            get_tasks_by_priority) — a soft ceiling: the subtree that crosses
            it is finished rather than cut in half
        offset: Skip this many TOP-LEVEL tasks — use it to read the tail; the
            footer prints the exact offset that continues the list
    """
    err = _ensure_official()
    if err:
        return err

    try:
        if ticktick_v2:
            tasks = await _run_blocking(lambda: ticktick_v2.get_open_tasks())
            if not tasks:
                return "No tasks found."
            # def-E1: раньше печатались ВСЕ задачи — на боевом аккаунте
            # владельца это 164 871 символ на 1481 задачу, то есть ответ не
            # влезал в лимит и инструмент возвращал ошибку вместо данных.
            # Срез считается по КОРНЯМ и уезжает вместе с поддеревьями, чтобы
            # подзадача не осиротела на границе страницы (_page_task_forest).
            page, total_roots, offset, shown_to = _page_task_forest(tasks, limit, offset)
            if not page:
                # Пустая страница за концом списка — это НЕ «задач нет».
                # Страницы тут плавающие (поддерево дописывается целиком), так
                # что называть «начало последней страницы» нельзя — врать про
                # неё хуже, чем назвать диапазон допустимых offset.
                return (f"All open tasks: {total_roots} top-level tasks total, but "
                        f"offset={offset} is past the end "
                        f"({_valid_offset_range(total_roots)}).")
            names = _v2_project_names()
            by_project: Dict[str, list] = {}
            for t in page:
                pid = t.get("projectId", "")
                by_project.setdefault(pid, []).append(t)
            if offset or shown_to < total_roots:
                out = (f"All open tasks ({len(tasks)} total, {total_roots} top-level; "
                       f"showing top-level {offset + 1}-{shown_to} with their "
                       f"subtasks):\n\n")
            else:
                out = f"All open tasks ({len(tasks)}):\n\n"
            for pid, ptasks in by_project.items():
                pname = names.get(pid, pid or "Inbox")
                # The WHOLE per-project list goes in, subtasks included:
                # format_task_tree builds the hierarchy itself (children nested
                # under their parent, orphans promoted to top level). Handing it
                # only parent-less tasks — as this did until 2026-08-07 — dropped
                # every subtask from the output while the header kept counting
                # them, so the tool under-reported by hundreds of tasks (1477
                # promised vs 1234 printed) with no marker that anything was
                # missing. Never pre-filter on parentId here.
                out += f"── {pname} ({len(ptasks)} tasks) ──\n"
                out += format_task_tree(ptasks, 500)
                out += "\n"
            if shown_to < total_roots:
                out += (f"... and {total_roots - shown_to} more top-level tasks — "
                        f"call again with offset={shown_to}.\n")
            return out

        # Fallback: official API per project (projects fetched inside helper)
        out = _get_project_tasks_by_filter(lambda t: True, "included")
        # This path pages by its own rules inside the helper; pretending the
        # caller's limit/offset were honoured here would be a silent lie.
        if limit != _ALL_TASKS_PAGE or offset:
            out += ("\n\n⚠️ limit/offset not applied: the v2 state is unavailable, so "
                    "this listing came from the official API with its own paging.")
        return out

    except Exception as e:
        logger.exception("Error in get_all_tasks")
        return _tool_error("fetching tasks", e)

@mcp.tool(annotations=READONLY)
async def get_tasks_by_priority(priority_id: int, limit: int = 200, offset: int = 0) -> str:
    """
    Get all tasks from TickTick by priority. Ignores closed projects.

    The output is paged: the header always states the TOTAL number of matches,
    and when it doesn't fit, the last line says how many are left and which
    offset continues the list.

    Args:
        priority_id: Priority of tasks to retrieve {0: "None", 1: "Low", 3: "Medium", 5: "High"}
        limit: Maximum tasks to show in one call (default 200)
        offset: Skip this many matches — use it to read the tail past `limit`
    """
    err = _ensure_official()
    if err:
        return err

    if priority_id not in PRIORITY_MAP:
        return f"Invalid priority_id. Valid values: {list(PRIORITY_MAP.keys())}"

    try:
        def priority_filter(task: Dict[str, Any]) -> bool:
            return task.get('priority', 0) == priority_id

        priority_name = f"{PRIORITY_MAP[priority_id]} ({priority_id})"
        return _get_project_tasks_by_filter(priority_filter, f"priority '{priority_name}'",
                                            limit=limit, offset=offset)

    except Exception as e:
        logger.exception("Error in get_tasks_by_priority")
        return _tool_error("fetching tasks by priority", e)

@mcp.tool(annotations=READONLY)
async def get_tasks_due_today() -> str:
    """Get all tasks from TickTick that are due today. Ignores closed projects."""
    err = _ensure_official()
    if err:
        return err
    
    try:
        def today_filter(task: Dict[str, Any]) -> bool:
            return _is_task_due_today(task)

        return _get_project_tasks_by_filter(today_filter, "due today")

    except Exception as e:
        logger.exception("Error in get_tasks_due_today")
        return _tool_error("fetching tasks due today", e)

@mcp.tool(annotations=READONLY)
async def get_overdue_tasks() -> str:
    """Get all overdue tasks from TickTick. Ignores closed projects."""
    err = _ensure_official()
    if err:
        return err
    
    try:
        def overdue_filter(task: Dict[str, Any]) -> bool:
            return _is_task_overdue(task)

        return _get_project_tasks_by_filter(overdue_filter, "overdue")

    except Exception as e:
        logger.exception("Error in get_overdue_tasks")
        return _tool_error("fetching overdue tasks", e)

@mcp.tool(annotations=READONLY)
async def get_tasks_due_tomorrow() -> str:
    """Get all tasks from TickTick that are due tomorrow. Ignores closed projects."""
    err = _ensure_official()
    if err:
        return err

    try:
        def tomorrow_filter(task: Dict[str, Any]) -> bool:
            return _is_task_due_in_days(task, 1)

        return _get_project_tasks_by_filter(tomorrow_filter, "due tomorrow")

    except Exception as e:
        logger.exception("Error in get_tasks_due_tomorrow")
        return _tool_error("fetching tasks due tomorrow", e)
    
@mcp.tool(annotations=READONLY)
async def get_tasks_due_in_days(days: int) -> str:
    """
    Get all tasks from TickTick that are due in exactly X days. Ignores closed projects.
    
    Args:
        days: Number of days from today (0 = today, 1 = tomorrow, etc.)
    """
    err = _ensure_official()
    if err:
        return err
    
    if days < 0:
        return "Days must be a non-negative integer."
    
    try:
        def days_filter(task: Dict[str, Any]) -> bool:
            return _is_task_due_in_days(task, days)

        day_description = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
        return _get_project_tasks_by_filter(days_filter, f"due {day_description}")

    except Exception as e:
        logger.exception("Error in get_tasks_due_in_days")
        return _tool_error("fetching tasks due in days", e)

@mcp.tool(annotations=READONLY)
async def get_tasks_due_this_week() -> str:
    """Get all tasks from TickTick due from today through 7 days from now
    (today and the following 7 calendar days, 8 days inclusive — not a strict
    "next 7 days" window). Ignores closed projects."""
    err = _ensure_official()
    if err:
        return err
    
    try:
        def week_filter(task: Dict[str, Any]) -> bool:
            d = _task_due_local_date(task)
            if d is None:
                return False
            today = _today_local()
            return today <= d <= today + timedelta(days=7)

        return _get_project_tasks_by_filter(week_filter, "due this week")

    except Exception as e:
        logger.exception("Error in get_tasks_due_this_week")
        return _tool_error("fetching tasks due this week", e)

@mcp.tool(annotations=READONLY)
async def search_tasks(search_term: str) -> str:
    """
    Search for tasks in TickTick by title, content, or subtask titles. Ignores closed projects.
    
    Args:
        search_term: Text to search for (case-insensitive)
    """
    err = _ensure_official()
    if err:
        return err
    
    if not search_term.strip():
        return "Search term cannot be empty."

    try:
        # Prefer the v2 open-task pool: it includes the Inbox (which the
        # official API omits from the project list) and is one fast call.
        if ticktick_v2:
            open_tasks = await _run_blocking(ticktick_v2.get_open_tasks)
            tasks = [t for t in open_tasks
                     if _task_matches_search(t, search_term)]
            if not tasks:
                return f"No tasks found matching '{search_term}'."
            return (f"Tasks matching '{search_term}' ({len(tasks)}):\n"
                    + format_task_tree(tasks, 100))

        # Fallback (no v2): iterate official projects — note this misses the Inbox.
        def search_filter(task: Dict[str, Any]) -> bool:
            return _task_matches_search(task, search_term)

        return _get_project_tasks_by_filter(search_filter, f"matching '{search_term}'")

    except Exception as e:
        logger.exception("Error in search_tasks")
        return _tool_error("searching tasks", e)

@mcp.tool(annotations=READONLY)
async def get_recurring_tasks(search_term: str = "") -> str:
    """
    Get all tasks that have a recurrence rule (repeatFlag set), i.e. repeating tasks.
    Optionally filter by title/content search term.

    Do NOT call this in a loop — it already scans all open tasks at once.

    Args:
        search_term: Optional text to further filter by title/content/subtask
                     titles (case-insensitive). Leave empty to return all
                     recurring tasks.
    """
    err = _ensure_official()
    if err:
        return err

    try:
        if ticktick_v2:
            all_open = await _run_blocking(lambda: ticktick_v2.get_open_tasks())
        else:
            projects = await _run_blocking(lambda: ticktick.get_projects())
            if 'error' in projects:
                return _tool_error("fetching projects", projects['error'])
            all_open = []
            for p in projects:
                pid = p.get("id")
                data = await _run_blocking(lambda: ticktick.get_project_with_data(pid))
                all_open.extend(data.get("tasks", []))

        tasks = [t for t in all_open if t.get("repeatFlag")]
        if search_term.strip():
            tasks = [t for t in tasks if _task_matches_search(t, search_term.strip())]

        if not tasks:
            msg = (f"No recurring tasks found matching '{search_term}'." if search_term
                   else "No recurring tasks found.")
            return msg

        label = (f"Recurring tasks matching '{search_term}' ({len(tasks)}):" if search_term
                 else f"Recurring tasks ({len(tasks)}):")
        return label + "\n" + format_task_tree(tasks, 200)

    except Exception as e:
        logger.exception("Error in get_recurring_tasks")
        return _tool_error("fetching recurring tasks", e)

# New MCP Tools for Getting things done framework (Priority / Due Dates)

@mcp.tool(annotations=READONLY)
async def get_engaged_tasks() -> str:
    """
    Get all tasks from TickTick that are 'engaged'.
    This includes tasks marked as high priority (5), due today or overdue.
    """
    err = _ensure_official()
    if err:
        return err
    
    try:
        def engaged_filter(task: Dict[str, Any]) -> bool:
            is_high_priority = task.get('priority', 0) == 5
            is_overdue = _is_task_overdue(task)
            is_today = _is_task_due_today(task)
            return is_high_priority or is_overdue or is_today

        return _get_project_tasks_by_filter(engaged_filter, "engaged")

    except Exception as e:
        logger.exception("Error in get_engaged_tasks")
        return _tool_error("fetching engaged tasks", e)

@mcp.tool(annotations=READONLY)
async def get_next_tasks() -> str:
    """
    Get all tasks from TickTick that are "Next".
    This includes tasks marked as medium priority (3) or due tomorrow.
    """
    err = _ensure_official()
    if err:
        return err
    
    try:
        def next_filter(task: Dict[str, Any]) -> bool:
            is_medium_priority = task.get('priority', 0) == 3
            is_due_tomorrow = _is_task_due_in_days(task, 1)
            return is_medium_priority or is_due_tomorrow

        return _get_project_tasks_by_filter(next_filter, "next")

    except Exception as e:
        logger.exception("Error in get_next_tasks")
        return _tool_error("fetching next tasks", e)

def _describe_create_subtask(p: Dict) -> str:
    return (f'Создаю подзадачу «{p.get("subtask_title")}» под «'
            f'{p.get("parent_task_title")}»')


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def create_subtask(
    parent_task_title: str,
    subtask_title: str,
    parent_task_id: str,
    project_id: str,
    content: str = None,
    priority: int = 0,
    manifest_id: str = "",
    user_reply: str = "",
    automation_key: str = "",
) -> str:
    """
    Create a subtask for a parent task within the same project. Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    created on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is created yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        parent_task_title: Title of the parent task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        subtask_title: Title of the new subtask
        parent_task_id: ID of the parent task
        project_id: ID of the project (must be same for both parent and subtask)
        content: Optional content/description for the subtask
        priority: Priority level (0: None, 1: Low, 3: Medium, 5: High) (optional)
        manifest_id: from call #1's response — pass on call #2 to actually create
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    parent_task_id and parent_task_title are cross-checked against the LIVE
    task list TWICE (same pattern as delete_habit, def-116, 2026-08-07): once
    while BUILDING the plan (call #1, before anything is shown to the owner —
    a mismatched or gone parent never reaches the plan card at all) and again,
    independently, right before the actual create (call #2, unchanged). If the
    live read itself fails while building the plan, the plan still gets built
    (a read hiccup must not block every subtask creation), but its text says
    so honestly — the call #2 check is unconditional and still guards the
    mutation either way.
    """
    err = _ensure_official()
    if err:
        return err

    # Validate priority up front (cheap, applies to both calls).
    if priority not in [0, 1, 3, 5]:
        return "Invalid priority. Must be 0 (None), 1 (Low), 3 (Medium), or 5 (High)."

    # Перенос identity-guard (parent_task_id↔parent_task_title) на построение
    # плана — тот же _guard_task, что уже стоит в _create_subtask_impl НА
    # ИСПОЛНЕНИИ, но здесь — ДО показа карточки владельцу (тот же перенос,
    # что в delete_habit, def-116, и в группе A: attach_file_to_task/
    # update_task_comment/delete_task_comment). mismatch И missing здесь
    # блокируют план целиком — _create_subtask_impl уже трактует ОБА как 🛑
    # на исполнении (не только mismatch), перенос это не ужесточает. Про
    # класс операций, законных над ЗАВЕРШЁННОЙ задачей (комментарии,
    # вложения, duplicate_task), см. _guard_task_incl_completed — подзадача
    # у ЗАВЕРШЁННОГО родителя смысла не имеет, поэтому здесь по-прежнему
    # обычный _guard_task «только открытые». Временная недоступность
    # живого чтения — fail-open с предупреждением в тексте плана, а
    # исполнение (не тронуто) перепроверит заново и остаётся последней
    # линией защиты. Действует только на call #1 (manifest_id пуст):
    # call #2 обслуживает СОХРАНЁННЫЕ параметры плана, а не свежие аргументы
    # вызова, и identity guard на исполнении там уже стоит. automation_key
    # НЕ пропускает эту проверку — она стоит раньше самого гейта, поэтому
    # headless-путь (карточки с кнопками не видит вовсе) тоже защищён.
    name_warning = ""
    if not manifest_id:
        g = _guard_task(parent_task_id, parent_task_title or "", project_id)
        refusal, warn = _guard_or_refuse(
            g, stage="план", expected=parent_task_title, parent=True,
            unavailable_note=_UNVERIFIED_PARENT_TITLE)
        if refusal:
            return refusal
        name_warning = f" {warn}" if warn else ""

    params = {"parent_task_title": parent_task_title, "subtask_title": subtask_title,
              "parent_task_id": parent_task_id, "project_id": project_id,
              "content": content, "priority": priority}
    describe_fn = ((lambda p: _describe_create_subtask(p) + name_warning)
                   if name_warning else _describe_create_subtask)
    outcome = await _gate_single("create_subtask", "create_subtask",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _create_subtask_impl(**outcome.extra)


async def _create_subtask_impl(parent_task_title: str, subtask_title: str,
                               parent_task_id: str, project_id: str,
                               content: str = None, priority: int = 0) -> str:
    """Pure mutation logic for create_subtask — no consent gate. Called only
    by the gated create_subtask() above once the plan is approved."""
    # Fetch live state ONCE — reused for the identity guard AND the depth
    # check below (both must see the same snapshot).
    by_id = _open_by_id(fresh=True)
    if by_id is None:
        return _STATE_UNAVAILABLE_MSG
    # Identity guard on the PARENT: a stale parent_task_id would attach the new
    # subtask under a different task (or a dead one) while reporting success.
    g = _guard_task(parent_task_id, parent_task_title or "", project_id, by_id=by_id)
    refusal, _warn = _guard_or_refuse(
        g, stage="исполнение", verb="НЕ создал подзадачу",
        expected=parent_task_title, parent=True)
    if refusal:
        return refusal
    # Depth guard: TickTick supports at most MAX_TASK_NEST_LEVELS total levels
    # counting from the root task — see MAX_TASK_NEST_LEVELS above for the
    # source. Computed from the LIVE parentId chain, not from anything the
    # caller claims.
    parent_level = _task_level(parent_task_id, by_id)
    new_level = parent_level + 1
    if new_level > MAX_TASK_NEST_LEVELS:
        return (f"🛑 НЕ создал подзадачу — «{g.title or parent_task_title}» "
                f"уже на уровне {parent_level} из {MAX_TASK_NEST_LEVELS} "
                "(считая от корневой задачи), новая подзадача оказалась бы "
                f"на уровне {new_level}. TickTick не поддерживает вложенность "
                f"глубже {MAX_TASK_NEST_LEVELS} уровней. Ничего не тронул.")
    # The subtask must live in the parent's REAL project.
    project_id = g.project_id or project_id
    try:
        subtask = await _run_blocking(
            ticktick.create_subtask,
            subtask_title=subtask_title,
            parent_task_id=parent_task_id,
            project_id=project_id,
            content=content,
            priority=priority
        )

        if 'error' in subtask:
            return _tool_error("creating subtask", subtask['error'])

        # Post-verify: the created task must exist AND point at the parent.
        sid = subtask.get("id")
        rid = _op_journal("parent", [{"taskId": sid, "title": subtask_title,
                                      "expect": {"parentId": parent_task_id}}],
                          f"Подзадача «{subtask_title}» под «{g.title or parent_task_title}»")
        fresh = _open_by_id(fresh=True)
        if fresh is None:
            verdict = f"⚠️ Создание отправлено, но {_UNVERIFIED_MSG}"
        else:
            live = fresh.get(sid) or {}
            if not live:
                verdict = ("❌ Создание НЕ подтвердилось — задачи нет среди "
                           "открытых, проверь вручную.")
            elif live.get("parentId") != parent_task_id:
                verdict = ("⚠️ Задача создана, но НЕ привязана к родителю "
                           f"(parentId={live.get('parentId')!r}).")
            else:
                verdict = (f"✅ Подзадача «{subtask_title}» создана под "
                           f"«{g.title or parent_task_title}» (проверено).")
        return (verdict + "\n\n" + format_task(subtask) + "\n" + _report_line(rid))
    except Exception as e:
        logger.exception("Error in create_subtask")
        return _tool_error("creating subtask", e)

# ---------------------------------------------------------------------------
# v2 API tools (unofficial). Available when TICKTICK_V2_TOKEN (the `t` cookie
# from a logged-in ticktick.com browser session) is configured. They cover
# what the official Open API cannot do.
# ---------------------------------------------------------------------------

_V2_DISABLED_MSG = (
    "The unofficial v2 API is not enabled (or its session token expired). "
    "Set TICKTICK_V2_TOKEN to the `t` cookie from a logged-in ticktick.com "
    "browser session to use tags, completed tasks, the Inbox, and moving "
    "tasks between lists."
)


@mcp.tool(annotations=READONLY)
async def get_completed_tasks(limit: int = 100) -> str:
    """
    Get recently completed tasks across all lists (requires v2 API).

    Asking for more than the feed can serve is answered honestly: the reply
    says the feed's own per-call ceiling was hit, instead of passing off
    "the newest 100" as "all of them".

    Args:
        limit: Maximum number of completed tasks to return (default 100 —
            the API's own hard cap, so there's no reason to default lower)
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        tasks = await _run_blocking(lambda: ticktick_v2.get_completed_tasks(limit=limit))
        if not tasks:
            return "No completed tasks found."
        out = f"Completed tasks ({len(tasks)}):\n\n"
        # limit=len(tasks), NOT format_task_list's default 100: the client has
        # already applied the caller's limit (and the feed's ceiling) when
        # asking TickTick, and a second, hidden cut here is the get_trash
        # defect of 2026-08-07. It happens to be harmless today only because
        # COMPLETED_MAX_LIMIT is also 100 — correctness must not rest on two
        # unrelated constants in two files staying equal.
        out += format_task_list(tasks, limit=len(tasks))
        if limit > COMPLETED_MAX_LIMIT and len(tasks) >= COMPLETED_MAX_LIMIT:
            # The caller asked for more than TickTick serves per call. Silence
            # here reads as "that's all there is"; say it's a ceiling.
            out += (f"\n(Asked for {limit}, but TickTick's completed feed caps "
                    f"at {COMPLETED_MAX_LIMIT} per call — these are the "
                    f"{COMPLETED_MAX_LIMIT} most recent, not everything.)")
        return out
    except Exception as e:
        logger.exception("Error in get_completed_tasks")
        return _tool_error("fetching completed tasks", e)


@mcp.tool(annotations=READONLY)
async def list_tags() -> str:
    """List all tags in the account (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        tags = await _run_blocking(lambda: ticktick_v2.get_tags())
        if not tags:
            return "No tags found."
        lines = [f"- {t.get('label', t.get('name', '?'))}" for t in tags]
        return f"Tags ({len(tags)}):\n\n" + "\n".join(lines)
    except Exception as e:
        logger.exception("Error in list_tags")
        return _tool_error("fetching tags", e)


@mcp.tool(annotations=READONLY)
async def get_tasks_by_tag(tag: str) -> str:
    """
    Get open tasks that carry a given tag (requires v2 API).

    Args:
        tag: Tag label, with or without the leading '#'
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        tasks = await _run_blocking(lambda: ticktick_v2.get_tasks_by_tag(tag))
        if not tasks:
            return f"No open tasks found with tag '{tag}'."
        out = f"Tasks tagged '{tag}' ({len(tasks)}):\n\n"
        return out + format_task_tree(tasks)
    except Exception as e:
        logger.exception("Error in get_tasks_by_tag")
        return _tool_error("fetching tasks by tag", e)


@mcp.tool(annotations=READONLY)
async def get_inbox_tasks(limit: int = _TREE_PAGE, offset: int = 0) -> str:
    """Get open tasks in the Inbox (requires v2 API).

    The output is paged: the header always states the TOTAL number of Inbox
    tasks and which range is shown, and when they don't fit, the last line says
    how many top-level tasks are left and which offset continues the list.
    Subtasks always travel with their parent, so no page ever shows a subtask
    torn off the task it belongs to.

    Args:
        limit: Maximum tasks per call (default 200, same page size as
            get_all_tasks) — a soft ceiling: the subtree that crosses it is
            finished rather than cut in half
        offset: Skip this many TOP-LEVEL tasks — use it to read the tail; the
            footer prints the exact offset that continues the list
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        tasks = await _run_blocking(lambda: ticktick_v2.get_inbox_tasks())
        if not tasks:
            return "No open tasks in the Inbox."
        # def-E1 (2-я волна): раньше сюда уходил весь Inbox, а format_task_tree
        # резал его на своём потолке в 200 строк — на боевых 344 задачах 144 из
        # них были недостижимы этим инструментом вовсе (а get_project_tasks
        # отсылал за постраничным чтением Входящих именно сюда).
        page, total_roots, offset, shown_to = _page_task_forest(tasks, limit, offset)
        if not page:
            # Пустая страница за концом списка — это НЕ «Входящие пусты».
            return (f"Inbox: {len(tasks)} task(s) ({total_roots} top-level), but "
                    f"offset={offset} is past the end "
                    f"({_valid_offset_range(total_roots)}).")
        if offset or shown_to < total_roots:
            out = (f"Inbox tasks ({len(tasks)} total, {total_roots} top-level; "
                   f"showing top-level {offset + 1}-{shown_to} with their "
                   f"subtasks):\n\n")
        else:
            out = f"Inbox tasks ({len(tasks)}):\n\n"
        out += format_task_tree(page, max(len(page), 1))
        if shown_to < total_roots:
            out += (f"\n... and {total_roots - shown_to} more top-level tasks — "
                    f"call again with offset={shown_to}.\n")
        return out
    except Exception as e:
        logger.exception("Error in get_inbox_tasks")
        return _tool_error("fetching inbox tasks", e)


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def move_tasks(summary: str, tasks: List[Dict[str, str]] = None,
                     to_project_id: str = "", to_project_name: str = None,
                     manifest_id: str = "", user_reply: str = "",
                     automation_key: str = "") -> str:
    """
    Move one or more open tasks to a destination list in one call (requires
    v2 API). All tasks go to the same destination. Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest from `tasks` +
    the destination and returns a preview — nothing is moved yet.
    Call #2 (after the user actually replied): repeat the call with
    manifest_id=<id from call #1> and user_reply=<the user's literal last
    message> — `tasks`/`to_project_id`/`to_project_name` are all ignored on
    this call (the manifest's own stored values are used). Do NOT make call
    #2 in the same turn as call #1.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user, e.g. «Перемещаю задачу
    „Купить молоко" из „Inbox" в „Покупки"» or «Перемещаю 3 задачи в
    „Покупки"».

    Put the human title inside each task object so the dialog shows what
    moves: [{"title": "Buy milk", "taskId": "abc"}].

    {{AUTOMATION_KEY_NOTE}}

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title": "...", "taskId": "..."} objects — required
            on call #1, ignored on call #2
        to_project_id: Destination project/list ID for ALL tasks — required
            on call #1, ignored on call #2
        to_project_name: Destination list name (shown in the dialog)
        manifest_id: from call #1's response — pass on call #2 to actually move
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err
    # И задача, и СПИСОК НАЗНАЧЕНИЯ должны быть названы: «→ 6a21home»
    # человек сверить не может — а именно этим и решается, туда ли уедет
    # задача. Оба резолвятся один раз, до гейта (describe зовётся только на
    # call #1, поэтому при manifest_id не платим ничем).
    titles = _plan_task_titles(tasks) if not manifest_id else {}
    dest = (_plan_project_name(to_project_id, to_project_name)
            if not manifest_id else "")

    def _describe(t: Dict[str, Any]) -> str:
        return f"**{_plan_task_name(t, titles)}** → {dest}"

    # Сверка с живым состоянием на фазе ПЛАНА — см. `_plan_live_check`.
    notes = None
    if not manifest_id and tasks:
        _describe, notes, refusal = _plan_live_check(tasks, _describe)
        if refusal:
            return refusal
    outcome = await _gate_batch(
        "move", "move_tasks", tasks, summary, manifest_id, user_reply,
        _describe, notes=notes,
        extra={"to_project_id": to_project_id, "to_project_name": to_project_name},
        automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    ex = outcome.extra
    return await _move_tasks_impl(
        outcome.summary, outcome.tasks,
        ex.get("to_project_id") or to_project_id,
        ex.get("to_project_name") if ex.get("to_project_name") is not None else to_project_name)


async def _move_tasks_impl(summary: str, tasks: List[Dict[str, str]],
                           to_project_id: str, to_project_name: str = None) -> str:
    """Pure mutation logic for move_tasks — no consent gate. Called only
    by the public gated move_tasks() below."""
    err = _ensure_ready()
    if err:
        return err
    try:
        # Destination guard: the id must resolve to a LIVE project, and when
        # the caller also names it, the name must match — otherwise tasks land
        # in «Архив» while the success line claims «Работа».
        refusal = _guard_project_or_refuse(to_project_id, to_project_name or "",
                                           fresh=True, require_known=True)
        if refusal:
            return refusal
        # Render the destination from the LIVE map — never echo the caller.
        to_name = _v2_project_names().get(to_project_id, to_project_id)
        by_id = _open_by_id(fresh=True)
        if by_id is None:
            return _STATE_UNAVAILABLE_MSG
        found, mismatch, missing = _split_tasks_by_state(tasks, by_id=by_id)
        moved, failed = [], []
        unverified = False
        api_fail = {}
        if found:
            # batch_move_tasks_raw (not batch_move_tasks): each item's
            # fromProjectId is already CONFIRMED by the identity guard above
            # (including its official-API fallback for a task missing from
            # the v2 open-task snapshot — see _official_task_snapshot's
            # docstring). batch_move_tasks() would re-derive fromProjectId
            # by looking the task up in THAT SAME v2 snapshot again and
            # silently drop it if not found there — which is exactly how a
            # guard-approved move turned into a live no-op with no error at
            # all (2026-08-07 incident): found the task via the fallback,
            # called the OLD batch_move_tasks with just the id, which
            # couldn't find it in get_open_tasks() either and dropped it
            # from the request body before TickTick was ever asked.
            resp = await _run_blocking(lambda: ticktick_v2.batch_move_tasks_raw([
                {"taskId": f["taskId"], "fromProjectId": f["projectId"],
                 "toProjectId": to_project_id} for f in found]))
            api_fail = id2error_failures(resp, [f["taskId"] for f in found])
            # verify the tasks actually landed — retried (see
            # _reread_open_until) so an immediate re-read racing TickTick's
            # own v2 sync doesn't misreport a real move as a failure; items
            # TickTick itself already rejected (api_fail) are excluded from
            # the wait condition, so a genuine failure isn't delayed.
            fresh = await _run_blocking(_reread_open_until, lambda m: all(
                (m.get(f["taskId"]) or {}).get("projectId") == to_project_id
                for f in found if f["taskId"] not in api_fail))
            if fresh is None:
                unverified = True
            else:
                for f in found:
                    cur = fresh.get(f["taskId"])
                    ok = (cur and cur.get("projectId") == to_project_id
                          and f["taskId"] not in api_fail)
                    (moved if ok else failed).append(f["title"])
        lines = []
        if moved:
            lines.append(f"↪ Перемещено {len(moved)} → «{to_name}»: "
                         + ", ".join(f"«{t}»" for t in moved))
        if unverified:
            lines.append(f"Отправлено на перемещение {len(found)}, но "
                         f"{_UNVERIFIED_MSG}")
        note = _unarmed_note(found)
        if note:
            lines.append(note)
        if mismatch:
            lines.append(_mismatch_report(mismatch, "переместил"))
        if missing:
            lines.append(
                f"↷ Не найдены среди открытых {len(missing)} "
                "(неверный id/уже завершены): "
                + ", ".join(f"«{t['title']}»" for t in missing))
        if failed:
            extra = "; ".join(f"{k[:8]}…: {v}" for k, v in api_fail.items())
            lines.append(f"❌ НЕ перемещено {len(failed)} (остались на месте"
                         + (f"; TickTick сообщил: {extra}" if extra else "")
                         + "): " + ", ".join(f"«{t}»" for t in failed))
        if found:
            rid = _op_journal("move", [
                {"taskId": f["taskId"], "title": f["title"],
                 "expect": {"projectId": to_project_id}} for f in found], summary)
            lines.append(_report_line(rid))
        return "\n".join(lines) if lines else "Ничего не перемещено."
    except Exception as e:
        logger.exception("Error in move_tasks")
        return _tool_error("moving tasks", e)


# ---------------------------------------------------------------------------
# Habits (v2)
# ---------------------------------------------------------------------------

def _ensure_ready() -> Optional[str]:
    """Return an error string if the v2 client isn't ready, else None.
    Lazily (re-)initializes the clients on first use; v2 is optional and only
    present when TICKTICK_V2_TOKEN is set and valid."""
    if not ticktick_v2:
        initialize_client()
    if not ticktick_v2:
        return _V2_DISABLED_MSG
    return None


@mcp.tool(annotations=READONLY)
async def get_habits() -> str:
    """List all habits with their goal and current streak (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        habits = await _run_blocking(lambda: ticktick_v2.get_habits())
        if not habits:
            return "No habits found."
        # totalCheckIns считает ТОЛЬКО успешные отметки — проверено живьём:
        # привычка с «totalCheckIns: 18» отдаёт 31 запись в get_habit_checkins
        # (18 выполнено + 13 провалено/пропущено). Слово «total» читалось как
        # «все отметки», то есть завышало регулярность: 18 из 18 вместо 18 из
        # 31. Поле не пересчитываем (это счётчик TickTick), но называем честно
        # и говорим, где смотреть полную историю.
        out = (f"Habits ({len(habits)}):\n"
               "(«done» = SUCCESSFUL check-ins only — failed and skipped days are "
               "NOT in that number; get_habit_checkins lists every entry)\n\n")
        for h in habits:
            out += (f"- {h.get('name','?')}  (id: {h.get('id')})\n"
                    f"    goal: {h.get('goal')} {h.get('unit','')} | type: {h.get('type')} | "
                    f"done: {h.get('totalCheckIns', 0)}\n"
                    f"    repeat: {h.get('repeatRule','')}\n")
        return out
    except Exception as e:
        logger.exception("Error in get_habits")
        return _tool_error("fetching habits", e)


_HABIT_STATUS_LABELS = {2: "выполнено", 1: "провалено", 0: "не выполнено"}


def _checkin_date_str(stamp) -> str:
    """Дата отметки привычки по-человечески: 20260529 → «2026-05-29».

    TickTick хранит день привычки как целое YYYYMMDD, и оно печаталось как
    есть — читать «20260529» и мысленно резать на части приходилось человеку.
    Значение неожиданной формы отдаём как есть: подгонять его под маску
    значило бы выдумать дату."""
    text = str(stamp or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text or "?"


def _checkin_sort_key(stamp) -> tuple:
    """Ключ сортировки отметок по дате. Значения непонятной формы уезжают в
    конец, но НЕ выбрасываются — молча потерянная отметка хуже некрасивой."""
    text = str(stamp or "").strip()
    if text.isdigit():
        return (0, int(text))
    return (1, 0)


def _describe_checkin_habit(p: Dict) -> str:
    label = _HABIT_STATUS_LABELS.get(p.get("status"), p.get("status"))
    when = p.get("date") or "сегодня"
    return f'Отмечаю привычку «{p.get("habit_name")}» на {when}: {label}'


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def checkin_habit(habit_name: str, habit_id: str, date: str = None,
                        status: int = 2, value: float = None,
                        manifest_id: str = "", user_reply: str = "",
                        automation_key: str = "") -> str:
    """
    Record a habit check-in (requires v2 API). Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    recorded on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is recorded yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    После записи сервер сам перечитывает check-ins свежим запросом
    (независимо от того, что было отправлено) и подтверждает в ответе, что
    отметка реально видна в TickTick — а не просто "запрос ушёл". Возврати
    результат пользователю ДОСЛОВНО, не пересказывай своими словами.

    Args:
        habit_name: Name of the habit (shown first in the summary you show the user, see get_habits) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        habit_id: ID of the habit
        date: Date to check in as YYYY-MM-DD (optional; defaults to today — pass a past date to backfill)
        status: 2 = done (default), 1 = failed, 0 = not done
        value: Numeric value for quantitative habits (optional; defaults to the goal when done)
        manifest_id: from call #1's response — pass on call #2 to actually record
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err
    if status not in (0, 1, 2):
        return "🛑 Неверный status. Используй 2 (done), 1 (failed) или 0 (not done). Ничего не записано."
    # Strict date validation: '2026-7-4' would silently become stamp 202674
    # downstream (int(date.replace("-", ""))). strptime alone does NOT catch
    # this — it happily parses non-zero-padded '2026-7-4' — so the format is
    # also round-tripped back through strftime to enforce YYYY-MM-DD exactly.
    if date is not None:
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != date:
                raise ValueError("not zero-padded")
        except ValueError:
            return (f"🛑 Неверный формат даты {date!r} — нужно строго "
                    "YYYY-MM-DD (например 2026-07-04). Ничего не записано.")

    params = {"habit_name": habit_name, "habit_id": habit_id, "date": date,
              "status": status, "value": value}
    outcome = await _gate_single("checkin_habit", "checkin_habit",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, _describe_checkin_habit,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _checkin_habit_impl(**outcome.extra)


async def _checkin_habit_impl(habit_name: str, habit_id: str, date: str = None,
                              status: int = 2, value: float = None) -> str:
    """Pure mutation logic for checkin_habit — no consent gate. Called only
    by the gated checkin_habit() above once the plan is approved."""
    labels = _HABIT_STATUS_LABELS
    try:
        # Identity guard: the id must exist among live habits AND resolve to
        # the given name — a swapped pair would check in the WRONG habit while
        # the reply names the right one.
        habits = await _run_blocking(lambda: ticktick_v2.get_habits())
        habit = next((h for h in habits if h.get("id") == habit_id), None)
        if habit is None:
            return (f"🛑 Привычка с id {str(habit_id)[:12]}… не найдена "
                    "(get_habits). Ничего не записано.")
        real_name = habit.get("name") or ""
        if not _names_agree(habit_name, real_name):
            return (f"🛑 НЕ отметил — habit_id указывает на «{real_name}», а НЕ "
                    f"«{habit_name}» (защита от «не той привычки»). Ничего не записано.")
        try:
            goal = float(habit.get("goal") or 1.0)
        except (TypeError, ValueError):
            goal = 1.0
        stamp = int((date or datetime.now().strftime("%Y-%m-%d")).replace("-", ""))
        when_fmt = datetime.strptime(str(stamp), "%Y%m%d").strftime("%d.%m.%Y")
        # Duplicate detection: an unconditional 'add' on retry would double
        # the value for the same day.
        existing = await _run_blocking(
            lambda: ticktick_v2.get_habit_checkins([habit_id], stamp - 1))
        dup = next((e for e in existing.get(habit_id, [])
                    if e.get("checkinStamp") == stamp), None)
        if dup is not None:
            dup_label = labels.get(dup.get("status"), dup.get("status"))
            return (f"### ↷ Чек-ин «{real_name}» не записан\n\n"
                    f"- дата: {when_fmt}\n"
                    f"- на эту дату чек-ин **уже есть** (статус {dup_label}, "
                    f"{dup.get('value')}/{dup.get('goal')})\n"
                    "- повторная запись задвоила бы значение — ничего не изменено")
        await _run_blocking(lambda: ticktick_v2.checkin_habit(
            habit_id, date=date, status=status, value=value, goal=goal))
        val = value if value is not None else (goal if status == 2 else 0.0)

        # Post-verify: habits are not journaled (no operation_report later),
        # so this is the ONLY independent confirmation this tool ever gets —
        # a fresh, separate read of live check-ins, not a replay of what we
        # just sent. Reuses the same call shape as the dup-check above.
        try:
            fresh = await _run_blocking(
                lambda: ticktick_v2.get_habit_checkins([habit_id], stamp - 1))
            written = next((e for e in fresh.get(habit_id, [])
                            if e.get("checkinStamp") == stamp), None)
        except Exception as e:
            return (f"### ⚠️ Чек-ин «{real_name}» отправлен, проверка не выполнена\n\n"
                    f"- дата: {when_fmt}\n"
                    f"- запрос на статус **{labels[status]}** ({val}/{goal}) отправлен\n"
                    f"- ⚠️ независимое перечитывание (`get_habit_checkins`) упало с ошибкой: {_redact_for_user(e)} — "
                    "исход НЕ подтверждён")

        if written is None:
            return (f"### ⚠️ Чек-ин «{real_name}» отправлен, но НЕ подтверждён\n\n"
                    f"- дата: {when_fmt}\n"
                    f"- запрос на статус **{labels[status]}** ({val}/{goal}) отправлен\n"
                    "- ❌ при независимом перечитывании (`get_habit_checkins`) записи на эту "
                    "дату НЕ нашлось — исход не подтверждён, возможно нужно время на синхронизацию")

        w_status = written.get("status")
        w_value = written.get("value")
        w_goal = written.get("goal")
        ok = (w_status == status)
        w_label = labels.get(w_status, w_status)
        if ok:
            return (f"### ✅ Чек-ин привычки «{real_name}»\n\n"
                    f"- дата: {when_fmt}\n"
                    f"- статус: **{w_label}**, значение {w_value}/{w_goal}\n"
                    "- 🧾 подтверждено независимым чтением (`get_habit_checkins`) сразу после записи")
        return (f"### ❌ Чек-ин «{real_name}» разошёлся с подтверждением\n\n"
                f"- дата: {when_fmt}\n"
                f"- запрошено: **{labels[status]}** ({val}/{goal})\n"
                f"- при независимом перечитывании: **{w_label}** ({w_value}/{w_goal})\n"
                "- ⚠️ запись есть, но не совпадает с тем, что отправляли — проверь вручную")
    except Exception as e:
        logger.exception("Error in checkin_habit")
        return _tool_error("checking in habit", e)


def _describe_create_habit(p: Dict) -> str:
    kind = ("да/нет" if str(p.get("habit_type")).lower() == "boolean"
            else f'количественная, цель {p.get("goal")} {p.get("unit")}')
    return (f'Создаю привычку «{p.get("name")}» ({kind}), раздел '
            f'{p.get("section")}, повтор {p.get("repeat_rule")}')


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def create_habit(name: str, goal: float = 1.0,
                       unit: str = "Count", habit_type: str = "boolean",
                       repeat_rule: str = "RRULE:FREQ=DAILY;INTERVAL=1",
                       section: str = "morning", color: str = "#97E38B",
                       icon: str = "habit_daily_check_in",
                       encouragement: str = "",
                       manifest_id: str = "", user_reply: str = "",
                       automation_key: str = "") -> str:
    """
    Create a habit (requires v2 API — habits exist only in TickTick's
    unofficial API). Gated 🟡 (docs/DESIGN_approval_gate.md): two calls, same
    tool name — nothing is created on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is created yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    A habit whose name already exists is NOT created a second time (TickTick
    itself allows duplicates; this tool refuses instead of quietly making a
    twin). After the write the server re-reads the live habit list in a
    separate request and only claims success if the habit is really there.

    Args:
        name: Name of the new habit (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        goal: Target per period — 1 for a plain yes/no habit, e.g. 8 for "8 glasses of water"
        unit: Unit for a quantitative habit ("Count", "ml", "min", ...); ignored for a boolean one
        habit_type: "boolean" (just done/not done, default) or "quantitative" (counts up to `goal`)
        repeat_rule: RRULE string, default daily (e.g. "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,WE,FR")
        section: Time-of-day section: "morning" (default), "afternoon" or "night"
        color: Hex colour of the habit card
        icon: TickTick icon name (see an existing habit's iconRes via get_habits)
        encouragement: Short motivational line TickTick shows on check-in
        manifest_id: from call #1's response — pass on call #2 to actually create
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err
    if not (name or "").strip():
        return "🛑 Пустое имя привычки — нечего создавать. Ничего не изменено."
    if str(habit_type).lower() not in ("boolean", "quantitative"):
        return ('🛑 Неверный habit_type — допустимо "boolean" или '
                '"quantitative". Ничего не изменено.')
    if str(section).lower().lstrip("_") not in TickTickV2Client.HABIT_SECTIONS:
        return ('🛑 Неверный section — допустимо "morning", "afternoon" или '
                '"night". Ничего не изменено.')
    try:
        goal = float(goal)
    except (TypeError, ValueError):
        return f"🛑 goal должен быть числом, а не {goal!r}. Ничего не изменено."
    if goal <= 0:
        return "🛑 goal должен быть больше нуля. Ничего не изменено."

    params = {"name": name.strip(), "goal": goal, "unit": unit,
              "habit_type": str(habit_type).lower(), "repeat_rule": repeat_rule,
              "section": str(section).lower().lstrip("_"), "color": color,
              "icon": icon, "encouragement": encouragement}
    outcome = await _gate_single("create_habit", "create_habit",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, _describe_create_habit,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _create_habit_impl(**outcome.extra)


async def _create_habit_impl(name: str, goal: float = 1.0, unit: str = "Count",
                             habit_type: str = "boolean",
                             repeat_rule: str = "RRULE:FREQ=DAILY;INTERVAL=1",
                             section: str = "morning", color: str = "#97E38B",
                             icon: str = "habit_daily_check_in",
                             encouragement: str = "") -> str:
    """Pure mutation logic for create_habit — no consent gate. Called only by
    the gated create_habit() above once the plan is approved (or by the
    Telegram button's background executor, which is the same thing)."""
    api_type = "Boolean" if habit_type == "boolean" else "Real"
    try:
        # Duplicate guard: TickTick happily creates a second habit with the
        # same name, and the pair is then indistinguishable in every list the
        # user sees. Refuse instead (fail-closed), same spirit as create_tag.
        before = await _run_blocking(lambda: ticktick_v2.get_habits())
        twin = next((h for h in before
                     if _names_agree(name, h.get("name") or "")), None)
        if twin is not None:
            return (f"### ↷ Привычка «{name}» не создана\n\n"
                    f"- такая привычка **уже есть** (id: {twin.get('id')}, "
                    f"успешных отметок: {twin.get('totalCheckIns', 0)})\n"
                    "- второй одноимённой не завожу — их было бы не отличить "
                    "друг от друга; ничего не изменено")
        hid = await _run_blocking(lambda: ticktick_v2.create_habit(
            name, goal=goal, unit=unit, habit_type=api_type,
            repeat_rule=repeat_rule, section=section, color=color, icon=icon,
            encouragement=encouragement))
    except RuntimeError as e:
        return f"### ❌ Привычка «{name}» НЕ создана — TickTick отклонил: {_redact_for_user(e)}"
    except Exception as e:
        logger.exception("Error in create_habit")
        return _tool_error("creating habit", e)

    # Post-verify: a SEPARATE fresh read of the live habit list — never the
    # write response — must show the habit, under the requested name.
    try:
        after = await _run_blocking(lambda: ticktick_v2.get_habits())
    except Exception as e:
        return (f"### ⚠️ Привычка «{name}» отправлена (id: {hid}), проверка не выполнена\n\n"
                f"- ⚠️ независимое перечитывание (`get_habits`) упало с ошибкой: {_redact_for_user(e)}\n"
                f"- {_UNVERIFIED_MSG}")
    live = next((h for h in after if h.get("id") == hid), None)
    if live is None:
        return (f"### ⚠️ Привычка «{name}» отправлена (id: {hid}), но НЕ подтверждена\n\n"
                "- ❌ при независимом перечитывании (`get_habits`) её в списке НЕ нашлось\n"
                "- исход не подтверждён, проверь вручную")
    real_name = live.get("name") or ""
    if not _names_agree(name, real_name):
        return (f"### ❌ Привычка создана, но имя разошлось\n\n"
                f"- просили «{name}», в живом состоянии «{real_name}» (id: {hid})\n"
                "- проверь вручную")
    kind = ("да/нет" if api_type == "Boolean"
            else f"количественная, цель {live.get('goal')} {live.get('unit')}")
    return (f"### ✅ Привычка «{real_name}» создана (проверено)\n\n"
            f"- тип: {kind}\n"
            f"- повтор: {live.get('repeatRule') or repeat_rule}\n"
            f"- отмечать: `checkin_habit(habit_name=\"{real_name}\", habit_id=\"{hid}\")`\n"
            "- 🧾 подтверждено отдельным живым чтением списка привычек "
            f"(`get_habits`) сразу после создания (id: {hid})")


def _describe_delete_habit(p: Dict) -> str:
    return (f'Удаляю привычку «{p.get("habit_name")}» ВМЕСТЕ со всей историей '
            "отметок — восстановить нельзя (корзины для привычек в TickTick нет)")


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def delete_habit(habit_name: str, habit_id: str, manifest_id: str = "",
                       user_reply: str = "", automation_key: str = "") -> str:
    """
    ⚠️ Delete a habit permanently, together with its whole check-in history
    (requires v2 API). There is no habit trash in TickTick — this is
    IRREVERSIBLE. Gated 🟡 (docs/DESIGN_approval_gate.md): two calls, same
    tool name — nothing is deleted on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is deleted yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Both habit_name AND habit_id are required and must describe the SAME
    habit: the server re-reads the live habit list and refuses when the id
    resolves to a different name — TWICE (def-116, 2026-08-07): once while
    BUILDING the plan (call #1, before anything is shown to the owner — a
    mismatched name never reaches the plan card at all) and again,
    independently, right before the actual delete (call #2). If the live
    read itself fails while building the plan, the plan still gets built
    (a read hiccup must not block every deletion), but its text says so
    honestly — the call #2 check is unconditional and still guards the
    mutation either way. A snapshot of the habit is written to the mutation
    journal first, since nothing else can bring it back.

    Args:
        habit_name: Name of the habit (shown first in the summary you show the user, see get_habits) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        habit_id: ID of the habit to delete (see get_habits)
        manifest_id: from call #1's response — pass on call #2 to actually delete
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err
    if not (habit_name or "").strip() or not (habit_id or "").strip():
        return ("🛑 Нужны И имя, И id привычки — удаление вслепую по одному id "
                "не делаю (см. get_habits). Ничего не изменено.")
    # def-116 (2026-08-07): та же сверка habit_id↔habit_name, что уже стоит
    # в _delete_habit_impl НА ИСПОЛНЕНИИ (identity guard), но здесь — ДО
    # построения плана. Живой пример: подали habit_name реальной привычки
    # («Растяжка», 18 отметок) вместе с habit_id ДРУГОЙ (тестовой) —
    # владельцу ушла карточка «Удаляю привычку «Растяжка»…», хотя id вёл не
    # туда. Гейт на исполнении это ловит, но к тому моменту человек уже
    # читал (и мог одобрить) непроверенное имя — по образцу
    # `_resolve_triage_ops` в manual_triage ("название не совпало — по
    # этому id сейчас «X», а в плане «Y»"), сверка сдвинута на ПОСТРОЕНИЕ.
    #
    # Действует только на call #1 (не manifest_id): call #2 обслуживает
    # СОХРАНЁННЫЕ параметры плана, а не свежие аргументы вызова (тот же
    # анти-подмен контракт, что у всех гейтов), и identity guard на
    # исполнении там уже стоит, не тронут. automation_key НЕ пропускает эту
    # проверку — она стоит раньше самого гейта, поэтому и headless-путь
    # (карточки вообще не видит) защищён, а не только интерактивный.
    name_warning = ""
    if not manifest_id:
        try:
            habits = await _run_blocking(lambda: ticktick_v2.get_habits())
        except Exception as e:
            # Живое чтение недоступно ВРЕМЕННО — не блокируем удаление
            # наглухо (fail-closed здесь стоил бы дороже, чем однократный
            # непроверенный план: исполнение всё равно перепроверит), но и
            # не выдаём непроверенное имя за факт — карточка честно скажет
            # об этом сама (см. name_warning ниже).
            habits = None
            logger.warning(f"delete_habit: не удалось прочитать список "
                           f"привычек на этапе плана ({e}) — план всё равно "
                           "строится, сверка имени повторится на исполнении.")
        if habits is not None:
            habit = next((h for h in habits if h.get("id") == habit_id), None)
            if habit is None:
                return (f"🛑 План НЕ построен — привычки с id "
                        f"{str(habit_id)[:12]}… нет в живом списке (уже "
                        "удалена или неверный id, см. get_habits). Ничего не "
                        "изменено.")
            real_name = habit.get("name") or ""
            if not _names_agree(habit_name, real_name):
                return (f"🛑 План НЕ построен — habit_id указывает на "
                        f"«{real_name}», а НЕ «{habit_name}» (защита от «не "
                        "той привычки»). Ничего не изменено.")
        else:
            name_warning = (" ⚠️ Имя НЕ удалось сверить с живым списком "
                            "привычек (чтение не удалось) — сверка "
                            "повторится при подтверждении, и расхождение "
                            "остановит удаление.")
    params = {"habit_name": habit_name, "habit_id": habit_id}
    describe_fn = ((lambda p: _describe_delete_habit(p) + name_warning)
                   if name_warning else _describe_delete_habit)
    outcome = await _gate_single("delete_habit", "delete_habit",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _delete_habit_impl(**outcome.extra)


async def _delete_habit_impl(habit_name: str, habit_id: str) -> str:
    """Pure mutation logic for delete_habit — no consent gate. Called only by
    the gated delete_habit() above once the plan is approved (or by the
    Telegram button's background executor, which is the same thing)."""
    try:
        # Identity guard (fresh read, never a cached plan-time copy): the id
        # must exist AND resolve to the given name.
        habits = await _run_blocking(lambda: ticktick_v2.get_habits())
        habit = next((h for h in habits if h.get("id") == habit_id), None)
        if habit is None:
            return (f"🛑 НЕ удалил — привычки с id {str(habit_id)[:12]}… нет в "
                    "живом списке (уже удалена/неверный id). Ничего не тронул.")
        real_name = habit.get("name") or ""
        if not _names_agree(habit_name, real_name):
            return (f"🛑 НЕ удалил — habit_id указывает на «{real_name}», а НЕ "
                    f"«{habit_name}» (защита от «не той привычки»). Ничего не тронул.")

        # Pre-snapshot BEFORE the irreversible call: habits have no trash, so
        # this journal line is the only thing left to rebuild it by hand. The
        # write is best-effort (an unwritable journal must not block a
        # confirmed-by-the-owner delete), but the RESULT text below reports
        # what actually happened — claiming a snapshot that was never written
        # would be exactly the kind of lie the output rules forbid.
        checkins = habit.get("totalCheckIns", 0)
        journal_path = _journal_write({
            "ts": datetime.now(timezone.utc).isoformat(),
            "record": "delete_habit-" + uuid.uuid4().hex[:8],
            "op": "delete_habit",
            "summary": f"Удаление привычки «{real_name}» ({checkins} отметок)",
            "snapshot": habit,
        })

        resp = await _run_blocking(lambda: ticktick_v2.delete_habit(habit_id))
        api_err = id2error_failures(resp, [habit_id]).get(habit_id)
        if api_err:
            return f"### ❌ Привычка «{real_name}» НЕ удалена — TickTick отклонил: {api_err}"

        # Post-verify: a SEPARATE fresh read must no longer show the habit.
        try:
            after = await _run_blocking(lambda: ticktick_v2.get_habits())
        except Exception as e:
            return (f"### ⚠️ Привычка «{real_name}» отправлена на удаление, проверка не выполнена\n\n"
                    f"- ⚠️ независимое перечитывание (`get_habits`) упало с ошибкой: {_redact_for_user(e)}\n"
                    f"- {_UNVERIFIED_MSG}")
        if any(h.get("id") == habit_id for h in after):
            return (f"### ❌ Привычка «{real_name}» ВСЁ ЕЩЁ в списке\n\n"
                    "- удаление не подтвердилось при независимом перечитывании "
                    "(`get_habits`) — проверь вручную")
        snap_line = ("- восстановить нельзя; снимок привычки записан в журнал "
                     "мутаций перед удалением" if journal_path else
                     "- ⚠️ восстановить нельзя, и снимок в журнал мутаций "
                     "записать НЕ удалось (журнал недоступен) — вернуть её "
                     "по данным сервера не выйдет")
        return (f"### ✅ Привычка «{real_name}» удалена (проверено)\n\n"
                f"- вместе с ней ушла вся история отметок (успешных было "
                f"**{checkins}**, провалы и пропуски тоже стёрты)\n"
                f"{snap_line}\n"
                "- 🧾 подтверждено отдельным живым чтением списка привычек "
                "(`get_habits`) сразу после удаления")
    except Exception as e:
        logger.exception("Error in delete_habit")
        return _tool_error("deleting habit", e)


@mcp.tool(annotations=READONLY)
async def get_habit_checkins(habit_name: str, habit_id: str, after_date: str) -> str:
    """
    Get a habit's check-in history (requires v2 API).

    Args:
        habit_name: Name of the habit (shown first in the summary you show the user, see get_habits) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        habit_id: ID of the habit
        after_date: Only return check-ins on/after this date, as YYYY-MM-DD
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        # afterStamp is exclusive (>) on the API side; subtract 1 so the
        # requested date itself is included (YYYYMMDD is monotonic).
        stamp = int(after_date.replace("-", "")) - 1
        result = await _run_blocking(lambda: ticktick_v2.get_habit_checkins([habit_id], stamp))
        entries = result.get(habit_id, [])
        if not entries:
            return f"No check-ins for '{habit_name}' since {after_date}."
        labels = {2: "✓ done", 1: "✗ failed", 0: "○ not done"}
        # API отдаёт записи в произвольном порядке (живьём: 20260529 → 20260531
        # → 20260530 → 20260605) — серию по такому списку глазами не посчитать.
        # Сортируем по дате, от старых к новым: это календарь, а не лента
        # событий.
        entries = sorted(entries, key=lambda e: _checkin_sort_key(e.get("checkinStamp")))
        done = sum(1 for e in entries if e.get("status") == 2)
        failed = sum(1 for e in entries if e.get("status") == 1)
        skipped = len(entries) - done - failed
        lines = [f"- {_checkin_date_str(e.get('checkinStamp'))}: "
                 f"{labels.get(e.get('status'), e.get('status'))} "
                 f"(value {e.get('value')}/{e.get('goal')})" for e in entries]
        # Одно число «(31)» прочитывалось как «31 раз сделал» — разбивка не
        # даёт спутать записи в журнале с выполнениями.
        return (f"Check-ins for '{habit_name}' — {len(entries)} entries: "
                f"{done} done, {failed} failed, {skipped} not done "
                f"(oldest first):\n" + "\n".join(lines))
    except Exception as e:
        logger.exception("Error in get_habit_checkins")
        return _tool_error("fetching habit check-ins", e)


# ---------------------------------------------------------------------------
# Filters / smart lists (v2)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READONLY)
async def list_filters() -> str:
    """List saved smart-list filters with their query rules (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        filters = await _run_blocking(lambda: ticktick_v2.get_filters())
        if not filters:
            return "No filters found."
        out = f"Filters ({len(filters)}):\n\n"
        for f in filters:
            out += f"- {f.get('name','?')}  (id: {f.get('id')})\n    rule: {f.get('rule','')}\n"
        return out
    except Exception as e:
        logger.exception("Error in list_filters")
        return _tool_error("fetching filters", e)


# ---------------------------------------------------------------------------
# Subtasks (v2)
# ---------------------------------------------------------------------------

@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def set_task_parent(summary: str, tasks: List[Dict[str, str]] = None,
                          parent_task_id: str = "", project_id: str = "",
                          parent_task_title: str = None,
                          manifest_id: str = "", user_reply: str = "",
                          automation_key: str = "") -> str:
    """
    Nest one or more tasks under a parent in one call (requires v2 API).
    All tasks and the parent must be in the same project. Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest from `tasks` +
    the target parent and returns a preview — nothing is nested yet.
    Call #2 (after the user actually replied): repeat the call with
    manifest_id=<id from call #1> and user_reply=<the user's literal last
    message> — `tasks`/`parent_task_id`/`project_id`/`parent_task_title` are
    all ignored on this call (the manifest's own stored values are used, so
    nothing can be swapped between the two calls). Do NOT make call #2 in the
    same turn as call #1.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user, e.g. «Делаю задачу „Шаг 1"
    подзадачей „Большой проект"» or «Делаю 3 задачи подзадачами „Большой
    проект"».

    Put the human title inside each task object so the dialog shows what's
    being nested: [{"title": "Step 1", "taskId": "abc"}].

    {{AUTOMATION_KEY_NOTE}}

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title": "...", "taskId": "..."} objects — required
            on call #1, ignored on call #2
        parent_task_id: ID of the parent task — required on call #1, ignored
            on call #2
        project_id: ID of the project all tasks live in — required on call
            #1, ignored on call #2
        parent_task_title: Title of the parent (shown in the dialog)
        manifest_id: from call #1's response — pass on call #2 to actually nest
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    parent_task_id and parent_task_title are cross-checked against the LIVE
    task list TWICE (same pattern as delete_habit, def-116, 2026-08-07): once
    while BUILDING the plan (call #1, before anything is shown to the owner —
    a mismatched or gone parent never reaches the plan card at all) and again,
    independently, right before the actual nesting (call #2, unchanged). If
    the live read itself fails while building the plan, the plan still gets
    built (a read hiccup must not block every nesting), but its text says so
    honestly — the call #2 check is unconditional and still guards the
    mutation either way. NOTE: each task in `tasks` is ALSO cross-checked
    against the live task list while BUILDING the plan (call #1, via the
    same _plan_live_check helper as the parent) — not just the parent's
    identity — and each is resolved found/mismatch/missing again,
    independently, on execution (see _set_task_parent_impl /
    _split_tasks_by_state).
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (parent_task_id↔parent_task_title) на построение
    # плана — тот же _guard_task, что уже стоит в _set_task_parent_impl НА
    # ИСПОЛНЕНИИ для РОДИТЕЛЯ, но здесь — ДО показа карточки владельцу (тот
    # же перенос, что в create_subtask/unset_task_parent/duplicate_task).
    # mismatch И missing здесь блокируют план целиком — _set_task_parent_impl
    # уже трактует ОБА как 🛑 («НЕ вложил») на исполнении для родителя,
    # перенос это не ужесточает. Вкладывать в ЗАВЕРШЁННОГО родителя смысла
    # нет, поэтому здесь обычный _guard_task «только открытые», а не
    # _guard_task_incl_completed (класс операций над завершёнными). Временная недоступность живого чтения — fail-open с
    # предупреждением (через notes батч-карточки), исполнение (не тронуто)
    # перепроверит заново. Действует только на call #1 (manifest_id пуст);
    # automation_key НЕ пропускает эту проверку — она стоит раньше самого
    # гейта.
    #
    # СКОУП ТОЙ ПРАВКИ был ТОЛЬКО parent_task_id↔parent_task_title (сам
    # родитель) — единственный именованный объект, который карточка печатала
    # БЕЗ сверки. Личность КАЖДОЙ задачи из списка `tasks` разбиралась лишь в
    # _set_task_parent_impl (через _split_tasks_by_state), и переносить эту
    # частичную батч-логику на план значило переписать формат карточки
    # заново: «нужен live-статус на КАЖДЫЙ элемент до его показа, с иным
    # типом отчёта».
    #
    # КРУГ 8: ровно такой формат теперь и существует — `_plan_live_check`
    # (⛔ на обречённой строке + ⚠️-сводка, отказ когда исполнять нечего,
    # честное ⚠️ когда сверка не удалась), и строки списка пропускаются через
    # него ниже. Пропуск нашёл сплошной прогон одного корзинного входа по
    # всем тулам: КОРЗИННАЯ ЗАДАЧА В РОЛИ РЕБЁНКА (живой родитель, мёртвый
    # ребёнок) давала обычный план — проверялся-то только родитель.
    notes = None
    titles: Dict[str, str] = {}
    # Как называется РОДИТЕЛЬ в карточке. Живое имя тут уже прочитано
    # guard'ом строкой ниже — и раньше выбрасывалось: печаталось
    # `parent_task_title or parent_task_id`, то есть при не переданном
    # названии человек видел «→ под «6a7571238f0854e347f51407»» и сверить,
    # под ту ли задачу вкладывают, не мог.
    parent_label = ""
    if not manifest_id:
        titles = _plan_task_titles(tasks)
        pg = _guard_task(parent_task_id, parent_task_title or "", project_id)
        refusal, warn = _guard_or_refuse(
            pg, stage="план", expected=parent_task_title, parent=True,
            missing_name=parent_task_title or parent_task_id,
            missing_extra=" — вложение под мёртвого родителя осиротит задачи",
            unavailable_note=_UNVERIFIED_PARENT_TITLE)
        if refusal:
            return refusal
        if warn:
            notes = [warn]
        # ЖИВОЕ написание побеждает переданное: `_names_agree` пропускает
        # разницу в регистре/маркерах, а карточка должна показывать
        # состояние аккаунта, а не пересказ вызывающего. При недоступном
        # чтении `pg.title` — это переданное название (guard возвращает
        # ожидаемое), и предупреждение об этом уже стоит в notes выше.
        parent_label = _plan_task_name(
            {"taskId": parent_task_id,
             "title": (pg.title or parent_task_title or "")})

    def _describe(t: Dict[str, Any]) -> str:
        return f"**{_plan_task_name(t, titles)}** → под {parent_label}"

    # Сверка ВКЛАДЫВАЕМЫХ задач с живым состоянием — см. `_plan_live_check`.
    if not manifest_id and tasks:
        _describe, row_notes, refusal = _plan_live_check(tasks, _describe)
        if refusal:
            return refusal
        notes = ((notes or []) + (row_notes or [])) or None
    outcome = await _gate_batch(
        "parent", "set_task_parent", tasks, summary, manifest_id, user_reply,
        _describe,
        extra={"parent_task_id": parent_task_id, "project_id": project_id,
               "parent_task_title": parent_task_title},
        notes=notes,
        automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    ex = outcome.extra
    return await _set_task_parent_impl(
        outcome.summary, outcome.tasks,
        ex.get("parent_task_id") or parent_task_id,
        ex.get("project_id") or project_id,
        ex.get("parent_task_title") if ex.get("parent_task_title") is not None else parent_task_title)


async def _set_task_parent_impl(summary: str, tasks: List[Dict[str, str]],
                                parent_task_id: str, project_id: str,
                                parent_task_title: str = None) -> str:
    """Pure mutation logic for set_task_parent — no consent gate. Called
    by the public gated set_task_parent() below AND directly by
    execute_declutter/resume_declutter (an already-approved declutter
    manifest must not be asked to confirm twice)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        # Guard the parent AND the children against live state.
        by_id = _open_by_id(fresh=True)
        if by_id is None:
            return _STATE_UNAVAILABLE_MSG
        pg = _guard_task(parent_task_id, parent_task_title or "", project_id,
                         by_id=by_id)
        refusal, _warn = _guard_or_refuse(
            pg, stage="исполнение", verb="НЕ вложил",
            expected=parent_task_title, parent=True,
            missing_name=parent_task_title or parent_task_id,
            missing_extra=" — вложение под мёртвого родителя осиротит задачи")
        if refusal:
            return refusal
        parent_pid = pg.project_id or project_id
        # Ancestor chain of the parent — nesting a task under its own
        # descendant (or under itself) would corrupt the tree with a cycle.
        ancestors = set()
        cur = parent_task_id
        while cur and cur not in ancestors:
            ancestors.add(cur)
            cur = (by_id.get(cur) or {}).get("parentId")
        # Depth guard: the parent's own live level (root = 1) plus however
        # many levels the task being nested ALREADY spans below itself (it
        # may already have its own subtasks, which move with it) must not
        # exceed TickTick's real cap — see MAX_TASK_NEST_LEVELS above.
        parent_level = len(ancestors)
        children_of = _children_index(by_id)
        found, mismatch, missing = _split_tasks_by_state(tasks, by_id=by_id)
        rows, cycle_refused, cross_refused, depth_refused = [], [], [], []
        ok_items = []
        for f in found:
            if f["taskId"] in ancestors:
                cycle_refused.append(f["title"])
                continue
            if f["projectId"] and f["projectId"] != parent_pid:
                cross_refused.append(
                    f"«{f['title']}» (в «{_v2_project_names().get(f['projectId'], f['projectId'])}»)")
                continue
            height = _subtree_height(f["taskId"], children_of)
            resulting_level = parent_level + height
            if resulting_level > MAX_TASK_NEST_LEVELS:
                extra = (f" (у неё уже есть свои подзадачи на {height - 1} "
                         "уровень(ей) вниз)" if height > 1 else "")
                depth_refused.append(
                    f"«{f['title']}»{extra}: получилось бы "
                    f"{resulting_level} из {MAX_TASK_NEST_LEVELS} уровней")
                continue
            # Each child's OWN live projectId — never stamp the parent's onto
            # a row TickTick would reject or corrupt.
            rows.append({"parentId": parent_task_id, "taskId": f["taskId"],
                         "projectId": f["projectId"] or parent_pid})
            ok_items.append(f)
        api_fail = {}
        if rows:
            resp = await _run_blocking(lambda: ticktick_v2.set_task_parents(rows))
            api_fail = id2error_failures(resp, [r["taskId"] for r in rows])
        pname = pg.title or parent_task_title or _lookup_task_title(parent_task_id)
        # Inline post-verify: each child's live parentId must now BE the parent.
        nested, failed = [], []
        unverified = False
        if ok_items:
            # Retried re-read (see _reread_open_until / _POSTVERIFY_RETRY_*):
            # an immediate single re-read can race TickTick's own v2 sync
            # right after the SAME v2 API just wrote parentId — the exact
            # class of false-❌ found live 2026-08-06 on move_tasks.
            fresh = await _run_blocking(_reread_open_until, lambda m: all(
                (m.get(f["taskId"]) or {}).get("parentId") == parent_task_id
                for f in ok_items if f["taskId"] not in api_fail))
            if fresh is None:
                unverified = True
            else:
                for f in ok_items:
                    live = fresh.get(f["taskId"]) or {}
                    ok = (live.get("parentId") == parent_task_id
                          and f["taskId"] not in api_fail)
                    (nested if ok else failed).append(f["title"])
        lines = []
        if nested:
            lines.append(f"🔗 Вложено {len(nested)} под «{pname}»: "
                         + ", ".join(f"«{t}»" for t in nested))
        if unverified:
            lines.append(f"Отправлено {len(ok_items)}, но {_UNVERIFIED_MSG}")
        if cycle_refused:
            lines.append(f"🛑 НЕ вложено {len(cycle_refused)} — задача не может "
                         "стать подзадачей самой себя или своего потомка "
                         "(цикл): " + ", ".join(f"«{t}»" for t in cycle_refused))
        if cross_refused:
            lines.append(f"🛑 НЕ вложено {len(cross_refused)} — задачи в ДРУГОМ "
                         f"проекте, а родитель в «{_v2_project_names().get(parent_pid, parent_pid)}». "
                         "Сначала перенеси move_tasks: " + ", ".join(cross_refused))
        if depth_refused:
            lines.append(f"🛑 НЕ вложено {len(depth_refused)} — TickTick не "
                         f"поддерживает вложенность глубже {MAX_TASK_NEST_LEVELS} "
                         "уровней (считая от корневой задачи): "
                         + "; ".join(depth_refused))
        if failed:
            extra = "; ".join(f"{k[:8]}…: {v}" for k, v in api_fail.items())
            lines.append(f"❌ НЕ вложено {len(failed)} (parentId не применился"
                         + (f"; TickTick сообщил: {extra}" if extra else "")
                         + "): " + ", ".join(f"«{t}»" for t in failed))
        if mismatch:
            lines.append(_mismatch_report(mismatch, "вложил"))
        if missing:
            lines.append(f"↷ Не найдены среди открытых {len(missing)}: "
                         + ", ".join(f"«{m['title']}»" for m in missing))
        if ok_items:
            rid = _op_journal("parent", [
                {"taskId": f["taskId"], "title": f["title"],
                 "expect": {"parentId": parent_task_id}} for f in ok_items], summary)
            lines.append(_report_line(rid))
        return "\n".join(lines) if lines else "Ничего не вложено."
    except Exception as e:
        logger.exception("Error in set_task_parent")
        return _tool_error("nesting tasks", e)

def _describe_unset_task_parent(p: Dict) -> str:
    return (f'Отцепляю «{p.get("task_title")}» от родителя '
            f'«{p.get("parent_task_title")}»')


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def unset_task_parent(task_title: str, parent_task_title: str, task_id: str,
                            parent_task_id: str, project_id: str,
                            manifest_id: str = "", user_reply: str = "",
                            automation_key: str = "") -> str:
    """
    Detach a subtask from its parent, making it a top-level task (requires v2
    API). Gated 🟡 (docs/DESIGN_approval_gate.md): two calls, same tool name —
    nothing is changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is detached yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        task_title: Title of the subtask being detached (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        parent_task_title: Title of its current parent task
        task_id: ID of the subtask to detach
        parent_task_id: ID of its current parent
        project_id: ID of the project both tasks live in
        manifest_id: from call #1's response — pass on call #2 to actually detach
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    task_id and task_title (the SUBTASK being detached) are cross-checked
    against the LIVE task list TWICE (same pattern as delete_habit, def-116,
    2026-08-07): once while BUILDING the plan (call #1, before anything is
    shown to the owner — a mismatched title never reaches the plan card at
    all) and again, independently, right before the actual detach (call #2,
    unchanged). If the live read itself fails while building the plan, the
    plan still gets built (a read hiccup must not block every detach), but
    its text says so honestly — the call #2 check is unconditional and still
    guards the mutation either way.

    parent_task_id and parent_task_title (the claimed PARENT — NOT the
    subtask being detached) are cross-checked against the LIVE task list
    TWICE (def-126, 2026-08-07): once while BUILDING the plan (call #1,
    before anything is shown to the owner — a mismatched parent name never
    reaches the plan card at all) and again, independently, right before the
    actual detach (call #2, inside `_unset_task_parent_impl`). Unlike
    set_task_parent (where a MISSING parent is refused outright, because
    nesting under a dead parent orphans the child), a missing/unreadable
    PARENT here does NOT block: the whole point of this tool is to sever a
    link, and a parent that is completed/deleted (so it no longer shows up
    live) is exactly the ordinary case for wanting to detach from it — the
    subtask's own live parentId is still cross-checked against
    parent_task_id further down (_unset_task_parent_impl, unchanged), so the
    RELATIONSHIP itself is never taken on faith either way. Only a live
    parent that resolves to a DIFFERENT name than claimed is refused (🛑) —
    that is the actual "owner approved a card with the wrong name" defect
    this closes. A read hiccup while building the plan does not block either
    (fail-open, honestly noted in the card); the execution-side check is
    unconditional either way.

    The two guards above are INDEPENDENT and BOTH run on call #1 — the
    subtask's own name and the parent's name are separate claims on the plan
    card («Отцепляю «X» от родителя «Y»»), and each can be wrong on its own.
    Whichever soft warnings they produce are BOTH appended to that card (see
    `describe_fn` below): a card that could only carry one of them would
    silently hide the other from the owner.
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (task_id↔task_title, ТОЛЬКО субтаска, который
    # отцепляем — родителя проверяет ВТОРОЙ блок ниже, def-126) на построение
    # плана — тот же _guard_task, что уже стоит в _unset_task_parent_impl
    # НА ИСПОЛНЕНИИ.
    # mismatch И missing здесь блокируют план целиком — _unset_task_parent_impl
    # уже трактует ОБА как 🛑 на исполнении, перенос это не ужесточает.
    # Временная недоступность живого чтения — fail-open с предупреждением, а
    # исполнение (не тронуто) перепроверит заново. automation_key НЕ
    # пропускает эту проверку — она стоит раньше самого гейта.
    name_warning = ""
    if not manifest_id:
        g = _guard_task(task_id, task_title or "", project_id)
        refusal, warn = _guard_or_refuse(
            g, stage="план", expected=task_title,
            unavailable_note=_UNVERIFIED_TASK_TITLE)
        if refusal:
            return refusal
        name_warning = f" {warn}" if warn else ""
    # def-126: parent_task_id/parent_task_title (заявленный РОДИТЕЛЬ) не
    # сверялись НИ С ЧЕМ — ни здесь, ни в _unset_task_parent_impl, где есть
    # только проверка СВЯЗИ («live parentId субтаска == parent_task_id»), а не
    # сверка имя↔id самого родителя. Карточка «Отцепляю «X» от родителя «Y»»
    # могла нести любое Y. Добавляем ТОТ ЖЕ _guard_task, что уже используется
    # для субтаска в других инструментах, но здесь — для родителя, ДО
    # построения плана. Строгость: mismatch (родитель жив и назван иначе) —
    # 🛑, план не строится (владелец не должен одобрять по чужому имени).
    # missing/unavailable — сознательно НЕ блокируют: unset_task_parent
    # РАЗРЫВАЕТ связь, а не создаёт её (в отличие от set_task_parent, где
    # missing родитель — 🛑, «вложение под мёртвого родителя осиротит
    # задачи»); родитель, который завершён/удалён (потому и не резолвится
    # живьём), — обычный, ожидаемый повод отцепить subtask именно от него, и
    # блокировать это значило бы придумать более жёсткую политику, чем
    # принята в соседних методах. Имя в этом случае просто нельзя сверить —
    # карточка честно предупреждает, а связь (id) всё равно перепроверяется
    # ниже, в _unset_task_parent_impl. automation_key эту проверку не
    # обходит — она стоит раньше самого гейта (_gate_single).
    parent_name_warning = ""
    if not manifest_id:
        pg = _guard_task(parent_task_id, parent_task_title or "", project_id)
        refusal, warn = _guard_or_refuse(
            pg, stage="план", expected=parent_task_title, parent=True,
            missing_note=_PARENT_GONE_NOTE,
            unavailable_note=_UNVERIFIED_PARENT_NAME)
        if refusal:
            return refusal
        parent_name_warning = f" {warn}" if warn else ""
    params = {"task_title": task_title, "parent_task_title": parent_task_title,
              "task_id": task_id, "parent_task_id": parent_task_id,
              "project_id": project_id}
    # ОБА мягких предупреждения складываются в ОДНУ карточку плана. Здесь
    # сошлись две независимые правки (def-125 — имя субтаска, def-126 — имя
    # родителя), и каждая приносила СВОЁ присваивание describe_fn с одним и
    # тем же именем. Два присваивания подряд Python молча схлопнул бы в
    # последнее, и одно из предупреждений исчезло бы из карточки, которую
    # читает владелец, — без конфликта при мерже, без падения тестов (каждый
    # набор тестов проверяет только своё предупреждение) и вообще без единого
    # признака. Поэтому присваивание РОВНО ОДНО и складывает оба слагаемых
    # безусловно: при пустых warning'ах текст побайтово равен
    # _describe_unset_task_parent(p). Не разделять обратно на две ветки.
    # ЖЁСТКИЕ блокировки (🛑) обеих правок этим не затронуты — они
    # возвращаются выше и независимо друг от друга.
    def describe_fn(p):
        return (_describe_unset_task_parent(p)
                + name_warning + parent_name_warning)
    outcome = await _gate_single("unset_task_parent", "unset_task_parent",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _unset_task_parent_impl(**outcome.extra)


async def _unset_task_parent_impl(task_title: str, parent_task_title: str,
                                  task_id: str, parent_task_id: str,
                                  project_id: str) -> str:
    """Pure mutation logic for unset_task_parent — no consent gate. Called
    only by the gated unset_task_parent() above once the plan is approved."""
    try:
        by_id = _open_by_id(fresh=True)
        if by_id is None:
            return _STATE_UNAVAILABLE_MSG
        g = _guard_task(task_id, task_title or "", project_id, by_id=by_id)
        refusal, _warn = _guard_or_refuse(
            g, stage="исполнение", verb="НЕ отцепил", expected=task_title)
        if refusal:
            return refusal
        live_parent = (by_id.get(task_id) or {}).get("parentId")
        if not live_parent:
            return (f"↷ «{task_title}» и так не является подзадачей — "
                    "отцеплять нечего. Ничего не тронул.")
        if live_parent != parent_task_id:
            real_pname = (by_id.get(live_parent) or {}).get("title") or live_parent
            return (f"🛑 НЕ отцепил — «{task_title}» является подзадачей "
                    f"«{real_pname}», а НЕ «{parent_task_title}». Ничего не тронул.")
        # def-126, вторая линия защиты: связь id↔id выше (live_parent ==
        # parent_task_id) подтверждает, что это ДЕЙСТВИТЕЛЬНО текущий
        # родитель — но само ИМЯ parent_task_title до сих пор не сверялось ни
        # с чем (проверка выше — это сверка id с id, не имени с id). Между
        # планом (call #1) и подтверждением (call #2) проходит до часа —
        # состояние могло смениться, поэтому перепроверяем заново тем же
        # _guard_task, что и на плане. Строгость СИММЕТРИЧНА плану: mismatch
        # (родитель жив, но назван иначе) — 🛑, ничего не мутируем; missing
        # (родитель завершён/удалён — обычный повод отцеплять именно от
        # него, см. docstring unset_task_parent) молча пропускаем, связь уже
        # подтверждена по id.
        pg = _guard_task(parent_task_id, parent_task_title or "", project_id,
                         by_id=by_id)
        refusal, _warn = _guard_or_refuse(
            pg, stage="исполнение", verb="НЕ отцепил",
            expected=parent_task_title, parent=True, shield=True,
            missing_says="skip")
        if refusal:
            return refusal
        resp = await _run_blocking(lambda: ticktick_v2.unset_task_parent(
            task_id, live_parent, g.project_id or project_id))
        api_err = id2error_failures(resp, [task_id]).get(task_id)
        rid = _op_journal("parent", [{"taskId": task_id, "title": task_title,
                                      "expect": {"parentId": None}}],
                          f"Отцепить «{task_title}»")
        if api_err:
            return (f"❌ НЕ отцепил «{task_title}» — TickTick отклонил: {api_err}\n"
                    + _report_line(rid))
        # Post-verify: the live parentId must actually be gone. Retried (see
        # _reread_open_until) — same race class as move_tasks, found live
        # 2026-08-06: an immediate re-read can still show the pre-mutation
        # parentId for a moment right after the SAME v2 API cleared it.
        fresh = await _run_blocking(
            _reread_open_until,
            lambda m: not (m.get(task_id) or {}).get("parentId"))
        if fresh is None:
            return (f"Отцепление «{task_title}» отправлено, но {_UNVERIFIED_MSG}\n"
                    + _report_line(rid))
        if (fresh.get(task_id) or {}).get("parentId"):
            return (f"❌ НЕ отцепил «{task_title}» — parentId всё ещё стоит.\n"
                    + _report_line(rid))
        return (f"✅ «{task_title}» отцеплена от «{parent_task_title}» (проверено).\n"
                + _report_line(rid))
    except Exception as e:
        logger.exception("Error in unset_task_parent")
        return _tool_error("detaching subtask", e)


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def set_task_tags(summary: str, tasks: List[Dict[str, Any]] = None,
                        manifest_id: str = "", user_reply: str = "",
                        automation_key: str = "") -> str:
    """
    Replace tags on one or more tasks in one call (requires v2 API). Gated
    🟡 (docs/DESIGN_approval_gate.md): two calls, same tool name —
    nothing is changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest from `tasks`
    and returns a preview of the new tags per task — nothing is changed yet.
    Any tag that doesn't exist in the account yet is flagged in the preview
    as "будет создан" (will be created): TickTick keeps tags in two places —
    the account's own tag list and a raw label on the task — so a brand-new
    name is registered in the account's tag list FIRST (same path as
    create_tag), then applied to the task(s). This avoids creating an orphan
    tag that's invisible to list_tags and undeletable via delete_tag.
    Call #2 (after the user actually replied): repeat the call with
    manifest_id=<id from call #1> and user_reply=<the user's literal last
    message> — `tasks` is ignored on this call (the manifest's own stored
    items are used). Do NOT make call #2 in the same turn as call #1.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user, e.g. «Ставлю тег „работа" на
    задачу „Купить молоко"» or «Ставлю тег „работа" на 4 задачи».

    Each item carries the task's human title (for the dialog) and the full
    list of tags it should have (replaces existing):
    [{"title": "Buy milk", "taskId": "abc", "tags": ["errand", "today"]}]

    {{AUTOMATION_KEY_NOTE}}

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title","taskId","tags"} objects — required on call
            #1, ignored on call #2
        manifest_id: from call #1's response — pass on call #2 to actually retag
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err
    # Preview-time only (call #1, non-empty tasks): flag tags that don't
    # exist yet, so the user sees "will be created" BEFORE approving, not
    # just after the fact. Skipped on call #2 and on an empty/missing
    # `tasks` — _gate_batch refuses those without needing a tag lookup.
    #
    # Чтение живого списка тегов здесь — best-effort, и ронять фазу ПЛАНА оно
    # не имеет права: план обязан строиться, даже когда живое состояние
    # недоступно (клиент не поднят, сеть легла). Это НЕ ослабление
    # fail-closed: пометка «тег будет создан» — информационная, а настоящая
    # защита от тега-сироты стоит в _set_task_tags_impl, где отсутствующий
    # тег регистрируется в аккаунте ПЕРЕД записью на задачу — и там
    # недоступность состояния уже честно останавливает операцию.
    #
    # `tags_known` (круг 8) — тот же разбор фактa и сомнения, что у сверки
    # строк. Пока флага не было, упавшее чтение оставляло `existing_tags`
    # ПУСТЫМ, и правило «нет в списке → будет создан» печатало «тег не
    # существует — будет создан» РОВНО ПРО ВСЕ теги, включая давно
    # существующие: незнание выдавалось за факт, причём за противоположный
    # действительности (комментарий выше обещал, что пометка «не
    # показывается», — она показывалась на всех). Теперь при неудавшемся
    # чтении не печатается ни одна пометка, а сомнение уходит отдельной ⚠️
    # строкой плана.
    existing_tags: set = set()
    tags_known = True
    if not manifest_id and tasks:
        try:
            existing_tags = set(await _live_tag_names(force=True))
        except Exception as e:
            tags_known = False
            logger.warning(f"set_task_tags: не удалось прочитать список тегов "
                           f"для превью плана ({e}) — пометка «будет создан» "
                           "в этом плане не показывается")
    # Живые названия для строк, которым вызывающий их не дал: карточка
    # обязана называть задачу, а не показывать её id (_plan_task_name).
    titles = _plan_task_titles(tasks) if not manifest_id else {}

    def _describe_tags(t: Dict) -> str:
        wanted = t.get("tags") or []
        parts = []
        for tag in wanted:
            bare = tag.lstrip("#").lower()
            if tags_known and bare and bare not in existing_tags:
                parts.append(f"{tag} (тег не существует — будет создан)")
            else:
                parts.append(tag)
        return (f"**{_plan_task_name(t, titles)}** → теги: "
                + (", ".join(parts) or "(пусто)"))

    # Сверка с живым состоянием на фазе ПЛАНА — см. `_plan_live_check`.
    describe, notes = _describe_tags, None
    if not manifest_id and tasks:
        describe, notes, refusal = _plan_live_check(tasks, _describe_tags)
        if refusal:
            return refusal
        if not tags_known:
            notes = (notes or []) + [
                "⚠️ ПРОВЕРИТЬ, СУЩЕСТВУЮТ ЛИ ЭТИ ТЕГИ, НЕ УДАЛОСЬ (список "
                "тегов аккаунта не прочитался) — какой-то из них может быть "
                "новым и будет заведён в аккаунте при исполнении."]
    outcome = await _gate_batch(
        "tags", "set_task_tags", tasks, summary, manifest_id, user_reply,
        describe, notes=notes, automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _set_task_tags_impl(outcome.summary, outcome.tasks)


class _TagsOutcome:
    """ФАКТЫ одной простановки тегов — что зарегистрировано, что реально
    легло на задачи, что канал отклонил. Без единой строки отчёта.

    Зачем отдельный тип (2026-08-09, д6). Раньше ВСЯ логика тегов —
    регистрация тега в аккаунте (защита от «тега-сироты»), чтение отказов из
    id2error и живая сверка «что просили = что стало» — жила внутри
    `_set_task_tags_impl`, вперемешку со сборкой текста отчёта. Путь СОЗДАНИЯ
    задачи переиспользовать это не мог (ему нужен не готовый отчёт, а факты,
    которые он вплетёт в свою строку) и потому звал голый
    `ticktick_v2.set_task_tags` — без обеих проверок: теги при создании
    терялись молча. Ядро вынесено сюда, чтобы у обоих путей была ОДНА
    реализация, а не две похожие."""
    __slots__ = ("state_unavailable", "found", "mismatch", "missing",
                 "changes", "tags_by_id", "display_by_key", "registered",
                 "failed_register", "skipped", "applied", "failed",
                 "api_fail", "unverified")

    def __init__(self):
        self.state_unavailable = False  # живое состояние не прочиталось вовсе
        self.found, self.mismatch, self.missing = [], [], []
        self.changes = []               # то, что реально ушло в /batch/task
        self.tags_by_id = {}            # taskId -> запрошенный набор тегов
        self.display_by_key = {}        # ключ тега -> написание для человека
        self.registered = []            # новые теги, ЗАВЕДЁННЫЕ в аккаунте
        self.failed_register = []       # не удалось завести → не пишем никуда
        self.skipped = []               # [(title, [плохие теги])] — не тронуты
        self.applied = []               # titles: живьём видно запрошенный набор
        self.failed = []                # titles: НЕ видно (или отказ канала)
        self.api_fail = {}              # id2error по /batch/task
        self.unverified = False         # отправлено, но перечитать не удалось


async def _apply_tags_verified(tasks: List[Dict[str, Any]]) -> _TagsOutcome:
    """Set tags on tasks with BOTH guarantees the account expects, and report
    the facts (no formatting): every not-yet-existing tag is registered in the
    account tag list first (no orphan tags), per-item rejections inside the
    batch response are read (`id2error_failures`), and the live state is
    re-read afterwards so "applied" means observed, never assumed.

    The single implementation behind BOTH `set_task_tags` (the standalone
    gated tool) and the tagging step of task creation."""
    out = _TagsOutcome()
    by_id = _open_by_id(fresh=True)
    if by_id is None:
        out.state_unavailable = True
        return out
    found, mismatch, missing = _split_tasks_by_state(tasks, by_id=by_id)
    out.found, out.mismatch, out.missing = found, mismatch, missing
    ok = {f["taskId"]: f for f in found}
    # Normalise like the single-task path: TickTick keys tags by lowercase
    # bare name — a raw '#Работа' would create a phantom tag.
    changes = [{"taskId": t.get("taskId") or t.get("task_id"),
                "tags": [x.lstrip("#").lower() for x in (t.get("tags") or [])]}
               for t in tasks
               if (t.get("taskId") or t.get("task_id")) in ok]

    # TickTick keeps tags in TWO places: the account's tag list
    # (/batch/tag — what list_tags/create_tag/delete_tag see) and a raw
    # label string on each task (/batch/task). Writing a brand-new label
    # straight onto a task WITHOUT registering it in the account tag list
    # first creates an ORPHAN tag: invisible to list_tags, undeletable via
    # delete_tag, but still attached to the task. So: register every
    # not-yet-existing tag through the same account-level path create_tag
    # uses, BEFORE touching any task — and post-verify the registration
    # itself, not just assume the 200 meant it worked.
    requested = sorted({t for c in changes for t in c["tags"] if t})
    # Регистр (2026-08-07). У тега ДВА поля: `name` — ключ, всегда нижним
    # регистром (по нему тег ищется, он же пишется на задачу), и `label`
    # — написание, которое человек видит в списке тегов. Настоящий
    # `TickTickV2Client.create_tag` это уважает сам: он понижает `name` и
    # кладёт в `label` строку КАК ПЕРЕДАЛИ. Раньше сюда уходил уже
    # пониженный ключ — нормализация, нужная для записи на задачу,
    # утекала и в регистрацию, — и «Работа», заведённая постановкой на
    # задачу, оседала в аккаунте как «работа», хотя через create_tag то
    # же слово сохраняло регистр. Здесь запоминается первое встреченное
    # написание каждого ключа и отдаётся в регистрацию именно оно.
    display_by_key: Dict[str, str] = {}
    for t in tasks:
        for raw in (t.get("tags") or []):
            bare = str(raw).lstrip("#")
            if bare:
                display_by_key.setdefault(bare.lower(), bare)
    out.display_by_key = display_by_key
    # force=False: _open_by_id(fresh=True) just above already forced a
    # fresh sync-state fetch (tags included) within the TTL window — no
    # need for a second network round-trip for the same snapshot.
    existing_tags = set(await _live_tag_names(force=False))
    to_register = [t for t in requested if t not in existing_tags]
    for tag_name in to_register:
        try:
            shown = display_by_key.get(tag_name, tag_name)
            await _run_blocking(lambda tn=shown: ticktick_v2.create_tag(tn))
        except Exception:
            logger.exception(
                f"set_task_tags: auto-registration of tag '{tag_name}' "
                "raised")
    after_create = (set(await _live_tag_names(force=True))
                    if to_register else existing_tags)
    out.registered = [t for t in to_register if t in after_create]
    out.failed_register = [t for t in to_register if t not in after_create]

    # Fail closed for exactly the tags that couldn't be registered: drop
    # the WHOLE per-task change rather than send a truncated tag list —
    # set_task_tags REPLACES all tags on a task, so silently stripping
    # just the bad tag from a change could wipe tags the user never
    # asked to touch. The task is left completely untouched instead.
    if out.failed_register:
        bad_set = set(out.failed_register)
        kept = []
        for c in changes:
            bad = set(c["tags"]) & bad_set
            if bad:
                out.skipped.append((ok[c["taskId"]]["title"], sorted(bad)))
            else:
                kept.append(c)
        changes = kept
    out.changes = changes

    if changes:
        resp = await _run_blocking(
            lambda: ticktick_v2.batch_update_tasks(changes))
        out.api_fail = id2error_failures(resp, [c["taskId"] for c in changes])
    # Inline post-verify: live tags must equal the requested set, AND any
    # newly-registered tag must be visible in the account's own tag list
    # (list_tags) — this is the proof that (b) actually closed the
    # orphan hole, not just moved it.
    out.tags_by_id = {c["taskId"]: c["tags"] for c in changes}
    if changes:
        fresh = _open_by_id(fresh=True)
        if fresh is None:
            out.unverified = True
        else:
            for f in found:
                if f["taskId"] not in out.tags_by_id:
                    continue  # skipped above — never sent, don't verify
                want = set(out.tags_by_id.get(f["taskId"], []))
                got = set((fresh.get(f["taskId"]) or {}).get("tags") or [])
                ok_item = want == got and f["taskId"] not in out.api_fail
                (out.applied if ok_item else out.failed).append(f["title"])
    return out


def _tag_notes_for_create(out: _TagsOutcome) -> List[str]:
    """Short per-task notes about tagging, for the CREATE report.

    Same facts the standalone set_task_tags prints, condensed to the one task
    being created — silence here means the tags were verified live, never
    "we sent something and moved on"."""
    notes = []
    if out.state_unavailable:
        return ["⚠️ теги НЕ проставлены: живое состояние не прочиталось "
                "(перепроверь и поставь теги отдельно)"]
    if out.missing:
        return ["⚠️ теги НЕ проставлены: созданная задача не нашлась среди "
                "открытых при перепроверке"]
    if out.mismatch:
        return ["⚠️ теги НЕ проставлены: id созданной задачи указывает на "
                "другую задачу (сверка не прошла)"]
    if out.failed_register:
        notes.append(
            "⚠️ теги НЕ проставлены — не удалось завести их в аккаунте (иначе "
            "получился бы тег-сирота: не виден в list_tags, не удаляется "
            "delete_tag): "
            + ", ".join(f"«{out.display_by_key.get(t, t)}»"
                        for t in out.failed_register))
    if out.registered:
        notes.append(
            "🆕 новые теги заведены в аккаунте (проверено): "
            + ", ".join(f"«{out.display_by_key.get(t, t)}»"
                        for t in out.registered))
    if out.unverified:
        notes.append(f"⚠️ теги отправлены, но {_UNVERIFIED_MSG}")
    if out.failed:
        extra = "; ".join(f"{k[:8]}…: {v}" for k, v in out.api_fail.items())
        notes.append("⚠️ теги НЕ применились"
                     + (f" — TickTick сообщил: {extra}" if extra else
                        " (в живом состоянии их нет)"))
    if out.applied:
        shown = sorted({t for tags in out.tags_by_id.values() for t in tags})
        notes.append("🏷 теги проставлены (проверено): "
                     + ", ".join(f"«{t}»" for t in shown))
    return notes


async def _set_task_tags_impl(summary: str, tasks: List[Dict[str, Any]]) -> str:
    """Pure mutation logic for set_task_tags — no consent gate. Called
    only by the public gated set_task_tags() below."""
    err = _ensure_ready()
    if err:
        return err
    try:
        outcome = await _apply_tags_verified(tasks)
        if outcome.state_unavailable:
            return _STATE_UNAVAILABLE_MSG
        found, mismatch, missing = (outcome.found, outcome.mismatch,
                                    outcome.missing)
        # Имена локальных переменных ниже сохранены как были — отчёт ниже
        # не переписывался, у него ровно те же входные данные, просто теперь
        # они приходят из общего ядра, а не считаются здесь.
        registered = outcome.registered
        failed_register = outcome.failed_register
        skipped_tasks = outcome.skipped
        applied, failed = outcome.applied, outcome.failed
        api_fail = outcome.api_fail
        unverified = outcome.unverified
        changes = outcome.changes
        tags_by_id = outcome.tags_by_id
        display_by_key = outcome.display_by_key
        lines = []
        if applied:
            lines.append(f"🏷 Теги обновлены у {len(applied)} (проверено): "
                         + ", ".join(f"«{t}»" for t in applied))
        if registered:
            # Печатается написание, под которым тег ЗАВЕДЁН (то же, что
            # покажет list_tags), а не внутренний ключ — иначе отчёт
            # рассказывал бы про «работа» там, где в приложении «Работа».
            lines.append(
                f"🆕 Новые теги зарегистрированы в аккаунте (проверено — видны "
                f"в list_tags, удаляются delete_tag), {len(registered)}: "
                + ", ".join(f"«{display_by_key.get(t, t)}»" for t in registered))
        if failed_register:
            lines.append(
                f"⚠️ Не удалось зарегистрировать в аккаунте {len(failed_register)} "
                "тег(ов) — они НЕ проставлены ни на одну задачу (во избежание "
                "тега-сироты): "
                + ", ".join(f"«{display_by_key.get(t, t)}»" for t in failed_register))
        if skipped_tasks:
            parts = "; ".join(
                f"«{title}» (тег{'и' if len(bad) > 1 else ''} "
                + ", ".join(f"«{b}»" for b in bad) + ")"
                for title, bad in skipped_tasks)
            lines.append(f"🛑 Пропущено, задачи НЕ тронуты, {len(skipped_tasks)}: " + parts)
        if unverified:
            lines.append(f"Отправлено {len(changes)}, но {_UNVERIFIED_MSG}")
        if failed:
            extra = "; ".join(f"{k[:8]}…: {v}" for k, v in api_fail.items())
            lines.append(f"❌ Теги НЕ применились у {len(failed)}"
                         + (f" (TickTick сообщил: {extra})" if extra else "")
                         + ": " + ", ".join(f"«{t}»" for t in failed))
        if mismatch:
            lines.append(_mismatch_report(mismatch, "тегировал"))
        if missing:
            lines.append(f"↷ Не найдены среди открытых {len(missing)}: "
                         + ", ".join(f"«{m['title']}»" for m in missing))
        if changes:
            rid = _op_journal("tags", [
                {"taskId": f["taskId"], "title": f["title"],
                 "expect": {"tags": tags_by_id.get(f["taskId"], [])}}
                for f in found if f["taskId"] in tags_by_id], summary)
            lines.append(_report_line(rid))
        return "\n".join(lines) if lines else "Ничего не изменено."
    except Exception as e:
        logger.exception("Error in set_task_tags")
        return _tool_error("setting tags", e)# ---------------------------------------------------------------------------
# Batch operations (v2)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Builder helpers (no API call — produce strings for create_task/update_task)
# ---------------------------------------------------------------------------

# def-D3: BYDAY-токен по RFC 5545 — необязательный порядковый номер
# (±1..53, "2TU" = второй вторник периода, "-1FR" = последняя пятница) плюс
# двухбуквенный день недели. Раньше by_day уходил в правило как есть, так что
# опечатка ("MOND") давала синтаксически битое, но принятое правило.
_BYDAY_TOKEN = re.compile(r"^([+-]?(?:[1-9]|[1-4]\d|5[0-3]))?(MO|TU|WE|TH|FR|SA|SU)$")


@mcp.tool(annotations=READONLY)
async def build_recurrence_rule(frequency: str, interval: int = 1,
                                by_day: List[str] = None, count: int = None,
                                until: str = None, by_month_day: List[int] = None,
                                by_month: List[int] = None,
                                by_set_pos: List[int] = None) -> str:
    """
    Build an RRULE recurrence string to pass as repeat_flag in create_tasks/update_tasks.

    The FIRST line of the output is the rule itself — that is what goes into
    repeat_flag. Anything after it is a warning about a rule that is valid but
    probably not what was meant (read it, don't paste it).

    Args:
        frequency: DAILY, WEEKLY, MONTHLY, or YEARLY
        interval: Repeat every N units (default 1; must be >= 1)
        by_day: Days like ["MO","WE","FR"]. Works with ANY frequency, not just
            WEEKLY. An optional ordinal prefix picks the Nth such day of the
            period: "2TU" = 2nd Tuesday, "-1FR" = last Friday (MONTHLY/YEARLY).
        count: Stop after this many occurrences (optional). Mutually exclusive
            with `until` (RFC 5545 forbids both in one rule).
        until: Stop on this date YYYY-MM-DD (optional, INCLUSIVE). Relative
            words ("tomorrow"/"завтра", a weekday name) are resolved on this
            server's clock. The date is read as the END of that day in the
            OWNER's timezone (USER_TIMEZONE) and converted to UTC, so "until
            2026-08-31" really covers all of 31 August locally.
        by_month_day: Days of the month, 1..31 or -1..-31 counting back from
            its end (BYMONTHDAY). [-1] = last day of every month; [31] = the
            31st, which simply does not occur in short months.
        by_month: Months, 1..12 (BYMONTH). Needed for yearly rules like
            "2nd Tuesday of March": by_month=[3], by_day=["2TU"].
        by_set_pos: Pick the Nth match within each period (BYSETPOS), e.g.
            by_day=["MO","TU","WE","TH","FR"] + by_set_pos=[-1] = last weekday
            of the month. RFC 5545 requires another BY-rule alongside it.
    """
    freq = frequency.upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return "Invalid frequency. Use DAILY, WEEKLY, MONTHLY, or YEARLY."
    if count and until:
        return ("Invalid rule: COUNT and UNTIL cannot both be set (RFC 5545). "
                "Pass either count= (stop after N occurrences) or until= (stop on a date).")
    if interval < 1:
        # Раньше здесь стоял max(1, interval): 0 и -3 молча становились 1.
        return f"Invalid interval={interval}: must be 1 or greater."
    bad_days = [d for d in (by_day or []) if not _BYDAY_TOKEN.match(str(d).strip().upper())]
    if bad_days:
        return (f"Invalid by_day entries: {bad_days}. Use MO/TU/WE/TH/FR/SA/SU, "
                'optionally with an ordinal prefix ("2TU" = 2nd Tuesday, "-1FR" = last Friday).')
    bad_mdays = [d for d in (by_month_day or []) if not (1 <= abs(int(d)) <= 31 and int(d) != 0)]
    if bad_mdays:
        return (f"Invalid by_month_day entries: {bad_mdays}. Use 1..31, or -1..-31 "
                "to count back from the end of the month (-1 = last day).")
    bad_months = [m for m in (by_month or []) if not 1 <= int(m) <= 12]
    if bad_months:
        return f"Invalid by_month entries: {bad_months}. Use 1..12."
    bad_pos = [p for p in (by_set_pos or []) if int(p) == 0 or abs(int(p)) > 366]
    if bad_pos:
        return f"Invalid by_set_pos entries: {bad_pos}. Use -366..-1 or 1..366 (0 is not valid)."
    if by_set_pos and not (by_day or by_month_day or by_month):
        return ("Invalid rule: by_set_pos needs another BY-rule to pick from (RFC 5545). "
                'Add by_day (e.g. ["MO","TU","WE","TH","FR"] + by_set_pos=[-1] = '
                "last weekday of the month).")

    parts = [f"FREQ={freq}", f"INTERVAL={interval}"]
    if by_month:
        parts.append("BYMONTH=" + ",".join(str(int(m)) for m in by_month))
    if by_month_day:
        parts.append("BYMONTHDAY=" + ",".join(str(int(d)) for d in by_month_day))
    if by_day:
        parts.append("BYDAY=" + ",".join(str(d).strip().upper() for d in by_day))
    if by_set_pos:
        parts.append("BYSETPOS=" + ",".join(str(int(p)) for p in by_set_pos))
    if count:
        parts.append(f"COUNT={count}")
    if until:
        # def-D2: раньше здесь было `until.replace("-", "") + "T000000Z"` —
        # календарная дата владельца объявлялась ПОЛНОЧЬЮ UTC. В
        # America/Los_Angeles «до 31 августа» становилось 17:00 30 августа по
        # местному: правило обрывалось на ~7 часов раньше ожидаемого, молча.
        # Теперь дата читается как КОНЕЦ этого дня в таймзоне владельца и
        # честно переводится в UTC (RFC 5545 требует UNTIL в UTC при
        # Z-форме), а неразобранная дата отвергается вместо мусора в правиле.
        until_resolved = _resolve_relative_date(until.strip())
        try:
            d = datetime.strptime(until_resolved, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return (f"Invalid until={until!r}: expected a date as YYYY-MM-DD "
                    "(or a relative word like 'tomorrow' / 'завтра' / a weekday name).")
        end_local = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=_USER_TZ)
        parts.append("UNTIL=" + end_local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))

    rule = "RRULE:" + ";".join(parts)

    # def-D3: правило может быть синтаксически безупречным и при этом значить
    # не то, что просили. Такие случаи не переписываются молча — они
    # называются вслух ПОД правилом (первая строка остаётся чистым RRULE).
    warnings = []
    if freq == "YEARLY" and not by_month and any(
            _BYDAY_TOKEN.match(str(d).strip().upper()).group(1) for d in (by_day or [])):
        warnings.append(
            '⚠️ YEARLY + ordinal by_day without by_month: "2TU" here means the 2nd Tuesday '
            "of the YEAR, not of a month. Add by_month=[3] for \"2nd Tuesday of March\".")
    short_days = sorted({int(d) for d in (by_month_day or []) if int(d) in (29, 30, 31)})
    if short_days:
        warnings.append(
            f"⚠️ by_month_day={short_days}: months that are shorter simply have no such day, "
            "so the repeat is SKIPPED there (RFC 5545) — February never fires on 30/31. "
            "Use by_month_day=[-1] if you meant \"the last day of every month\".")
    if not warnings:
        return rule
    return rule + "\n\n" + "\n".join(warnings)


@mcp.tool(annotations=READONLY)
async def build_reminder(minutes_before: int = 0) -> str:
    """
    Build a reminder TRIGGER string to pass in the reminders list of create_tasks/update_tasks.

    Args:
        minutes_before: Minutes before the due time to remind. 0 = at the time
            of the event. Must not be negative — a negative value is rejected
            with an error instead of being silently treated as 0 (reminders
            AFTER the due time are not supported here).
    """
    if minutes_before < 0:
        # def-D1: раньше любое отрицательное значение молча становилось
        # "TRIGGER:PT0S" — ошибка в знаке превращалась в «напомнить ровно в
        # момент события» и выглядела как успех. Ошибочный ввод должен быть
        # отвергнут, а не подменён на ближайший возможный.
        return (f"Invalid minutes_before={minutes_before}: must be 0 or positive. "
                "0 = remind at the due time; a positive number = that many minutes "
                "before it. Reminders after the due time are not supported.")
    if minutes_before == 0:
        return "TRIGGER:PT0S"
    if minutes_before % (24 * 60) == 0:
        return f"TRIGGER:-P{minutes_before // (24 * 60)}D"
    if minutes_before % 60 == 0:
        return f"TRIGGER:-PT{minutes_before // 60}H"
    return f"TRIGGER:-PT{minutes_before}M"


# ---------------------------------------------------------------------------
# Smart-list execution (v2)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READONLY)
async def run_filter(filter: str, limit: int = _TREE_PAGE, offset: int = 0) -> str:
    """
    Run a saved smart-list filter and return the open tasks it matches (requires v2 API).

    The output is paged: the header always states the TOTAL number of matches
    and which range is shown, and when they don't fit, the last line says how
    many top-level tasks are left and which offset continues the list.
    Subtasks always travel with their parent, so no page ever shows a subtask
    torn off the task it belongs to.

    Args:
        filter: Filter name or ID (from list_filters)
        limit: Maximum tasks per call (default 200, same page size as
            get_all_tasks) — a soft ceiling: the subtree that crosses it is
            finished rather than cut in half
        offset: Skip this many TOP-LEVEL matches — use it to read the tail; the
            footer prints the exact offset that continues the list
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        tasks, unsupported = await _run_blocking(
            lambda: ticktick_v2.run_filter_detailed(filter))
        # A condition this server cannot evaluate narrows NOTHING, so without
        # this warning an unfiltered pool reads as a filtered result — exactly
        # what happened live on 2026-08-07, when «For me» (an `assignee` rule)
        # returned all 1477 open tasks and looked like a legitimate answer.
        warning = ""
        if unsupported:
            names = ", ".join(f"«{c}»" for c in unsupported)
            warning = (f"⚠️ Условие {names} этот сервер вычислять не умеет — "
                       f"фильтрация по нему НЕ применялась, поэтому в списке "
                       f"могут быть лишние задачи.\n\n")
        if not tasks:
            return warning + f"Filter '{filter}' matched no open tasks."
        # def-E1 (2-я волна): раньше сюда уходил ВЕСЬ пул, а format_task_tree
        # молча резал его на своём потолке в 200 строк и приписывал
        # «... and 1236 more.» — то есть инструмент сам сообщал, что 86 %
        # найденного отброшено, и не давал способа это дочитать. Срез теперь
        # считается по КОРНЯМ и уезжает вместе с поддеревьями, чтобы подзадача
        # не осиротела на границе страницы (_page_task_forest).
        page, total_roots, offset, shown_to = _page_task_forest(tasks, limit, offset)
        if not page:
            # Пустая страница за концом списка — это НЕ «фильтр ничего не нашёл».
            # Страницы плавающие (поддерево дописывается целиком), поэтому
            # называется диапазон допустимых offset, а не «начало последней».
            return (warning + f"Filter '{filter}' — {len(tasks)} task(s) "
                    f"({total_roots} top-level), but offset={offset} is past the "
                    f"end ({_valid_offset_range(total_roots)}).")
        if offset or shown_to < total_roots:
            out = (warning + f"Filter '{filter}' — {len(tasks)} task(s) "
                   f"({total_roots} top-level; showing top-level "
                   f"{offset + 1}-{shown_to} with their subtasks):\n\n")
        else:
            out = warning + f"Filter '{filter}' — {len(tasks)} task(s):\n\n"
        # Потолок дерева = размер уже нарезанной страницы: резать второй раз
        # (и снова печатать «... and N more.») больше нечего.
        out += format_task_tree(page, max(len(page), 1))
        if shown_to < total_roots:
            out += (f"\n... and {total_roots - shown_to} more top-level tasks — "
                    f"call again with offset={shown_to}.\n")
        return out
    except Exception as e:
        logger.exception("Error in run_filter")
        return _tool_error("running filter", e)


# ---------------------------------------------------------------------------
# Project groups / folders (v2)
# ---------------------------------------------------------------------------

_GROUP_MEMBERS_CAP = 12  # project names listed per folder before "+N more"


def _group_members_suffix(projects_in_group: List[str]) -> str:
    """" — 2 projects: Дом, Финансы" / " — empty" for one folder line."""
    if not projects_in_group:
        return " — empty"
    shown = projects_in_group[:_GROUP_MEMBERS_CAP]
    extra = len(projects_in_group) - len(shown)
    tail = f", +{extra} more" if extra else ""
    word = "project" if len(projects_in_group) == 1 else "projects"
    return f" — {len(projects_in_group)} {word}: " + ", ".join(shown) + tail


@mcp.tool(annotations=READONLY)
async def list_project_groups() -> str:
    """List project groups (folders) with the projects inside each one, plus
    a count of the projects that are in no folder (requires v2 API).

    Both lists come from the same cached v2 sync snapshot, so showing the
    contents costs no extra network request.
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        groups = await _run_blocking(lambda: ticktick_v2.list_project_groups())
        groups = [g for g in groups if not g.get("deleted")]
        live_ids = {g.get("id") for g in groups}
        # Membership comes from the same cached snapshot the groups just came
        # from — no extra fetch. If it can't be read, the folder list itself
        # still renders (degraded, without contents) rather than erroring out.
        members: Dict[str, List[str]] = {}
        ungrouped = orphaned = 0
        have_members = True
        try:
            projects = await _run_blocking(lambda: ticktick_v2.list_projects())
            for p in (projects or []):
                if p.get("deleted"):
                    continue
                gid = p.get("groupId")
                if not gid or gid == "NONE":
                    ungrouped += 1
                elif gid in live_ids:
                    members.setdefault(gid, []).append(p.get("name") or "?")
                else:
                    orphaned += 1  # points at a folder that no longer exists
        except Exception as e:
            logger.warning(f"list_project_groups: membership unavailable: {e}")
            have_members = False
        if not groups:
            return "No project groups found."
        lines = [f"- {g.get('name','?')}  (id: {g.get('id')})"
                 + (_group_members_suffix(members.get(g.get("id"), []))
                    if have_members else "")
                 for g in groups]
        out = f"Project groups ({len(groups)}):\n\n" + "\n".join(lines)
        if have_members:
            out += f"\n\n{ungrouped} project(s) in no folder."
            if orphaned:
                out += (f"\n{orphaned} project(s) point at a folder that no "
                        "longer exists.")
        return out
    except Exception as e:
        logger.exception("Error in list_project_groups")
        return _tool_error("fetching project groups", e)


async def _live_groups(fresh: bool = True) -> List[Dict]:
    """Non-deleted project groups from the (optionally force-fresh) v2 state."""
    if fresh:
        await _run_blocking(lambda: ticktick_v2.get_state(force=True))
    groups = await _run_blocking(lambda: ticktick_v2.list_project_groups())
    return [g for g in groups if not g.get("deleted")]


def _describe_create_project_group(p: Dict) -> str:
    return f'Создаю папку проектов «{p.get("name")}»'


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def create_project_group(name: str, manifest_id: str = "",
                               user_reply: str = "",
                               automation_key: str = "") -> str:
    """
    Create a project group (folder) (requires v2 API). Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    created on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is created yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — `name` is ignored on this
    call (the manifest's own stored value is used). Do NOT make call #2 in
    the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        name: Name of the new group (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        manifest_id: from call #1's response — pass on call #2 to actually create
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"name": name}
    outcome = await _gate_single("create_project_group", "create_project_group",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, _describe_create_project_group,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _create_project_group_impl(**outcome.extra)


async def _create_project_group_impl(name: str) -> str:
    """Pure mutation logic for create_project_group — no consent gate. Called
    only by the gated create_project_group() above once the plan is approved."""
    try:
        gid = await _run_blocking(lambda: ticktick_v2.create_project_group(name))
    except RuntimeError as e:
        return f"❌ Группа «{name}» НЕ создана — TickTick отклонил: {_redact_for_user(e)}"
    except Exception as e:
        logger.exception("Error in create_project_group")
        return _tool_error("creating project group", e)
    # Post-verify: the new group must appear in the force-refreshed list.
    try:
        groups = await _live_groups()
        if not any(g.get("id") == gid for g in groups):
            return (f"❌ Группа «{name}» НЕ подтвердилась — её нет в списке "
                    "групп после создания, проверь вручную.")
    except Exception as e:
        return f"Группа «{name}» отправлена (id: {gid}), но {_UNVERIFIED_MSG} ({_redact_for_user(e)})"
    return f"✅ Группа проектов «{name}» создана (проверено). (id: {gid})"


def _describe_delete_project_group(p: Dict) -> str:
    return (f'Удаляю папку проектов «{p.get("group_name")}» (сами проекты '
            "останутся, просто без папки)")


def _group_members_note(projects: Optional[List[Dict]], group_id: str) -> str:
    """Что именно лежит внутри папки — приписка к КАРТОЧКЕ её удаления.

    Живая приёмка 2026-08-07: папка с одним проектом и папка с двумя давали
    ПОБАЙТОВО одинаковый текст плана — ни числа, ни имён, пустая папка
    неотличима от полной. Образец обратного уже в этом файле:
    plan_task_deletion для задачи с подзадачами разворачивает всё поддерево
    и показывает список на одобрение. Ситуация та же — действие над
    контейнером.

    `projects` — сырой список проектов из живого снапшота, или None, когда
    прочитать его не удалось: None печатается ВСЛУХ как неизвестность и
    НИКОГДА не выдаётся за пустую папку (это разные вещи для того, кто
    решает, нажимать ли). Список режется тем же _GROUP_MEMBERS_CAP, что и
    вывод list_project_groups, но остаток называется числом, а не молча
    отбрасывается."""
    if projects is None:
        return (" ⚠️ Состав папки прочитать НЕ УДАЛОСЬ — какие проекты внутри "
                "и сколько их, неизвестно.")
    names = [p.get("name") or "?" for p in projects
             if not p.get("deleted") and p.get("groupId") == group_id]
    if not names:
        return " Папка пуста — внутри нет ни одного проекта."
    shown = names[:_GROUP_MEMBERS_CAP]
    tail = f" и ещё {len(names) - len(shown)}" if len(names) > len(shown) else ""
    word = _ru_plural(len(names), "проект", "проекта", "проектов")
    return (f" Внутри {len(names)} {word}: "
            + ", ".join(f"«{n}»" for n in shown) + tail + ".")


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def delete_project_group(group_name: str, group_id: str,
                               manifest_id: str = "", user_reply: str = "",
                               automation_key: str = "") -> str:
    """
    Delete a project group/folder (projects inside are kept, just ungrouped)
    (requires v2 API). Gated 🟡 (docs/DESIGN_approval_gate.md): two calls,
    same tool name — nothing is changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is deleted yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        group_name: Name of the group (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        group_id: ID of the group
        manifest_id: from call #1's response — pass on call #2 to actually delete
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    group_id and group_name are cross-checked against the LIVE group list
    TWICE (same pattern as delete_habit, def-116, 2026-08-07): once while
    BUILDING the plan (call #1, before anything is shown to the owner — a
    mismatched or gone group never reaches the plan card at all) and again,
    independently, right before the actual delete (call #2, unchanged). If
    the live read itself fails while building the plan, the plan still gets
    built (a read hiccup must not block every deletion), but its text says
    so honestly — the call #2 check is unconditional and still guards the
    mutation either way.

    The plan card also NAMES the projects currently inside the folder (count
    + names, long lists capped with "и ещё N"), so an empty folder can never
    read the same as a folder holding eight live projects. If that list
    can't be read, the card says exactly that instead of looking empty.
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (group_id↔group_name) на построение плана — та
    # же сверка (_live_groups + _names_agree), что уже стоит в
    # _delete_project_group_impl НА ИСПОЛНЕНИИ, но здесь — ДО показа карточки
    # владельцу, до необратимого удаления. missing И mismatch блокируют план
    # целиком — _delete_project_group_impl уже трактует ОБА как 🛑 на
    # исполнении, перенос это не ужесточает. Временная недоступность живого
    # чтения (исключение при _live_groups()) — fail-open с предупреждением, а
    # исполнение (не тронуто) перепроверит заново. automation_key НЕ
    # пропускает эту проверку — она стоит раньше самого гейта.
    name_warning = ""
    if not manifest_id:
        try:
            groups = await _live_groups()
        except Exception as e:
            groups = None
            logger.warning("delete_project_group: не удалось прочитать "
                           f"список групп на этапе плана ({e}) — план всё "
                           "равно строится, сверка имени повторится на "
                           "исполнении.")
        if groups is not None:
            grp = next((g for g in groups if g.get("id") == group_id), None)
            if grp is None:
                return (f"🛑 План НЕ построен — группы с id "
                        f"{str(group_id)[:12]}… нет в живом списке групп "
                        "(уже удалена/неверный id). Ничего не изменено.")
            real = grp.get("name") or ""
            if not _names_agree(group_name, real):
                return (f"🛑 План НЕ построен — group_id указывает на "
                        f"«{real}», а НЕ «{group_name}» (защита от «не той "
                        "папки»). Ничего не изменено.")
        else:
            name_warning = (" ⚠️ Имя папки НЕ удалось сверить с живым списком "
                            "групп (чтение не удалось) — сверка повторится "
                            "при подтверждении, и расхождение остановит "
                            "удаление.")
    # Состав папки — В КАРТОЧКУ. Читается из ТОГО ЖЕ живого снапшота, что
    # _live_groups() выше уже обновил (list_projects() берёт projectProfiles
    # из кэша get_state), поэтому сетевого запроса это не добавляет — тот же
    # довод, по которому list_project_groups показывает состав папок «at no
    # extra network request». Приписка не гейт: она ничего не блокирует,
    # неудачное чтение печатается как неизвестность (см. _group_members_note)
    # и НЕ выдаётся за пустую папку.
    members_note = ""
    if not manifest_id:
        try:
            projects = await _run_blocking(lambda: ticktick_v2.list_projects())
        except Exception as e:
            projects = None
            logger.warning("delete_project_group: не удалось прочитать состав "
                           f"папки на этапе плана ({e}) — план всё равно "
                           "строится, но состав в нём назван неизвестным.")
        members_note = _group_members_note(projects, group_id)
    params = {"group_name": group_name, "group_id": group_id}
    plan_note = members_note + name_warning
    describe_fn = ((lambda p: _describe_delete_project_group(p) + plan_note)
                   if plan_note else _describe_delete_project_group)
    outcome = await _gate_single("delete_project_group", "delete_project_group",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _delete_project_group_impl(**outcome.extra)


async def _delete_project_group_impl(group_name: str, group_id: str) -> str:
    """Pure mutation logic for delete_project_group — no consent gate. Called
    only by the gated delete_project_group() above once the plan is approved."""
    try:
        # Identity guard (fresh): the id must exist AND resolve to the name.
        groups = await _live_groups()
        grp = next((g for g in groups if g.get("id") == group_id), None)
        if grp is None:
            return (f"🛑 НЕ удалил — группы с id {str(group_id)[:12]}… нет в "
                    "живом списке групп (уже удалена/неверный id). Ничего не тронул.")
        real = grp.get("name") or ""
        if not _names_agree(group_name, real):
            return (f"🛑 НЕ удалил — group_id указывает на «{real}», а НЕ "
                    f"«{group_name}» (защита от «не той папки»). Ничего не тронул.")
        resp = await _run_blocking(lambda: ticktick_v2.delete_project_group(group_id))
        api_err = id2error_failures(resp, [group_id]).get(group_id)
        if api_err:
            return f"❌ Группа «{real}» НЕ удалена — TickTick отклонил: {api_err}"
        # Post-verify: the group must be gone from the fresh list.
        groups = await _live_groups()
        if any(g.get("id") == group_id for g in groups):
            return f"❌ Группа «{real}» ВСЁ ЕЩЁ в списке — удаление не сработало."
        return (f"✅ Группа проектов «{real}» удалена (проверено; проекты "
                "остались, просто без папки).")
    except Exception as e:
        logger.exception("Error in delete_project_group")
        return _tool_error("deleting project group", e)


def _describe_move_project_to_group(p: Dict, dest_name: Optional[str] = None,
                                    unknown_reason: str = "") -> str:
    # Живая приёмка 2026-08-07: «Перемещаю проект «__AUTOTEST__btn-tt-01-
    # retest» в папку id:c4d38a807dfe452e964e89b9» — источник по имени,
    # назначение идентификатором. Имя папки сервер знает (тот же кэшированный
    # v2-снапшот, из которого читает list_project_groups), и
    # _move_project_to_group_impl уже печатает его в ОТЧЁТЕ после нажатия
    # кнопки — то есть имя было там, где оно уже не нужно, и его не было
    # там, где человек принимает решение.
    #
    # `dest_name` резолвит вызывающий ДО гейта. None означает «установить не
    # удалось» и печатается ВСЛУХ вместе с причиной: молчаливый показ сырого
    # id и есть дефект.
    gid = p.get("group_id")
    if gid == "NONE":
        dest = "без папки"
    elif dest_name:
        dest = f'в папку «{dest_name}» (id {gid})'
    else:
        dest = (f'в папку id {gid} — ⚠️ ИМЯ ПАПКИ УСТАНОВИТЬ НЕ УДАЛОСЬ'
                + (f' ({unknown_reason})' if unknown_reason else ''))
    return f'Перемещаю проект «{p.get("project_name")}» {dest}'


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def move_project_to_group(project_name: str, project_id: str, group_id: str,
                                manifest_id: str = "", user_reply: str = "",
                                automation_key: str = "") -> str:
    """
    Move a project into a group/folder (requires v2 API). Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is moved yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        project_name: Name of the project (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        project_id: ID of the project to move
        group_id: ID of the destination group, or "NONE" to ungroup
        manifest_id: from call #1's response — pass on call #2 to actually move
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    project_id and project_name are cross-checked against the LIVE project
    list TWICE (same pattern as delete_habit, def-116, 2026-08-07): once
    while BUILDING the plan (call #1, before anything is shown to the owner)
    and again, independently, right before the actual move (call #2,
    unchanged).
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (project_id↔project_name) на построение плана —
    # тот же _guard_project(..., require_known=True), что уже стоит в
    # _move_project_to_group_impl НА ИСПОЛНЕНИИ, вызванный ТЕМИ ЖЕ
    # аргументами. _guard_project, в отличие от _guard_task, не различает
    # «сверить не удалось» от «id не найден» — у неё бинарный исход (отказ
    # либо ок), и это уже так на исполнении. Поэтому здесь НЕТ отдельной
    # мягкой ветки на «временную недоступность»: воспроизвожу ТУ ЖЕ строгость
    # (переносим момент проверки, не меняем её), а не изобретаю смягчение,
    # которого в оригинале нет. Префикс/хвост сообщения приведены к тому же
    # виду, что у остальных plan-отказов в этом файле («План НЕ построен» /
    # «Ничего не изменено») — это текстовая правка отображения, сама
    # сверка/строгость не меняется. Проверка группы-назначения (group_id
    # существует) НЕ переносится: владелец не передаёт group_name для
    # сверки — подменить нечего, это остаётся проверкой исполнения, как
    # было. Но карточка теперь папку НАЗЫВАЕТ (блок ниже) — это
    # отображение, а не гейт. Действует только на call #1
    # (manifest_id пуст); automation_key НЕ пропускает эту проверку — она
    # стоит раньше самого гейта.
    if not manifest_id:
        refusal = _guard_project_or_refuse(project_id, project_name, fresh=True,
                                           require_known=True, prefix=_PLAN_REFUSAL_PREFIX)
        if refusal:
            return refusal
    # Имя папки-НАЗНАЧЕНИЯ для карточки. Читается из ТОГО ЖЕ кэшированного
    # v2-снапшота, из которого работает list_project_groups (fresh=False —
    # _guard_project выше уже сбросил кэш, так что снапшот свежий и лишнего
    # сетевого запроса тут нет), и уходит в описание замыканием, а НЕ ключом
    # в `params`: params едут в манифест, в object_hash и дословно в
    # `_impl(**params)`. Ни отказать, ни заблокировать план резолвинг не
    # может — сверять переданное имя не с чем (group_name владелец не
    # передаёт), а сама проверка существования группы как стояла на
    # исполнении, так и стоит. Неудача печатается вслух вместе с причиной.
    dest_name: Optional[str] = None
    dest_unknown = ""
    if not manifest_id and group_id != "NONE":
        try:
            groups = await _live_groups(fresh=False)
        except Exception as e:
            groups = None
            logger.warning("move_project_to_group: не удалось прочитать список "
                           f"групп на этапе плана ({e}) — имя папки в карточке "
                           "не будет названо.")
        if groups is None:
            dest_unknown = "живой список групп прочитать не удалось"
        else:
            grp = next((g for g in groups if g.get("id") == group_id), None)
            dest_name = (grp or {}).get("name") or None
            if not dest_name:
                dest_unknown = ("папки с таким id нет в живом списке групп — "
                                "перемещение сорвётся на исполнении")
    params = {"project_name": project_name, "project_id": project_id, "group_id": group_id}
    outcome = await _gate_single("move_project_to_group", "move_project_to_group",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply,
                                 lambda p: _describe_move_project_to_group(
                                     p, dest_name, dest_unknown),
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _move_project_to_group_impl(**outcome.extra)


async def _move_project_to_group_impl(project_name: str, project_id: str,
                                      group_id: str) -> str:
    """Pure mutation logic for move_project_to_group — no consent gate.
    Called only by the gated move_project_to_group() above once the plan is
    approved."""
    try:
        # Identity guard on the project (fresh, fail-closed) …
        refusal = _guard_project_or_refuse(project_id, project_name, fresh=True,
                                           require_known=True)
        if refusal:
            return refusal
        # … and the destination group must actually exist (unless ungrouping).
        dest_name = None
        if group_id != "NONE":
            groups = await _live_groups(fresh=False)
            grp = next((g for g in groups if g.get("id") == group_id), None)
            if grp is None:
                return (f"🛑 НЕ переместил — группы с id {str(group_id)[:12]}… "
                        "нет в живом списке групп (list_project_groups). "
                        "Ничего не тронул.")
            dest_name = grp.get("name") or group_id
        live_pname = _v2_project_names().get(project_id, project_name)
        await _run_blocking(lambda: ticktick_v2.move_project_to_group(project_id, group_id))
        want = None if group_id == "NONE" else group_id
        # Post-verify: the project's live groupId must equal the target.
        # Retried (see _reread_projects_until / _POSTVERIFY_RETRY_*) — same
        # race class as move_tasks, found live 2026-08-06: an immediate
        # re-read can still show the pre-mutation groupId for a moment right
        # after the SAME v2 API just changed it.
        projs = await _run_blocking(_reread_projects_until, lambda ps: any(
            p.get("id") == project_id and (p.get("groupId") or None) == want
            for p in ps))
        proj = next((p for p in projs if p.get("id") == project_id), None)
        got = (proj or {}).get("groupId")
        dest = "без папки (ungrouped)" if group_id == "NONE" else f"папку «{dest_name}»"
        if proj is None:
            return (f"Проект «{live_pname}» отправлен в {dest}, но "
                    f"{_UNVERIFIED_MSG}")
        if (got or None) != want:
            return (f"❌ Проект «{live_pname}» НЕ переместился — живой groupId "
                    f"{got!r}, ожидался {want!r}.")
        return f"✅ Проект «{live_pname}» перемещён в {dest} (проверено)."
    except Exception as e:
        logger.exception("Error in move_project_to_group")
        return _tool_error("moving project", e)


# ---------------------------------------------------------------------------
# Task comments (v2)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READONLY)
async def get_task_comments(task_title: str, project_id: str, task_id: str) -> str:
    """
    Get comments on a task (requires v2 API). Read-only — no confirmation
    needed. task_title is used only to label the output; it is not verified
    against the live task (unlike write tools, there is no identity guard here
    since nothing is mutated).

    Args:
        task_title: Title of the task, for display in the output only
        project_id: ID of the project
        task_id: ID of the task
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        comments = await _run_blocking(lambda: ticktick_v2.get_task_comments(project_id, task_id))
        if not comments:
            return f"No comments on task '{task_title}'."
        out = f"Comments on '{task_title}' ({len(comments)}):\n\n"
        for c in comments:
            who = (c.get("userProfile") or {}).get("displayName") or c.get("userName", "?")
            # Include the comment id — delete_task_comment/update_task_comment need it.
            out += f"- (id:{c.get('id')}) [{who}] {c.get('title','')}\n"
        return out
    except Exception as e:
        logger.exception("Error in get_task_comments")
        return _tool_error("fetching comments", e)


def _describe_add_task_comment(p: Dict) -> str:
    return f'Добавляю комментарий к «{p.get("task_title")}»: «{p.get("text")}»'


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def add_task_comment(task_title: str, text: str, project_id: str, task_id: str,
                           manifest_id: str = "", user_reply: str = "",
                           automation_key: str = "") -> str:
    """
    Add a comment to a task (requires v2 API). Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    added on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is added yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        task_title: Title of the task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        text: Comment text
        project_id: ID of the project
        task_id: ID of the task
        manifest_id: from call #1's response — pass on call #2 to actually add
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    task_id and task_title are cross-checked against the LIVE task list
    TWICE (same pattern as delete_habit, def-116, 2026-08-07): once while
    BUILDING the plan (call #1, before anything is shown to the owner — a
    mismatched title never reaches the plan card at all) and again,
    independently, right before the actual add (call #2, unchanged). If the
    live read itself fails while building the plan, the plan still gets
    built (a read hiccup must not block every comment), but its text says so
    honestly — the call #2 check is unconditional and still guards the
    mutation either way.

    Works on a COMPLETED task too (attaching a receipt to a finished job,
    appending the outcome, duplicating it as a template are all normal): the
    check then runs against the source that still knows the task, so the
    title IS verified, and the plan/result say the task is completed. A task
    that a source DOES know but that sits in the TRASH is refused instead
    (see restore_tasks) — trash gets its own refusal, separate from an id
    that no source knows at all.
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (task_id↔task_title) на построение плана — тот
    # же _guard_task, что уже стоит в _add_task_comment_impl НА ИСПОЛНЕНИИ, но
    # здесь — ДО показа карточки владельцу (тот же перенос, что в группе A:
    # attach_file_to_task/update_task_comment/delete_task_comment —
    # add_task_comment это тот же самый паттерн, mismatch блокирует, missing
    # смягчается до ⚠️, ТОЧНО как _add_task_comment_impl уже делает на
    # исполнении). Действует только на call #1 (manifest_id пуст);
    # automation_key НЕ пропускает эту проверку — она стоит раньше самого
    # гейта.
    name_warning = ""
    if not manifest_id:
        g = _guard_task_incl_completed(task_id, task_title or "", project_id)
        refusal, warn = _guard_or_refuse(
            g, stage="план", expected=task_title, missing_says="message",
            unavailable_note=_UNVERIFIED_TASK_TITLE)
        if refusal:
            return refusal
        name_warning = f" {warn}" if warn else ""
    params = {"task_title": task_title, "text": text, "project_id": project_id,
              "task_id": task_id}
    describe_fn = ((lambda p: _describe_add_task_comment(p) + name_warning)
                   if name_warning else _describe_add_task_comment)
    outcome = await _gate_single("add_task_comment", "add_task_comment",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _add_task_comment_impl(**outcome.extra)


async def _add_task_comment_impl(task_title: str, text: str, project_id: str,
                                 task_id: str) -> str:
    """Pure mutation logic for add_task_comment — no consent gate. Called
    only by the gated add_task_comment() above once the plan is approved.

    POST-VERIFY И ФОРМАТ ОТЧЁТА (2026-08-07). Раньше отсюда возвращалось
    `f"Comment added to '{task_title}'."` — без ведущего ✅ и по-английски, —
    хотя соседи по классу (update_task_comment, delete_task_comment,
    attach_file_to_task) к единому формату уже приведены. Цена была не
    косметическая: когда журнала мутаций для инструмента нет (а для
    комментариев его нет вообще), ЕДИНСТВЕННОЕ доказательство успеха для
    `_verified_auto_execute_report` — ведущий ✅ самоотчёта
    (`_auto_execute_report_is_success`). Без него РЕАЛЬНО созданный
    комментарий приходил владельцу как «❓ НЕ подтверждено», то есть
    неотличимо от упавшей операции.

    И ✅ теперь заслуженный, а не переписанный: комментарий перечитывается
    свежим чтением списка и ищется ПО СВОЕМУ id (тот, что клиент минтит при
    создании), а не «последним в списке» — хвост списка не доказывает
    авторство записи."""
    try:
        g = _guard_task_incl_completed(task_id, task_title or "", project_id)
        refusal, _warn = _guard_or_refuse(
            g, stage="исполнение", verb="НЕ добавил комментарий",
            expected=task_title, missing_says="message")
        if refusal:
            return refusal
        note = ""
        if g.status == "completed":
            # Комментировать завершённую задачу законно (дописать вывод,
            # отметить исход) — и название при этом СВЕРЕНО с живой записью,
            # так что это пометка о состоянии задачи, а не о пробеле в
            # проверке (значок ℹ️, см. _COMPLETED_TASK_NOTE).
            note = f"\nℹ️ {_COMPLETED_TASK_NOTE}."
        name = g.title or task_title
        pid = g.project_id or project_id
        created = await _run_blocking(
            lambda: ticktick_v2.add_task_comment(pid, task_id, text))
        new_id = (created or {}).get("id") if isinstance(created, dict) else None
        # Post-verify отдельным чтением. Его собственный сбой — это НЕ провал
        # операции: комментарий уже отправлен, поэтому здесь ⚠️ «исход не
        # подтверждён» (настоящее сомнение проверки), а не ❌ и не «Error …»,
        # которое читалось бы как «мутация не прошла».
        try:
            cms = await _run_blocking(
                lambda: ticktick_v2.get_task_comments(pid, task_id))
        except Exception as e:
            logger.warning(f"add_task_comment: post-verify чтение не удалось: {e}")
            return (f"⚠️ Комментарий отправлен в «{name}», но исход НЕ "
                    f"ПОДТВЕРЖДЁН (перечитать комментарии не удалось: {_redact_for_user(e)}) — "
                    f"проверь вручную.{note}")
        found = next((c for c in (cms or [])
                      if (new_id and c.get("id") == new_id)
                      or (not new_id and (c.get("title") or "") == text)), None)
        if found is None:
            return (f"❌ Комментарий к «{name}» после добавления НЕ найден в "
                    f"свежем списке — исход не подтверждён, проверь "
                    f"вручную.{note}")
        return f"✅ Комментарий добавлен к «{name}» (проверено).{note}"
    except Exception as e:
        logger.exception("Error in add_task_comment")
        return _tool_error("adding comment", e)


# ---------------------------------------------------------------------------
# Statistics & trash (v2)
# ---------------------------------------------------------------------------

# Расследование расхождения (живая приёмка 2026-08-07/08), чтобы следующий не
# начинал с нуля. get_statistics отдавал «today 2 | yesterday 6», а независимая
# сверка по get_changes за те же дни показывала СЕМЬ завершений с датой
# 2026-08-07 (09:10, 18:14, 18:15, 18:16, 18:16, 21:04, 21:28 UTC; что метки
# ленты именно UTC — подтверждено get_task_activity: T_DONE в 14:28:59
# America/Los_Angeles = 21:28 UTC). Ни UTC-сутки, ни сутки владельца (те же 7)
# не дают 2.
#
# Решающее наблюдение — повторный вызов ПОСЛЕ полуночи UTC вернул те же
# «today 2 | yesterday 6». Считай TickTick по UTC, «today» обнулилось бы.
# Значит окно «сегодня» — сутки ЧУЖОЙ зоны: граница между 18:16 и 21:04 UTC,
# то есть зона аккаунта TickTick со смещением примерно UTC+3…+5, куда попали
# ровно два завершения. Наш код при этом ничего не считает — все три числа
# приходят полями todayCompleted/yesterdayCompleted/totalCompleted.
#
# Отсюда и правка: не «чинить» арифметикой (нечего чинить), а назвать
# происхождение чисел и нарезку суток. Встречная лента тоже не эталон:
# get_changes режет календарные UTC-сутки, тянет завершения с капом 100 и
# корзину с капом 300, а завершённую-и-затем-удалённую задачу показывает как
# «удалено», то есть недосчитывает завершения.
@mcp.tool(annotations=READONLY)
async def get_statistics() -> str:
    """Get productivity statistics: achievement score/level and completion
    counts (requires v2 API).

    The counts are TickTick's OWN counters, passed through untouched. TickTick
    slices "today"/"yesterday" in ITS ACCOUNT's timezone — neither UTC nor this
    server's USER_TIMEZONE — so they are not expected to match a get_changes
    feed (which slices calendar days in UTC). A mismatch between the two is not
    evidence that either is broken."""
    err = _ensure_ready()
    if err:
        return err
    try:
        s = await _run_blocking(lambda: ticktick_v2.get_statistics())
        if not s:
            return "No statistics available."

        def num(key: str) -> str:
            # Отсутствующее поле — «нет данных», а не 0: ноль здесь читается
            # как утверждение «сегодня ничего не завершено», которого источник
            # не делал (и «None» — тоже не ответ человеку).
            v = s.get(key)
            return "—" if v is None else str(v)

        return (
            f"Achievement score: {num('score')}  |  Level: {num('level')}\n"
            f"Completed today: {num('todayCompleted')}  |  "
            f"yesterday: {num('yesterdayCompleted')}  |  "
            f"total: {num('totalCompleted')}\n"
            "\nℹ️ Это счётчики самого TickTick, отданные как есть — мы их не "
            "пересчитываем. «Сегодня»/«вчера» он нарезает по зоне АККАУНТА "
            f"TickTick, а не по зоне этого сервера ({_USER_TZ.key}) и не по "
            "UTC, по которому режет сутки лента get_changes. Поэтому числа "
            "здесь и в ленте сходиться не обязаны — расхождение само по себе "
            "не значит, что одна из сторон врёт."
        )
    except Exception as e:
        logger.exception("Error in get_statistics")
        return _tool_error("fetching statistics", e)


@mcp.tool(annotations=READONLY)
async def get_trash(limit: int = 50) -> str:
    """
    List recently deleted (trashed) tasks (requires v2 API). Each line carries
    the task's id and original project — restore_tasks needs both: the id/title
    pair to identify the task (title is checked against the live trash entry
    before restoring), and to_project_id only if you want to override the
    original list it restored to.

    A truncated answer ALWAYS says so and names the real total: the trash page
    is fetched up to its 500-entry ceiling regardless of `limit`, so "showing
    50 of 497" can be stated honestly. `limit` therefore controls how much is
    PRINTED, not how much is known.

    Args:
        limit: How many trashed tasks to print (default 50, max 500 — the
            page ceiling). Anything not printed is announced, never dropped
            silently.
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        # Always ask for the full page, not `limit`: asking for exactly what we
        # print makes truncation INVISIBLE — a bare get_trash() used to answer
        # «Trashed tasks (50):» with nothing else while the trash really held
        # 497 (live, 2026-08-07). Fetching the ceiling costs the same single
        # request (restore_tasks already does it) and is what lets the answer
        # state the true total. «Showing 50» must never read as «there are 50».
        tasks = await _run_blocking(lambda: ticktick_v2.get_trash(TRASH_MAX_LIMIT))
        if not tasks:
            return "Trash is empty."
        show = max(1, min(limit, len(tasks)))
        # A full page means TickTick had at least that many — the true total is
        # unknown, so say "500+" rather than inventing an exact number.
        at_ceiling = len(tasks) >= TRASH_MAX_LIMIT
        total = f"{TRASH_MAX_LIMIT}+" if at_ceiling else str(len(tasks))
        if show < len(tasks) or at_ceiling:
            out = f"Trashed tasks (showing {show} of {total}):\n\n"
        else:
            out = f"Trashed tasks ({len(tasks)}):\n\n"
        # limit=show: the slice is already exact, so format_task_list must not
        # apply its own hidden 100-entry cut on top (the 2026-08-07 defect).
        out += format_task_list(tasks[:show], limit=show)
        rest = len(tasks) - show
        if rest > 0:
            out += (f"\n... and {rest}{'+' if at_ceiling else ''} more — "
                    f"call get_trash(limit={min(len(tasks), TRASH_MAX_LIMIT)}) "
                    "to see them.")
        return out
    except Exception as e:
        logger.exception("Error in get_trash")
        return _tool_error("fetching trash", e)


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def restore_tasks(summary: str, tasks: List[Dict[str, str]] = None,
                        to_project_id: str = None,
                        manifest_id: str = "", user_reply: str = "",
                        automation_key: str = "") -> str:
    """
    Restore one or more tasks from the trash in one call (requires v2 API).
    Gated 🟡 (docs/DESIGN_approval_gate.md): two calls, same tool
    name — nothing is changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest from `tasks`
    and returns a preview of what would be restored — nothing is restored
    yet. Call #2 (after the user actually replied): repeat the call with
    manifest_id=<id from call #1> and user_reply=<the user's literal last
    message> — `tasks`/`to_project_id` are ignored on this call (the
    manifest's own stored values are used). Do NOT make call #2 in the same
    turn as call #1.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user, e.g. «Восстанавливаю из
    корзины задачу „Купить молоко"» or «Восстанавливаю из корзины 3 задачи».

    {{AUTOMATION_KEY_NOTE}}

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"taskId": "...", "title": "..."} objects — required
            on call #1, ignored on call #2. Get IDs/titles from get_trash.
        to_project_id: Optional destination list for all tasks; defaults to
            each task's original list — required (if used) on call #1,
            ignored on call #2. Either way, the destination is verified
            against the live task after restoring (TickTick's restore call
            can silently drop it to Inbox) and auto-corrected with one
            follow-up move if it missed; a mismatch that survives that is
            reported, never hidden as success.
        manifest_id: from call #1's response — pass on call #2 to actually restore
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err
    # Живые названия — ИЗ КОРЗИНЫ: задачи этого тула по определению не
    # открыты, искать их в снимке открытых бессмысленно. Тот же источник, с
    # которым ниже сверяется identity-guard самого восстановления.
    titles = (_plan_task_titles(tasks, source="trash")
              if not manifest_id else {})
    outcome = await _gate_batch(
        "restore", "restore_tasks", tasks, summary, manifest_id, user_reply,
        lambda t: f"**{_plan_task_name(t, titles)}**",
        extra={"to_project_id": to_project_id},
        automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    ex = outcome.extra
    return await _restore_tasks_impl(
        outcome.summary, outcome.tasks,
        ex.get("to_project_id") if ex.get("to_project_id") is not None else to_project_id)


async def _restore_tasks_impl(summary: str, tasks: List[Dict[str, str]],
                              to_project_id: str = None) -> str:
    """Pure mutation logic for restore_tasks — no consent gate. Called
    only by the public gated restore_tasks() below."""
    err = _ensure_ready()
    if err:
        return err
    try:
        # Destination (when overridden) must be a live project.
        if to_project_id:
            refusal = _guard_project_or_refuse(to_project_id, "", fresh=True,
                                               require_known=True)
            if refusal:
                return refusal
        # Identity guard against the TRASH: the caller's title must match the
        # trash entry, mirroring _split_tasks_by_state for open tasks.
        trashed = await _run_blocking(lambda: ticktick_v2.get_trash(500))
        trash_by_id = {x.get("id"): x for x in trashed}
        ok_items, mismatch, absent = [], [], []
        for t in tasks:
            tid = t.get("taskId") or t.get("task_id")
            exp = t.get("title") or ""
            entry = trash_by_id.get(tid)
            if not entry:
                absent.append(exp or f"[task {str(tid)[:8]}…]")
                continue
            real = entry.get("title") or ""
            if not _names_agree(exp, real):
                mismatch.append(f"«{exp}» → в корзине по этому id «{real}»")
                continue
            # Ожидаемый пункт назначения: явный override побеждает; иначе —
            # СОБСТВЕННЫЙ исходный список задачи из записи в корзине (то, что
            # обещает докстринг: «defaults to each task's original list»).
            # Считаем это ЗДЕСЬ, из уже имеющегося снимка корзины, чтобы
            # post-verify ниже реально мог это сверить, а не слепо доверять,
            # что вызов восстановления сам всё сделал правильно.
            orig_pid = (entry.get("projectId") or entry.get("projectID")
                       or entry.get("listId"))
            want_pid = to_project_id or orig_pid
            ok_items.append({"taskId": tid, "title": exp or real,
                             "want_pid": want_pid})
        if mismatch:
            return ("🛑 НЕ восстановил — id НЕ совпал с названием в корзине "
                    "(защита от «не той задачи»): " + "; ".join(mismatch)
                    + ". Ничего не тронул.")
        api_fail = {}
        if ok_items:
            resp = await _run_blocking(lambda: ticktick_v2.batch_restore_tasks(
                [i["taskId"] for i in ok_items], to_project_id))
            api_fail = id2error_failures(resp, [i["taskId"] for i in ok_items])
        # Post-verify: восстановленные задачи ДОЛЖНЫ снова появиться среди
        # ОТКРЫТЫХ и ДОЛЖНЫ оказаться в ожидаемом проекте. Замечено, что
        # /trash/restore у TickTick теряет пункт назначения и кидает задачу
        # в Inbox независимо от переданного projectId — поэтому «снова среди
        # открытых» САМО ПО СЕБЕ не доказательство того, что обещание
        # докстринга («original list») выполнилось. Проект сверяется явно,
        # и если он не совпал — чинится одним явным дополнительным
        # перемещением (тем же вызовом, что использует move_tasks), прежде
        # чем результат уходит в отчёт. Расхождение, пережившее и этот
        # фикс, идёт в вывод как расхождение — никогда молча как успех.
        names = _v2_project_names()
        restored, unknown_dest, failed = [], [], []
        # Вернулись, но ЗАВЕРШЁННЫМИ (были завершены до удаления): успех,
        # который нельзя ни назвать провалом, ни свалить в общую кучу с
        # «снова среди открытых» — статус обязан быть назван вслух.
        restored_completed: List[str] = []
        wrong_project = []  # [(title, want_pid, got_pid)] — так и не туда даже после фикса
        wrong_project_completed: List[Tuple[str, str, str]] = []  # то же, но для вернувшихся завершёнными
        unverified = False
        if ok_items:
            # Retried re-read (see _reread_open_until / _POSTVERIFY_RETRY_*):
            # only waits for each item to become VISIBLE again (not for it to
            # land in any particular project — "wrong project" below is a
            # real, expected TickTick quirk, not the race this guards
            # against) — the same v2-sync-lag class found live 2026-08-06 on
            # move_tasks, which showed a just-restored task as fully absent
            # for a moment right after the SAME v2 API restored it.
            fresh = await _run_blocking(_reread_open_until, lambda m: all(
                m.get(i["taskId"]) is not None
                for i in ok_items if i["taskId"] not in api_fail))
            if fresh is None:
                unverified = True
            else:
                to_fix = []
                for i in ok_items:
                    live = fresh.get(i["taskId"])
                    if live is None and i["taskId"] not in api_fail:
                        # НЕ провал сам по себе. Задача, завершённая ДО
                        # удаления, возвращается из корзины ЗАВЕРШЁННОЙ — в
                        # снимке открытых её не будет никогда, сколько ни
                        # перечитывай. Прежний код объявлял такой исход
                        # «❌ НЕ восстановлено», хотя операция УДАЛАСЬ (живая
                        # приёмка 2026-08-07): человек получал красный
                        # вердикт и шёл перепроверять руками то, что
                        # сработало. Спрашиваем ровно те же три ленты, что
                        # уже спрашивает identity-guard в начале этой же
                        # функции, — и КОРЗИНУ в том числе: только по ней
                        # видно настоящий провал.
                        found, where, readable = _locate_task_any_state(i["taskId"])
                        if not readable:
                            unverified = True
                            continue
                        if where == "completed":
                            got_pid = (found or {}).get("projectId")
                            if not i["want_pid"]:
                                unknown_dest.append(i["title"])
                            elif got_pid != i["want_pid"]:
                                # Починочное перемещение здесь НЕ делается
                                # намеренно: перемещать завершённую задачу
                                # этим сырым вызовом живьём не проверено, а
                                # выдавать непроверенное за исправленное —
                                # тот же дефект наизнанку. Расхождение
                                # называется вслух — отдельным списком,
                                # потому что формулировка соседнего («и
                                # попытка переместить не помогла») тут была
                                # бы ложью: попытки не было.
                                wrong_project_completed.append(
                                    (i["title"], i["want_pid"], got_pid))
                            else:
                                restored_completed.append(i["title"])
                            continue
                        failed.append(i["title"])
                        continue
                    if live is None or i["taskId"] in api_fail:
                        failed.append(i["title"])
                    elif not i["want_pid"]:
                        # Не удалось определить исходный список из записи в
                        # корзине — честно сказать об этом, а не молча
                        # засчитать как успех, который на деле не проверен.
                        unknown_dest.append(i["title"])
                    elif live.get("projectId") != i["want_pid"]:
                        # Carry the CURRENT live projectId along (usually
                        # Inbox, where /trash/restore dropped it) — needed
                        # below as the raw move's fromProjectId, since
                        # batch_move_tasks() would otherwise have to
                        # re-derive it from get_open_tasks() (see that
                        # method's docstring for why that can silently drop
                        # the task from the request entirely).
                        to_fix.append({**i, "cur_pid": live.get("projectId")})
                    else:
                        restored.append(i["title"])
                if to_fix:
                    fix_by_pid: Dict[str, List[Dict]] = {}
                    for i in to_fix:
                        fix_by_pid.setdefault(i["want_pid"], []).append(i)
                    fix_api_fail: Dict[str, str] = {}
                    for pid, items in fix_by_pid.items():
                        ids = [i["taskId"] for i in items]
                        try:
                            mresp = await _run_blocking(
                                lambda pid=pid, items=items:
                                    ticktick_v2.batch_move_tasks_raw([
                                        {"taskId": i["taskId"],
                                         "fromProjectId": i["cur_pid"],
                                         "toProjectId": pid} for i in items]))
                            fix_api_fail.update(id2error_failures(mresp, ids))
                        except Exception as e:
                            logger.exception(
                                f"restore_tasks: corrective move to {pid} failed")
                            for tid2 in ids:
                                fix_api_fail.setdefault(tid2, str(e))
                    # Same retried re-read, now checking the STRICT
                    # identity-changing criterion (projectId == destination)
                    # after the corrective move — mirrors move_tasks's own
                    # post-verify exactly, same race class.
                    fresh2 = await _run_blocking(_reread_open_until, lambda m: all(
                        (m.get(i["taskId"]) or {}).get("projectId") == i["want_pid"]
                        for i in to_fix if i["taskId"] not in fix_api_fail))
                    for i in to_fix:
                        live2 = (fresh2 or {}).get(i["taskId"])
                        if (fresh2 is not None and live2 is not None
                                and i["taskId"] not in fix_api_fail
                                and live2.get("projectId") == i["want_pid"]):
                            restored.append(i["title"])
                        elif fresh2 is None:
                            unverified = True
                        else:
                            got = (live2 or {}).get("projectId")
                            wrong_project.append((i["title"], i["want_pid"], got))
        lines = []
        if restored:
            lines.append(f"↩ Восстановлено из корзины {len(restored)} "
                         "(проверено — снова среди открытых, в нужном "
                         "списке): " + ", ".join(f"«{t}»" for t in restored))
        if restored_completed:
            lines.append(f"↩ Восстановлено из корзины {len(restored_completed)} "
                         "(проверено — вернулись ЗАВЕРШЁННЫМИ, поэтому их нет "
                         "среди открытых; список нужный): "
                         + ", ".join(f"«{t}»" for t in restored_completed))
        if unverified:
            lines.append(f"Восстановление {len(ok_items)} отправлено, но "
                         f"{_UNVERIFIED_MSG}")
        if wrong_project:
            parts = [f"«{t}» — попала в «{names.get(got, got)}», а не в "
                    f"«{names.get(want, want)}» (восстановилась не туда, и "
                    "попытка переместить в нужный список не помогла)"
                    for t, want, got in wrong_project]
            lines.append("⚠️ Восстановлено НЕ в исходный/запрошенный список "
                        f"{len(wrong_project)}: " + "; ".join(parts))
        if wrong_project_completed:
            parts = [f"«{t}» — вернулась ЗАВЕРШЁННОЙ и лежит в "
                     f"«{names.get(got, got)}», а не в «{names.get(want, want)}» "
                     "(перемещать завершённую сервер не пробовал)"
                     for t, want, got in wrong_project_completed]
            lines.append("⚠️ Восстановлено НЕ в исходный/запрошенный список "
                         f"{len(wrong_project_completed)}: " + "; ".join(parts))
        if unknown_dest:
            lines.append(f"⚠️ Восстановлено {len(unknown_dest)}, но исходный "
                        "список не удалось определить из записи в корзине — "
                        "куда реально попали, не проверено: "
                        + ", ".join(f"«{t}»" for t in unknown_dest))
        if failed:
            extra = "; ".join(f"{k[:8]}…: {v}" for k, v in api_fail.items())
            lines.append(f"❌ НЕ восстановлено {len(failed)} (не появились среди "
                         "открытых" + (f"; TickTick сообщил: {extra}" if extra else "")
                         + "): " + ", ".join(f"«{t}»" for t in failed))
        if absent:
            lines.append(f"↷ Не найдены в корзине {len(absent)}: "
                         + ", ".join(f"«{t}»" for t in absent))
        if ok_items:
            rid = _op_journal("restore", [
                {"taskId": i["taskId"], "title": i["title"],
                 "expect": {"projectId": i["want_pid"]}} for i in ok_items
            ], summary)
            lines.append(_report_line(rid))
        return "\n".join(lines) if lines else "Ничего не восстановлено."
    except Exception as e:
        logger.exception("Error in restore_tasks")
        return _tool_error("restoring tasks", e)


def _describe_attach_file_to_task(p: Dict) -> str:
    name = p.get("filename") or (p.get("url") or "").split("?")[0].rstrip("/").split("/")[-1] or "файл"
    return f'Прикрепляю «{name}» к задаче «{p.get("task_title")}»'


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def attach_file_to_task(task_title: str, task_id: str, project_id: str,
                              url: str = None,
                              content_base64: str = None, filename: str = None,
                              manifest_id: str = "", user_reply: str = "",
                              automation_key: str = "") -> str:
    """
    Attach a file to a task (requires v2 API). Provide the file either by URL
    (the server downloads it) or as base64 content — e.g. a file fetched from
    Google Drive or generated by Claude. Max 20 MB. Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    attached on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is attached yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        task_title: Title of the task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        task_id: ID of the task
        project_id: ID of the task's project (auto-corrected if stale)
        url: Public/direct URL to download the file from (optional)
        content_base64: Base64-encoded file content (optional, alternative to url)
        filename: File name to store it as (optional; inferred from url if omitted)
        manifest_id: from call #1's response — pass on call #2 to actually attach
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    task_id and task_title are cross-checked against the LIVE task list
    TWICE (same pattern as delete_habit, def-116, 2026-08-07): once while
    BUILDING the plan (call #1, before anything is shown to the owner — a
    mismatched title never reaches the plan card at all) and again,
    independently, right before the actual upload (call #2, unchanged). If
    the live read itself fails while building the plan, the plan still gets
    built (a read hiccup must not block every attach), but its text says so
    honestly — the call #2 check is unconditional and still guards the
    mutation either way.

    Works on a COMPLETED task too (attaching a receipt to a finished job,
    appending the outcome, duplicating it as a template are all normal): the
    check then runs against the source that still knows the task, so the
    title IS verified, and the plan/result say the task is completed. A task
    that a source DOES know but that sits in the TRASH is refused instead
    (see restore_tasks) — trash gets its own refusal, separate from an id
    that no source knows at all.
    """
    err = _ensure_ready()
    if err:
        return err
    if not url and not content_base64:
        return "Provide either a url or content_base64 for the file."
    # Перенос identity-guard (task_id↔task_title) на построение плана — тот
    # же _guard_task, что уже стоит в _attach_file_to_task_impl НА
    # ИСПОЛНЕНИИ, но здесь — ДО показа карточки владельцу (тот же перенос,
    # что в delete_habit, def-116: mismatch блокирует план целиком;
    # временная недоступность живого чтения — fail-open с предупреждением в
    # тексте плана, а исполнение — не тронуто этой правкой — перепроверит
    # заново и остаётся последней линией защиты). Действует только на call #1
    # (manifest_id пуст): call #2 обслуживает СОХРАНЁННЫЕ параметры плана, а
    # не свежие аргументы вызова, и identity guard на исполнении там уже
    # стоит. automation_key НЕ пропускает эту проверку — она стоит раньше
    # самого гейта, поэтому headless-путь (карточки не видит вовсе) тоже
    # защищён.
    name_warning = ""
    if not manifest_id:
        g = _guard_task_incl_completed(task_id, task_title or "", project_id)
        refusal, warn = _guard_or_refuse(
            g, stage="план", expected=task_title, says="message",
            missing_says="message", unavailable_note=_UNVERIFIED_TASK_TITLE)
        if refusal:
            return refusal
        name_warning = f" {warn}" if warn else ""
    params = {"task_title": task_title, "task_id": task_id, "project_id": project_id,
              "url": url, "content_base64": content_base64, "filename": filename}
    describe_fn = ((lambda p: _describe_attach_file_to_task(p) + name_warning)
                   if name_warning else _describe_attach_file_to_task)
    outcome = await _gate_single("attach_file_to_task", "attach_file_to_task",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _attach_file_to_task_impl(**outcome.extra)


async def _attach_file_to_task_impl(task_title: str, task_id: str, project_id: str,
                                    url: str = None, content_base64: str = None,
                                    filename: str = None) -> str:
    """Pure mutation logic for attach_file_to_task — no consent gate. Called
    only by the gated attach_file_to_task() above once the plan is approved."""
    title = task_title or _lookup_task_title(task_id)
    pre = _open_by_id(fresh=True)
    if pre is None:
        return _STATE_UNAVAILABLE_MSG
    g = _guard_task_incl_completed(task_id, task_title or "", project_id, by_id=pre)
    # Имя для строки результата: _lookup_task_title выше смотрит только в
    # ОТКРЫТЫЕ задачи, поэтому у завершённой давал «[task 6a757123…]» —
    # хотя guard прямо здесь уже установил живое имя (тот же фолбэк, что в
    # _duplicate_task_impl).
    title = title if title != f"[task {task_id[:8]}…]" else (g.title or title)
    refusal, _warn = _guard_or_refuse(
        g, stage="исполнение", verb="НЕ прикрепил", expected=task_title,
        missing_says="message")
    if refusal:
        return refusal
    note = ""
    if g.status == "completed":
        note = f"\nℹ️ {_COMPLETED_TASK_NOTE}."
    try:
        pid = g.project_id or _resolve_project_id(task_id, project_id)
        # Счётчик ДО — из источника, который знает задачу В ЕЁ состоянии.
        # Для открытой это снимок открытых (уже прочитан, бесплатно), для
        # завершённой — тот же слитый список вложений, которым их читает
        # list_task_attachments.
        if task_id in pre:
            pre_atts = list((pre.get(task_id) or {}).get("attachments") or [])
        else:
            pre_atts = await _run_blocking(_attachments_any_state, task_id)
        pre_count = len(pre_atts) if pre_atts is not None else None
        att = await _run_blocking(lambda: ticktick_v2.upload_attachment(
            pid, task_id, url=url, content_base64=content_base64, filename=filename))
        # The endpoint can return a 2xx with an empty body — don't fabricate
        # details from {}; post-verify against the task's attachment list.
        shown_name = att.get("fileName") or filename or \
            ((url or "").split("?")[0].rstrip("/").split("/")[-1] or "attachment")
        size = att.get("size")
        size_str = f"{size} байт" if size is not None else "размер неизвестен"
        post = _open_by_id(fresh=True)
        if post is None:
            marker, verify = "⚠️", f" {_UNVERIFIED_MSG}"
        elif task_id in post:
            post_count = len((post.get(task_id) or {}).get("attachments") or [])
            if post_count > pre_count:
                marker, verify = "✅", " (проверено: вложение видно на задаче)"
            else:
                marker, verify = ("⚠️",
                    " вложение НЕ видно на задаче — проверь вручную")
        else:
            # ЗАДАЧА НЕ СРЕДИ ОТКРЫТЫХ (завершена). Раньше здесь стояло
            # честное на тот момент «вложение не проверить»: пост-проверка
            # смотрела в выборку ОТКРЫТЫХ задач, где завершённой нет по
            # определению. Но источник, знающий её вложения, существует и уже
            # используется соседним инструментом — `list_task_attachments`
            # читает ту же завершённую задачу и отдаёт её файлы без единой
            # жалобы (`_merged_task_attachments` → `find_task_any_state`).
            # Тот же корень, что чинили eee96d8/15898e6: источник для guard'а
            # ≠ источник для ФАКТА, — здесь он не был доведён до третьего
            # потребителя. Цена — те же 1-2 запроса, что платит чтение
            # вложений, и только на этой ветке.
            post_atts = await _run_blocking(_attachments_any_state, task_id)
            if post_atts is None:
                marker, verify = ("⚠️", " (перечитать вложения задачи не "
                                        "удалось — проверь вручную)")
            elif pre_count is not None and len(post_atts) > pre_count:
                marker, verify = "✅", " (проверено: вложение видно на задаче)"
            elif pre_count is None and any(
                    (a.get("fileName") or a.get("name")) == shown_name
                    for a in post_atts):
                # Счётчика «до» нет (то чтение не удалось) — доказываем
                # наличием файла С ЭТИМ ИМЕНЕМ, а не молчанием.
                marker, verify = "✅", " (проверено: файл виден на задаче)"
            else:
                marker, verify = ("⚠️",
                    " вложение НЕ видно на задаче — проверь вручную")
        return f"{marker} Прикреплён файл «{shown_name}» ({size_str}) к «{title}»{verify}{note}"
    except Exception as e:
        logger.exception("Error in attach_file_to_task")
        return _tool_error("attaching file", e)


# 2026-08-09 (П9 пакет ТЗ, пункт 4): было 15 МБ — теоретический потолок,
# который на практике никогда не достигался, потому что ответ падал ГОРАЗДО
# раньше. Живые случаи: фотография чека вернула 572 879 символов base64
# (~420 КБ сырых байт), вторая — 1 081 351 символов (~792 КБ) — и ОБА ответа
# были отброшены целиком транспортом MCP, до всякой проверки этого предела.
# Base64 добавляет ~4/3 к размеру, плюс накладные расходы самого транспорта
# MCP — отсюда предел ниже, с запасом, чем самый маленький из двух реальных
# провалов. Ставить впритык к наблюдаемой границе бессмысленно: возврат
# всё равно есть только у get_attachment_download_url (ссылка, без предела).
DOWNLOAD_ATTACHMENT_MAX_BYTES = 256 * 1024  # 256 KB


def _merged_task_attachments(task_id: str) -> List[Dict]:
    """Attachment refs for a task from BOTH sources, de-duplicated by id:
    the structured `attachments` array (has fileName; id/size are not
    guaranteed present on every account) and the ids/fileNames embedded as
    ![file](id/name) tokens in the task's content/desc (always has an id).
    Merging lets download/list work even when one source is incomplete."""
    struct = ticktick_v2.get_task_attachments(task_id)
    content_refs = ticktick_v2.get_content_attachment_refs(task_id)
    by_name = {r.get("fileName"): r for r in content_refs if r.get("fileName")}
    merged = []
    seen_ids = set()
    for a in struct:
        name = a.get("fileName") or a.get("name")
        att_id = a.get("id") or a.get("attachmentId") or a.get("fileId") \
            or (by_name.get(name) or {}).get("id")
        row = dict(a)
        if att_id:
            row["id"] = att_id
            seen_ids.add(att_id)
        merged.append(row)
    # Any content-parsed ref not already covered by the structured array
    # (e.g. attachments array was empty/unavailable for this account).
    for r in content_refs:
        if r.get("id") and r["id"] not in seen_ids:
            merged.append(dict(r))
            seen_ids.add(r["id"])
    return merged


def _attachments_any_state(task_id: str) -> Optional[List[Dict]]:
    """Вложения задачи В ЛЮБОМ ЕЁ СОСТОЯНИИ — или None, если прочитать не
    удалось. Ровно `_merged_task_attachments`, только неудача чтения здесь не
    исключение, а честное «не знаю».

    Нужна пост-проверке `attach_file_to_task`: та смотрела в выборку ОТКРЫТЫХ
    задач и поэтому на ЗАВЕРШЁННОЙ задаче всегда писала «вложение не
    проверить» — хотя источник, знающий её файлы, тут же рядом и уже работает
    (list_task_attachments читает ту же задачу без единой жалобы). Разделять
    надо не «удалось/не удалось», а «не смогли прочитать» и «прочитали, файла
    нет» — первое это ⚠️, второе это факт."""
    try:
        return _merged_task_attachments(task_id)
    except Exception as e:
        logger.warning(f"вложения задачи {str(task_id)[:8]}… прочитать не "
                       f"удалось: {e}")
        return None


@mcp.tool(annotations=READONLY)
async def list_task_attachments(task_id: str, project_id: str = None) -> str:
    """
    List a task's file attachments (requires v2 API): filename, id (needed by
    download_task_attachment), and size when known. Combines the structured
    attachment metadata with ids/filenames parsed out of the task's own
    content (TickTick embeds them there as ![file](id/name) tokens), since
    not every account's attachment entries carry an id field directly.

    Works for tasks that are no longer open too: a COMPLETED or TRASHED task's
    files stay listable (that receipt/invoice is usually exactly what someone
    comes back for). Those are looked up in the 100 most recently completed
    tasks and the 500 newest trash entries — anything older than that has to be
    reopened/restored first, and the error message says so.

    Args:
        task_id: ID of the task
        project_id: ID of the task's project (optional; auto-resolved)
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        atts = await _run_blocking(lambda: _merged_task_attachments(task_id))
        if not atts:
            return f"No attachments on task {task_id}."
        out = f"Attachments on task {task_id} ({len(atts)}):\n"
        for i, a in enumerate(atts, 1):
            name = a.get("fileName") or a.get("name") or "(unnamed)"
            att_id = a.get("id") or "?"
            size = a.get("fileSize") or a.get("size")
            size_str = f", {size // 1024} KB" if isinstance(size, (int, float)) else ""
            out += f"  {i}. {name}  (id:{att_id}{size_str})\n"
        return out
    except Exception as e:
        logger.exception("Error in list_task_attachments")
        return _tool_error("listing attachments", e)


def _attachment_project_id(task_id: str, given: str = None) -> Optional[str]:
    """The projectId to build an attachment URL with, for a task in ANY state.

    _resolve_project_id() only looks at OPEN tasks (deliberately — mutation
    guards must not silently retarget a completed/trashed task), so on its own
    it leaves every attachment of a completed task unreachable: the download
    endpoint needs a projectId. This read-only path may look further, into the
    completed feed and the trash (find_task_any_state, cached — no extra
    request when the attachment list already resolved the same task)."""
    pid = given or _resolve_project_id(task_id, given)
    if pid:
        return pid
    try:
        task, _state = ticktick_v2.find_task_any_state(task_id)
        return (task or {}).get("projectId")
    except Exception as e:
        logger.warning(f"_attachment_project_id fallback failed for {task_id}: {e}")
        return None


async def _resolve_attachment_ref(task_id: str, project_id: str = None,
                                  attachment_id: str = None,
                                  filename: str = None, index: int = None
                                  ) -> Tuple[Optional[Tuple[str, str, str]], Optional[str]]:
    """Turn "which attachment?" (id / exact filename / 1-based index) into a
    concrete (projectId, attachmentId, fileName) triple.
    Returns (triple, None) on success or (None, message-for-the-user) — shared
    by download_task_attachment and get_attachment_download_url so both accept
    exactly the same ways of naming an attachment."""
    if not attachment_id and not filename and not index:
        return None, ("Provide one of attachment_id, filename, or index "
                      "(see list_task_attachments).")
    pid = await _run_blocking(lambda: _attachment_project_id(task_id, project_id))
    if not pid:
        return None, f"Could not resolve project_id for task {task_id}; pass it explicitly."
    atts = await _run_blocking(lambda: _merged_task_attachments(task_id))
    if not atts:
        return None, f"No attachments found on task {task_id}."

    target = None
    if attachment_id:
        target = next((a for a in atts if a.get("id") == attachment_id), None)
        if not target:
            target = {"id": attachment_id}  # caller may have it from elsewhere
    elif filename:
        target = next((a for a in atts
                       if (a.get("fileName") or a.get("name")) == filename), None)
        if not target:
            return None, (f"No attachment named '{filename}' on this task. "
                          "Run list_task_attachments to see exact names.")
    elif index:
        if index < 1 or index > len(atts):
            return None, f"index {index} out of range (task has {len(atts)} attachments)."
        target = atts[index - 1]

    att_id = target.get("id")
    if not att_id:
        return None, ("Could not determine this attachment's id (neither the "
                      "attachments metadata nor the content markdown had one) — "
                      "try list_task_attachments and pass attachment_id explicitly.")
    name = filename or target.get("fileName") or target.get("name") or f"attachment_{att_id}"
    return (pid, att_id, name), None


@mcp.tool()
async def download_task_attachment(task_id: str, project_id: str = None,
                                   attachment_id: str = None,
                                   filename: str = None,
                                   index: int = None) -> str:
    """
    RULE: for photos/scans and any file that might be over ~256 KB, use
    get_attachment_download_url instead — it hands back a short-lived link,
    the bytes never enter this conversation, and it has no size limit. Use
    THIS tool only for small text-like attachments you actually need inlined
    (e.g. to read/quote their content). Refuses anything over 256 KB outright
    (base64-bloated response; confirmed live that responses this size get
    dropped by the transport well before that, see DOWNLOAD_ATTACHMENT_MAX_BYTES).

    When it doesn't refuse: downloads a file attachment from a task (requires
    v2 API) and returns its content as base64, so it can be re-saved
    elsewhere (e.g. uploaded to Google Drive). Identify the attachment by ONE
    of: attachment_id (from list_task_attachments), filename (exact match),
    or index (1-based, as shown by list_task_attachments).

    Endpoint: GET /api/v1/attachment/{projectId}/{taskId}/{attachmentId}
    (mirrors the working upload path minus '/upload'; confirmed by probing —
    this exact shape 401s, i.e. the route exists, while /api/v2/... and
    other shapes 404).

    Args:
        task_id: ID of the task
        project_id: ID of the task's project (optional; auto-resolved)
        attachment_id: Attachment id (optional; see list_task_attachments)
        filename: Exact attachment filename (optional, alternative to id)
        index: 1-based position in list_task_attachments' output (optional)
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        ref, ref_err = await _resolve_attachment_ref(
            task_id, project_id, attachment_id, filename, index)
        if ref_err:
            return ref_err
        pid, att_id, want_name = ref

        name, data, mime = await _run_blocking(lambda: ticktick_v2.download_attachment(
            pid, task_id, att_id, filename=want_name))

        if len(data) > DOWNLOAD_ATTACHMENT_MAX_BYTES:
            return (f"'{name}' is {len(data) // 1024} KB — over the "
                    f"{DOWNLOAD_ATTACHMENT_MAX_BYTES // 1024} KB limit for this tool "
                    "(base64-bloated responses this size get dropped by the transport). "
                    "Not downloaded. Use get_attachment_download_url instead — it returns "
                    "a direct link with no size limit and costs no tokens.")

        b64 = base64.b64encode(data).decode("ascii")
        return (f"filename: {name}\n"
                f"mime: {mime}\n"
                f"size_bytes: {len(data)}\n"
                f"content_base64: {b64}")
    except Exception as e:
        logger.exception("Error in download_task_attachment")
        return _tool_error("downloading attachment", e)


def _clamp_link_ttl(ttl_minutes: Optional[int]) -> int:
    """Keep a requested link lifetime inside 1..ATTACHMENT_LINK_TTL_MAX_MIN."""
    try:
        ttl = int(ttl_minutes) if ttl_minutes else ATTACHMENT_LINK_TTL_DEFAULT_MIN
    except (TypeError, ValueError):
        ttl = ATTACHMENT_LINK_TTL_DEFAULT_MIN
    return max(1, min(ttl, ATTACHMENT_LINK_TTL_MAX_MIN))


@mcp.tool(annotations=READONLY)
async def get_attachment_download_url(task_id: str, project_id: str = None,
                                      attachment_id: str = None,
                                      filename: str = None,
                                      index: int = None,
                                      ttl_minutes: int = 15) -> str:
    """
    RULE: use this for photos/scans and anything over ~256 KB — and whenever
    the user actually wants the file on their phone or computer, not inlined
    in chat. download_task_attachment base64-encodes into the answer and
    refuses over 256 KB; this tool has no size limit and costs no tokens.

    The link points at this MCP server (not at TickTick — TickTick has no
    public/pre-signed file URLs), which streams the bytes through; the TickTick
    session cookie never leaves the server. It is signed and expires (15 min by
    default, 120 max), and anyone holding it can fetch that one file until it
    does — so treat it like a one-off secret and don't post it publicly.

    Identify the attachment by ONE of: attachment_id (from
    list_task_attachments), filename (exact match), or index (1-based).
    Requires the server to know its own public address (PUBLIC_BASE_URL or
    Railway's RAILWAY_PUBLIC_DOMAIN) and MCP_SECRET to be set.

    Args:
        task_id: ID of the task
        project_id: ID of the task's project (optional; auto-resolved)
        attachment_id: Attachment id (optional; see list_task_attachments)
        filename: Exact attachment filename (optional, alternative to id)
        index: 1-based position in list_task_attachments' output (optional)
        ttl_minutes: How long the link stays valid, 1-120 (default 15)
    """
    err = _ensure_ready()
    if err:
        return err
    base = _public_base_url()
    if not base:
        return _NO_PUBLIC_URL_MSG
    if not SECRET:
        return _NO_SECRET_MSG
    try:
        ref, ref_err = await _resolve_attachment_ref(
            task_id, project_id, attachment_id, filename, index)
        if ref_err:
            return ref_err
        pid, att_id, name = ref
        ttl = _clamp_link_ttl(ttl_minutes)
        token = _sign_attachment_token("dl", pid, task_id, att_id, name, ttl)
        if not token:
            return _NO_SECRET_MSG
        return (f"Ссылка на скачивание «{name}» (действует {ttl} мин):\n"
                f"{base}/dl/{token}\n\n"
                "Открой её в браузере или на телефоне — файл скачается напрямую, "
                "минуя этот чат. После истечения срока ссылка перестанет работать.")
    except Exception as e:
        logger.exception("Error in get_attachment_download_url")
        return _tool_error("building download link", e)


def _describe_create_attachment_upload_url(p: Dict,
                                           task_title: Optional[str] = None) -> str:
    # НИ ОДНОГО фрагмента будущей ссылки/токена здесь быть не может: токен
    # подписывается только в `_impl`, уже ПОСЛЕ подтверждения. Иначе пропуск
    # утёк бы в превью (и в сообщение Telegram) ещё до согласия человека.
    #
    # `task_title` — живое название задачи, добытое вызывающим до гейта
    # (_live_task_title). Живая приёмка 2026-08-07 дала карточку «Выдаю
    # ссылку на загрузку «файл.txt» в задачу 6a7571238f0854e347f51407»: файл
    # назван по-человечески, задача — голым id. Это тот самый метод, который
    # ВРУЧАЕТ право записи в аккаунт, и подтверждающий не мог глазами
    # сверить, куда ляжет файл. None (имя не установлено) печатается ВСЛУХ
    # как неизвестность — молчаливый показ id и был дефектом.
    name = p.get("filename") or "файл без имени"
    tid = p.get("task_id")
    target = (f'в задачу «{task_title}» (id {tid})' if task_title else
              f'в задачу id {tid} — ⚠️ НАЗВАНИЕ ЗАДАЧИ УСТАНОВИТЬ НЕ УДАЛОСЬ '
              '(её нет в живом состоянии аккаунта или оно недоступно), '
              'сверить глазами, в какую задачу ляжет файл, нельзя')
    return (f'Выдаю ссылку на загрузку «{name}» {target} '
            f'(действует {_clamp_link_ttl(p.get("ttl_minutes"))} мин; по ней '
            'кто угодно сможет положить файл в аккаунт)')


# ГЕЙТОВАН (аудит 2026-08-06, пересмотр прежнего «намеренно без гейта»). Гейт
# стоит не за то, что тул ДЕЛАЕТ, а за то, что он ВРУЧАЕТ: маршрут PUT
# /ul/{token} публичен и проверяет ровно одно — подпись токена
# (_verify_attachment_token), ни заголовка, ни куки, ни MCP_SECRET предъявлять
# не нужно, а пишет в TickTick сам сервер сессией владельца. Токен при этом
# многоразовый (ни nonce, ни реестра использованных) и живёт до
# ATTACHMENT_LINK_TTL_MAX_MIN=120 минут, то есть это предъявительский пропуск
# на запись в аккаунт. Прежний довод «модель не может сделать raw PUT»
# ограничивает интерфейс, а не возможности: агент с оболочкой шлёт обычный
# HTTP-запрос, а сам тул печатает готовую команду curl. Выдача ссылки —
# единственный момент, когда сервер вообще способен спросить человека.
@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def create_attachment_upload_url(task_id: str, project_id: str = None,
                                       filename: str = None,
                                       size_bytes: int = None,
                                       ttl_minutes: int = 15,
                                       manifest_id: str = "", user_reply: str = "",
                                       automation_key: str = "") -> str:
    """
    Create a temporary upload LINK that puts a file onto a task (requires v2
    API) without the file passing through this conversation. Counterpart of
    get_attachment_download_url; use it when the file is large or simply not in
    Claude's hands — e.g. it sits on the user's phone or on a server. Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — NO link is
    handed out on call #1 (the link is a bearer write-capability into the
    owner's account, so it is minted only after the approval).

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — no token is signed yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    HONEST LIMITATION: an MCP client (Claude) cannot use this link itself — it
    cannot make raw PUT requests. The link is for a HUMAN with a browser/phone
    or for a script. When Claude already has the bytes, attach_file_to_task is
    the right tool. What the holder does with the link:
        curl -X PUT --upload-file ./myfile.pdf "<the link>"
    The server relays the body into TickTick as the multipart upload it expects
    and answers with JSON. Max 20 MB (TickTick's own cap).

    Requires the server to know its own public address (PUBLIC_BASE_URL or
    Railway's RAILWAY_PUBLIC_DOMAIN) and MCP_SECRET to be set.

    Args:
        task_id: ID of the task the file will be attached to
        project_id: ID of the task's project (optional; auto-resolved)
        filename: Name the file will get in TickTick (recommended — the
            extension decides the stored content type)
        size_bytes: Expected file size, if known (checked against the 20 MB cap
            up front so the user isn't told "too big" only after uploading)
        ttl_minutes: How long the link stays valid, 1-120 (default 15)
        manifest_id: from call #1's response — pass on call #2 to actually get the link
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err
    base = _public_base_url()
    if not base:
        return _NO_PUBLIC_URL_MSG
    if not SECRET:
        return _NO_SECRET_MSG
    if size_bytes and size_bytes > ATTACHMENT_MAX_BYTES:
        return (f"Файл {size_bytes // (1024*1024)} МБ — TickTick принимает "
                f"максимум {ATTACHMENT_MAX_BYTES // (1024*1024)} МБ. Ссылку не делаю.")
    params = {"task_id": task_id, "project_id": project_id,
              "filename": filename, "ttl_minutes": ttl_minutes}
    # Имя задачи для карточки резолвится ДО гейта и уходит в describe
    # замыканием, а НЕ ключом в `params`: `params` уезжают в манифест, в его
    # object_hash и дословно в `_impl(**params)` — лишний ключ сломал бы и
    # вызов исполнителя, и привязку одобренного плана к тому, что показали.
    # На call #2 карточка не строится, поэтому и резолвить нечего.
    task_title = (_live_task_title(task_id, project_id or "")
                  if not manifest_id else None)
    # ПОЛИТИКА КОРЗИНЫ ДЕЙСТВУЕТ И ЗДЕСЬ (круг 8, найдено сплошным прогоном
    # одного корзинного входа по всем тулам — живой прогон по кнопкам этого
    # не поймал). Иначе она обходится ссылкой: `attach_file_to_task` кладёт
    # файл в удалённую задачу и отказывает, а этот тул на ТУ ЖЕ задачу выдавал
    # РАБОЧУЮ ссылку-полномочие на запись — файл уезжал в корзину и исчезал
    # вместе с ней при очистке. Проверяется ТОЛЬКО корзина: остальные
    # состояния этот тул обслуживает намеренно (в том числе завершённую
    # задачу — см. `_attachment_project_id` в исполнителе).
    if not manifest_id and _task_is_in_trash(task_id):
        return ("🛑 Ссылку НЕ выдаю — " + _TRASHED_TASK_NOTE.format(
            title=task_title or str(task_id)[:8] + "…"))
    outcome = await _gate_single("create_attachment_upload_url",
                                 "create_attachment_upload_url",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply,
                                 lambda p: _describe_create_attachment_upload_url(
                                     p, task_title),
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _create_attachment_upload_url_impl(**outcome.extra)


async def _create_attachment_upload_url_impl(task_id: str, project_id: str = None,
                                             filename: str = None,
                                             ttl_minutes: int = 15) -> str:
    """Mints the actual bearer upload link — no consent gate. Called only by
    the gated create_attachment_upload_url() above once the plan is approved
    (or by the Telegram-button auto-executor, which replays the same params)."""
    base = _public_base_url()
    if not base:
        return _NO_PUBLIC_URL_MSG
    # Вторая линия той же политики корзины: сюда приходят и по нажатию кнопки
    # в Telegram (исполнитель зовётся напрямую, минуя код тула), и по
    # манифесту, построенному ДО удаления задачи.
    if _task_is_in_trash(task_id):
        return ("🛑 Ссылку НЕ выдаю — " + _TRASHED_TASK_NOTE.format(
            title=_lookup_task_title(task_id) or str(task_id)[:8] + "…"))
    try:
        # _attachment_project_id, а не _resolve_project_id: второй смотрит
        # ТОЛЬКО в открытые задачи (guard-политика — мутация не должна молча
        # перенацелиться на завершённую), и из-за этого ссылку на файл к
        # завершённой задаче нельзя было выдать вовсе — отказ «Could not
        # resolve project_id» прилетал УЖЕ ПОСЛЕ того, как человек одобрил
        # выдачу права записи в аккаунт. Эталон лежал рядом и применён на
        # download-пути (см. его докстринг).
        pid = _attachment_project_id(task_id, project_id)
        if not pid:
            return f"Could not resolve project_id for task {task_id}; pass it explicitly."
        att_id = new_attachment_id()
        name = filename or f"attachment_{att_id}"
        ttl = _clamp_link_ttl(ttl_minutes)
        token = _sign_attachment_token("ul", pid, task_id, att_id, name, ttl)
        if not token:
            return _NO_SECRET_MSG
        url = f"{base}/ul/{token}"
        return (f"Ссылка для загрузки «{name}» на задачу {task_id} "
                f"(действует {ttl} мин, максимум "
                f"{ATTACHMENT_MAX_BYTES // (1024*1024)} МБ):\n{url}\n\n"
                "Загрузить может человек с телефона/компьютера или скрипт — "
                "я сам по этой ссылке файл отправить не могу (обычный PUT-запрос "
                "мне недоступен). Команда:\n"
                f"  curl -X PUT --upload-file ПУТЬ_К_ФАЙЛУ \"{url}\"\n\n"
                "Файл появится на задаче сразу после успешной загрузки; "
                "проверить — list_task_attachments.")
    except Exception as e:
        logger.exception("Error in create_attachment_upload_url")
        return _tool_error("building upload link", e)


# ---------------------------------------------------------------------------
# Tag write operations (v2)
# ---------------------------------------------------------------------------

def _describe_create_tag(p: Dict) -> str:
    return f'Создаю тег «{p.get("name")}»' + (f' (цвет {p["color"]})' if p.get("color") else "")


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def create_tag(name: str, color: str = None, manifest_id: str = "",
                     user_reply: str = "", automation_key: str = "") -> str:
    """
    Create a tag (requires v2 API). color is an optional hex like '#FF6161'.
    Gated 🟡 (docs/DESIGN_approval_gate.md): two calls, same tool name —
    nothing is created on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is created yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — name/color are ignored on
    this call (the manifest's own stored values are used). Do NOT make call
    #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        name: Tag name (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        color: Optional hex color like '#FF6161'
        manifest_id: from call #1's response — pass on call #2 to actually create
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"name": name, "color": color}
    outcome = await _gate_single("create_tag", "create_tag",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, _describe_create_tag,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _create_tag_impl(**outcome.extra)


async def _create_tag_impl(name: str, color: str = None) -> str:
    """Pure mutation logic for create_tag — no consent gate. Called only by
    the gated create_tag() above once the plan is approved."""
    try:
        await _run_blocking(lambda: ticktick_v2.create_tag(name, color))
        # Lightweight inline check — create_tag doesn't write to the journal
        # (tag creation isn't journaled), so this is the only proof available.
        after = await _live_tag_names()
        if name.lower() in after:
            return f"✅ Тег «{name}» создан (проверено)."
        return (f"⚠️ Тег «{name}» отправлен на создание, но не виден в "
                "свежем списке тегов — проверь вручную.")
    except Exception as e:
        logger.exception("Error in create_tag")
        return _tool_error("creating tag", e)


async def _live_tag_names(force: bool = True) -> List[str]:
    """Lowercased names of live tags (optionally force-fresh)."""
    if force:
        await _run_blocking(lambda: ticktick_v2.get_state(force=True))
    tags = await _run_blocking(lambda: ticktick_v2.get_tags())
    return [(t.get("name") or "").lower() for t in tags]


@mcp.tool()
async def rename_tag(old_name: str, new_name: str, allow_merge: bool = False,
                     user_reply: str = "") -> str:
    """Rename a tag (requires v2 API).

    If new_name already exists as a tag, TickTick MERGES the two tags — that
    is irreversible (which tasks carried which tag is lost), so this branch
    is gated 🔴 (docs/DESIGN_approval_gate.md): it's refused unless BOTH
    allow_merge=True AND user_reply=<the user's literal message actually
    confirming the merge> are given. Setting allow_merge=True on your own
    judgement, without user_reply, is NOT sufficient — don't fabricate the
    reply.

    TELEGRAM CONFIRMATION LAYER (optional, off by default) — merge branch
    only: the refusal that describes the merge is ALSO sent by this server to
    the owner in Telegram, as a message with [✅ Подтвердить]/[🛑 Отклонить]
    buttons; that is the server's own second factor, not an external relay
    wrapped around MCP. In that mode the TEXT path is CLOSED: repeating the
    call with allow_merge=true and a genuine `user_reply` is refused whether
    or not the button was pressed. Pressing ✅ makes the SERVER merge the tags
    on its own (background poller) and report into that same message. The
    plain-rename branch (no existing target tag) is not gated at all and never
    notifies.

    Args:
        old_name: current tag name
        new_name: new tag name
        allow_merge: pass True ONLY after the user confirmed merging into an
            existing tag (also requires user_reply — see above)
        user_reply: REQUIRED when a merge would happen — the user's literal
            message confirming it
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        existing = await _live_tag_names()
        if old_name.lower() not in existing:
            near = ", ".join(n for n in existing if n[:3] == old_name.lower()[:3]) \
                or "нет похожих"
            return (f"🛑 НЕ переименовал — тега «{old_name}» не существует "
                    f"(возможно опечатка; похожие: {near}). Ничего не тронул.")
        will_merge = new_name.lower() in existing
        if will_merge:
            # Тот же перевод на манифест, что у delete_project (2026-08-06):
            # раньше здесь стоял inline-🔴 (`manifest=None`) без `tool=`, и
            # необратимое слияние тегов оставалось вне Telegram-контура.
            # Дописать один `tool=` было нельзя — плана в Telegram нет,
            # `check_approval` вернул бы "none" и тул отказывал бы ВСЕГДА при
            # включённом слое. Ключ связи двух вызовов — пара имён тегов;
            # публичная сигнатура не тронута.
            key = f"{old_name.lower()}→{new_name.lower()}"
            mid, m = _find_live_inline_manifest("rename_tag_merge", key)
            cr = _require_consent(action="rename_tag_merge", tier=2,
                                  manifest=m, user_reply=user_reply,
                                  object_ids=([old_name.lower(), new_name.lower()]
                                              if m is not None else None),
                                  manifest_id=mid,
                                  # см. delete_project: анти-дуплетного
                                  # таймера у этой ветки не было — не вводим
                                  # его вместе с манифестом.
                                  min_gap=0)
            if cr.ok and m is None and tg_approval.enabled_for(_TG_CFG, "rename_tag"):
                # см. тот же комментарий в delete_project: без этого вызов
                # сразу с allow_merge=true + user_reply="да" слил бы теги в
                # обход кнопки при включённом слое. Откатываемся к фазе
                # плана, а не отказываем навсегда.
                cr = ConsentResult(False, "")
            if not (allow_merge and cr.ok):
                # Человеческая часть и инструкция ДЛЯ МОДЕЛИ — РАЗДЕЛЬНО
                # (2026-08-06, дефект №2): раньше это была одна строка `msg`,
                # и «Покажи это пользователю дословно и... повтори с
                # allow_merge=true и user_reply=...» уходило дословно в
                # Telegram-карточку плана вместе с остальным. Ранний возврат
                # ниже (план уже есть/отказ) НЕ ходит в Telegram — там `msg`
                # как раньше несёт обе части одной строкой для модели.
                human_msg = (f"🛑 Тег «{new_name}» уже существует — это будет "
                            f"СЛИЯНИЕ тегов «{old_name}» и «{new_name}» "
                            "(необратимо: какие задачи носили какой тег — "
                            "потеряется). Ничего не тронул.")
                # `cr.reason` здесь (когда непусто) — тоже реплика ДЛЯ МОДЕЛИ
                # от `_require_consent` («вызывай его ТОЛЬКО после того, как
                # человек ответил... Передай user_reply=...»), а не для
                # человека — та же утечка, найденная тестом на этой ветке
                # (2026-08-06). Кладём её к agent_tail, а не к human_msg.
                agent_tail = ("Покажи это пользователю дословно и, ТОЛЬКО "
                             "после его явного согласия, повтори с "
                             "allow_merge=true и user_reply=<дословная "
                             "реплика пользователя>." +
                             (f" ({cr.reason})"
                              if (not cr.ok and cr.reason) else ""))
                msg = human_msg + " " + agent_tail
                if m is not None or _is_negative_reply(user_reply):
                    # План уже есть (возможно, уже висит кнопкой в Telegram)
                    # либо человек отказался — второй раз не шлём.
                    if m is not None:
                        # Живой план обязан назвать себя — это тот же id, что
                        # в callback_data кнопок и в строке `tg_approvals`.
                        msg += "\n" + _plan_id_line(mid, "ничего ещё не тронуто")
                    return msg
                new_mid = uuid.uuid4().hex[:12]
                now = time.monotonic()
                _MANIFESTS[new_mid] = {
                    "kind": "rename_tag_merge", "key": key,
                    "old_name": old_name, "new_name": new_name,
                    "created": now, "plan_shown_at": now,
                    "summary": f"Слияние тегов «{old_name}» → «{new_name}»",
                    "consumed": False, "tool": "rename_tag",
                    "_gate": "rename_tag_merge",
                    "object_hash": _manifest_object_hash(
                        "rename_tag_merge",
                        [old_name.lower(), new_name.lower()])}
                # Идентификатор плана — В ОТВЕТЕ (2026-08-06), см. тот же
                # комментарий в delete_project: до этой строки план слияния
                # тегов был снаружи безымянным, и нажать по нему кнопку мог
                # только код, живущий В ЭТОМ ЖЕ процессе (он читал `_MANIFESTS`
                # напрямую) — ни один сетевой клиент так не может.
                human_msg += "\n" + _plan_id_line(new_mid, "ничего ещё не тронуто")
                return await _maybe_tg_notify_plan("rename_tag", new_mid,
                                                   human_msg, agent_tail)
            if m is not None:
                _mark_manifest_consumed(m, mid)  # one-shot
        return await _rename_tag_impl(old_name, new_name,
                                      merged=bool(allow_merge and will_merge))
    except Exception as e:
        logger.exception("Error in rename_tag")
        return _tool_error("renaming tag", e)


async def _rename_tag_impl(old_name: str, new_name: str,
                           merged: bool = False) -> str:
    """Само переименование/слияние тега, БЕЗ гейта — согласие уже получено
    вызывающим (`rename_tag` после `_require_consent` в merge-ветке, обычное
    переименование гейта не имеет вовсе, либо фоновый поллер по нажатой
    кнопке через `_auto_execute_rename_tag`).

    Вынесена 2026-08-06 по той же причине, что и `_delete_project_impl`: без
    неё нажатие кнопки на плане СЛИЯНИЯ тегов не приводило ни к чему."""
    await _run_blocking(lambda: ticktick_v2.rename_tag(old_name, new_name))
    # Post-verify against a fresh tag list.
    after = await _live_tag_names()
    if old_name.lower() in after:
        return (f"❌ Тег «{old_name}» ВСЁ ЕЩЁ существует — переименование "
                "не сработало.")
    if new_name.lower() not in after:
        return (f"❌ Тега «{new_name}» нет после переименования — исход "
                "не подтверждён, проверь вручную.")
    note = " (слито с существующим)" if merged else ""
    return f"✅ Тег «{old_name}» переименован в «{new_name}» (проверено){note}."


# Конфликт слияния 2026-08-06, разрешён сохранением ОБЕИХ сторон: эта ветка
# добавляла сюда `_rename_tag_impl` (исполнитель для кнопки), а пришедшая из
# #12 — `_describe_delete_tag` (описатель плана удаления тега). Обе нужны и
# друг с другом никак не связаны — совпало только место вставки, сразу после
# `rename_tag`.
def _describe_delete_tag(p: Dict) -> str:
    return f'Удаляю тег «{p.get("name")}»'


@mcp.tool()
@_shared_notes(automation=True, gate_args=True)
async def delete_tag(name: str, manifest_id: str = "", user_reply: str = "",
                     automation_key: str = "") -> str:
    """
    Delete a tag (requires v2 API). Tasks keep existing; they just lose the
    tag. Gated 🟡 (docs/DESIGN_approval_gate.md): two calls, same tool name —
    nothing is deleted on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is deleted yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — name is ignored on this
    call (the manifest's own stored value is used). Do NOT make call #2 in
    the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        name: Tag name (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        manifest_id: from call #1's response — pass on call #2 to actually delete
        {{GATE_ARGS_TAIL}}
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"name": name}
    outcome = await _gate_single("delete_tag", "delete_tag",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, _describe_delete_tag,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _delete_tag_impl(**outcome.extra)


async def _delete_tag_impl(name: str) -> str:
    """Pure mutation logic for delete_tag — no consent gate. Called only by
    the gated delete_tag() above once the plan is approved."""
    try:
        existing = await _live_tag_names()
        if name.lower() not in existing:
            near = ", ".join(n for n in existing if n[:3] == name.lower()[:3]) \
                or "нет похожих"
            return (f"🛑 НЕ удалил — тега «{name}» не существует (возможно "
                    f"опечатка — Latin/Cyrillic? похожие: {near}). Ничего не тронул.")
        # Blast radius: how many tasks are about to lose the tag.
        carriers = await _run_blocking(lambda: ticktick_v2.get_tasks_by_tag(name))
        await _run_blocking(lambda: ticktick_v2.delete_tag(name))
        after = await _live_tag_names()
        if name.lower() in after:
            return f"❌ Тег «{name}» ВСЁ ЕЩЁ существует — удаление не сработало."
        return (f"✅ Тег «{name}» удалён (проверено). Снят с "
                f"**{len(carriers)}** открытых задач(и); сами задачи не тронуты.")
    except Exception as e:
        logger.exception("Error in delete_tag")
        return _tool_error("deleting tag", e)




# ---------------------------------------------------------------------------
# Won't-do / duplicate (v2)
# ---------------------------------------------------------------------------

def _describe_abandon_task(p: Dict, live_title: Optional[str] = None) -> str:
    # `live_title` — живое название, УЖЕ прочитанное identity-guard'ом
    # вызывающего (см. abandon_task ниже). Раньше сюда доставался фолбэк
    # `task_title or task_id`: при пустом summary карточка говорила «задачу
    # «6a7571238f0854e347f51407»» — id в позиции имени, в кавычках.
    # Фолбэк на `_live_task_title` (П15, 2026-08-09): у БЕЗЫМЯННОЙ задачи
    # guard возвращает пустое живое имя, и карточка говорила «её нет в живом
    # состоянии аккаунта» про задачу, которую сам же guard только что
    # подтвердил. `_live_task_title` в этом случае даёт заменитель по
    # содержимому и None только когда задачи действительно нет.
    return p.get("summary") or (
        "Отмечаю «не буду делать» задачу "
        + _plan_task_name({"taskId": p.get("task_id"),
                           "title": (live_title or p.get("task_title")
                                     or _live_task_title(p.get("task_id") or ""))}))


@mcp.tool()
@_shared_notes(automation=True, gate_args=True)
async def abandon_task(summary: str, task_id: str, task_title: str = None,
                       manifest_id: str = "", user_reply: str = "",
                       automation_key: str = "") -> str:
    """
    Mark a task as 'Won't do' (requires v2 API). Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    marked on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is changed yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    When this server's Telegram approval layer is enabled, call #1 ALSO sends
    the plan to the owner as a Telegram message carrying «✅ Подтвердить» /
    «🛑 Отклонить» buttons, and pressing «✅ Подтвердить» makes THIS server
    run the operation itself (a background poller executes the stored
    manifest and reports back into the same Telegram message) — no external
    relay is involved. In that mode a text user_reply alone is NOT enough:
    without the button press call #2 is refused and nothing is changed.

    {{AUTOMATION_KEY_NOTE}}

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's), e.g. «Отмечаю «не буду делать»
    задачу „Купить молоко"».

    Args:
        summary: Human-readable confirmation line (see above)
        task_id: ID of the task
        task_title: Title of the task (optional but recommended)
        manifest_id: from call #1's response — pass on call #2 to actually mark it
        {{GATE_ARGS_TAIL}}

    task_id (and task_title, when given) is cross-checked against the LIVE
    task list TWICE (same pattern as delete_habit, def-116, 2026-08-07): once
    while BUILDING the plan (call #1, before anything is shown to the owner)
    and again, independently, right before the actual abandon (call #2,
    unchanged). If the live read itself fails while building the plan, the
    plan still gets built (a read hiccup must not block every abandon), but
    its text says so honestly — the call #2 check is unconditional and still
    guards the mutation either way. Only an OPEN task can be abandoned — a
    task not among open tasks (already completed/deleted/wrong id) refuses
    the plan outright, matching _abandon_task_impl's own severity on
    execution.
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (task_id↔task_title) на построение плана — тот
    # же _guard_task, что уже стоит в _abandon_task_impl НА ИСПОЛНЕНИИ (той
    # же формой вызова, что duplicate_task: project_id не передаётся —
    # abandon_task его и не принимает). mismatch И missing здесь блокируют
    # план целиком — _abandon_task_impl уже трактует ОБА как 🛑 на исполнении
    # (в отличие от класса операций над завершёнными — комментарии,
    # вложения, duplicate_task, см. _guard_task_incl_completed):
    # «не буду делать» можно пометить ТОЛЬКО открытую задачу —
    # перенос это не ужесточает, он лишь воспроизводит существующую
    # политику раньше по времени. Временная недоступность живого чтения —
    # fail-open с предупреждением, исполнение (не тронуто) перепроверяет
    # заново. automation_key НЕ пропускает эту проверку — она стоит раньше
    # самого гейта.
    name_warning = ""
    live_title = None
    if not manifest_id:
        g = _guard_task(task_id, task_title or "")
        refusal, warn = _guard_or_refuse(
            g, stage="план", expected=task_title,
            missing_name=task_title or _lookup_task_title(task_id),
            unavailable_note=_UNVERIFIED_TASK)
        if refusal:
            return refusal
        name_warning = f" {warn}" if warn else ""
        # Живое название для карточки: guard его уже прочитал (status "ok"),
        # и это оно, а не переданное, должно стоять в позиции имени.
        live_title = g.title if g.ok else None
    params = {"summary": summary, "task_id": task_id, "task_title": task_title}
    describe_fn = (functools.partial(_describe_abandon_task, live_title=live_title)
                   if not name_warning else
                   (lambda p: _describe_abandon_task(p, live_title) + name_warning))
    outcome = await _gate_single("abandon_task", "abandon_task",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _abandon_task_impl(**outcome.extra)


async def _abandon_task_impl(summary: str, task_id: str,
                             task_title: str = None) -> str:
    """Pure mutation logic for abandon_task — no consent gate. Called only by
    the gated abandon_task() above once the plan is approved."""
    title = task_title or _lookup_task_title(task_id)
    g = _guard_task(task_id, task_title or "")
    # То же, что в _attach_file_to_task_impl/_duplicate_task_impl: задача,
    # выпавшая из v2-снапшота, но найденная guard'ом через официальный API
    # (реально наблюдалось — см. комментарий к _official_task_snapshot),
    # печаталась как «[task 6a757123…]» на УСПЕШНОМ пути.
    title = title if title != f"[task {task_id[:8]}…]" else (g.title or title)
    refusal, _warn = _guard_or_refuse(
        g, stage="исполнение", verb="НЕ отметил", expected=task_title,
        missing_name=title)
    if refusal:
        return refusal
    try:
        await _run_blocking(lambda: ticktick_v2.abandon_task(task_id))
        rid = _op_journal("abandon", [{"taskId": task_id, "title": title}], summary)
        # Post-verify: an abandoned task leaves the open pool.
        fresh = _open_by_id(fresh=True)
        if fresh is None:
            return (f"Отметка «не буду делать» для «{title}» отправлена, но "
                    f"{_UNVERIFIED_MSG}\n" + _report_line(rid))
        if task_id in fresh:
            return (f"❌ НЕ отмечено «{title}» — задача всё ещё среди открытых.\n"
                    + _report_line(rid))
        return f"✅ Не буду делать: «{title}» (проверено)\n" + _report_line(rid)
    except Exception as e:
        logger.exception("Error in abandon_task")
        return _tool_error("abandoning task", e)


def _describe_duplicate_task(p: Dict, live_title: Optional[str] = None) -> str:
    # `live_title` — то же, что у _describe_abandon_task выше: живое имя,
    # уже добытое guard'ом вызывающего, вместо голого id в позиции имени.
    return p.get("summary") or (
        "Дублирую задачу "
        + _plan_task_name({"taskId": p.get("task_id"),
                           "title": (live_title or p.get("task_title")
                                     or _live_task_title(p.get("task_id") or ""))}))


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def duplicate_task(summary: str, task_id: str, task_title: str = None,
                         manifest_id: str = "", user_reply: str = "",
                         automation_key: str = "") -> str:
    """
    Duplicate a task within the same project (requires v2 API). Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    duplicated on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is duplicated yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's), e.g. «Дублирую задачу „Купить молоко"».

    Args:
        summary: Human-readable confirmation line (see above)
        task_id: ID of the task
        task_title: Title of the task (optional but recommended for confirmation)
        manifest_id: from call #1's response — pass on call #2 to actually duplicate
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    task_id (and task_title, when given) is cross-checked against the LIVE
    task list TWICE (same pattern as delete_habit, def-116, 2026-08-07): once
    while BUILDING the plan (call #1, before anything is shown to the owner)
    and again, independently, right before the actual duplicate (call #2,
    unchanged). If the live read itself fails while building the plan, the
    plan still gets built (a read hiccup must not block every duplicate),
    but its text says so honestly — the call #2 check is unconditional and
    still guards the mutation either way.

    Works on a COMPLETED task too (attaching a receipt to a finished job,
    appending the outcome, duplicating it as a template are all normal): the
    check then runs against the source that still knows the task, so the
    title IS verified, and the plan/result say the task is completed. A task
    that a source DOES know but that sits in the TRASH is refused instead
    (see restore_tasks) — trash gets its own refusal, separate from an id
    that no source knows at all.
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (task_id↔task_title) на построение плана — тот
    # же guard, что уже стоит в _duplicate_task_impl НА ИСПОЛНЕНИИ, но
    # здесь — ДО показа карточки владельцу. task_title опционален — guard
    # всё равно выполняет проверку «id существует» даже без него
    # (_names_agree с пустой строкой всегда согласна), ровно как делает
    # _duplicate_task_impl. Временная недоступность живого чтения —
    # fail-open с предупреждением, исполнение (не тронуто) перепроверяет
    # заново. automation_key НЕ пропускает эту проверку — она стоит раньше
    # самого гейта.
    #
    # ЗАВЕРШЁННАЯ ЗАДАЧА БОЛЬШЕ НЕ ОТКАЗ (дефект №2, живая приёмка
    # 2026-08-07): раньше этот тул превращал guard'ов `missing` в 🛑, тогда
    # как четверо соседей по классу тот же самый ответ смягчали и работали.
    # Строгость была обратна риску — дублирование СОЗДАЁТ КОПИЮ и ничего не
    # портит, а «продублировать завершённое как шаблон» — обычный сценарий.
    # Решение принято один раз для всего класса в _guard_task_incl_completed.
    name_warning = ""
    live_title = None
    if not manifest_id:
        g = _guard_task_incl_completed(task_id, task_title or "")
        refusal, warn = _guard_or_refuse(
            g, stage="план", expected=task_title, missing_says="message",
            unavailable_note=_UNVERIFIED_TASK)
        if refusal:
            return refusal
        name_warning = f" {warn}" if warn else ""
        # Живое название для карточки — и для ЗАВЕРШЁННОЙ задачи тоже
        # (status "completed" означает, что источник её знает и имя
        # сверено): голый id в позиции имени не оправдан ни в одном из
        # двух состояний, с которыми работает этот тул.
        live_title = g.title if g.status in ("ok", "completed") else None
    params = {"summary": summary, "task_id": task_id, "task_title": task_title}
    describe_fn = (functools.partial(_describe_duplicate_task, live_title=live_title)
                   if not name_warning else
                   (lambda p: _describe_duplicate_task(p, live_title) + name_warning))
    outcome = await _gate_single("duplicate_task", "duplicate_task",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _duplicate_task_impl(**outcome.extra)


async def _duplicate_task_impl(summary: str, task_id: str, task_title: str = None) -> str:
    """Pure mutation logic for duplicate_task — no consent gate. Called only
    by the gated duplicate_task() above once the plan is approved."""
    title = task_title or _lookup_task_title(task_id)
    g = _guard_task_incl_completed(task_id, task_title or "")
    refusal, _warn = _guard_or_refuse(
        g, stage="исполнение", verb="НЕ дублировал", expected=task_title,
        missing_says="message")
    if refusal:
        return refusal
    warn = f"\nℹ️ {_COMPLETED_TASK_NOTE}." if g.status == "completed" else ""
    title = title if title != f"[task {task_id[:8]}…]" else (g.title or title)
    try:
        copy = await _run_blocking(lambda: ticktick_v2.duplicate_task(task_id))
        cid = copy.get("id")
        rid = _op_journal("create", [
            {"taskId": cid, "title": copy.get("title") or title,
             "expect": {"projectId": copy.get("projectId")}}],
            summary)
        # Post-verify: the copy must actually exist in fresh open state.
        fresh = _open_by_id(fresh=True)
        confirmed = fresh is not None and cid in fresh
        # Копия ЗАВЕРШЁННОЙ задачи может унаследовать её статус, и тогда её
        # нет среди открытых — искать подтверждение надо там, где такая
        # копия живёт, иначе законная операция систематически рапортует ❌
        # об успешном дублировании. project_id копии известен из ответа, так
        # что это одно точечное чтение, а не скан.
        if not confirmed and fresh is not None and g.status == "completed" and cid:
            confirmed = _closed_task_snapshot(cid, copy.get("projectId") or "") is not None
        if fresh is None:
            verdict = f"Дублирование отправлено, но {_UNVERIFIED_MSG}"
        elif not confirmed:
            verdict = ("❌ Копия НЕ подтвердилась — её нет ни среди открытых "
                       "задач, ни среди завершённых, проверь вручную.")
        else:
            verdict = (f"✅ Дублировано (проверено): «{title}» → копия "
                       f"«{copy.get('title') or title}»")
        # ℹ️, а не ⚠️: это ФАКТ о продукте копирования, известный заранее и
        # проверке не противоречащий. Пока он нёс ⚠️, `_EXEC_WARN_MARKERS`
        # понижал по нему вердикт КАЖДОГО успешного дублирования до
        # «⚠️ подтверждено частично» — оговорка стоила операции её «✅».
        return (verdict + warn + "\nℹ️ В копию НЕ переносятся: чек-лист (items), "
                "kanban-раздел (column) и привязка к родителю.\n"
                + _report_line(rid))
    except Exception as e:
        logger.exception("Error in duplicate_task")
        return _tool_error("duplicating task", e)


# ---------------------------------------------------------------------------
# Comment edit/delete (v2)
# ---------------------------------------------------------------------------

def _describe_update_task_comment(p: Dict) -> str:
    return f'Правлю комментарий на «{p.get("task_title")}»: новый текст «{p.get("text")}»'


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def update_task_comment(task_title: str, text: str, project_id: str,
                              task_id: str, comment_id: str,
                              manifest_id: str = "", user_reply: str = "",
                              automation_key: str = "") -> str:
    """
    Edit a task comment (requires v2 API). Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    changed on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is changed yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        task_title: Title of the task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        text: New comment text
        project_id: ID of the project
        task_id: ID of the task
        comment_id: ID of the comment to edit
        manifest_id: from call #1's response — pass on call #2 to actually edit
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    task_id and task_title are cross-checked against the LIVE task list
    TWICE (same pattern as delete_habit, def-116, 2026-08-07): once while
    BUILDING the plan (call #1, before anything is shown to the owner — a
    mismatched title never reaches the plan card at all) and again,
    independently, right before the actual edit (call #2, unchanged). If the
    live read itself fails while building the plan, the plan still gets
    built (a read hiccup must not block every comment edit), but its text
    says so honestly — the call #2 check is unconditional and still guards
    the mutation either way.

    Works on a COMPLETED task too (attaching a receipt to a finished job,
    appending the outcome, duplicating it as a template are all normal): the
    check then runs against the source that still knows the task, so the
    title IS verified, and the plan/result say the task is completed. A task
    that a source DOES know but that sits in the TRASH is refused instead
    (see restore_tasks) — trash gets its own refusal, separate from an id
    that no source knows at all.
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (task_id↔task_title) на построение плана — тот
    # же _guard_task, что уже стоит в _update_task_comment_impl НА
    # ИСПОЛНЕНИИ, но здесь — ДО показа карточки владельцу. Комментарий с тем
    # же обоснованием — см. attach_file_to_task выше (та же правка).
    name_warning = ""
    if not manifest_id:
        g = _guard_task_incl_completed(task_id, task_title or "", project_id)
        refusal, warn = _guard_or_refuse(
            g, stage="план", expected=task_title, says="message",
            missing_says="message", unavailable_note=_UNVERIFIED_TASK_TITLE)
        if refusal:
            return refusal
        name_warning = f" {warn}" if warn else ""
    params = {"task_title": task_title, "text": text, "project_id": project_id,
              "task_id": task_id, "comment_id": comment_id}
    describe_fn = ((lambda p: _describe_update_task_comment(p) + name_warning)
                   if name_warning else _describe_update_task_comment)
    outcome = await _gate_single("update_task_comment", "update_task_comment",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _update_task_comment_impl(**outcome.extra)


async def _update_task_comment_impl(task_title: str, text: str, project_id: str,
                                    task_id: str, comment_id: str) -> str:
    """Pure mutation logic for update_task_comment — no consent gate. Called
    only by the gated update_task_comment() above once the plan is approved."""
    try:
        g = _guard_task_incl_completed(task_id, task_title or "", project_id)
        refusal, _warn = _guard_or_refuse(
            g, stage="исполнение", verb="НЕ изменил комментарий",
            expected=task_title, missing_says="message")
        if refusal:
            return refusal
        pid = g.project_id or project_id
        # (client-side: update_task_comment fetches the comment first and
        # raises if comment_id is absent — a moved/stale pid errors loudly)
        await _run_blocking(lambda: ticktick_v2.update_task_comment(pid, task_id, comment_id, text))
        # Post-verify: the new text must be visible in the comment list.
        cms = await _run_blocking(lambda: ticktick_v2.get_task_comments(pid, task_id))
        cm = next((c for c in cms if c.get("id") == comment_id), None)
        if cm is None:
            return (f"❌ Комментарий к '{task_title}' после правки НЕ найден — "
                    "исход не подтверждён, проверь вручную.")
        if (cm.get("title") or "") != text:
            return (f"❌ Правка комментария к '{task_title}' НЕ применилась "
                    "(текст прежний).")
        # ✅ остаётся ✅ и на завершённой задаче: правка комментария
        # подтверждена post-verify выше, а название сверено с живой записью
        # (_guard_task_incl_completed) — подтверждено ВСЁ, что легенда
        # требует от ✅. Пометка про завершённость — это факт о состоянии
        # задачи, а не пробел в проверке (раньше здесь стояло «название НЕ
        # проверено», и это была правда лишь потому, что проверку не делали).
        warn = f"\nℹ️ {_COMPLETED_TASK_NOTE}." if g.status == "completed" else ""
        return f"✅ Комментарий на «{task_title}» обновлён (проверено).{warn}"
    except Exception as e:
        logger.exception("Error in update_task_comment")
        return _tool_error("updating comment", e)


def _describe_delete_task_comment(p: Dict) -> str:
    return f'Удаляю комментарий на «{p.get("task_title")}»'


@mcp.tool()
@_shared_notes(automation=True, gate_args=True)
async def delete_task_comment(task_title: str, project_id: str, task_id: str,
                              comment_id: str, manifest_id: str = "",
                              user_reply: str = "", automation_key: str = "") -> str:
    """
    Delete a task comment (requires v2 API). Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    deleted on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is deleted yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        task_title: Title of the task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        project_id: ID of the project
        task_id: ID of the task
        comment_id: ID of the comment to delete
        manifest_id: from call #1's response — pass on call #2 to actually delete
        {{GATE_ARGS_TAIL}}

    task_id and task_title are cross-checked against the LIVE task list
    TWICE (same pattern as delete_habit, def-116, 2026-08-07): once while
    BUILDING the plan (call #1, before anything is shown to the owner — a
    mismatched title never reaches the plan card at all) and again,
    independently, right before the actual delete (call #2, unchanged). If
    the live read itself fails while building the plan, the plan still gets
    built (a read hiccup must not block every comment deletion), but its
    text says so honestly — the call #2 check is unconditional and still
    guards the mutation either way.

    Works on a COMPLETED task too (attaching a receipt to a finished job,
    appending the outcome, duplicating it as a template are all normal): the
    check then runs against the source that still knows the task, so the
    title IS verified, and the plan/result say the task is completed. A task
    that a source DOES know but that sits in the TRASH is refused instead
    (see restore_tasks) — trash gets its own refusal, separate from an id
    that no source knows at all.
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (task_id↔task_title) на построение плана — тот
    # же _guard_task, что уже стоит в _delete_task_comment_impl НА
    # ИСПОЛНЕНИИ, но здесь — ДО показа карточки владельцу. Тот же перенос,
    # что и у attach_file_to_task/update_task_comment (см. их коммиты) —
    # здесь он особенно важен: удаление комментария необратимо.
    name_warning = ""
    if not manifest_id:
        g = _guard_task_incl_completed(task_id, task_title or "", project_id)
        refusal, warn = _guard_or_refuse(
            g, stage="план", expected=task_title, says="message",
            missing_says="message", unavailable_note=_UNVERIFIED_TASK_TITLE)
        if refusal:
            return refusal
        name_warning = f" {warn}" if warn else ""
    params = {"task_title": task_title, "project_id": project_id,
              "task_id": task_id, "comment_id": comment_id}
    describe_fn = ((lambda p: _describe_delete_task_comment(p) + name_warning)
                   if name_warning else _describe_delete_task_comment)
    outcome = await _gate_single("delete_task_comment", "delete_task_comment",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, describe_fn,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _delete_task_comment_impl(**outcome.extra)


async def _delete_task_comment_impl(task_title: str, project_id: str,
                                    task_id: str, comment_id: str) -> str:
    """Pure mutation logic for delete_task_comment — no consent gate. Called
    only by the gated delete_task_comment() above once the plan is approved."""
    try:
        g = _guard_task_incl_completed(task_id, task_title or "", project_id)
        refusal, _warn = _guard_or_refuse(
            g, stage="исполнение", verb="НЕ удалил комментарий",
            expected=task_title, missing_says="message")
        if refusal:
            return refusal
        pid = g.project_id or project_id
        # Existence pre-check: refuse a stale comment_id instead of no-opping.
        cms = await _run_blocking(lambda: ticktick_v2.get_task_comments(pid, task_id))
        if not any(c.get("id") == comment_id for c in cms):
            return (f"🛑 НЕ удалил — комментария {comment_id} нет на задаче "
                    f"'{task_title}' (уже удалён или чужой id). Ничего не тронул.")
        await _run_blocking(lambda: ticktick_v2.delete_task_comment(pid, task_id, comment_id))
        # Post-verify: the comment must actually be gone.
        cms = await _run_blocking(lambda: ticktick_v2.get_task_comments(pid, task_id))
        if any(c.get("id") == comment_id for c in cms):
            return (f"❌ Комментарий на '{task_title}' ВСЁ ЕЩЁ существует — "
                    "удаление не сработало.")
        # См. тот же комментарий про маркер в _update_task_comment_impl выше.
        warn = f"\nℹ️ {_COMPLETED_TASK_NOTE}." if g.status == "completed" else ""
        return f"✅ Комментарий на «{task_title}» удалён (проверено).{warn}"
    except Exception as e:
        logger.exception("Error in delete_task_comment")
        return _tool_error("deleting comment", e)


# ---------------------------------------------------------------------------
# Project update / archive
# ---------------------------------------------------------------------------

def _describe_update_project(p: Dict) -> str:
    changes = []
    if p.get("name") is not None:
        changes.append(f'имя → «{p.get("name")}»')
    if p.get("color") is not None:
        changes.append(f'цвет → {p.get("color")}')
    if p.get("view_mode") is not None:
        changes.append(f'вид → {p.get("view_mode")}')
    return (f'Обновляю проект «{p.get("project_name")}»: '
            + (", ".join(changes) or "без изменений"))


@mcp.tool()
@_shared_notes(automation=True, gate_args=True)
async def update_project(project_name: str, project_id: str, name: str = None,
                         color: str = None, view_mode: str = None,
                         manifest_id: str = "", user_reply: str = "",
                         automation_key: str = "") -> str:
    """
    Update a project's name, color, or view mode (uses the official API).
    Gated 🟡 (docs/DESIGN_approval_gate.md): two calls, same tool name —
    nothing is updated on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is changed yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    When this server's Telegram approval layer is enabled, call #1 ALSO sends
    the plan to the owner as a Telegram message carrying «✅ Подтвердить» /
    «🛑 Отклонить» buttons, and pressing «✅ Подтвердить» makes THIS server
    run the operation itself (a background poller executes the stored
    manifest and reports back into the same Telegram message) — no external
    relay is involved. In that mode a text user_reply alone is NOT enough:
    without the button press call #2 is refused and nothing is changed.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        project_name: Current name of the project (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        project_id: ID of the project
        name: New name (optional)
        color: New color hex like '#F18181' (optional)
        view_mode: 'list', 'kanban', or 'timeline' (optional)
        manifest_id: from call #1's response — pass on call #2 to actually update
        {{GATE_ARGS_TAIL}}

    project_id and project_name are cross-checked against the LIVE project
    list TWICE (same pattern as delete_habit, def-116, 2026-08-07): once
    while BUILDING the plan (call #1, before anything is shown to the owner)
    and again, independently, right before the actual update (call #2,
    unchanged).
    """
    err = _ensure_official()
    if err:
        return err
    if not manifest_id:
        # Cheap, purely local sanity checks — kept BEFORE the gate so an
        # obviously broken request is refused without ever building a plan.
        # (They only look at the arguments; no network.)
        if name is None and color is None and view_mode is None:
            return ("🛑 Нечего менять — все поля (name/color/view_mode) пусты. "
                    "Ничего не тронул.")
        for label, val in (("name", name), ("color", color), ("view_mode", view_mode)):
            if val is not None and not str(val).strip():
                return (f"🛑 Пустая строка в поле {label} — клиент молча выбросил бы "
                        "её и изменение не применилось бы. Передай значение или "
                        "убери поле. Ничего не тронул.")
        if view_mode is not None and view_mode not in ("list", "kanban", "timeline"):
            return "Invalid view_mode. Must be one of: list, kanban, timeline."
        # Перенос identity-guard (project_id↔project_name) на построение
        # плана — тот же _guard_project(..., require_known=True), что уже
        # стоит в _update_project_impl НА ИСПОЛНЕНИИ, вызванный ТЕМИ ЖЕ
        # аргументами (см. move_project_to_group, 14907a9). Бинарный исход
        # (отказ либо ок) — здесь нет отдельной мягкой ветки на «временную
        # недоступность»: воспроизвожу ТУ ЖЕ строгость, что уже на
        # исполнении, а не изобретаю смягчение, которого в оригинале нет.
        # Префикс/хвост сообщения приведены к тому же виду, что у остальных
        # plan-отказов («План НЕ построен» / «Ничего не изменено») —
        # текстовая правка отображения, сама сверка не меняется.
        refusal = _guard_project_or_refuse(project_id, project_name, fresh=True,
                                           require_known=True, prefix=_PLAN_REFUSAL_PREFIX)
        if refusal:
            return refusal
    params = {"project_name": project_name, "project_id": project_id,
              "name": name, "color": color, "view_mode": view_mode}
    outcome = await _gate_single("update_project", "update_project",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, _describe_update_project,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _update_project_impl(**outcome.extra)


async def _update_project_impl(project_name: str, project_id: str,
                               name: str = None, color: str = None,
                               view_mode: str = None) -> str:
    """Pure mutation logic for update_project — no consent gate. Called only
    by the gated update_project() above once the plan is approved."""
    refusal = _guard_project_or_refuse(project_id, project_name, fresh=True,
                                       require_known=True)
    if refusal:
        return refusal
    try:
        proj = await _run_blocking(lambda: ticktick.update_project(
            project_id, name=name, color=color, view_mode=view_mode))
        if 'error' in proj:
            return (f"### ❌ Проект «{project_name}» НЕ обновлён\n\nTickTick отклонил: "
                    f"{_redact_for_user(proj['error'])}")
    except Exception as e:
        logger.exception("Error in update_project")
        return f"### ❌ Проект «{project_name}» НЕ обновлён\n\nОшибка: {_redact_for_user(e)}"

    # Post-verify: independent fresh re-read, compare each field that was
    # actually requested against what TickTick shows live — the write
    # response alone is never trusted as proof.
    try:
        fresh = await _run_blocking(ticktick.get_project, project_id)
        if not (isinstance(fresh, dict) and not fresh.get('error')):
            return (f"### ⚠️ Проект «{project_name}» обновлён, но НЕ подтверждён\n\n"
                    f"{format_project(proj)}\n\n"
                    f"⚠️ {_UNVERIFIED_MSG}")
        mismatches = []
        if name is not None and fresh.get('name') != name:
            mismatches.append(f"name: ожидалось «{name}», сейчас «{fresh.get('name')}»")
        if color is not None and fresh.get('color') != color:
            mismatches.append(f"color: ожидалось {color}, сейчас {fresh.get('color')}")
        if view_mode is not None and fresh.get('viewMode') != view_mode:
            mismatches.append(f"view_mode: ожидалось {view_mode}, сейчас {fresh.get('viewMode')}")
        new_name = fresh.get('name', project_name)
        if mismatches:
            return (f"### ❌ Проект «{project_name}» обновлён частично — расхождение\n\n"
                    f"{format_project(fresh)}\n\n"
                    "🧾 Проверка: " + "; ".join(mismatches))
        title = (f"«{project_name}» → «{new_name}»" if name is not None and new_name != project_name
                 else f"«{new_name}»")
        return (f"### ✅ Проект {title} обновлён (проверено)\n\n"
                f"{format_project(fresh)}\n\n"
                "🧾 Проверено: все изменённые поля подтверждены отдельным "
                "живым чтением TickTick.")
    except Exception as e:
        return (f"### ⚠️ Проект «{project_name}» обновлён, но НЕ подтверждён\n\n"
                f"{format_project(proj)}\n\n"
                f"⚠️ {_UNVERIFIED_MSG} ({_redact_for_user(e)})")


def _describe_archive_project(p: Dict) -> str:
    verb = "Архивирую" if p.get("archived", True) else "Разархивирую"
    return f'{verb} проект «{p.get("project_name")}»'


@mcp.tool()
@_shared_notes(automation=True, gate_args=True)
async def archive_project(project_name: str, project_id: str, archived: bool = True,
                          manifest_id: str = "", user_reply: str = "",
                          automation_key: str = "") -> str:
    """
    Archive (close) or unarchive a project (requires v2 API). Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    archived on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is changed yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    When this server's Telegram approval layer is enabled, call #1 ALSO sends
    the plan to the owner as a Telegram message carrying «✅ Подтвердить» /
    «🛑 Отклонить» buttons, and pressing «✅ Подтвердить» makes THIS server
    run the operation itself (a background poller executes the stored
    manifest and reports back into the same Telegram message) — no external
    relay is involved. In that mode a text user_reply alone is NOT enough:
    without the button press call #2 is refused and nothing is changed.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        project_name: Name of the project (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        project_id: ID of the project
        archived: True to archive, False to restore it to active
        manifest_id: from call #1's response — pass on call #2 to actually archive
        {{GATE_ARGS_TAIL}}

    project_id and project_name are cross-checked against the LIVE project
    list TWICE (same pattern as delete_habit, def-116, 2026-08-07): once
    while BUILDING the plan (call #1, before anything is shown to the owner)
    and again, independently, right before the actual archive/unarchive
    (call #2, unchanged). Same archived=True/False branching as
    _archive_project_impl in both places (see below).
    """
    err = _ensure_ready()
    if err:
        return err
    if not manifest_id:
        # Перенос identity-guard (project_id↔project_name) на построение
        # плана — ТЕ ЖЕ ветки archived=True/False, что уже стоят в
        # _archive_project_impl НА ИСПОЛНЕНИИ (см. update_project для
        # сиблинга без ветвления). archived=True (архивирование —
        # деструктивно-смежно, вынимает проект из пула синхронизации):
        # require_known=True, фейл-клоуз на неразрешимый id. archived=False
        # (разархивирование): require_known=False — _guard_project молча
        # пропускает id, который не резолвится в живое имя (архивированный
        # проект МОГ не попасть в список активных имён — это легитимный
        # сценарий, не баг), точно как уже делает _archive_project_impl на
        # исполнении; перенос это не ужесточает и не смягчает. Бинарный
        # исход — нет отдельной мягкой ветки на «временную недоступность»
        # (см. move_project_to_group, 14907a9, тот же класс guard'а).
        if archived:
            refusal = _guard_project_or_refuse(project_id, project_name, fresh=True,
                                               require_known=True,
                                               prefix=_PLAN_REFUSAL_PREFIX)
        else:
            refusal = _guard_project_or_refuse(project_id, project_name, fresh=True,
                                               prefix=_PLAN_REFUSAL_PREFIX)
        if refusal:
            return refusal
    params = {"project_name": project_name, "project_id": project_id,
              "archived": archived}
    outcome = await _gate_single("archive_project", "archive_project",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply, _describe_archive_project,
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _archive_project_impl(**outcome.extra)


async def _archive_project_impl(project_name: str, project_id: str,
                                archived: bool = True) -> str:
    """Pure mutation logic for archive_project — no consent gate. Called only
    by the gated archive_project() above once the plan is approved."""
    if archived:
        # Archiving pulls the project out of the sync pool — destructive-
        # adjacent, so verify FRESH and fail closed on an unresolvable id.
        refusal = _guard_project_or_refuse(project_id, project_name, fresh=True,
                                           require_known=True)
    else:
        refusal = _guard_project_or_refuse(project_id, project_name, fresh=True)
    if refusal:
        return refusal
    live_name = _v2_project_names().get(project_id, project_name)
    verb = 'заархивирован' if archived else 'разархивирован'
    try:
        await _run_blocking(lambda: ticktick_v2.archive_project(project_id, closed=archived))
    except RuntimeError as e:
        return f"### ❌ Проект «{live_name}» НЕ {verb}\n\nTickTick отклонил: {_redact_for_user(e)}"
    except Exception as e:
        logger.exception("Error in archive_project")
        return _tool_error("archiving project", e)

    # Post-verify: independent fresh re-read of the project's own `closed`
    # flag (not the write response) — force the v2 cache first.
    try:
        await _run_blocking(lambda: ticktick_v2.get_state(force=True))
        projs = await _run_blocking(ticktick_v2.list_projects)
        proj = next((p for p in projs if p.get("id") == project_id), None)
        if proj is None:
            return (f"### ⚠️ Проект «{live_name}» {verb}, но НЕ подтверждён\n\n"
                    f"Проекта нет в свежем списке — {_UNVERIFIED_MSG}")
        got_closed = bool(proj.get("closed"))
        if got_closed != archived:
            return (f"### ❌ Проект «{live_name}» — расхождение после архивации\n\n"
                    f"Ожидался closed={archived}, живое состояние closed={got_closed}.")
        return (f"### ✅ Проект «{live_name}» {verb} (проверено)\n\n"
                f"🧾 closed={got_closed} — подтверждено отдельным живым чтением TickTick.")
    except Exception as e:
        return (f"### ⚠️ Проект «{live_name}» {verb}, но НЕ подтверждён\n\n"
                f"⚠️ {_UNVERIFIED_MSG} ({_redact_for_user(e)})")


# ---------------------------------------------------------------------------
# Search across open + completed (v2)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READONLY)
async def search_all_tasks(
    query: str,
    include_completed: bool = True,
    scope: Literal["both", "open", "closed"] = "both",
    match: Literal["substring", "word"] = "substring",
    fields: Literal["all", "title", "content"] = "all",
    search_comments: bool = False,
) -> str:
    """
    Search tasks with selectable scope, match mode, and which fields to look in.

    scope — which projects to look in:
      • 'both'   (default) open AND closed/archived projects, reported as two
                 separate groups.
      • 'open'   only open projects.
      • 'closed' only closed/archived projects.
      The v2 sync pool omits archived projects, so tasks in a CLOSED project
      (which can still be active) are fetched separately — that's why they're a
      distinct group and why an open-only search never shows them.

    match — how the query is compared (case-insensitive):
      • 'substring' (default) query appears anywhere in the searched field —
                    so a short query like "boa" also hits inside "board".
      • 'word'      query matches as a whole word — "boa" no longer matches
                    "board". Use this to cut noise from short queries.

    fields — which fields to search (title/content, always fast):
      • 'all'     (default) task title AND content (the note body).
      • 'title'   only the task title (its name).
      • 'content' only the note body.

    search_comments — also search task COMMENTS (default False). SLOW: TickTick
      has no bulk comment API, so comments are fetched one task at a time. To
      bound the cost we only fetch comments for tasks with commentCount > 0 (when
      that field is present) and stop after 100 fetches (COMMENT_FETCH_CAP),
      noting in the output how many were scanned and whether the cap was hit.
      Comment hits are reported in their own group. Turn this on only when you
      specifically need to find a task by something written in its comments.

    Args:
        query: Text to search for.
        include_completed: Also search recently completed tasks (default True).
        scope: 'both' | 'open' | 'closed'.
        match: 'substring' | 'word'.
        fields: 'all' | 'title' | 'content'.
        search_comments: also search comments (slow; default False).
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        q = query.lower()
        if match == "word":
            pat = re.compile(r"\b" + re.escape(query) + r"\b", re.IGNORECASE | re.UNICODE)

            def _text_hit(text: str) -> bool:
                return bool(pat.search(text or ""))
        else:  # substring

            def _text_hit(text: str) -> bool:
                return q in (text or "").lower()

        def _hit(t: Dict[str, Any]) -> bool:
            if fields in ("all", "title") and _text_hit(t.get("title", "") or ""):
                return True
            if fields in ("all", "content") and _text_hit(t.get("content", "") or ""):
                return True
            return False

        want_open = scope in ("both", "open")
        want_closed = scope in ("both", "closed")

        open_pool: List[Dict[str, Any]] = []
        if want_open:
            open_pool = list(await _run_blocking(ticktick_v2.get_open_tasks))
            if include_completed:
                open_pool += await _run_blocking(lambda: ticktick_v2.get_completed_tasks(limit=100))
        open_matches = [t for t in open_pool if _hit(t)]

        # Closed/archived projects: not in the sync pool — fetch each one's data.
        closed_pool: List[Dict[str, Any]] = []
        if want_closed:
            projects = await _run_blocking(ticktick.get_projects)
            if isinstance(projects, list):
                for p in projects:
                    if not p.get("closed"):
                        continue
                    pid = p.get("id")
                    data = await _run_blocking(
                        lambda pid=pid: ticktick.get_project_with_data(pid)
                    )
                    closed_pool += data.get("tasks", []) or []
        closed_matches = [t for t in closed_pool if _hit(t)]

        # Comments: slow opt-in. Fetch per task (no bulk API), skip tasks known to
        # have zero comments, and stop after a fixed number of fetches.
        # 100, not 150: these are sequential network round-trips (no
        # concurrency, no batching), and the MCP client this tool runs
        # through has a hard ~60s timeout (see _DC_MAX_TASKS above for the
        # same constraint measured elsewhere in this file) — at 150 fetches
        # even a modest ~350ms/call already eats ~53s on top of the open/
        # closed pool work that runs before this loop, leaving no margin; a
        # timeout returns nothing at all, which is worse than an honestly
        # capped partial result.
        COMMENT_FETCH_CAP = 100
        comment_matches: List[Dict[str, Any]] = []
        comment_fetches = 0
        comment_capped = False
        if search_comments:
            already = {t.get("id") for t in open_matches + closed_matches}
            for t in open_pool + closed_pool:
                tid, pid = t.get("id"), t.get("projectId")
                if not tid or not pid or tid in already:
                    continue
                if t.get("commentCount") == 0:  # skip only when explicitly zero
                    continue
                if comment_fetches >= COMMENT_FETCH_CAP:
                    comment_capped = True
                    break
                comment_fetches += 1
                try:
                    comments = await _run_blocking(
                        lambda pid=pid, tid=tid: ticktick_v2.get_task_comments(pid, tid)
                    )
                except Exception:  # noqa: BLE001
                    continue
                # A comment's text lives in its "title" field.
                if any(_text_hit(c.get("title", "") or "") for c in comments):
                    comment_matches.append(t)
                    already.add(tid)

        if not open_matches and not closed_matches and not comment_matches:
            base = f"No tasks matched '{query}' (scope={scope}, match={match}, fields={fields}"
            if search_comments:
                base += f", comments: scanned {comment_fetches} task(s)"
                # «Не найдено» + оборванный на потолке осмотр = ответ, которого
                # не было: без этой пометки «просмотрено 100 задач» читается
                # как «просмотрены ВСЕ». Пометка стояла только в ветке с
                # результатами, где она нужна меньше — там уже видно, что
                # что-то нашлось.
                if comment_capped:
                    base += " — CAP HIT, not all tasks scanned"
            return base + ")."

        out = f"Matches for '{query}' (scope={scope}, match={match}, fields={fields}):\n\n"
        if want_open:
            out += f"── Open projects ({len(open_matches)}) ──\n"
            out += format_task_tree(open_matches, 100) if open_matches else "(none)\n"
            out += "\n"
        if want_closed:
            out += f"── Closed / archived projects ({len(closed_matches)}) ──\n"
            out += (
                "\n".join(format_task(t) for t in closed_matches[:100])
                if closed_matches
                else "(none)\n"
            )
            out += "\n"
        if search_comments:
            cap_note = " — CAP HIT, not all tasks scanned" if comment_capped else ""
            out += (
                f"── Comment matches ({len(comment_matches)}; "
                f"fetched comments for {comment_fetches} task(s){cap_note}) ──\n"
            )
            out += (
                "\n".join(format_task(t) for t in comment_matches[:100])
                if comment_matches
                else "(none)\n"
            )
        return out
    except Exception as e:
        logger.exception("Error in search_all_tasks")
        return _tool_error("searching tasks", e)


@mcp.tool(annotations=READONLY)
async def get_task_info(task_id: str) -> str:
    """
    Detailed view of a task (requires v2 API): all fields, who created it and
    when, last-modified time, its checklist items, AND its subtasks (child
    tasks). Use this when you need the full picture of a task.

    Args:
        task_id: ID of the task
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        state = await _run_blocking(lambda: ticktick_v2.get_state())
        owner = (state.get("inboxId") or "").replace("inbox", "")
        names = _v2_project_names()
        tasks = state.get("syncTaskBean", {}).get("update", []) or []
        t = next((x for x in tasks if x.get("id") == task_id), None)
        if not t:
            # Three distinct causes hide behind "not in the open sync": the task
            # is completed, it is in the trash, or it lives in an archived/closed
            # project. The old one-liner named two of them and guessed. The trash
            # one can be ANSWERED — one extra request, paid only on this branch
            # (a miss), never on a successful read.
            in_trash, _ = await _trash_state(task_id)
            if in_trash:
                return (f"Task {task_id} is IN THE TRASH (deleted). "
                        "restore_tasks can bring it back.")
            if in_trash is False:
                return (f"Task {task_id} not found among open tasks and it is NOT in "
                        "the trash — it is either completed, or in an archived/closed "
                        "project.")
            return (f"Task {task_id} not found among open tasks; the trash could NOT "
                    "be checked. It may be completed, in the trash, or in an "
                    "archived/closed project.")

        pr = PRIORITY_MAP.get(t.get("priority", 0))
        status = {0: "Active", 2: "Completed", -1: "Won't do"}.get(t.get("status", 0), t.get("status"))
        creator = str(t.get("creator", ""))
        # Люди в этом выводе назывались номерами: «assignee: 333444» и
        # «created … by user 333444». Номер участника человек глазами не
        # сверяет — а в аккаунте с общими проектами именно по этой строке
        # понимают, КТО поставил задачу и на кого она. Список участников
        # проекта читается только когда есть кого называть, и его
        # недоступность честно печатается словами, а не молчаливым номером.
        members: Dict[str, str] = {}
        if t.get("assignee") or (creator and creator != owner):
            members = await _run_blocking(_member_names, t.get("projectId") or "")
        if not creator:
            who = "автор неизвестен (TickTick не вернул поле creator)"
        elif creator == owner:
            who = "you"
        else:
            who = _person_label(creator, members)

        out = f"Task: {t.get('title')}\n"
        out += f"  id: {t.get('id')}  |  project: {names.get(t.get('projectId'), t.get('projectId'))}\n"
        out += f"  status: {status}  |  priority: {pr}\n"
        if t.get("parentId"):
            all_tasks = state.get("syncTaskBean", {}).get("update", []) or []
            parent = next((x for x in all_tasks if x.get("id") == t["parentId"]), None)
            pname = parent.get("title") if parent else t["parentId"]
            out += f"  parent: «{pname}»  (id:{t['parentId']})\n"
        # Same renderer as format_task()/format_task_line() — deliberately not
        # a second, local implementation. This branch printed the raw stored
        # instant for any TIMED deadline ("2026-08-08T21:00:00.000+0000"),
        # while get_task and the listings had already moved to the owner's
        # zone: one live task then answered 2026-08-06 in one tool and
        # 2026-08-07 in this one, with no third source to break the tie.
        # _local_datetime_str keeps all-day values verbatim (#36) and appends
        # its own "(all-day)" marker, so no extra suffix here.
        if t.get("startDate"):
            out += f"  start: {_local_datetime_str(t, 'startDate')}\n"
        if t.get("dueDate"):
            out += f"  due: {_local_datetime_str(t, 'dueDate')}\n"
        repeat = t.get("repeatFlag") or t.get("repeatRule")
        if repeat:
            out += f"  repeat: {repeat}\n"
        reminders = t.get("reminders") or []
        if reminders:
            out += f"  reminders: {', '.join(str(r) for r in reminders)}\n"
        if t.get("assignee"):
            out += f"  assignee: {_person_label(t['assignee'], members)}\n"
        if t.get("tags"):
            out += f"  tags: {', '.join('#'+x for x in t['tags'])}\n"
        if t.get("columnId"):
            # Раздел (колонка) назывался голым id — «columnId: 6a75…».
            # Название читается из того же места, что list_project_columns;
            # id остаётся рядом (он нужен как column_id в create/update).
            cols = await _run_blocking(_column_names, t.get("projectId") or "")
            cname = (cols.get(t["columnId"]) or "").strip()
            out += (f"  columnId: «{cname}» (id: {t['columnId']})\n" if cname else
                    f"  columnId: id {t['columnId']} — ⚠️ НАЗВАНИЕ РАЗДЕЛА "
                    "УСТАНОВИТЬ НЕ УДАЛОСЬ (раздела нет в проекте или список "
                    "недоступен)\n")
        content = t.get("content") or t.get("desc") or ""
        if content:
            out += f"  content: {content[:300]}\n"
        # Activity (no full edit-log endpoint exists; these are the task's stamps)
        # Same treatment as the due/start lines above: these three were raw UTC
        # stamps with no zone stated at all, so a task created 23:10 local read
        # as the NEXT day and nothing in the output hinted why.
        out += "\nActivity:\n"
        out += f"  created: {_local_stamp_str(t.get('createdTime'))} by {who}\n"
        out += f"  last modified: {_local_stamp_str(t.get('modifiedTime'))}\n"
        if t.get("completedTime"):
            out += f"  completed: {_local_stamp_str(t['completedTime'])}\n"
        # Checklist items
        items = t.get("items") or []
        if items:
            out += f"\nChecklist ({len(items)}):\n"
            for it in items:
                mark = "x" if it.get("status") == 1 else " "
                out += f"  [{mark}] {it.get('title')}\n"
        # Subtasks = child tasks (parentId points here)
        kids = [x for x in tasks if x.get("parentId") == task_id]
        if kids:
            out += f"\nSubtasks ({len(kids)}):\n"
            for k in kids:
                km = "x" if k.get("status") in (2, -1) else " "
                out += f"  [{km}] {k.get('title')}  (id:{k.get('id')})\n"
        # Attachments
        attachments = t.get("attachments") or []
        if attachments:
            out += f"\nAttachments ({len(attachments)}):\n"
            for a in attachments:
                name = a.get("fileName") or a.get("name") or "(unnamed)"
                size = a.get("fileSize")
                size_str = f"  {size // 1024} KB" if size else ""
                url = a.get("fileUrl") or a.get("url") or ""
                out += f"  📎 {name}{size_str}"
                if url:
                    out += f"\n     {url}"
                out += "\n"
        if not items and not kids and not attachments:
            out += "\n(no checklist items, subtasks, or attachments)\n"
        return out
    except Exception as e:
        logger.exception("Error in get_task_info")
        return _tool_error("fetching task info", e)


def _task_activity_fallback(task_id: str) -> Optional[str]:
    """Best-effort substitute for get_task_activity, used only when the real
    /task/activity/{taskId} endpoint genuinely returns nothing (empty list
    or 404) for this specific task — e.g. it has no logged activity. Not a
    real edit log: no per-field diffs, no actor attribution beyond the
    creator. Reuses the create/modify/complete stamps TickTick already
    returns on the task object itself (same fields get_task_info shows under
    "Activity"). Returns None if the task can't be found at all (open or
    trashed)."""
    if not ticktick_v2:
        return None
    try:
        state = ticktick_v2.get_state()
        owner = (state.get("inboxId") or "").replace("inbox", "")
        tasks = state.get("syncTaskBean", {}).get("update", []) or []
        t = next((x for x in tasks if x.get("id") == task_id), None)
        if not t:
            trashed = ticktick_v2.get_trash(limit=500)
            t = next((x for x in trashed if x.get("id") == task_id), None)
        if not t:
            return None
        creator = str(t.get("creator", ""))
        # Тот же класс, что в get_task_info: автор назывался номером
        # («by user 333444»). Эта подстановка — то, что читатель видит
        # ВМЕСТО настоящего лога, и называть в ней человека номером тем
        # более нечестно.
        if not creator:
            who = "автор неизвестен (TickTick не вернул поле creator)"
        elif creator == owner:
            who = "you"
        else:
            who = _person_label(creator, _member_names(t.get("projectId") or ""))
        # Same three stamps get_task_info shows, so they are rendered by the
        # same helper: this fallback is what the reader sees INSTEAD of the
        # real log, and a substitute that dates events differently from the
        # tool it substitutes for is worse than no substitute.
        lines = [f"  created: {_local_stamp_str(t.get('createdTime'))}  by {who}"]
        if t.get("modifiedTime"):
            lines.append(f"  last modified: {_local_stamp_str(t['modifiedTime'])}")
        if t.get("completedTime"):
            lines.append(f"  completed: {_local_stamp_str(t['completedTime'])}")
        return "\n".join(lines)
    except Exception:
        return None


# Коды действий в логе активности — ПО ФАКТИЧЕСКОМУ трафику TickTick, а не по
# догадке о названиях. Предыдущая версия словаря жила внутри
# get_task_activity() и содержала выдуманный "T_COMPLETE", которого TickTick не
# шлёт, при этом не содержала реального "T_DONE" (каждое завершение), а также
# "T_ASSIGN", "T_COLUMN", "T_ADD_FILE", "T_DEL_FILE" — и все они вываливались
# сырьём в середину фразы («someone T_DONE»). Каждый код здесь наблюдался в
# живом ответе; расширять список — только по новому наблюдению.
ACTIVITY_ACTION_LABELS = {
    "T_TITLE":    "renamed",
    "T_CONTENT":  "edited description",
    "T_DUE":      "changed due date",
    "T_MOVE":     "moved to another list",
    "T_PARENT":   "changed parent/subtask",
    "T_CREATE":   "created",
    "T_DONE":     "completed",
    "T_DELETE":   "deleted",
    "T_PRIORITY": "changed priority",
    "T_TAG":      "changed tags",
    "T_ASSIGN":   "changed the assignee",
    "T_COLUMN":   "moved to another column",
    "T_ADD_FILE": "attached a file",
    "T_DEL_FILE": "removed a file",
}


def _activity_label(action: str) -> str:
    """Человеческая подпись действия, а для незнакомого кода — ЗАМЕТНАЯ
    заглушка вместе с самим кодом.

    Голый код («someone T_DONE») читался как название действия, поэтому
    пропуск в словаре был невидим — именно так T_DONE и прожил незамеченным.
    Заглушка обязана и признаваться в незнании, и печатать код, чтобы
    следующий пропуск был виден с первого взгляда."""
    label = ACTIVITY_ACTION_LABELS.get(action)
    if label:
        return label
    return f"did something unrecognised ({action or '?'})"


def _activity_project_label(pid: str, names: Dict) -> str:
    """Имя проекта для строки ленты активности — и ЧЕСТНАЯ пометка, когда
    имени нет.

    Дефект №3 (живая приёмка 2026-08-07): событие перемещения печаталось
    сырыми идентификаторами — «you moved to another list
    6a755ff58f08e34527a29b31 → 6a752d718f083125df116c9d», хотя оба проекта
    живые и резолвятся, а соседний `get_changes` в этом же файле уже
    переводит id в имена тем же `_v2_project_names()`. Лента активности —
    именно то место, куда приходят с вопросом «куда делась задача»; 24
    hex-символа на него не отвечают.

    Неудачный резолвинг не заминается: id остаётся видимым (по нему хотя бы
    можно спросить дальше), но рядом сказано, что имени нет — иначе голый
    id снова читается как название (то же правило, что для карточек
    подтверждения: у отображающего пути пустой ответ значит «не знаю», и это
    надо произнести)."""
    if not pid:
        return "(проект не указан)"
    name = names.get(pid)
    return f"«{name}»" if name else f"{pid} (имя неизвестно)"


@mcp.tool(annotations=READONLY)
async def get_task_activity(task_id: str, project_id: str) -> str:
    """
    Get the edit-history / activity log for a task (requires v2 API).
    Shows who changed what and when: title edits, due-date changes, moves,
    content updates, parent changes, etc.

    Backed by TickTick's "Task Activities" panel endpoint (task detail →
    "..." → Task Activities), confirmed via live capture:
        GET /api/v1/task/activity/{taskId}
    (v1, singular "activity", no projectId in the path). Automatically walks
    all pages. If this specific task genuinely has no logged activity (empty
    result or 404), falls back to the created/modified/completed stamps
    already present on the task record (a much smaller "mini-history", no
    per-field diffs or non-owner actor names).

    Args:
        task_id: ID of the task
        project_id: ID of the project the task belongs to (kept for backward
            compatibility; no longer needed by the underlying endpoint)
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        events = await _run_blocking(lambda: ticktick_v2.get_task_activity(project_id, task_id))
        if not events:
            base = "No activity found for this task (empty result from TickTick)."
            fallback = await _run_blocking(lambda: _task_activity_fallback(task_id))
            if fallback:
                base += "\n\nWhat we do know from the task itself:\n" + fallback
            return base

        # Times are the OWNER's, and the zone is named ONCE in the header
        # rather than on each of N lines (with_zone=False below) — a log of 40
        # events would otherwise repeat "(America/Los_Angeles)" 40 times. The
        # old `[:19]` slice just chopped the offset off the raw UTC string, so
        # an event at 23:30 local was logged under the NEXT calendar day.
        # Seconds are kept: `[:19]` carried them and an edit log is exactly
        # where two changes a minute apart need distinguishing.
        out = (f"Activity log ({len(events)} events; times in "
               f"{_USER_TZ.key}):\n\n")
        # Имена проектов читаются ОДИН раз на всю ленту (кэшированный
        # v2-снапшот с фолбэком на официальный API) — тем же хелпером, что
        # уже применён в get_changes ниже.
        project_names = _v2_project_names()
        for e in events:
            action = e.get("action", "?")
            when = _local_stamp_str(e.get("when"), with_zone=False, seconds=True)
            who = e.get("whoProfile", {})
            actor = "you" if who.get("isMyself") else who.get("displayName") or "someone"
            channel = e.get("deviceChannel", "")
            label = _activity_label(action)

            line = f"  {when}  {actor} {label}"
            if action == "T_TITLE" and e.get("title"):
                line += f' → "{e["title"]}"'
            elif action == "T_DUE":
                # Both ends of the move are CALENDAR DAYS in the owner's zone,
                # so they go through the same _local_date_str() the listings
                # use. The old [:10] slice off the raw UTC string made the
                # history of "when did we push this deadline" show days the
                # owner never saw — worse here than in a list, because this is
                # the record people reconstruct decisions from. The event dict
                # carries its own isAllDay, which _local_date_str honours, so
                # an all-day move stays verbatim (#36). "none" stays the word
                # "none" — str(None)[:10] would have printed "None".
                before = (_local_date_str(e, "dueDateBefore")
                          if e.get("dueDateBefore") else "none")
                after = (_local_date_str(e, "dueDate")
                         if e.get("dueDate") else "none")
                line += f"  {before} → {after}"
                if e.get("isAllDay"):
                    line += " (all-day)"
            elif action == "T_MOVE":
                line += (f"  {_activity_project_label(e.get('fromProjectId'), project_names)}"
                         f" → {_activity_project_label(e.get('toProjectId'), project_names)}")
            elif action == "T_CONTENT" and e.get("content"):
                snippet = str(e["content"])[:80].replace("\n", " ")
                line += f'  "{snippet}…"' if len(str(e["content"])) > 80 else f'  "{snippet}"'
            if channel:
                line += f"  [{channel}]"
            out += line + "\n"
        return out
    except Exception as e:
        logger.exception("Error in get_task_activity")
        msg = _tool_error("fetching task activity", e)
        if "404" in str(e):
            fallback = await _run_blocking(lambda: _task_activity_fallback(task_id))
            if fallback:
                msg += ("\n\nThis task's activity endpoint 404d — falling back "
                        "to what's on the task record itself:\n" + fallback)
        return msg


@mcp.tool(annotations=READONLY)
async def get_changes(since: str, until: str = None,
                      project_id: str = None, limit: int = 100,
                      offset: int = 0) -> str:
    """
    Audit feed: everything that changed across the account in a date range —
    what was CREATED, COMPLETED, DELETED, and MODIFIED (requires v2 API).

    Use this to answer "what happened to my tasks yesterday / last week" —
    e.g. find tasks that disappeared (deleted) or got moved/edited. For the
    exact per-task history (who renamed it, which list it moved from→to, and
    WHO did it on shared lists) drill into a specific task with get_task_activity.

    Dates are matched at day granularity in UTC; a task completed late at night
    local time may land on the next UTC day.

    Newest events come first. The header always states the TOTAL number of
    events in the range; when they don't fit in one page, the footer says how
    many are left and which offset continues the feed.

    Args:
        since: Start date YYYY-MM-DD (inclusive)
        until: End date YYYY-MM-DD (inclusive; defaults to today)
        project_id: Optional — limit the feed to one list/project
        limit: Maximum events per call (default 100, same shape as
            get_completed_tasks / get_trash)
        offset: Skip this many events — use it to read the tail
    """
    err = _ensure_ready()
    if err:
        return err

    since = since.strip()
    until = (until or datetime.now(timezone.utc).strftime("%Y-%m-%d")).strip()

    def in_range(ts: str) -> bool:
        if not ts:
            return False
        d = ts[:10]
        return since <= d <= until

    def when(ts: str) -> str:
        return (ts or "")[:16].replace("T", " ") if ts else "?"

    try:
        names = _v2_project_names()

        def pname(pid):
            # При промахе печатался ГОЛЫЙ id — и читался как название
            # проекта. Тот же класс, что дефект №3 в get_task_activity
            # (найдено при его разборе): у отображающего пути пустой ответ
            # значит «не знаю», и это надо произнести, а не выдать id за имя.
            if not pid:
                return "проект не указан"
            return names.get(pid) or f"{pid} — имя неизвестно"

        _COMPLETED_SRC_CAP, _TRASH_SRC_CAP = 100, 300
        open_tasks = await _run_blocking(lambda: ticktick_v2.get_open_tasks())
        completed = await _run_blocking(lambda: ticktick_v2.get_completed_tasks(
            limit=_COMPLETED_SRC_CAP, from_str=since + " 00:00:00", to_str=until + " 23:59:59"))
        trash = await _run_blocking(lambda: ticktick_v2.get_trash(limit=_TRASH_SRC_CAP))

        if project_id:
            open_tasks = [t for t in open_tasks if t.get("projectId") == project_id]
            completed = [t for t in completed if t.get("projectId") == project_id]
            trash = [t for t in trash if t.get("projectId") == project_id]

        events = []  # (timestamp, icon, line)

        for t in open_tasks:
            ct = t.get("createdTime")
            mt = t.get("modifiedTime")
            if in_range(ct):
                events.append((ct, "🆕",
                    f'{when(ct)}  Создано: «{t.get("title","?")}» в «{pname(t.get("projectId"))}»'))
            elif in_range(mt):
                events.append((mt, "✏️",
                    f'{when(mt)}  Изменено: «{t.get("title","?")}» в «{pname(t.get("projectId"))}»'))

        for t in completed:
            cm = t.get("completedTime") or t.get("modifiedTime")
            if in_range(cm):
                events.append((cm, "✅",
                    f'{when(cm)}  Завершено: «{t.get("title","?")}» в «{pname(t.get("projectId"))}»'))

        for t in trash:
            dt = t.get("modifiedTime") or t.get("createdTime")
            if in_range(dt):
                events.append((dt, "🗑",
                    f'{when(dt)}  Удалено (в корзине): «{t.get("title","?")}» из «{pname(t.get("projectId"))}»'))

        if not events:
            return f"С {since} по {until} изменений не найдено."

        events.sort(key=lambda e: e[0] or "", reverse=True)

        # def-D6: раньше здесь печаталась ВСЯ лента — реальный вызов на три дня
        # выдал 461 событие (~56 КБ) и упёрся в лимит токенов при чтении, то
        # есть аудит-фид было физически не прочитать. Теперь страница
        # ограничена (как у get_completed_tasks / get_trash), общее число
        # названо, а хвост достижим через offset.
        total = len(events)
        offset = max(0, offset)
        limit = max(1, limit)
        page = events[offset:offset + limit]
        if not page:
            return (f"Изменений с {since} по {until}: всего {total}, но offset={offset} "
                    f"уже за концом ленты (последняя страница начинается с offset="
                    f"{_last_page_offset(total, limit)}).")
        shown_to = offset + len(page)
        if offset or shown_to < total:
            header = (f"Изменения с {since} по {until} (всего {total}, "
                      f"показаны {offset + 1}-{shown_to}):\n\n")
        else:
            header = f"Изменения с {since} по {until} ({total}):\n\n"
        body = "\n".join(f"{icon} {line}" for _, icon, line in page)
        note = ""
        if shown_to < total:
            note += (f"\n\n… ещё {total - shown_to} событий — повтори вызов "
                     f"с offset={shown_to}.")
        # Источники тоже имеют свои потолки: когда пачка пришла ровно по
        # лимиту, лента заведомо неполна — молчать об этом нельзя.
        if len(completed) >= _COMPLETED_SRC_CAP:
            note += (f"\n⚠️ Завершённых пришло ровно {_COMPLETED_SRC_CAP} (потолок API) — "
                     "в диапазоне их может быть больше, сузь период.")
        if len(trash) >= _TRASH_SRC_CAP:
            note += (f"\n⚠️ Корзина отдала ровно {_TRASH_SRC_CAP} записей (потолок запроса) — "
                     "более старые удаления в ленту не попали.")
        note += ("\n\nℹ️ Для точной истории конкретной задачи (кто/куда перенёс, "
                 "что переименовал) используй get_task_activity.")
        return header + body + note
    except Exception as e:
        logger.exception("Error in get_changes")
        return _tool_error("fetching changes", e)


@mcp.tool(annotations=READONLY)
async def get_project_members(project_id: str) -> str:
    """
    List the members of a shared project — owner and collaborators — with
    their user IDs (requires v2 API). Use a member's userId as the assignee
    field in create_tasks/update_tasks to assign a task to them.

    Args:
        project_id: ID of the shared project
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        members = await _run_blocking(lambda: ticktick_v2.get_project_members(project_id))
        if not members:
            return ("Участники не найдены — проект не расшарен "
                    "или у API нет доступа к нему.")
        pname = _v2_project_names().get(project_id, project_id)
        out = f"Участники проекта «{pname}» ({len(members)}):\n"
        for m in members:
            name = m.get("displayName") or m.get("username") or "?"
            uid = m.get("userId") or m.get("userCode") or "?"
            role = " (владелец)" if m.get("isOwner") or m.get("owner") else ""
            status = "" if m.get("accepted", True) else "  [приглашение не принято]"
            out += f"- {name}{role} — userId: {uid}{status}\n"
        return out
    except Exception as e:
        logger.exception("Error in get_project_members")
        return _tool_error("fetching project members", e)


def _build_assignee_index(tasks: List[Dict]) -> Dict[str, str]:
    """Map assignee userId -> display name by scanning shared projects the
    tasks live in. Best-effort: names for ids we can resolve, ids otherwise."""
    id_to_name: Dict[str, str] = {}
    project_ids = {t.get("projectId") for t in tasks if t.get("assignee")}
    for pid in project_ids:
        if not pid:
            continue
        try:
            for m in ticktick_v2.get_project_members(pid):
                uid = str(m.get("userId") or m.get("userCode") or "")
                nm = m.get("displayName") or m.get("username")
                if uid and nm:
                    id_to_name[uid] = nm
        except Exception:
            continue
    return id_to_name


@mcp.tool(annotations=READONLY)
async def get_tasks_by_assignee(assignee: str, include_completed: bool = False) -> str:
    """
    List tasks assigned to a specific person (requires v2 API). Assignment
    exists only for tasks in SHARED projects that were explicitly assigned via
    TickTick's "Assignee" field — a task merely mentioning someone, or created
    by them, is NOT assigned and won't appear here.

    Args:
        assignee: a person's name (matched against shared-project members,
                  case-insensitive substring) OR their numeric userId.
        include_completed: also include completed tasks (default: only open).
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        tasks = await _run_blocking(lambda: ticktick_v2.get_open_tasks())
        assigned = [t for t in tasks if t.get("assignee")]
        if not assigned:
            return ("Ни у одной открытой задачи нет назначенного исполнителя. "
                    "Назначение работает только в общих (shared) проектах через "
                    "поле «Assignee» в TickTick — задачи, где человек просто упомянут "
                    "или которые он создал, не считаются назначенными.")

        id_to_name = await _run_blocking(lambda: _build_assignee_index(assigned))
        name_to_ids: Dict[str, List[str]] = {}
        for uid, nm in id_to_name.items():
            name_to_ids.setdefault(nm.lower(), []).append(uid)

        q = assignee.strip().lower()
        # resolve query -> set of target userIds
        target_ids = set()
        if q.isdigit():
            target_ids.add(q)
        else:
            for nm, ids in name_to_ids.items():
                if q in nm:
                    target_ids.update(ids)
        if not target_ids:
            known = ", ".join(sorted(set(id_to_name.values()))) or "(никого не удалось определить)"
            return (f"Не нашёл исполнителя «{assignee}» среди участников общих проектов.\n"
                    f"Известные исполнители: {known}\n"
                    "Можно также передать числовой userId.")

        matched = [t for t in assigned if str(t.get("assignee")) in target_ids]
        if not include_completed:
            matched = [t for t in matched if t.get("status", 0) == 0]
        if not matched:
            return f"Нет {'' if include_completed else 'незавершённых '}задач на «{assignee}»."

        who = id_to_name.get(next(iter(target_ids)), assignee)
        header = (f"Задачи на «{who}» "
                  f"({'все' if include_completed else 'незавершённые'}) — {len(matched)}:")
        return header + "\n" + format_task_tree(matched, 200)
    except Exception as e:
        logger.exception("Error in get_tasks_by_assignee")
        return _tool_error("fetching tasks by assignee", e)


@mcp.tool(annotations=READONLY)
async def list_project_columns(project_id: str) -> str:
    """
    List the kanban columns/sections of a project, with their IDs (uses the
    official API). Use a column id as column_id in create_tasks/update_tasks.

    Args:
        project_id: ID of the project
    """
    err = _ensure_official()
    if err:
        return err
    try:
        data = await _run_blocking(lambda: ticktick.get_project_with_data(project_id))
        if 'error' in data:
            return _tool_error("fetching project", data['error'])
        cols = data.get("columns", []) or []
        if not cols:
            return ("This project has no kanban columns (it may be a list-view "
                    "project). Switch its view to kanban to use sections.")
        cols = sorted(cols, key=lambda x: x.get("sortOrder", 0))
        return f"Columns of project {project_id} ({len(cols)}):\n" + "\n".join(
            f"- {col.get('name', '?')}  (id: {col.get('id')})" for col in cols)
    except Exception as e:
        logger.exception("Error in list_project_columns")
        return _tool_error("fetching columns", e)


def _describe_create_project_column(p: Dict, live_name: Optional[str] = None) -> str:
    # `live_name` — живое имя проекта, прочитанное вызывающим ДО гейта.
    # Раньше здесь стояло `project_name or project_id`, то есть при не
    # переданном имени карточка показывала сырой id («в проекте «p1»») —
    # тот же класс, что «в задачу 6a7571…» и «в папку id:c4d38a…».
    # Оправдания «имени неоткуда взять» больше нет: стоящий выше
    # _guard_project(require_known=True) ОТКАЗЫВАЕТ строить план, если id не
    # резолвится в живое имя, — значит везде, где карточка вообще строится,
    # имя известно. Ветки «установить не удалось» тут поэтому нет: она
    # недостижима, а `project_name`/id остаются просто фолбэком на случай
    # вызова описателя вне этого пути.
    #
    # Живое написание предпочтительнее переданного даже когда сверка прошла:
    # _names_agree допускает разницу в регистре/маркерах («работа» ≡
    # «🔥 Работа»), а карточка должна показывать состояние аккаунта, а не
    # пересказ вызывающего.
    dest = live_name or p.get("project_name") or p.get("project_id")
    return f'Создаю раздел (колонку) «{p.get("name")}» в проекте «{dest}»'


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def create_project_column(project_id: str, name: str,
                                project_name: str = "",
                                manifest_id: str = "", user_reply: str = "",
                                automation_key: str = "") -> str:
    """
    Create a kanban column/section inside a project (including the Inbox) and
    return its id (requires v2 API). Use the returned id as column_id in
    create_tasks/update_tasks to route tasks into this section. Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    created on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is created yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    {{AUTOMATION_KEY_NOTE}}

    Sections only render in a project's kanban view; switch the project's view
    to kanban to see them.

    Args:
        project_id: ID of the project (or the Inbox id from get_projects)
        name: Name of the new column/section
        project_name: Name of the project (recommended — arms the identity
            guard so a stale/wrong project_id is refused instead of silently
            creating the column elsewhere; the check runs BEFORE the plan
            card is built, so a wrong pair never reaches your approval)
        manifest_id: from call #1's response — pass on call #2 to actually create
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}

    project_id and project_name are cross-checked against the LIVE project
    list TWICE (same pattern as update_project / move_project_to_group): once
    while BUILDING the plan (call #1, before the card is shown to the owner —
    a wrong pair never reaches the approval) and again, independently, right
    before the column is actually created (call #2, unchanged).
    """
    err = _ensure_ready()
    if err:
        return err
    # Перенос identity-guard (project_id↔project_name) на построение плана —
    # тот же _guard_project(..., require_known=True) с ТЕМИ ЖЕ аргументами,
    # что уже стоит в _create_project_column_impl НА ИСПОЛНЕНИИ (образец:
    # update_project / move_project_to_group).
    #
    # Живая приёмка 2026-08-07: докстринг `project_name` обещал «arms the
    # identity guard so a stale/wrong project_id is refused instead of
    # silently creating the column elsewhere», но на call #1 не проверялось
    # НИЧЕГО — верный project_id и заведомо ложное имя «Совершенно другой
    # проект» дали карточку «📋 План — Создаю раздел (колонку) «…» в проекте
    # «Совершенно другой проект»». Человек подтверждал по имени, которое
    # сервер даже не сверял; обещанной защиты не существовало до момента,
    # когда подтверждение уже получено.
    #
    # Строгость НЕ меняется, меняется момент: `project_name or ""` +
    # require_known=True — ровно то, чем зовёт impl, включая фейл-клоуз на
    # id, который не резолвится ни в одно живое имя (там же, ниже).
    # automation_key НЕ пропускает эту проверку — она стоит раньше гейта.
    if not manifest_id:
        refusal = _guard_project_or_refuse(project_id, project_name or "",
                                           fresh=True, require_known=True,
                                           prefix=_PLAN_REFUSAL_PREFIX)
        if refusal:
            return refusal
    # Живое имя проекта для карточки — читается из снапшота, который
    # _guard_project(fresh=True) выше только что обновил (лишнего сетевого
    # запроса нет), и уходит в описание замыканием, а НЕ ключом в `params`:
    # params едут в манифест, в object_hash и дословно в `_impl(**params)`.
    live_pname = (_v2_project_names().get(project_id) if not manifest_id else None)
    params = {"project_id": project_id, "name": name, "project_name": project_name}
    outcome = await _gate_single("create_project_column", "create_project_column",
                                 params if not manifest_id else None,
                                 manifest_id, user_reply,
                                 lambda p: _describe_create_project_column(
                                     p, live_pname),
                                 automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _create_project_column_impl(**outcome.extra)


async def _create_project_column_impl(project_id: str, name: str,
                                      project_name: str = "") -> str:
    """Pure mutation logic for create_project_column — no consent gate.
    Called only by the gated create_project_column() above once the plan is
    approved."""
    # Identity guard: the id must resolve to a live project (and to the given
    # name when one is passed) — a wrong id would create the column elsewhere.
    refusal = _guard_project_or_refuse(project_id, project_name or "",
                                       fresh=True, require_known=True)
    if refusal:
        return refusal
    live_pname = _v2_project_names().get(project_id, project_id)
    try:
        cid = await _run_blocking(lambda: ticktick_v2.create_column(project_id, name))
    except RuntimeError as e:
        return f"### ❌ Раздел «{name}» НЕ создан\n\nTickTick отклонил: {_redact_for_user(e)}"
    except Exception as e:
        logger.exception("Error in create_project_column")
        return _tool_error("creating column", e)

    # Post-verify: independent fresh re-read of the project's columns (via the
    # official API, not the v2 write response) — the new column must be there.
    try:
        data = await _run_blocking(lambda: ticktick.get_project_with_data(project_id))
        cols = (data.get("columns") or []) if isinstance(data, dict) and 'error' not in data else []
        match = next((c for c in cols if c.get("id") == cid), None)
        if match is None:
            return (f"### ⚠️ Раздел «{name}» создан (id: {cid}), но НЕ подтверждён\n\n"
                    f"Его нет в свежем списке колонок проекта «{live_pname}» — "
                    f"{_UNVERIFIED_MSG}")
        return (f"### ✅ Раздел «{match.get('name', name)}» создан в проекте «{live_pname}» "
                f"(проверено)\n\n"
                f"🧾 Подтверждено отдельным живым чтением TickTick (id: {cid}).")
    except Exception as e:
        return (f"### ⚠️ Раздел «{name}» создан (id: {cid}), но НЕ подтверждён\n\n"
                f"⚠️ {_UNVERIFIED_MSG} ({_redact_for_user(e)})")


# ---------------------------------------------------------------------------
# manual_triage — ОДИН смешанный план из РАЗНОРОДНЫХ операций, ОДНО
# подтверждение, одно исполнение с общим честным отчётом.
#
# Зачем: владелец разбирает инбокс живым текстом («эти удалить, эту
# переименовать, эту в проект X, эти два — дубли, объедини, эту закрой»).
# Раньше каждая такая операция была отдельным гейтованным тулом со своим
# подтверждением, и разбор 20 задач превращался в 20 циклов «план → да».
#
# ЧЕГО ЭТОТ ТУЛ НЕ УМЕЕТ — НАМЕРЕННО (следствие инцидента с plan_declutter,
# который отключён навсегда, см. docs/DESIGN_approval_gate.md §6.4): у него
# НЕТ и не может быть ни одного параметра-фильтра/скоупа/запроса. Он не
# сканирует аккаунт и физически не способен «предложить, что удалить»:
# единственный вход — явный список операций с явными task_id, каждый со
# словами ЧЕЛОВЕКА в поле `said`. Живое состояние читается ТОЛЬКО чтобы
# проверить переданные id (identity guard) и достать имена проектов —
# никогда чтобы ДОБАВИТЬ кандидата в план.
# ---------------------------------------------------------------------------

# Порядок = возрастание разрушительности. Он же порядок показа в превью и
# порядок исполнения: сначала обратимое, необратимое — последним, чтобы сбой
# на середине не оставил задачу удалённой раньше, чем её успели поправить.
_TRIAGE_OPS = ("update", "move", "complete", "merge", "delete")
_TRIAGE_ORDER = {op: i for i, op in enumerate(_TRIAGE_OPS)}
_TRIAGE_EMOJI = {"update": "✏️", "move": "↪", "complete": "✅",
                 "merge": "🔗", "delete": "🗑"}
_TRIAGE_VERB = {"update": "изменить", "move": "перенести", "complete": "закрыть",
                "merge": "объединить", "delete": "удалить"}
# Ключи, которые НЕЛЬЗЯ класть в `changes`: они пересеклись бы со служебными
# полями элемента, который уходит в _update_tasks_impl, и молча разоружили бы
# identity-guard (например changes={"title": ...} подменил бы «текущее
# название» на желаемое, и сверка id↔задача сравнила бы значение сама с собой).
_TRIAGE_FORBIDDEN_CHANGE_KEYS = ("title", "taskId", "task_id", "projectId",
                                 "project_id")
# ДВА РАЗНЫХ ПРЕДЕЛА, И СВОДИТЬ ИХ В ОДИН НЕЛЬЗЯ (Д12, 2026-08-09).
#
# `_TRIAGE_PLAN_DAMAGE_CAP` — предел разового УЩЕРБА: сколько операций может
# оказаться в манифесте, то есть сколько объектов человек способен изменить
# одним нажатием. Считается по тому, что ПРОШЛО сверку и реально уходит в
# план (правка Д9 прошлого раунда: отказывать из-за мусора, который и так
# выброшен, — это отказ ни за что). Кап, который волен поднять сам
# вызывающий (`max_items=10000` строил план на 200 удалений), — не кап,
# поэтому потолок живёт КОНСТАНТОЙ в коде, а `max_items` может его только
# опустить.
#
# `_TRIAGE_INPUT_VOLUME_CAP` — предел разового ОБЪЁМА ВХОДА: сколько операций
# вообще можно передать за один вызов. Проверяется ДО чтения живого состояния
# и ДО сверки, и `max_items` его не касается вовсе.
#
# Почему одного мало. После переезда капа на «прошедшие сверку» верхней
# границы на длину входа не осталось ни одной: 5000 операций, 4960 не прошли
# — в план идут 40, отказа нет, а 4960 справочных записей уезжают в превью (в
# Telegram, где обрезка снята, это сотни сообщений), в манифест и в Postgres
# вместе с планом, и ещё раз в архивный отчёт. Ущерб при этом нулевой —
# ограничение на ущерб такой вызов честно пропускает. Ограничивать объём
# обязан ОТДЕЛЬНЫЙ предел, иначе «предел один» означает «объём не ограничен».
#
# Почему входной заметно выше плана, а не равен ему: разбор, где половина
# строк отвалилась по дрейфу, — законный сценарий (ради него Д9 и делался), и
# сводить два числа к одному значило бы вернуть отказ по капу из-за
# выброшенного мусора. 200 — «человек надиктовал разбор», 5000 — «модель
# высыпала весь аккаунт».
_TRIAGE_PLAN_DAMAGE_CAP = 50
_TRIAGE_INPUT_VOLUME_CAP = 200
# Ключи `changes`, которые сервер РЕАЛЬНО применяет (_update_tasks_impl) и
# показывает в предпросмотре (_update_change_bits). Всё, чего здесь нет,
# молча не сделалось бы, а отчёт при этом отрапортовал бы «обновлено».
# Разбиты по ожидаемому типу: JSON типизации не несёт, а модель однажды
# положила `due_date=20260810` числом — превью построилось, задача удалилась,
# и упала УЖЕ ПОСЛЕ необратимой мутации, на разборе даты.
_TRIAGE_STR_CHANGE_KEYS = ("new_title", "content", "due_date", "start_date",
                           "repeat_flag", "column_id")
_TRIAGE_LIST_CHANGE_KEYS = ("tags", "reminders")
_TRIAGE_ALLOWED_CHANGE_KEYS = (_TRIAGE_STR_CHANGE_KEYS
                               + _TRIAGE_LIST_CHANGE_KEYS
                               + ("priority", "assignee"))
_TRIAGE_PRIORITIES = (0, 1, 3, 5)


def _triage_orphan_note(n: int) -> str:
    """«…у неё N открытых подзадач — они останутся без родителя». Удаление и
    закрытие родителя НЕ трогают детей (и не должны: план не имеет права
    разрастаться сверх названного человеком), но человек обязан видеть, что
    после «да» дети осиротеют, — иначе это сюрприз, а не решение."""
    if not n:
        return ""
    if n % 10 == 1 and n % 100 != 11:
        return "у неё 1 открытая подзадача — она останется без родителя"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"у неё {n} открытые подзадачи — они останутся без родителя"
    return f"у неё {n} открытых подзадач — они останутся без родителя"


def _describe_triage_op(op: Dict) -> str:
    """Одна человекочитаемая строка про одну операцию — то, что человек
    реально увидит перед тем, как сказать «да». Никаких голых id: настоящие
    названия задач и проектов из ЖИВОГО состояния, плюс дословный хвост «по
    вашим словам», чтобы было видно, откуда взялась каждая строка плана."""
    said = (op.get("said") or "").strip()
    tail = f" — по вашим словам: «{said}»" if said else ""
    kind = op.get("op")
    proj = op.get("_project_name") or ""
    where = f" (проект «{proj}»)" if proj else ""
    if op.get("_skip"):
        # Фолбэк «... or task_id» в позиции имени убран (2026-08-07, тот же
        # класс, что в карточках гейта): молчаливый показ id читается как
        # имя. Здесь он и достижим-то только когда имени нет НИОТКУДА —
        # значит это надо сказать словами.
        shown = _triage_task_label(
            {**op, "_live_title": op.get("title") or op.get("_live_title")})
        return f"⚠️ ПРОПУЩЕНО — {shown}: {op['_skip']}"
    title = _triage_task_label(op)
    orphan = _triage_orphan_note(op.get("_open_children") or 0)
    if kind in ("delete", "complete"):
        bits = ([f"проект «{proj}»"] if proj else []) + ([orphan] if orphan else [])
        note = f" ({'; '.join(bits)})" if bits else ""
        verb = "🗑 Удалить" if kind == "delete" else "✅ Закрыть"
        return f"{verb} {title}{note}{tail}"
    if kind == "update":
        return (f"✏️ Изменить {title}{where}: "
                f"{_update_change_bits(op.get('changes') or {}, sep=', ')}{tail}")
    if kind == "move":
        to = op.get("_to_project_name") or op.get("to_project") \
            or op.get("to_project_id") or "?"
        # Пустое имя исходного проекта печаталось как «» → «Работа» — строка,
        # из которой человек не может понять, откуда задача переезжает.
        frm = proj or "неизвестный проект"
        return f"↪ Перенести {title}: «{frm}» → «{to}»{tail}"
    if kind == "merge":
        keep_title = op.get("_keep_live_title") or op.get("keep_title") or "?"
        keep_proj = op.get("_keep_project_name") or ""
        keep_where = f" (проект «{keep_proj}»)" if keep_proj else ""
        # ЧЕСТНО про то, что здесь происходит на самом деле: «объединить» —
        # это удаление дубля, а НЕ слияние полей. Заметки/срок/теги дубля
        # исчезают вместе с ним (проверено: у дубля был content с телефоном и
        # dueDate, у оригинала — нет; после merge их не стало). Настоящее
        # слияние — отдельная фича; пока её нет, слово не должно обещать
        # больше, чем делает код.
        bits = ([f"проект «{proj}»"] if proj else []) \
            + ["его заметки, срок и теги НЕ переносятся"] \
            + ([orphan] if orphan else [])
        return (f"🔗 Объединить дубли: удалить {title} ({'; '.join(bits)}), "
                f"оставить «{keep_title}»{keep_where}{tail}")
    return f"• {kind} {title}{tail}"


def _triage_change_refusal(i: int, title: str, changes: Dict) -> Optional[str]:
    """Проверка СОДЕРЖИМОГО `changes` ДО единой мутации: ключи, которые сервер
    действительно применяет, и типы, на которых не развалится ни предпросмотр,
    ни пост-сверка. Без неё `due_date=20260810` (число) проходило превью,
    отправляло update, доводило план до необратимого удаления — и падало уже
    ПОСЛЕ него, на разборе даты в сверке: человек получал traceback вместо
    отчёта и делал вывод «упало, значит ничего не сделано»."""
    unknown = [k for k in changes if k not in _TRIAGE_ALLOWED_CHANGE_KEYS]
    if unknown:
        return (f"🛑 Отказ: операция #{i} («{title}») — в changes ключи "
                f"{unknown}, которых сервер не применяет: они молча не "
                "сделались бы, а отчёт отрапортовал бы «обновлено». "
                f"Допустимо: {', '.join(_TRIAGE_ALLOWED_CHANGE_KEYS)}. "
                "Ничего не сделано.")
    for key in _TRIAGE_STR_CHANGE_KEYS:
        if key in changes and not isinstance(changes[key], str):
            got = type(changes[key]).__name__
            hint = (' Дата пишется строкой: "2026-08-10" (можно «завтра»/'
                    '«понедельник»).' if key in ("due_date", "start_date") else "")
            return (f"🛑 Отказ: операция #{i} («{title}») — changes[{key!r}] "
                    f"должно быть строкой, а пришло {got} ({changes[key]!r})."
                    f"{hint} Ничего не сделано.")
    for key in _TRIAGE_LIST_CHANGE_KEYS:
        if key not in changes:
            continue
        val = changes[key]
        if not isinstance(val, list):
            return (f"🛑 Отказ: операция #{i} («{title}») — changes[{key!r}] "
                    f"должно быть списком, а пришло "
                    f"{type(val).__name__} ({val!r}). Ничего не сделано.")
        if key == "tags":
            bad = [x for x in val if not isinstance(x, str)]
            if bad:
                return (f"🛑 Отказ: операция #{i} («{title}») — changes['tags'] "
                        f"должен быть списком СТРОК, а внутри {bad!r}. "
                        "Ничего не сделано.")
    if "priority" in changes:
        pr = changes["priority"]
        if isinstance(pr, bool) or not isinstance(pr, int) \
                or pr not in _TRIAGE_PRIORITIES:
            return (f"🛑 Отказ: операция #{i} («{title}») — "
                    f"changes['priority']={pr!r} недопустим. Допустимо: 0 "
                    "(нет), 1 (низкий), 3 (средний), 5 (высокий). Ничего не "
                    "сделано.")
    return None


def _triage_input_volume_refusal(received: int) -> Optional[str]:
    """Предел разового ОБЪЁМА ВХОДА — ДЛИНА переданного списка, проверяется ДО
    чтения живого состояния и ДО сверки (Д12, 2026-08-09).

    Это НЕ второй экземпляр предела ущерба и не его дубль: ущерб меряется
    исполнимым (`_triage_plan_damage_refusal` ниже), объём — всем, что вообще
    приехало в вызов. Разошлись они не на бумаге: 5000 операций, из которых
    сверку не прошли 4960, дают план на 40 — предел ущерба такой вызов
    пропускает честно, а 4960 справочных записей всё равно уезжают в превью
    (в Telegram — сотнями сообщений), в манифест, в Postgres и в архивный
    отчёт. Сведёшь два предела в один — исчезнет либо ограничение объёма
    (как и случилось), либо законный разбор, где половина строк отвалилась.

    `max_items` сюда не передаётся НАМЕРЕННО: он — рычаг вызывающего на
    ущерб («сделай мне план поменьше»), а объём собственного ввода вызывающий
    регулировать не вправе."""
    if received <= _TRIAGE_INPUT_VOLUME_CAP:
        return None
    return (f"🛑 Отказ: передано {received} операций — больше предела на объём "
            f"одного вызова ({_TRIAGE_INPUT_VOLUME_CAP}). Это ОТДЕЛЬНЫЙ предел, "
            "не тот, что ограничивает размер плана: столько строк не поместится "
            "ни в предпросмотр, ни в отчёт, даже если сверку из них переживёт "
            "десяток. Разбери список частями. Ничего не сделано, живое "
            "состояние даже не читалось.")


def _triage_plan_damage_refusal(planned: int, max_items: int) -> Optional[str]:
    """Предел разового УЩЕРБА — число операций В ПЛАНЕ. Считается по тому, что
    РЕАЛЬНО уходит в манифест, а не по длине входного списка (2026-08-09).

    Кап — это предел разового ущерба, а ущерб наносит только исполнимое. С
    тех пор как не прошедшее сверку в план не попадает вовсе (П19), «50
    валидных + 1 непрошедшая» отвергалось с формулировкой «операций 51 —
    больше капа 50», хотя исполнить предлагалось ровно 50. Отказ по мусору,
    который и так выброшен, — это отказ ни за что.

    Длину входа этот предел не сторожит и сторожить не должен — для неё есть
    `_triage_input_volume_refusal` выше (Д12).

    `max_items` может потолок только ОПУСТИТЬ: кап, который волен поднять сам
    вызывающий, — не кап (он же и передаёт аргумент)."""
    cap = max_items if isinstance(max_items, int) and not isinstance(max_items, bool) \
        else _TRIAGE_PLAN_DAMAGE_CAP
    cap = max(1, min(cap, _TRIAGE_PLAN_DAMAGE_CAP))
    if planned <= cap:
        return None
    raised = (f" Переданный max_items={max_items} потолок НЕ поднимает: "
              f"{_TRIAGE_PLAN_DAMAGE_CAP} задан константой в коде."
              if isinstance(max_items, int) and max_items > _TRIAGE_PLAN_DAMAGE_CAP
              else "")
    return (f"🛑 Отказ: операций {planned} — больше капа "
            f"{cap}.{raised} Разбей разбор на части и подтверди каждую "
            "отдельно. Ничего не сделано.")


def _triage_untitled_claim(op: Dict, field: str) -> Tuple[bool, str]:
    """Читает СТРУКТУРНЫЙ маркер «у этого объекта нет названия» (Д1,
    2026-08-09) → (заявлен ли маркер, текст претензии к его типу).

    Почему отдельное булево поле, а не пустая строка в `title` и не текст
    заменителя. Пустая строка в поле названия неотличима от «модель забыла
    название» — а именно на это требование и опиралась вся сверка id↔задача.
    Текст заменителя («(без названия: 📎 1 файл)») печатает САМ сервер для
    человека; принимать его обратно как имя значит объявить именем свою же
    подпись — и тогда любой объект, чью подпись видно в списке, «называется»
    ею. Маркер поэтому — признак, а не строка: его нельзя ни угадать по
    выводу, ни подставить случайно.

    Тип проверяется строго (`is True` / `is False`): "true" строкой или 1
    молча читались бы как «маркера нет», и вызывающий получил бы отказ про
    пустой title, не поняв, что его поле просто выброшено."""
    raw = op.get(field)
    if raw is None or raw is False:
        return False, ""
    if raw is True:
        return True, ""
    return False, (f"поле {field} должно быть булевым true (или отсутствовать), "
                   f"а не {raw!r} — маркер «названия нет» не строка и не число.")


def _validate_triage_ops(operations: List[Dict], max_items: int) -> Optional[str]:
    """Fail-closed валидация ВСЕГО плана до единой мутации. Любое нарушение —
    отказ ЦЕЛИКОМ (не «выкинем плохую строку и сделаем остальное»): человек
    диктовал разбор как одно решение, и молча исполненная половина хуже, чем
    честный отказ с указанием, что именно поправить. Возвращает текст отказа
    или None."""
    if not operations:
        return ("🛑 Пустой список операций — разбирать нечего. Этот инструмент "
                "НЕ выбирает задачи сам: передай явный список того, что "
                "человек сказал сделать.")
    # Предел ОБЪЁМА — первым делом, до построчной валидации и задолго до
    # чтения живого состояния (Д12, 2026-08-09): смысл верхней границы на
    # длину входа в том, чтобы вызов на 5000 строк не доехал ни до сети, ни
    # до превью, ни до базы. Предел УЩЕРБА живёт отдельно и считается позже,
    # по прошедшим сверку, — см. `_triage_plan_damage_refusal`.
    refusal = _triage_input_volume_refusal(len(operations))
    if refusal:
        return refusal
    kind_of: Dict[str, str] = {}
    for i, op in enumerate(operations, 1):
        if not isinstance(op, dict):
            return (f"🛑 Отказ: операция #{i} — не объект. Каждая операция это "
                    "словарь с полями op/task_id/title/said. Ничего не сделано.")
        kind = str(op.get("op") or "").strip().lower()
        if kind not in _TRIAGE_ORDER:
            return (f"🛑 Отказ: операция #{i} — неизвестный op={op.get('op')!r}. "
                    f"Допустимо: {', '.join(_TRIAGE_OPS)}. Ничего не сделано.")
        tid = str(op.get("task_id") or "").strip()
        if not tid:
            return (f"🛑 Отказ: операция #{i} ({kind}) — пустой task_id. "
                    "Ничего не сделано.")
        title = str(op.get("title") or "").strip()
        untitled, bad_flag = _triage_untitled_claim(op, "untitled")
        if bad_flag:
            return (f"🛑 Отказ: операция #{i} ({kind}) — {bad_flag} "
                    "Ничего не сделано.")
        if untitled and title:
            return (f"🛑 Отказ: операция #{i} ({kind}) — одновременно "
                    f"untitled=true и title=«{title}»: это два разных "
                    "утверждения о ТОМ ЖЕ объекте («названия нет» и «название "
                    "такое»), и сервер не выбирает за тебя, какое из них "
                    "проверять. Ничего не сделано.")
        if not title and not untitled:
            # Д1 (2026-08-09). Пока здесь стояло голое «название обязательно»,
            # безымянная задача была через агрегатор НЕДОСТИЖИМА: любое имя,
            # присланное за неё, дальше не проходило сверку с пустым живым.
            # Речь про реальные объекты владельца — фотография чека Home Depot
            # на возврат $374.92 и скриншот дефекта, обе выглядели в списке
            # пустой строкой. Дверь открыта СТРУКТУРНЫМ маркером, а не
            # послаблением на строку: см. `_triage_untitled_claim`.
            return (f"🛑 Отказ: операция #{i} ({kind}) — пустой title. Точное "
                    "текущее название обязательно: по нему сервер проверяет, "
                    "что id указывает на ТУ задачу. Если у задачи "
                    "ДЕЙСТВИТЕЛЬНО нет названия (в списке она показана "
                    "заменителем «(без названия …)») — передай untitled=true "
                    "ВМЕСТО названия; сам заменитель как title слать нельзя, "
                    "это подпись сервера, а не имя объекта. Ничего не сделано.")
        shown = title or "без названия"
        said = str(op.get("said") or "").strip()
        if not said:
            return (f"🛑 Отказ: операция #{i} («{shown}») — пустое поле said. "
                    "В нём должны быть СЛОВА ЧЕЛОВЕКА про ЭТУ задачу "
                    "(дословно или сжато), иначе в предпросмотре не видно, "
                    "откуда взялась строка плана. Ничего не сделано.")
        if tid in kind_of:
            return (f"🛑 Отказ: task_id {tid[:8]}… встречается в плане дважды "
                    f"({kind_of[tid]} и {kind}) — одну задачу нельзя и "
                    "изменить, и удалить одним планом. Ничего не сделано.")
        kind_of[tid] = kind
        if kind == "update":
            changes = op.get("changes")
            if not isinstance(changes, dict) or not changes:
                return (f"🛑 Отказ: операция #{i} («{shown}») — update без "
                        "непустого changes. Ничего не сделано.")
            bad = [k for k in _TRIAGE_FORBIDDEN_CHANGE_KEYS if k in changes]
            if bad:
                return (f"🛑 Отказ: операция #{i} («{shown}») — в changes "
                        f"запрещённые ключи {bad}: они разоружили бы сверку "
                        "id↔задача. Переименование — это changes={\"new_title\": "
                        "\"...\"}, а перенос — отдельная операция op=\"move\". "
                        "Ничего не сделано.")
            refusal = _triage_change_refusal(i, shown, changes)
            if refusal:
                return refusal
        if kind == "move" and not (str(op.get("to_project_id") or "").strip()
                                   or str(op.get("to_project") or "").strip()):
            return (f"🛑 Отказ: операция #{i} («{shown}») — move без "
                    "to_project_id и без to_project. Ничего не сделано.")
        if kind == "merge":
            keep_untitled, bad_keep = _triage_untitled_claim(op, "keep_untitled")
            if bad_keep:
                return (f"🛑 Отказ: операция #{i} ({kind}) — {bad_keep} "
                        "Ничего не сделано.")
            if not str(op.get("keep_task_id") or "").strip():
                return (f"🛑 Отказ: операция #{i} («{shown}») — merge без "
                        "keep_task_id (какую копию оставляем). Ничего не сделано.")
            if keep_untitled and str(op.get("keep_title") or "").strip():
                return (f"🛑 Отказ: операция #{i} («{shown}») — одновременно "
                        "keep_untitled=true и непустой keep_title. Ничего не "
                        "сделано.")
            if not str(op.get("keep_title") or "").strip() and not keep_untitled:
                # Та же дверь, что и у `untitled`, — для ОСТАВЛЯЕМОЙ копии:
                # два безымянных дубля иначе объединить нечем (Д1, 2026-08-09).
                return (f"🛑 Отказ: операция #{i} («{shown}») — merge без "
                        "keep_title. Если у оставляемой копии названия нет — "
                        "keep_untitled=true. Ничего не сделано.")
    # Отдельным проходом: «оставляемая» копия не должна сама исчезнуть в этом
    # же плане — иначе объединение снесёт ОБЕ копии и данные пропадут совсем.
    doomed = {tid for tid, k in kind_of.items()
              if k in ("delete", "merge", "complete")}
    for i, op in enumerate(operations, 1):
        if str(op.get("op") or "").strip().lower() != "merge":
            continue
        keep = str(op.get("keep_task_id") or "").strip()
        if keep == str(op.get("task_id") or "").strip():
            return (f"🛑 Отказ: операция #{i} — keep_task_id совпадает с "
                    "task_id (задача объединяется сама с собой). Ничего не "
                    "сделано.")
        if keep in doomed:
            return (f"🛑 Отказ: операция #{i} — задача, которую надо ОСТАВИТЬ "
                    f"({keep[:8]}…), в этом же плане удаляется/закрывается "
                    "другой операцией. Так пропали бы обе копии. Ничего не "
                    "сделано.")
    return None


def _resolve_triage_destination(op: Dict, names: Dict) -> Tuple[str, str, str]:
    """Проект назначения для move → (id, имя, причина-отказа). По id — через
    обычный guard проекта; по имени — ТОЛЬКО точное совпадение (_names_agree),
    никакого поиска по подстроке: именно подстрочный матчинг проектов был
    одной из причин declutter-инцидента («Работа» ловила «Работа/архив»)."""
    to_id = str(op.get("to_project_id") or "").strip()
    claim = str(op.get("to_project") or "").strip()
    if to_id:
        refusal = _guard_project_or_refuse(to_id, claim, fresh=False,
                                           require_known=True)
        if refusal:
            return "", "", ("проект назначения не подтверждён — "
                            + refusal.lstrip("🛑 ").rstrip())
        return to_id, names.get(to_id, to_id), ""
    matches = [pid for pid, nm in names.items() if _names_agree(claim, nm)]
    if not matches:
        return "", "", (f"проект назначения «{claim}» не найден среди живых "
                        "проектов (точное совпадение имени, не подстрока)")
    if len(matches) > 1:
        return "", "", (f"под именем «{claim}» найдено {len(matches)} проектов "
                        "— неоднозначно, передай to_project_id")
    return matches[0], names.get(matches[0], claim), ""


def _resolve_triage_ops(operations: List[Dict], by_id: Dict[str, Dict],
                        names: Dict) -> List[Dict]:
    """Сверяет КАЖДУЮ переданную операцию с живым состоянием и обогащает её
    тем, что нужно для предпросмотра и исполнения. Ничего не добавляет и
    ничего не выкидывает: операция, не прошедшая сверку, помечается `_skip` с
    причиной.

    2026-08-09: пометка `_skip` — это ПРИГОВОР, а не примечание. Разбор, что
    делать с помеченными, живёт у вызывающего (`manual_triage`), и там они
    из плана ВЫБРАСЫВАЮТСЯ: в манифест попадает только прошедшее сверку.
    Здесь по-прежнему возвращается ПОЛНЫЙ список — чтобы про каждую
    непрошедшую операцию было что сказать человеку поимённо."""
    # Сколько ОТКРЫТЫХ детей у каждой задачи — считается по УЖЕ прочитанному
    # живому состоянию, без единого дополнительного запроса. Дети в план не
    # добавляются (тул не имеет права разрастаться сверх названного), но
    # строка про родителя обязана сказать, что они осиротеют.
    kids: Dict[str, int] = collections.Counter()
    for live_task in by_id.values():
        parent = live_task.get("parentId")
        if parent:
            kids[parent] += 1
    resolved: List[Dict] = []
    for op in operations:
        e = dict(op)
        e["op"] = str(op.get("op") or "").strip().lower()
        e["task_id"] = str(op.get("task_id") or "").strip()
        # Относительные даты («завтра», «понедельник») разрешаются ЗДЕСЬ, в
        # фазе плана, по часам сервера — чтобы в превью и в манифесте стояла
        # ровно та дата, которая потом запишется (тот же приём, что в
        # update_tasks перед _gate_batch).
        if e["op"] == "update" and isinstance(e.get("changes"), dict):
            ch = dict(e["changes"])
            for key in ("due_date", "start_date"):
                if key in ch:
                    ch[key] = _resolve_relative_date(ch[key])
            e["changes"] = ch
        live = by_id.get(e["task_id"])
        if not live:
            e["_skip"] = ("не найдена среди открытых задач (кто-то удалил или "
                          "закрыл её вручную?)")
            resolved.append(e)
            continue
        live_title = live.get("title") or ""
        # Маркер «названия нет» (Д1, 2026-08-09) — ОТДЕЛЬНАЯ ветка сверки, а
        # не поблажка на пустую строку: он утверждает про живую задачу ровно
        # то же, что и обычное название, и потому обязан проверяться так же
        # строго. Стоит маркер, а имя у задачи ЕСТЬ → id ведёт не туда, куда
        # думал вызывающий (её могли переименовать, могли перепутать) — это
        # расхождение, и операция из плана выбрасывается.
        #
        # Судим `_looks_untitled`, а не `_is_untitled`: маркер — это ответ на
        # то, ЧТО СЕРВЕР НАПЕЧАТАЛ, а печатает он заменитель именно по
        # `_looks_untitled` (название из одних невидимых символов человек
        # видит пустым местом). Разойдись показ со сверкой — и задача,
        # показанная безымянной, снова стала бы недостижимой: настоящее её
        # «имя» вызывающий напечатать не может, он его не видел.
        if e.get("untitled") is True:
            if not _looks_untitled(live_title):
                # Живое имя кладётся В ОПЕРАЦИЮ, а не только в текст причины:
                # у маркерной операции своего `title` нет вовсе, и без этого
                # справка «❌ Не вошло» печатала про НАЙДЕННУЮ задачу «её нет
                # в живом состоянии аккаунта» — та самая ложь, ради которой
                # П15 и делался, только с другой стороны.
                e["_live_title"] = live_title
                e["_skip"] = ("в плане стоит untitled=true («названия нет»), а "
                              f"по этому id сейчас «{live_title}» — это другой "
                              "объект, чем имели в виду")
                resolved.append(e)
                continue
        elif not _names_agree(e.get("title") or "", live_title):
            e["_skip"] = (f"название не совпало — по этому id сейчас "
                          f"«{live_title}», а в плане «{e.get('title')}»")
            resolved.append(e)
            continue
        pid = live.get("projectId") or ""
        e["_project_id"] = pid
        e["_project_name"] = names.get(pid, "")
        e["_live_title"] = live_title
        # Как НАЗВАТЬ эту задачу в превью и в отчёте (П15, 2026-08-09).
        # Задача найдена — значит «имени нет» здесь означает «у неё его нет»,
        # а не «мы её не нашли», и печатать про недоступное живое состояние
        # (как делал общий фолбэк) было бы прямой ложью.
        e["_untitled"] = _looks_untitled(live_title)
        e["_label"] = (_untitled_label(live) if _looks_untitled(live_title)
                       else live_title)
        e["_snapshot"] = _snapshot_of(live)
        if e["op"] in ("delete", "complete", "merge"):
            e["_open_children"] = kids.get(e["task_id"], 0)
        if e["op"] == "move":
            to_id, to_name, why = _resolve_triage_destination(e, names)
            if why:
                e["_skip"] = why
                resolved.append(e)
                continue
            e["_to_project_id"] = to_id
            e["_to_project_name"] = to_name
        if e["op"] == "merge":
            keep_id = str(e.get("keep_task_id") or "").strip()
            keep_live = by_id.get(keep_id)
            if not keep_live:
                e["_skip"] = (f"основная задача «{e.get('keep_title')}» не "
                              "найдена среди открытых — дубль НЕ удаляю, иначе "
                              "не осталось бы ни одной копии")
                resolved.append(e)
                continue
            keep_live_title = keep_live.get("title") or ""
            if e.get("keep_untitled") is True:
                if not _looks_untitled(keep_live_title):
                    e["_skip"] = ("для основной копии стоит keep_untitled=true "
                                  f"(«названия нет»), а она называется "
                                  f"«{keep_live_title}» — дубль НЕ удаляю")
                    resolved.append(e)
                    continue
            elif not _names_agree(e.get("keep_title") or "", keep_live_title):
                e["_skip"] = ("основная задача по keep_task_id называется "
                              f"«{keep_live.get('title')}», а не "
                              f"«{e.get('keep_title')}» — дубль НЕ удаляю")
                resolved.append(e)
                continue
            e["_keep_live_title"] = keep_live.get("title") or ""
            e["_keep_project_name"] = names.get(keep_live.get("projectId") or "", "")
        resolved.append(e)
    return resolved


def _triage_summary_with_counts(summary: str, ops: List[Dict]) -> str:
    """Заголовок предпросмотра (он же уходит в Telegram): исходная фраза плюс
    сводка по типам. Считается по операциям, которые ДЕЙСТВИТЕЛЬНО пойдут в
    работу — не прошедшие сверку вынесены отдельным хвостом, а не спрятаны в
    числах.

    2026-08-09: формулировка хвоста — «не вошло в план N», а не «пропущено N».
    «Пропущено» читается как «строка плана с пометкой» — ровно та иллюзия,
    которую и убирали: этих операций в плане БОЛЬШЕ НЕТ, подтверждение к ним
    не относится."""
    doing = [o for o in ops if not o.get("_skip")]
    counts = collections.Counter(o["op"] for o in doing)
    parts = [f"{_TRIAGE_VERB[k]} {counts[k]}" for k in _TRIAGE_OPS if counts.get(k)]
    out = f"{summary} — " + ", ".join(parts) if parts else summary
    skipped = len(ops) - len(doing)
    if skipped:
        out += f"; не вошло в план {skipped}"
    return out


def _ops_plural(n: int) -> str:
    """«1 операция» / «3 операции» / «17 операций» — число вместе со словом.
    Без склонения текст читается как машинный лог, а это то самое место, где
    человек должен ПОНЯТЬ, сколько строк он подтверждает."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} операция"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} операции"
    return f"{n} операций"


def _short_task_id(task_id: str) -> str:
    """`6a73adfc…` — якорь для следующего вызова, а не текст для чтения
    глазами. Многоточие ставится ТОЛЬКО когда за ним правда что-то обрезано:
    «id a1…» обещало продолжение, которого у короткого id нет (2026-08-09)."""
    tid = str(task_id or "")
    return tid[:8] + "…" if len(tid) > 8 else tid


def _triage_not_planned_records(blocked: List[Dict]) -> List[Dict]:
    """Не прошедшие сверку операции → ПЛОСКИЕ справочные записи (2026-08-09).

    Зачем отдельная форма, а не сами операции. Эти записи едут в манифест —
    то есть в Postgres и через перезапуск сервера, — чтобы отчёт ПОСЛЕ
    нажатия кнопки мог назвать невошедшее поимённо. Класть туда операции как
    есть нельзя: рядом с `tasks` они читались бы как «ещё немного работы», а
    вся суть П19 в том, что исполнять их нельзя. Здесь остаются только
    ОПИСАТЕЛЬНЫЕ поля, ни одного служебного ключа исполнения — такую запись
    просто не во что превратить обратно в операцию.

    `label`/`untitled` (Д10, 2026-08-09) — чем ЭТУ задачу называть человеку.
    Без них запись строилась из четырёх полей, и у безымянной задачи, которая
    в живом состоянии НАЙДЕНА и прочитана (метку ей проставил
    `_resolve_triage_ops`), а `_skip` получила по другой причине — например,
    проект назначения `move` не резолвится, — справка печатала общий фолбэк
    «⚠️ НАЗВАНИЕ ЗАДАЧИ УСТАНОВИТЬ НЕ УДАЛОСЬ (её нет в живом состоянии)».
    Это дословно тот текст, который П15 объявил ложью: задачу нашли секундой
    раньше. И уезжает он не только в чат, но и в Postgres вместе с манифестом
    и в архивный отчёт после кнопки — то есть навсегда."""
    out: List[Dict] = []
    for o in blocked:
        rec = {"task_id": str(o.get("task_id") or ""),
               "op": str(o.get("op") or ""),
               "title": str(o.get("_live_title") or o.get("title") or ""),
               "why": str(o.get("_skip") or "")}
        if o.get("_label"):
            # Ключи появляются ТОЛЬКО когда метка реально посчитана (задача
            # найдена и прочитана). Пустые поля в записи, которая уезжает в
            # Postgres, — шум, а не информация.
            rec["label"] = str(o["_label"])
            rec["untitled"] = bool(o.get("_untitled"))
        out.append(rec)
    return out


def _triage_not_planned_line(rec: Dict) -> str:
    """Одна строка справки: id, что за операция, какая задача и ПОЧЕМУ она не
    прошла — дословной причиной сверки, где уже сказано «сейчас такое-то, а в
    плане такое-то».

    Метка записи (`label`/`untitled`, Д10 2026-08-09) отдаётся
    `_plan_task_name` тем же словарём, что и в `_triage_task_label`: у
    найденной безымянной задачи это заменитель по её содержимому плюс
    признание «опознана по id», а не ложь «её нет в живом состоянии».
    Записи, сохранённые в базе ДО этой правки, ключей не имеют — для них
    поведение прежнее, по названию."""
    titles = ({str(rec.get("task_id") or ""):
               {"label": rec["label"], "untitled": bool(rec.get("untitled"))}}
              if rec.get("label") else {})
    shown = _plan_task_name({"task_id": rec.get("task_id"),
                             "title": rec.get("title")}, titles)
    emoji = _TRIAGE_EMOJI.get(rec.get("op"), "•")
    verb = _TRIAGE_VERB.get(rec.get("op"), rec.get("op"))
    tid = _short_task_id(rec.get("task_id") or "")
    head = f"id {tid}" if tid else "id не указан"
    return f"• {head} — {emoji} {verb} {shown}: {rec.get('why')}"


def _triage_not_planned_lines(records: List[Dict]) -> List[str]:
    """Заголовок «❌ Не вошло: N» плюс строка на КАЖДУЮ запись.

    Число в заголовке и количество строк берутся из одного списка намеренно:
    аудит 2026-08-09 внёс порчу `records[:1]` — счётчик оставался верным, а
    печаталась одна строка из трёх. Расхождение между «сколько сказано» и
    «сколько показано» — ровно тот класс «человек не узнал про часть», ради
    которого пакет и делался, поэтому тест сверяет их друг с другом."""
    if not records:
        return []
    return [f"❌ Не вошло: {len(records)} — разберите с человеком в чате"] \
        + [_triage_not_planned_line(r) for r in records]


def _triage_mismatch_block(doable: List[Dict], records: List[Dict]) -> List[str]:
    """Справочный блок «что НЕ вошло в план» для ПРЕВЬЮ (2026-08-09, П19).

    Раньше не прошедшие сверку операции оставались строками ТОГО ЖЕ плана с
    пометкой ⚠️ ПРОПУЩЕНО. Человек видел двадцать строк, три из них помеченные,
    жал одну кнопку — и пометки проходили мимо внимания: решение принималось
    по большинству. Теперь их в плане нет, а сюда выносится справка: она
    печатается ПОД списком операций (в чат и в Telegram одним и тем же
    текстом, через `notes`), и кнопки к ней не относятся."""
    if not records:
        return []
    head = (f"✅ В план вошло: {_ops_plural(len(doable))} → отправлено на "
            "подтверждение")
    lines = _triage_not_planned_lines(records)
    lines[0] += " (подтверждение относится ТОЛЬКО к списку выше)"
    return ["\n".join([head] + lines)]


def _triage_plan_notes(ops: List[Dict]) -> List[str]:
    """Предупреждения ПРО ВЕСЬ план, которые печатаются отдельными строками
    предпросмотра (а не внутри строки операции), чтобы попасть и в чат, и в
    Telegram одним и тем же текстом.

    Сейчас здесь одно: повторяющееся `said`. Докстринг тула требует, чтобы в
    `said` были слова человека про ЭТУ задачу, но проверить это машинно нельзя
    — сервер видит только строку. Жёсткий отказ при совпадении был бы
    ложноположительным на законном «эти пять удали» (одна фраза действительно
    про пять задач), поэтому здесь предупреждение, а не запрет: решает
    человек, но с открытыми глазами."""
    doing = [o for o in ops if not o.get("_skip")]
    groups: Dict[str, int] = collections.Counter()
    for o in doing:
        key = " ".join(str(o.get("said") or "").lower().split())
        if key:
            groups[key] += 1
    dupes = [(said, n) for said, n in groups.items() if n >= 2]
    if not dupes:
        return []
    dupes.sort(key=lambda p: -p[1])
    notes = []
    for said, n in dupes[:3]:
        shown = said if len(said) <= 60 else said[:60] + "…"
        notes.append(f"⚠️ Одно и то же обоснование у {n} строк («{shown}») — "
                     "проверьте, что вы действительно называли каждую из этих "
                     "задач.")
    if len(dupes) > 3:
        notes.append(f"⚠️ …и ещё {len(dupes) - 3} повторяющихся обоснования — "
                     "проверьте план целиком.")
    return notes


# СНЯТО ПРИ СЛИЯНИИ 2026-08-06 — здесь стоял `_triage_tg_preview_refusal`:
# fail-closed отказ строить план `manual_triage`, который не помещается в ОДНО
# Telegram-сообщение. Он появился не от осторожности, а по конкретной причине:
# общий слой резал превью по искусственному `PREVIEW_CAP` (`_clip`) и слал
# обрезок, а кнопка ✅ исполняла ВЕСЬ манифест — то есть человек подтверждал
# строки, которых не видел. Автор той ветки прямо пометил меру как временную
# («общий слой не трогаем, его параллельно переделывает другая ветка»).
#
# Ветка отчётов в группу эту причину устранила: `PREVIEW_CAP` УБРАН по прямому
# требованию Максима («длинный план/отчёт нельзя молча резать, его надо
# доставить целиком, разбив на несколько сообщений» — см. комментарий над
# TELEGRAM_TEXT_LIMIT в tg_approval.py), и превью уходит целиком через
# `split_for_telegram`/`send_message_chunked`. Обрезки больше нет, значит нет
# и того, от чего защищались, — а сама защита стала вредной: она запрещала бы
# законные длинные планы, которые теперь доезжают до владельца полностью.
# Предел разового ущерба при этом никуда не делся — его держит `_TRIAGE_PLAN_DAMAGE_CAP`
# (50 операций, константой в коде, `max_items` может только опустить).


def _triage_task_label(op: Dict) -> str:
    """Как строка триажа НАЗЫВАЕТ свою задачу. Обёртка над `_plan_task_name`,
    которая доносит до него уже прочитанное живое состояние строки: у
    безымянной задачи (П15, 2026-08-09) это заменитель по содержимому плюс
    признание, что личность сверена по id, а не общий текст «её нет в живом
    состоянии аккаунта» — задачу-то нашли."""
    tid = str(op.get("task_id") or "")
    titles = ({tid: {"label": op["_label"], "untitled": bool(op.get("_untitled"))}}
              if op.get("_label") else {})
    return _plan_task_name(
        {"task_id": tid, "title": op.get("_live_title") or op.get("title")},
        titles)


def _triage_expected_changes(changes: Dict) -> Dict:
    """Перевод «интерфейсных» полей changes (в том виде, в каком их понимает
    _update_tasks_impl) в поля ЖИВОЙ задачи, по которым _verify_item умеет
    судить факт. Поля, невидимые в списке открытых задач (напоминания, повтор,
    колонка), сюда НЕ попадают — про них отчёт честно скажет «не проверяется
    автоматически», а не выдаст непроверенное за подтверждённое.

    Исполнитель (2026-08-09, д6) — ИСКЛЮЧЕНИЕ из этого правила, а не пример
    его: раньше этот докстринг называл его «невидимым в списке открытых
    задач» — неверно. `_open_by_id`/`ticktick_v2.get_open_tasks()` отдают
    тот же сырой объект задачи, что несёт `assignee` (get_task_info читает
    его оттуда же), — значит поле сверяемо, и перевод ниже это делает."""
    exp: Dict[str, Any] = {}
    if changes.get("new_title") is not None:
        exp["title"] = changes["new_title"]
    if changes.get("content") is not None:
        exp["content"] = changes["content"]
    if changes.get("priority") is not None:
        exp["priority"] = changes["priority"]
    if changes.get("tags") is not None:
        exp["tags"] = [str(x).lstrip("#").lower() for x in changes["tags"]]
    if changes.get("assignee") is not None:
        exp["assignee"] = changes["assignee"]
    for src, dst in (("due_date", "dueDate"), ("start_date", "startDate")):
        if changes.get(src):
            val, _all_day = _normalize_date(changes[src])
            exp[dst] = val
    return exp


def _triage_drift_reason(op: Dict, by_id: Dict[str, Dict],
                         names: Dict) -> str:
    """Повторная сверка ПЕРЕД самой мутацией: между показом плана и «да»
    могли пройти минуты, и человек мог что-то поправить руками. Возвращает
    причину, по которой операцию исполнять НЕЛЬЗЯ, или пустую строку."""
    live = by_id.get(op.get("task_id"))
    if not live:
        return "исчезла из открытых задач между планом и исполнением"
    # Маркер «названия нет» обязан пере-проверяться ЗДЕСЬ отдельной веткой
    # (Д1, 2026-08-09). Через `_names_agree` он не проверяется вообще: у такой
    # операции `title` пуст, а пустое ожидание — «претензии нет, пропускай».
    # То есть без этой ветки задача, которой между планом и нажатием кнопки
    # ДАЛИ название, была бы удалена как безымянная — ровно та подмена
    # объекта, от которой сверка и стоит.
    if op.get("untitled") is True:
        if not _looks_untitled(live.get("title") or ""):
            return ("в плане стояло «названия нет», а после плана задаче дали "
                    f"название («{live.get('title')}»)")
    elif not _names_agree(op.get("title") or "", live.get("title") or ""):
        return (f"название изменилось после плана (сейчас «{live.get('title')}»)")
    if op["op"] == "move":
        to_id = op.get("_to_project_id") or ""
        if not to_id or to_id not in names:
            return "проект назначения больше не существует"
    if op["op"] == "merge":
        keep = by_id.get(str(op.get("keep_task_id") or ""))
        if not keep:
            return ("основная задача исчезла из открытых — дубль НЕ трогаю "
                    "(иначе не осталось бы ни одной копии)")
        if not _names_agree(op.get("keep_title") or "", keep.get("title") or ""):
            return (f"основную задачу переименовали (сейчас «{keep.get('title')}») "
                    "— дубль НЕ трогаю")
    return ""


def _verify_triage_op(op: Dict, live_map: Dict[str, Dict],
                      names: Dict) -> Tuple[str, str]:
    """Независимый вердикт по ОДНОЙ операции, судимый по свежему живому
    состоянию, а НЕ по тексту ответа под-исполнителя. Возвращает
    (статус ∈ ok/fail/unchecked, строка вердикта)."""
    kind = op["op"]
    # `_label` — заменитель для безымянной задачи (П15, 2026-08-09): без него
    # вердикт печатал «- ✅ **«»** — удалена», то есть отчитывался о том, чего
    # человек не может опознать.
    title = op.get("_live_title") or op.get("title") or op.get("_label") or ""
    item: Dict[str, Any] = {"taskId": op.get("task_id"), "title": title}
    if kind in ("delete", "merge"):
        status, line = _verify_item("delete", item, live_map, names)
    elif kind == "complete":
        status, line = _verify_item("complete", item, live_map, names)
    elif kind == "move":
        item["expect"] = {"projectId": op.get("_to_project_id")}
        status, line = _verify_item("move", item, live_map, names)
    else:
        expect = _triage_expected_changes(op.get("changes") or {})
        if not expect:
            return "unchecked", (
                f"- ⚠️ **«{title}»** — изменения отправлены, но по живому "
                "списку открытых задач их не проверить (повтор/напоминание/"
                "колонка/исполнитель там не видны) — см. текст ниже")
        item["expect"] = {"changes": expect}
        status, line = _verify_item("update", item, live_map, names)
    # `_verify_item` отдаёт ЯВНЫЙ статус ("ok"/"warn"/"bad") вместе со строкой —
    # и это единственный источник правды (см. его докстринг: восстанавливать
    # статус парсингом эмодзи в начале строки было отдельным багом, из-за
    # которого расхождения терялись и печаталось «расхождений: 0»). Здесь
    # статусов только два, поэтому и "bad", и "warn" считаются «не
    # подтверждено»: успехом объявляется ровно то, что сверка подтвердила.
    return ("ok" if status == "ok" else "fail"), line


def _triage_blocked_lines(blocked: List[Tuple[Dict, str]]) -> List[str]:
    out = ["#### ⏭ Пропущено — НЕ выполнено"]
    for op, why in blocked:
        shown = _triage_task_label(op)
        emoji = _TRIAGE_EMOJI.get(op.get("op"), "•")
        verb = _TRIAGE_VERB.get(op.get("op"), op.get("op"))
        out.append(f"- {emoji} {shown} ({verb}): {why}")
    return out


def _triage_not_planned_report_lines(records: Optional[List[Dict]]) -> List[str]:
    """Тот же перечень невошедшего, но для ОТЧЁТА после исполнения.

    Отдельная рубрика, а не хвост к «⏭ Пропущено», потому что случаи разные и
    человек обязан их различать: «пропущено» — было в плане, подтверждено
    кнопкой, но сдрейфовало между «да» и мутацией; «не вошло в план» — не
    подтверждалось ВООБЩЕ, кнопка к нему никогда не относилась.

    Почему это обязано быть в отчёте, а не только в превью: превью живёт до
    нажатия (сообщение с планом перезаписывается сводкой, лишние куски
    удаляются), а отчёт уходит в группу-архив навсегда. До П19 невошедшее
    попадало в архив само — оно лежало в манифесте помеченными строками."""
    if not records:
        return []
    return ["", "#### ❌ Не вошло в план — сверка НА ЭТАПЕ ПЛАНА, "
            "подтверждения по ним не было"] \
        + [_triage_not_planned_line(r) for r in records]


@mcp.tool()
@_shared_notes(tg=True, automation=True, gate_args=True)
async def manual_triage(summary: str, operations: List[Dict[str, Any]] = None,
                        max_items: int = 50, manifest_id: str = "",
                        user_reply: str = "", automation_key: str = "") -> str:
    """
    Apply a MIXED batch of triage decisions the HUMAN has ALREADY MADE AND
    SAID OUT LOUD — delete / complete / update / move / merge-duplicates — in
    ONE plan, ONE confirmation, ONE execution with one honest report. Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    changed on call #1.

    ⛔ THIS TOOL DOES NOT ANALYSE ANYTHING. Do NOT scan the user's task list,
    do NOT propose what to delete, do NOT "helpfully" add tasks the user did
    not name. It has NO filter/scope/query parameter on purpose (an earlier
    auto-scanning tool once mixed real tasks with test ones into a
    ready-to-run plan and was disabled forever). EVERY operation must
    correspond to a concrete sentence the human said, and that sentence goes
    into that operation's `said` field, VERBATIM (or tightly condensed).
    A blanket phrase reused on every row («разобрать инбокс», «cleanup») is
    NOT what this field is for — `said` must be what the human said about
    THAT task — but the server does not block it: when two or more rows carry
    the same `said`, the preview gets a ⚠️ warning naming how many, and the
    human decides with open eyes. Telling a lazy copy-paste from a legitimate
    one is impossible from the string alone («эти пять удали» genuinely is
    one sentence about five tasks), so an automatic block would hit honest
    plans — and would push you to invent five different phrasings, the exact
    fabrication this field exists to prevent.
    Empty `said` → the whole plan is refused.

    Call #1 (manifest_id omitted): each `task_id` is checked against LIVE
    state (does it exist, does its live title still match the `title` you
    sent, for move — does the destination project resolve, for merge — is the
    task you want to KEEP alive and correctly named). This check is the first
    step INSIDE the tool and CANNOT be skipped — no argument turns it off,
    because there is no other way to create a manifest.
    Whatever fails it is DROPPED FROM THE PLAN — it is not a plan row with a
    warning on it, it is not in the manifest, and the confirmation the human
    gives does not cover it. It comes back to you, the caller, in a separate
    "❌ Не вошло" block (id, what was expected, what is there now, why it
    blocks) — sort those out with the human in chat and, if still needed,
    build a NEW plan. The same block is shown to the owner below the line in
    Telegram, WITHOUT buttons. If nothing survives the check, no manifest is
    created at all and nothing is sent to Telegram.
    What does survive becomes a numbered preview, ordered
    least-destructive-first (update → move → complete → merge → delete),
    where every line shows the real task/project names and the human's own
    words. NOTHING is mutated.
    Call #2 (ONLY after the human actually replied, in a LATER turn): repeat
    the call with manifest_id=<id from call #1> and user_reply=<their literal
    last message>. `operations` may be repeated verbatim or omitted — either
    way it is IGNORED on call #2 (the manifest's stored operations are
    executed, so the set cannot be swapped between plan and execution). Do NOT
    make call #2 in the same turn as call #1.

    Each element of `operations`:
      {
        "op":      "delete" | "complete" | "update" | "move" | "merge",
        "task_id": "<task id>",                      # required, non-empty
        "title":   "<the task's exact CURRENT title>",  # required — identity guard
        "untitled": true,   # INSTEAD of "title", ONLY for a task that really
                            # has NO title — see below
        "said":    "<what the HUMAN said about THIS task>",  # required
        # op="update" only — same field names update_tasks itself takes:
        "changes": {"new_title": "...", "due_date": "YYYY-MM-DD",
                    "start_date": "...", "priority": 0|1|3|5,
                    "content": "...", "tags": ["..."]},
        # op="move" only — one of:
        "to_project_id": "<project id>",   # preferred
        "to_project":    "<exact project name>",   # EXACT match, not substring
        # op="merge" only. merge DELETES the duplicate and keeps the original;
        # it does NOT merge any field — the duplicate's notes, due date and
        # tags are LOST with it (real field-merging is a separate feature that
        # does not exist yet). The preview says so out loud.
        "keep_task_id": "<id of the copy that STAYS>",
        "keep_title":   "<its exact current title>",
        "keep_untitled": true   # INSTEAD of "keep_title", same rule as
                                # "untitled" but for the copy that STAYS
      }

    `untitled` — the ONLY way to name a task that has no name. Some tasks
    genuinely carry an EMPTY title (a photo of a receipt, a screenshot of a
    bug) and the server prints them with a stand-in like
    «(без названия: 📎 1 файл)». That stand-in is the SERVER's caption for a
    human, NOT the task's name: sending it back as `title` is a mismatch and
    the operation gets dropped. Set `untitled: true` and OMIT `title`
    instead — then the identity guard checks that the live task really has no
    title. If it turns out to HAVE one, the operation is dropped exactly like
    a wrong title would be (the id points at something else than you meant).
    Never set `untitled` for a task that shows a real name in the list, and
    never send both `untitled` and `title` — that is refused outright.
    `untitled: true` does NOT block renaming: an `update` whose `changes`
    carry `new_title` still goes through for a task that really has no name —
    the marker is what keeps that legitimate case open now that renaming with
    no name to verify is refused everywhere (see update_tasks).

    `changes` accepts ONLY these keys: new_title, content, due_date,
    start_date, priority (0|1|3|5), tags (list of strings), reminders,
    repeat_flag, column_id, assignee. Anything else — an unknown key, a date
    passed as a number, tags that are not strings — is refused OUTRIGHT: a key
    the server cannot apply would silently do nothing while the report claimed
    success.
    delete/complete do NOT touch the task's subtasks (they stay, parentless) —
    when the task has open children the preview line says how many.

    Example (one call, five different decisions):
      operations=[
        {"op":"delete","task_id":"a1","title":"Купить молоко",
         "said":"это уже неактуально"},
        {"op":"update","task_id":"b2","title":"Отчёт",
         "changes":{"new_title":"Сдать отчёт за июль","due_date":"2026-08-10"},
         "said":"переименуй и поставь на понедельник"},
        {"op":"move","task_id":"c3","title":"Позвонить Ивану",
         "to_project_id":"p_work","said":"это рабочее"},
        {"op":"complete","task_id":"d4","title":"Оплатить интернет",
         "said":"уже сделал"},
        {"op":"merge","task_id":"e5","title":"Позвонить в банк",
         "keep_task_id":"e6","keep_title":"Позвонить в банк",
         "said":"это одно и то же, оставь одну"}]

    TWO SEPARATE LIMITS, do not confuse them: (1) VOLUME — more than 200
    operations in one call is refused before live state is even read (the
    list would not fit a preview or a report no matter how many rows survive
    the check, and `max_items` does not affect it); (2) BLAST — more than 50
    operations in the resulting PLAN, counted after the live check, so rows
    that were dropped never cost you the plan; `max_items` can only LOWER
    that 50, never raise it.

    Refused OUTRIGHT (nothing is mutated, no manifest is created): an empty
    list, more operations than either limit above, an unknown `op`, a missing
    task_id/said, a missing
    title that carries no `untitled: true` either, `title` and `untitled`
    together, an `untitled` that is not a real boolean, the same
    task_id in two operations, update without `changes`, an unknown or
    wrongly-typed key inside `changes`, move without a destination, merge
    without keep_task_id, merge without keep_title and without
    `keep_untitled: true`, a merge whose kept task is itself
    deleted/closed elsewhere in the same plan, or (when the Telegram approval
    layer is on) a plan too long to fit one Telegram message.

    {{AUTOMATION_KEY_NOTE}}

    Args:
        summary: one-line human sentence in the user's language, e.g. «Разбираю входящие после созвона» — the server appends the per-type counts to it
        operations: the explicit list described above — required on call #1, IGNORED on call #2 (may be repeated verbatim)
        max_items: refuse to plan more operations than this (BLAST limit, counted on what passed the live check); the server's own hard cap is 50 and this argument can only lower it — it does NOT touch the separate 200-operation limit on how much you may send in one call
        manifest_id: from call #1's response — pass on call #2 to actually apply
        {{GATE_ARGS_TAIL}}

    {{TG_APPROVAL_NOTE}}
    """
    err = _ensure_ready()
    if err:
        return err

    enriched: Optional[List[Dict]] = None
    notes: Optional[List[str]] = None
    extra: Optional[Dict] = None
    if not manifest_id:
        refusal = _validate_triage_ops(list(operations or []), max_items)
        if refusal:
            return refusal
        by_id = _open_by_id(fresh=True)
        if by_id is None:
            return _STATE_UNAVAILABLE_MSG
        names = _v2_project_names()
        checked = _resolve_triage_ops(list(operations), by_id, names)
        # 2026-08-09 (П19). Не прошедшее сверку В ПЛАН НЕ ПОПАДАЕТ ВОВСЕ.
        # Раньше оно оставалось строкой того же плана с пометкой ⚠️ ПРОПУЩЕНО,
        # и это была видимость честности: человеку показывали двадцать строк,
        # три из них помеченные, он жал ОДНУ кнопку — пометки проходили мимо
        # внимания, решение принималось по большинству. Теперь результат
        # делится надвое: выполнимое → манифест и кнопки, невыполнимое →
        # справка человеку (ниже черты, без кнопок) и в ответ модели.
        blocked = [o for o in checked if o.get("_skip")]
        enriched = [o for o in checked if not o.get("_skip")]
        # list.sort устойчива: внутри одного типа исходный порядок сохраняется.
        enriched.sort(key=lambda o: _TRIAGE_ORDER[o["op"]])
        not_planned = _triage_not_planned_records(blocked)
        if not enriched:
            # Просить «да» на план, где исполнять нечего, — это выпрашивать
            # согласие на пустоту. Манифест не создаётся вовсе, в Telegram
            # ничего не уходит.
            return ("🛑 Ни одна операция не прошла сверку с живым состоянием — "
                    "план НЕ построен, ничего не изменено:\n"
                    + "\n".join(_triage_not_planned_lines(not_planned)))
        # Кап считается по тому, что РЕАЛЬНО уходит в манифест, — см.
        # `_triage_plan_damage_refusal`; ДЛИНУ ВХОДА сторожит отдельный
        # предел объёма, проверенный ещё до чтения живого состояния (Д12).
        refusal = _triage_plan_damage_refusal(len(enriched), max_items)
        if refusal:
            # Д9 (2026-08-09): справка о непрошедших едет ВМЕСТЕ с отказом.
            # Она к этому моменту уже посчитана, а жить ей больше негде: плана
            # нет, манифеста нет, второго шанса рассказать про эти операции не
            # будет. Без неё модель видит только «больше предела», делит список
            # пополам и строит новый план на те же непрошедшие — они отвалятся
            # снова, уже без единого слова о причине.
            return "\n".join(
                [refusal, ""] + _triage_not_planned_lines(not_planned)) \
                if not_planned else refusal
        summary = _triage_summary_with_counts(summary, checked)
        # Справка едет через тот же `notes`, что и прочие предупреждения про
        # весь план: он печатается ПОД списком операций и попадает разом и в
        # чат, и в Telegram — своей копии сборки текста здесь не заводим.
        notes = _triage_plan_notes(enriched) \
            + _triage_mismatch_block(enriched, not_planned)
        # …и ОТДЕЛЬНО едет в манифест, чтобы пережить нажатие кнопки
        # (2026-08-09, найдено независимым аудитом). Превью живёт до нажатия:
        # `summarize_in_owner_chat` перезаписывает сообщение с планом короткой
        # сводкой, `_cleanup_plan_leftovers` стирает остальные куски. Единственный
        # текст, который остаётся навсегда, — отчёт об исполнении, уходящий в
        # группу-архив. До П19 невошедшее попадало туда само (оно лежало в
        # манифесте помеченными строками); выбросив их из плана, справку надо
        # донести до отчёта явно — иначе улучшение превью оплачено потерей
        # архива. `extra` манифеста для этого и есть: он сохраняется вместе с
        # планом в Postgres и возвращается исполнителю и после перезапуска.
        extra = {"not_planned": not_planned} if not_planned else None

    outcome = await _gate_batch("manual_triage", "manual_triage", enriched, summary,
                                manifest_id, user_reply, _describe_triage_op,
                                extra=extra, items_arg="operations", notes=notes,
                                automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    # `**extra` — тот же путь, которым план доезжает до исполнителя после
    # кнопки (`_generic_gate_auto_execute` зовёт impl(summary, tasks, **extra)):
    # чат-«да», кнопка и headless-ключ обязаны давать ОДИН отчёт, а не три.
    return await _manual_triage_impl(outcome.summary, outcome.tasks,
                                     **(outcome.extra or {}))


async def _manual_triage_impl(summary: str, tasks: List[Dict],
                              not_planned: Optional[List[Dict]] = None) -> str:
    """Pure mutation logic for manual_triage — NO consent gate (the gate lives
    in the public manual_triage() above; this is also what the Telegram button
    replays via _generic_gate_auto_execute, which calls impl(summary, tasks,
    **extra)).

    Two hard rules here:
      1. Every operation is re-checked against LIVE state immediately before
         the mutation — anything that drifted since the plan is skipped, never
         "applied anyway".
      2. The final verdict is NOT parsed out of the sub-executors' texts: after
         all of them run, this reads fresh live state ONE more time and judges
         each operation independently (_verify_item). Unreadable state ⇒
         «исход НЕ ПОДТВЕРЖДЁН», not «успех».

    `not_planned` (2026-08-09) — СПРАВОЧНЫЕ записи про операции, которые не
    прошли сверку на этапе плана и в `tasks` не попали. Здесь они ТОЛЬКО
    печатаются: ни одна ветка исполнения их не читает, и превратить запись
    обратно в операцию нечем (у неё нет ни `changes`, ни `keep_task_id`).
    Нужны потому, что отчёт — единственный текст, который переживает нажатие
    кнопки: превью в личке затирается сводкой, а отчёт уходит в группу-архив
    навсегда."""
    # Д8 (2026-08-09). КАЖДЫЙ ранний выход отсюда печатает справку о
    # невошедшем — иначе она пропадает навсегда: манифест к этому моменту уже
    # погашен (план одноразовый), превью в личке затёрто сводкой, а сама
    # справка жила только внутри плана. Ровно та потеря, ради предотвращения
    # которой она и писалась. Раньше её печатал только выход «ничего не
    # пережило сверку»; выходы «сервер не готов» и «живое состояние
    # недоступно» о ней молчали.
    err = _ensure_ready()
    if err:
        return "\n".join([err] + _triage_not_planned_report_lines(not_planned))
    ops = list(tasks or [])
    by_id = _open_by_id(fresh=True)
    if by_id is None:
        return "\n".join([_STATE_UNAVAILABLE_MSG]
                         + _triage_not_planned_report_lines(not_planned))
    names = _v2_project_names()

    ready: List[Dict] = []
    blocked: List[Tuple[Dict, str]] = []
    for op in ops:
        if op.get("_skip"):
            # С 2026-08-09 (П19) помеченные `_skip` в манифест не кладутся
            # вовсе — эта ветка осталась ради манифестов, СОХРАНЁННЫХ старым
            # кодом: они живут в базе и поднимаются `_rehydrate_manifest`
            # после перезапуска. Исполнить такую операцию «раз уж она в
            # плане» нельзя ни при каких условиях.
            blocked.append((op, op["_skip"]))
            continue
        why = _triage_drift_reason(op, by_id, names)
        if why:
            blocked.append((op, why))
            continue
        ready.append(op)

    if not ready:
        # Маркер 🛑 стоит В ЗАГОЛОВКЕ, а не только во второй строке, и это не
        # косметика: `_auto_execute_report_is_failure` (кнопочный путь) судит
        # об исходе по НАЧАЛУ отчёта, поэтому отчёт, который начинался с
        # нейтрального «### 🧾 Ручной разбор», получал надгробие «выполнено»
        # ровно в том случае, когда не выполнено НИЧЕГО. Соглашение по всему
        # серверу — «### <маркер> …», и этот отчёт ему теперь следует.
        return "\n".join(
            [f"### 🛑 Ручной разбор — {summary}",
             "🛑 НИЧЕГО НЕ ВЫПОЛНЕНО — ни одна операция не пережила повторную "
             "сверку с живым состоянием (между планом и подтверждением что-то "
             "изменилось). Ни одна задача не тронута. Манифест при этом уже "
             "погашен (план одноразовый): повторное «да» по нему ничего не "
             "сделает — если разбор всё ещё нужен, построй его заново новым "
             "вызовом manual_triage без manifest_id.", ""]
            + _triage_blocked_lines(blocked)
            + _triage_not_planned_report_lines(not_planned))

    sections: List[Tuple[str, str]] = []

    upd = [o for o in ready if o["op"] == "update"]
    if upd:
        # Маркер «названия нет» ПРОБРАСЫВАЕТСЯ В ЯДРО (2026-08-09). У операции
        # с `untitled: true` поле `title` пусто по определению, и без маркера
        # ядро видело ровно то, что запрещено: запись в поле названия при
        # пустом переданном имени. Проходило оно там по другому основанию —
        # «имени нет ни у вызывающего, ни у живой задачи», — то есть обещание
        # докстринга «законный случай держит открытым маркер» выполнял не
        # маркер. Разница не косметическая: утверждение «у этой задачи имени
        # нет» ядро СВЕРЯЕТ с живым состоянием, которое оно читает само,
        # непосредственно перед записью. Между сверкой разбора и этим чтением
        # задачу могли назвать в приложении — тогда id указывает уже не на тот
        # объект, о котором человек принимал решение, и строка обязана
        # получить отказ, а не тихо примениться.
        items = [{"taskId": o["task_id"],
                  "projectId": (by_id.get(o["task_id"]) or {}).get("projectId")
                  or o.get("_project_id", ""),
                  "title": o.get("title") or "",
                  **({"untitled": True} if o.get("untitled") is True else {}),
                  **(o.get("changes") or {})} for o in upd]
        sections.append(("✏️ Изменения", await _update_tasks_impl(summary, items)))

    mov = [o for o in ready if o["op"] == "move"]
    if mov:
        by_dest: Dict[str, List[Dict]] = {}
        for o in mov:
            by_dest.setdefault(o["_to_project_id"], []).append(o)
        # Один вызов на проект назначения: _move_tasks_impl переносит весь
        # переданный список В ОДИН проект.
        for dest, group in by_dest.items():
            text = await _move_tasks_impl(
                summary,
                [{"taskId": o["task_id"], "title": o.get("title") or ""}
                 for o in group],
                dest, names.get(dest))
            sections.append((f"↪ Перенос → «{names.get(dest, dest)}»", text))

    done = [o for o in ready if o["op"] == "complete"]
    if done:
        items = [{"taskId": o["task_id"], "title": o.get("title") or "",
                  "projectId": o.get("_project_id", "")} for o in done]
        sections.append(("✅ Закрытие", await _complete_tasks_impl(summary, items)))

    gone = [o for o in ready if o["op"] in ("merge", "delete")]
    if gone:
        # Удаление идёт через ТОТ ЖЕ проверенный движок, что и обычное
        # plan_task_deletion → execute_task_deletion: собираем синтетический
        # манифест ровно того формата, который строит plan_task_deletion, и
        # отдаём его _execute_task_deletion_impl (он сам ещё раз сверит
        # название+проект перед необратимым шагом).
        #
        # В ГЛОБАЛЬНЫЙ реестр `_MANIFESTS` он при этом НЕ кладётся вовсе:
        # `_execute_task_deletion_impl` читает реестр только когда манифест не
        # передан явно (`m is None`), а отчёт (`_build_operation_report`)
        # смотрит журнал на диске — то есть запись в реестр не нужна НИКОМУ.
        # Раньше она была, а от «публичный execute_task_deletion("triage-…")
        # исполнит удаление, которого человеку не показывали, по одному
        # чат-«да»» защищал единственный `finally`: любая будущая правка,
        # вставившая между записью и `try:` хоть один `await`, открыла бы это
        # окно заново. Не создавать окна вообще — надёжнее, чем закрывать его
        # аккуратностью. `mid` остаётся идентификатором ЗАПИСИ В ЖУРНАЛЕ (по
        # нему работает operation_report), а не ключом живого манифеста.
        mid = "triage-" + uuid.uuid4().hex[:12]
        items = []
        for o in gone:
            live = by_id.get(o["task_id"]) or {}
            pid = live.get("projectId") or o.get("_project_id", "")
            items.append({"taskId": o["task_id"], "projectId": pid,
                          "title": live.get("title") or o.get("title") or "",
                          "project": names.get(pid, ""),
                          "snapshot": o.get("_snapshot") or _snapshot_of(live)})
        now = time.monotonic()
        synthetic = {
            "kind": "delete", "items": items, "created": now,
            "plan_shown_at": now, "consumed": False, "summary": summary,
            "object_hash": _manifest_object_hash(
                "delete", [it["taskId"] for it in items]),
        }
        text = await _execute_task_deletion_impl(mid, synthetic)
        n_merge = sum(1 for o in gone if o["op"] == "merge")
        label = "🗑 Удаление" + (" и объединение дублей" if n_merge else "")
        sections.append((label, text))

    # ── Независимая сверка: своё свежее чтение, а НЕ разбор текстов выше ──
    #
    # ВЕСЬ блок — под try. Мутации к этому моменту УЖЕ отправлены (часть из них
    # необратима), и исключение здесь не имеет права выглядеть как «тул упал,
    # значит ничего не сделано»: именно так однажды и получилось (число в
    # `changes["due_date"]` уронило разбор даты уже ПОСЛЕ реального удаления —
    # человек увидел traceback вместо отчёта). Сама причина того случая
    # закрыта типизацией `changes` на фазе плана; этот перехват — второй
    # рубеж на любую будущую ошибку сверки.
    lines = ["### 🧾 Ручной разбор — итог", f"_{summary}_", ""]
    verdicts: List[str] = []
    try:
        fresh = _open_by_id(fresh=True)
        if fresh is None:
            lines.append(f"⚠️ Отправлено {len(ready)} операций из {len(ops)}, но "
                         f"{_UNVERIFIED_MSG} Считать выполненным НЕЛЬЗЯ — "
                         "проверьте в TickTick вручную.")
        else:
            fresh_names = _v2_project_names()
            statuses = []
            for o in ready:
                st, line = _verify_triage_op(o, fresh, fresh_names)
                statuses.append((o["op"], st))
                verdicts.append(line)
            n_ok = sum(1 for _k, st in statuses if st == "ok")
            n_fail = sum(1 for _k, st in statuses if st == "fail")
            n_unchecked = sum(1 for _k, st in statuses if st == "unchecked")
            per_kind = collections.Counter(k for k, st in statuses if st == "ok")
            # Значок по ФАКТУ, а не по шаблону (побочный пункт Д7,
            # 2026-08-09): «✅ Выполнено 0 из 3» ставит галочку рядом с нулём.
            # Читателю-человеку это подсказывает успех, а всякой текстовой
            # проверке по всему серверу («есть ли ✅ в отчёте») — тем более:
            # именно такая галочка гасила распознавание провала. Надгробие
            # теперь судится по вердикту и от этой строки не зависит, но
            # оставлять в отчёте символ, противоречащий собственному числу,
            # незачем.
            head = f"{'✅' if n_ok else '❌'} Выполнено {n_ok} из {len(ops)}"
            if blocked:
                head += f" · ⏭ пропущено {len(blocked)} (см. ниже)"
            if not_planned:
                head += f" · ❌ не вошло в план {len(not_planned)} (см. ниже)"
            lines.append(head)
            lines.append(
                f"✏️ Изменено {per_kind.get('update', 0)} · "
                f"↪ Перенесено {per_kind.get('move', 0)} · "
                f"✅ Закрыто {per_kind.get('complete', 0)} · "
                f"🗑 Удалено {per_kind.get('delete', 0)} · "
                f"🔗 Объединено {per_kind.get('merge', 0)}")
            tail = f"❌ Не подтверждено сверкой: {n_fail}"
            if n_unchecked:
                tail += f" · ⚠️ не проверяется автоматически: {n_unchecked}"
            lines.append(tail)
    except Exception as e:
        logger.exception("manual_triage: независимая сверка упала")
        verdicts = []
        lines.append(
            f"⚠️ МУТАЦИИ УЖЕ ОТПРАВЛЕНЫ ({len(ready)} операций из {len(ops)}), "
            f"но независимая сверка НЕ УДАЛАСЬ ({_redact_for_user(e)}). Это НЕ значит, что "
            "ничего не сделано — часть операций (в том числе необратимых) "
            "могла примениться. Проверьте результат в TickTick вручную; ниже "
            "— сырые ответы исполнителей.")
    if verdicts:
        lines += ["", "#### 🔍 Независимая сверка по каждой операции"] + verdicts
    for title, text in sections:
        lines += ["", f"#### {title}", text]
    if blocked:
        lines += [""] + _triage_blocked_lines(blocked)
    lines += _triage_not_planned_report_lines(not_planned)
    return "\n".join(lines)


# Исполнение по кнопке в Telegram вынесено в отдельный модуль
# (пункт 1.2.4 захода 1, 2026-08-09). Импорт стоит РОВНО на месте
# вырезанного куска — здесь уже определены все имена, которые модуль
# берёт снаружи, поэтому двусторонняя связь разрешается позицией, а не
# отложенным импортом внутри функции.
from . import tg_auto_execute  # noqa: E402
from .tg_auto_execute import (  # noqa: E402,F401
    # Три имени нужны самому server.py (_resolve_auto_executor,
    # _auto_execute_tool_of зовёт гейт; _tg_auto_execute_poller_loop
    # запускает main). Остальные — РЕЭКСПОРТ: снаружи (тесты,
    # отладка) реестр исполнителей и разбор отчётов кнопки
    # адресуются через `server.<имя>` с тех пор, как всё лежало в
    # одном файле. Перенос кода — не повод менять этот адрес, и это
    # ЯВНЫЙ список, а не `import *`: видно ровно то, что видно.
    _AUTO_EXECUTE_TOOL_FOR_KIND, _AUTO_EXECUTORS, _AutoExecutorEntry,
    _EXEC_WARN_MARKERS, _GENERIC_GATE_ENTRY, _OUTCOME_PUBLISHED_KEY,
    _TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S, _TG_AUTO_EXECUTE_INTERVAL_S,
    _TG_LOST_CLEARED, _TG_REAP_INTERVAL_S, _VERDICT_EMOJI,
    _announce_lost_manifests, _auto_executable_tool,
    _auto_execute_report_is_failure, _auto_execute_report_is_success,
    _auto_execute_tool_of, _consume_manifest_for_auto_execute,
    _find_tg_auto_execute_candidates, _generic_gate_auto_execute,
    _generic_gate_rehash, _journal_mentions_manifests,
    _manifest_affected_count, _parse_verify_totals,
    _publish_auto_execute_outcome, _register_auto_executor,
    _rehash_create_manifest, _resolve_auto_executor,
    _short_auto_execute_summary, _strip_trailing_independent_report,
    _tg_auto_execute_pending, _tg_auto_execute_poller_loop,
    _tg_auto_execute_tick, _tg_lost_manifest_rows, _tg_manifest_is_known,
    _verdict_from_totals, _verified_auto_execute_report)


def main():
    """Main entry point for the MCP server."""
    # ПЕРВЫМ ДЕЛОМ, до любой строки лога (#119): секрет доступа лежит в пути
    # (`/mcp/<SECRET>`), а uvicorn печатает путь в каждой access-строке —
    # без этого фильтра секрет открытым текстом оседает в логах Railway
    # навсегда, и доступ к логам равен доступу к серверу. Ставится ДО
    # uvicorn'овской настройки логирования; почему фильтр её переживает —
    # см. log_redaction.install().
    log_redaction.install(SECRET)
    if _TG_CFG.enabled:
        db_url = os.environ.get("CONSENT_DATABASE_URL", "").strip()
        if not db_url:
            raise RuntimeError(
                "TG_APPROVAL_ENABLED=true, но CONSENT_DATABASE_URL не задан — "
                "нужен общий Postgres (тот же, что у gmail/sheets/calendar/docs/"
                "drive-mcp) для таблицы tg_approvals."
            )
        tg_approval.init_store(db_url)
        # ТОТ ЖЕ DSN, отдельная таблица: план подтверждения обязан жить там же,
        # где решение по нему, иначе рестарт процесса обесценивает висящие
        # кнопки (см. шапку manifest_store.py). Миграция применяется здесь же,
        # кодом при старте, как и схема tg_approvals.
        manifest_store.init_store(db_url)
        if _TG_CFG.own_bot:
            logger.info("TG approval: Postgres подключен, слой активен "
                        "(server=ticktick, TG_BOT_TOKEN_OVERRIDE задан — свой бот, "
                        "свой /tg/webhook)")
        else:
            logger.info("TG approval: Postgres подключен, слой активен "
                        "(server=ticktick, webhook НЕ регистрируется — владелец gmail-mcp)")
        logger.info("Долговечные планы подтверждения: таблица mcp_manifests готова")
        # own_bot: регистрация /tg/webhook у Telegram (setWebhook) — ТОЛЬКО
        # при streamable-http, потому что при stdio нет никакого HTTP-сервера
        # (mcp.run_stdio_async() ниже), которому Telegram мог бы что-то
        # прислать: setWebhook на несуществующий адрес не упал бы сразу, но и
        # смысла в нём не было бы — кнопки own_bot молча не работали бы, и
        # это ДОЛЖНО быть видно в логе явно, а не тонуть в тишине (тот же дух,
        # что у остального fail-loud в этом файле).
        if _TG_CFG.own_bot:
            if TRANSPORT == "streamable-http":
                tg_approval.register_webhook(_TG_CFG, _public_base_url())
            else:
                logger.warning(
                    f"TG own_bot: MCP_TRANSPORT={TRANSPORT!r}, а не "
                    "'streamable-http' — вебхук физически невозможен (нет HTTP-"
                    "сервера, которому Telegram мог бы слать апдейты). Кнопки "
                    "own_bot работать НЕ будут, пока транспорт не переключат на "
                    "streamable-http. Обычный (не-TG) чат-путь подтверждения "
                    "по-прежнему работает без изменений.")

    if not initialize_client():
        # Don't stop the server: on streamable-http this leaves /health
        # reachable, and tools that need `ticktick` already lazily retry
        # initialize_client() on first call — so a token added later (env or
        # durable volume) is picked up on the next call without a hard restart.
        logger.warning("TickTick client not initialized yet. "
                        "Set TICKTICK_ACCESS_TOKEN (via the local `auth` flow) "
                        "and restart.")

    if TRANSPORT == "streamable-http":
        logger.info(f"Starting TickTick MCP server (streamable-http) on "
                    f"http://{HOST}:{PORT}{STREAMABLE_PATH}")
    else:
        logger.info("Starting TickTick MCP server (stdio)")
    # anyio.run(...) here mirrors exactly what FastMCP.run() does internally
    # (see mcp.server.fastmcp.FastMCP.run's source: `anyio.run(self.run_stdio_async)`
    # / `anyio.run(self.run_streamable_http_async)`) — swapped in so the TG
    # auto-execute poller can be started as a real asyncio task on the SAME
    # event loop the MCP server itself runs on, instead of `mcp.run(transport=...)`
    # which owns/blocks on its own loop with no hook to attach a background
    # task to. Behaviour is unchanged when TG_APPROVAL_ENABLED=false (no task
    # is created; the two transport branches call the exact same *_async
    # methods FastMCP.run() would have).
    anyio.run(_run_server_with_auto_execute_poller)


async def _run_server_with_auto_execute_poller() -> None:
    poller_task = None
    if _TG_CFG.enabled:
        poller_task = asyncio.create_task(_tg_auto_execute_poller_loop())
    try:
        if TRANSPORT == "streamable-http":
            await mcp.run_streamable_http_async()
        else:
            await mcp.run_stdio_async()
    finally:
        if poller_task is not None:
            poller_task.cancel()


def _assert_shared_note_slots_expanded() -> None:
    """Ни в одном докстринге этого модуля не должно остаться неразвёрнутого
    маркера общего абзаца (2026-08-09, ZAHOD1.md 1.2.2).

    Закрывает единственный сценарий, который декоратор `_shared_notes` сам
    поймать не может: маркер в докстринге есть, а декоратора над функцией
    нет вовсе (забыли навесить, снесли при слиянии веток). Тогда в описании,
    которое читает модель, вместо абзаца про кнопку оказалось бы имя маркера
    в двойных фигурных скобках — молча, без падения. Проверка стоит при
    импорте модуля, а не в тесте, чтобы такой сервер вообще не поднялся.
    """
    stale = []
    for name, obj in list(globals().items()):
        if not callable(obj) or getattr(obj, "__module__", None) != __name__:
            continue
        doc = getattr(obj, "__doc__", None)
        if isinstance(doc, str) and _SHARED_NOTE_SLOT_MARK in doc:
            stale.append(name)
    if stale:
        raise RuntimeError(
            "неразвёрнутый маркер общего абзаца в докстринге: "
            + ", ".join(sorted(stale))
            + " — над функцией не хватает декоратора _shared_notes(...)"
        )


_assert_shared_note_slots_expanded()


# --- Проброс подмены атрибутов на вынесенные модули (2026-08-09, П12) -------
# Разнос главного файла (1.2.4) увёл куски кода в отдельные модули, но точкой
# ОБРАЩЕНИЯ снаружи остался `server`: тесты и отладка пишут
# `setattr(server, "_TG_LOST_CLEARED", {})` или `setattr(server, "ticktick",
# fake)` и ждут, что подменённое увидит код, который этим именем пользуется.
# Пока всё лежало в одном файле, так и было. После переноса код читает глобал
# СВОЕГО модуля, и подмена по `server` до него не доезжает — молча, без
# ошибки: проверка зеленеет, ничего не проверив, либо падает на ровном месте.
#
# Поэтому присваивание атрибута модулю `server` проксируется в тот модуль, где
# имя действительно живёт. Это НЕ обход разноса: связанность не возвращается
# (модули по-прежнему отдельные файлы со своими импортами), просто у имени
# остаётся один-единственный владелец состояния, как и было до переноса.
#
# Важно: `global ticktick` внутри `initialize_client()` пишет в словарь модуля
# НАПРЯМУЮ, минуя `__setattr__`, — поэтому боевая инициализация клиента
# синхронизируется отдельной строкой там же, а не здесь.
import sys                          # noqa: E402 — нужен только здесь
from types import ModuleType        # noqa: E402 — и только для проброса


class _SplitModule(ModuleType):
    """Модуль `server`, который проводит подмену своих атрибутов до модулей,
    куда пункт 1.2.4 вынес соответствующий код."""

    def __setattr__(self, name, value):
        for mod in (consent, tg_auto_execute):
            if name in mod.__dict__:
                mod.__dict__[name] = value
        ModuleType.__setattr__(self, name, value)


sys.modules[__name__].__class__ = _SplitModule


if __name__ == "__main__":
    main()