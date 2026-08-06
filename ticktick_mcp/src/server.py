import asyncio
import base64
import collections
import contextvars
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
from .ticktick_v2_client import (ATTACHMENT_MAX_BYTES, TickTickAuthError,
                                 TickTickV2Client, id2error_failures,
                                 new_attachment_id)
from . import declutter_sheet
from . import tg_approval

# Set up logging
logging.basicConfig(level=logging.INFO)
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

        # Official-API writes must drop the v2 sync cache so v2 reads stay
        # consistent (e.g. create a task via the official API, then move it).
        TickTickClient.write_hook = lambda: (
            ticktick_v2.invalidate_cache() if ticktick_v2 else None)

        return True
    except Exception as e:
        logger.error(f"Failed to initialize TickTick client: {e}")
        return False


# --- HTTP routes ------------------------------------------------------------
# Single-tenant: this instance serves ONE person's TickTick account. Auth is
# established out-of-band (the local `auth` flow writes TICKTICK_ACCESS_TOKEN,
# or it is set as a Railway variable / durable volume file) — there is no
# in-server browser OAuth flow. Only /health is exposed here.


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    return JSONResponse({"status": "ok", "ticktick_connected": ticktick is not None})


# --- Attachment transfer links (/dl, /ul) -----------------------------------
# Big files must not travel through the MCP response body (download_task_
# attachment base64-encodes into the answer and caps at 15 MB). Instead a tool
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
    except TickTickAuthError as e:
        logger.error(f"/dl auth error: {e}")
        return PlainTextResponse("Сервер не может обратиться к TickTick "
                                 "(сессия истекла).\n", status_code=502)
    except ValueError:
        # Attachment genuinely gone upstream — same 404 wording as a bad link.
        return PlainTextResponse(_BAD_LINK_MSG, status_code=404)
    except Exception as e:
        logger.error(f"/dl upstream error: {e}")
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
    except TickTickAuthError as e:
        logger.error(f"/ul auth error: {e}")
        return PlainTextResponse("Сервер не может обратиться к TickTick "
                                 "(сессия истекла).\n", status_code=502)
    except Exception as e:
        logger.error(f"/ul upstream error: {e}")
        return PlainTextResponse(f"TickTick не принял файл: {e}\n",
                                 status_code=502)
    return JSONResponse({"status": "ok", "fileName": name,
                         "size_bytes": len(buf), "task_id": payload["t"],
                         "attachment_id": payload["a"]})


# Single source of truth for TickTick's priority levels (0/1/3/5).
PRIORITY_MAP = {0: "None", 1: "Low", 3: "Medium", 5: "High"}


# Format a task object from TickTick for better display
def format_task(task: Dict) -> str:
    """Format a task into a human-readable string (title first, ids at the end)."""
    formatted = f"Title: {task.get('title', 'No title')}\n"

    # Add dates if available
    if task.get('startDate'):
        formatted += f"Start Date: {task.get('startDate')}\n"
    if task.get('dueDate'):
        formatted += f"Due Date: {task.get('dueDate')}\n"
    
    # Add priority if available
    priority = task.get('priority', 0)
    formatted += f"Priority: {PRIORITY_MAP.get(priority, str(priority))}\n"
    
    # Add status if available
    status = "Completed" if task.get('status') == 2 else "Active"
    formatted += f"Status: {status}\n"
    
    # Add content if available
    if task.get('content'):
        formatted += f"\nContent:\n{task.get('content')}\n"
    
    # Add subtasks if available
    items = task.get('items', [])
    if items:
        formatted += f"\nSubtasks ({len(items)}):\n"
        for i, item in enumerate(items, 1):
            status = "✓" if item.get('status') == 1 else "□"
            formatted += f"{i}. [{status}] {item.get('title', 'No title')}\n"

    # Ids last — needed for follow-up calls, but not the headline.
    formatted += f"(id: {task.get('id', '?')} | project: {task.get('projectId', '?')})\n"
    return formatted

# Format a project object from TickTick for better display
def format_project(project: Dict) -> str:
    """Format a project into a human-readable string (name first, id at the end)."""
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
    bits.append(task.get("title") or "(no title)")
    meta = []
    if task.get("dueDate"):
        meta.append("due " + str(task["dueDate"])[:10])
    pr = _PRIO_SHORT.get(task.get("priority", 0))
    if pr:
        meta.append(pr)
    if task.get("tags"):
        meta.append(" ".join("#" + t for t in task["tags"]))
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


def _lookup_task_title(task_id: str) -> str:
    """Return the task's title from the v2 cache, or a fallback string."""
    if ticktick_v2:
        try:
            t = next((x for x in ticktick_v2.get_open_tasks()
                      if x.get("id") == task_id), None)
            if t and t.get("title"):
                return t["title"]
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


class _Guard:
    """Result of the identity guard for one task.
    status ∈ {ok, mismatch, missing, unavailable}."""
    __slots__ = ("status", "project_id", "title", "message")

    def __init__(self, status, project_id="", title="", message=""):
        self.status = status
        self.project_id = project_id   # the task's CURRENT projectId (corrected)
        self.title = title             # the live title
        self.message = message

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

    Title check is armed only when `expected_title` is given (back-compatible)."""
    if by_id is None:
        by_id = _open_by_id(fresh=fresh)
    if by_id is None:
        return _Guard("unavailable", project_id, expected_title,
                      _STATE_UNAVAILABLE_MSG)
    live = by_id.get(task_id)
    if not live:
        return _Guard("missing", project_id, expected_title,
                      f"id {str(task_id)[:8]}… не среди открытых задач "
                      "(завершена/удалена/неверный id)")
    real_pid = live.get("projectId") or project_id
    real_title = live.get("title") or ""
    names = _v2_project_names()
    if not _names_agree(expected_title, real_title):
        return _Guard("mismatch", real_pid, real_title,
                      f"id указывает на «{real_title}», а НЕ «{expected_title}»")
    if expected_project and not _names_agree(expected_project, names.get(real_pid, "")):
        return _Guard("mismatch", real_pid, real_title,
                      f"id в проекте «{names.get(real_pid, '')}», а НЕ «{expected_project}»")
    return _Guard("ok", real_pid, real_title)


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
    for t in tasks:
        tid = t.get("taskId") or t.get("task_id")
        given_pid = t.get("projectId") or t.get("project_id") or ""
        exp_title = t.get("title") or ""
        exp_proj = t.get("projectName") or ""
        g = _guard_task(tid, exp_title, given_pid, exp_proj, by_id=by_id)
        if g.status == "missing":
            missing.append({"taskId": tid, "projectId": given_pid,
                            "title": exp_title or f"[task {str(tid)[:8]}…]"})
        elif g.status == "mismatch":
            mismatch.append({"taskId": tid, "expected": exp_title or "(без названия)",
                             "actual": g.title or "(без названия)",
                             "project": names.get(g.project_id, "")})
        else:
            found.append({"taskId": tid, "title": exp_title or g.title,
                          "projectId": g.project_id,
                          "armed": bool((exp_title or "").strip())})
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
    loose = [f for f in found if not f.get("armed", True)]
    if not loose:
        return ""
    return (f"⚠️ {len(loose)} выполнено БЕЗ сверки названия (title не передан): "
            + ", ".join(f"«{f['title']}»" for f in loose))


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
    """Get all projects from TickTick."""
    err = _ensure_official()
    if err:
        return err

    try:
        projects = await _run_blocking(lambda: ticktick.get_projects())
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        if not projects:
            return "No projects found."
        
        result = f"Found {len(projects)} projects:\n\n"
        for i, project in enumerate(projects, 1):
            result += f"Project {i}:\n" + format_project(project) + "\n"
        
        return result
    except Exception as e:
        logger.error(f"Error in get_projects: {e}")
        return f"Error retrieving projects: {str(e)}"

@mcp.tool(annotations=READONLY)
async def get_project(project_id: str) -> str:
    """
    Get details about a specific project.
    
    Args:
        project_id: ID of the project
    """
    err = _ensure_official()
    if err:
        return err
    
    try:
        project = await _run_blocking(lambda: ticktick.get_project(project_id))
        if 'error' in project:
            return f"Error fetching project: {project['error']}"
        
        return format_project(project)
    except Exception as e:
        logger.error(f"Error in get_project: {e}")
        return f"Error retrieving project: {str(e)}"

@mcp.tool(annotations=READONLY)
async def get_project_tasks(project_id: str) -> str:
    """
    Get all tasks in a specific project.
    
    Args:
        project_id: ID of the project
    """
    err = _ensure_official()
    if err:
        return err
    
    try:
        project_data = await _run_blocking(lambda: ticktick.get_project_with_data(project_id))
        if 'error' in project_data:
            return f"Error fetching project data: {project_data['error']}"
        
        tasks = project_data.get('tasks', [])
        if not tasks:
            return f"No tasks found in project '{project_data.get('project', {}).get('name', project_id)}'."
        
        result = f"Found {len(tasks)} tasks in project '{project_data.get('project', {}).get('name', project_id)}':\n\n"
        for i, task in enumerate(tasks, 1):
            result += f"Task {i}:\n" + format_task(task) + "\n"
        
        return result
    except Exception as e:
        logger.error(f"Error in get_project_tasks: {e}")
        return f"Error retrieving project tasks: {str(e)}"

@mcp.tool(annotations=READONLY)
async def get_task(project_id: str, task_id: str) -> str:
    """
    Get details about a specific task.
    
    Args:
        project_id: ID of the project
        task_id: ID of the task
    """
    err = _ensure_official()
    if err:
        return err
    
    try:
        task = await _run_blocking(lambda: ticktick.get_task(project_id, task_id))
        if 'error' in task:
            return f"Error fetching task: {task['error']}"
        
        return format_task(task)
    except Exception as e:
        logger.error(f"Error in get_task: {e}")
        return f"Error retrieving task: {str(e)}"

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


@mcp.tool()
async def create_tasks(
    summary: str,
    tasks: List[Dict[str, Any]],
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

    automation_key is ONLY for headless automation clients (bots/pipelines):
    they pass their own connection secret to prove they are automation, which
    bypasses the interactive plan/approve requirement.

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
        summary: Human-readable confirmation line (see above)
        tasks: List of task definition objects — one item for a single task

    Returns:
        A formatted summary. Each successfully-created root task line ends with
        the created task's id as `(id:<id>)` so callers can link it without a
        follow-up title search.

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
    if not (SECRET and automation_key and hmac.compare_digest(automation_key, SECRET)):
        return ("🛑 Прямое создание — только для автоматики. Интерактивный флоу: "
                "plan_task_creation (покажи эхо пользователю дословно) → явное "
                "«да» → execute_task_creation(manifest_id, user_reply=<реплика>) "
                "→ operation_report. Ничего не создано.")
    return await _create_tasks_impl(summary, tasks)


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

    to_verify = []  # (title, id, expected_pid, expected_col) — checked at the end
    sub_verify = []  # (title, id) of created SUBTASKS — existence re-checked too
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
        refuse = _guard_project(project_id, exp_proj, require_known=True)
        if refuse:
            failed.append(f"#{i+1} «{title}»: {refuse}")
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
                tasks_flat, relations = _flatten_task_tree(
                    t, project_id, parent_id=t.get("parent_id"))
                sub_notes = []
                resp = await _run_blocking(
                    lambda: ticktick_v2.batch_create_tasks(tasks_flat))
                tree_fail = id2error_failures(
                    resp, [x["id"] for x in tasks_flat])
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
                        sub_notes.append(f"⚠️ раздел (column) не применился: {e}")
                total = len(tasks_flat)
                line = f"✓ «{title}» + {total - 1} подзадач (дерево, {total} всего)"
                if root_id:
                    line += f" (id:{root_id})"
                    to_verify.append((title, root_id, project_id, t.get("column_id")))
                for x in tasks_flat[1:]:
                    if x["id"] not in tree_fail:
                        sub_verify.append((x.get("title") or "?", x["id"]))
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
                failed.append(f"#{i+1} «{title}»: {task['error']}")
                continue
            task_id = task.get("id")

            sub_notes = []
            if ticktick_v2 and task_id:
                if t.get("tags"):
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_tags(task_id, t["tags"]))
                    except Exception as e:
                        logger.warning(f"Tagging failed: {e}")
                        sub_notes.append(f"⚠️ теги не применились: {e}")
                if t.get("assignee") is not None:
                    try:
                        await _run_blocking(lambda: ticktick_v2.batch_update_tasks(
                            [{"taskId": task_id, "assignee": t["assignee"]}]))
                    except Exception as e:
                        logger.warning(f"Assignee failed: {e}")
                        sub_notes.append(f"⚠️ исполнитель не назначен: {e}")
                if t.get("column_id"):
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_column(task_id, t["column_id"]))
                    except Exception as e:
                        logger.warning(f"Column failed: {e}")
                        sub_notes.append(f"⚠️ раздел (column) не применился: {e}")
                if t.get("parent_id"):
                    try:
                        await _run_blocking(lambda: ticktick_v2.batch_set_task_parent(
                            [task_id], t["parent_id"], project_id))
                    except Exception as e:
                        logger.warning(f"Parent link failed: {e}")
                        sub_notes.append(f"⚠️ привязка к родителю не применилась: {e}")
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
                    if all_sub_rels:
                        await _run_blocking(lambda: ticktick_v2._request(
                            "POST", "/batch/taskParent", json=all_sub_rels))
                    await _run_blocking(lambda: ticktick_v2.invalidate_cache())
                    sub_count = len(all_sub_tasks) - len(sub_fail)
                    if sub_fail:
                        sub_notes.append(
                            f"⚠️ TickTick отклонил {len(sub_fail)} подзадач: "
                            + "; ".join(f"{k[:8]}…: {v}" for k, v in sub_fail.items()))
                    for x in all_sub_tasks:
                        if x["id"] not in sub_fail:
                            sub_verify.append((x.get("title") or "?", x["id"]))
                except Exception as e:
                    logger.warning(f"Batch subtasks failed: {e}")
                    sub_notes.append(
                        f"⚠️ подзадачи НЕ созданы ({len(all_sub_tasks)} шт.): {e}")
            elif sub_items and task_id and not ticktick_v2:
                sub_notes.append(
                    f"⚠️ запрошено {len(sub_items)} подзадач, но они требуют "
                    "v2 API — v2 недоступен, подзадачи НЕ созданы")

            line = f"✓ «{title}»"
            if sub_count:
                line += f" + {sub_count} подзадач"
            if task_id:
                line += f" (id:{task_id})"
                to_verify.append((title, task_id, project_id, t.get("column_id")))
            if sub_notes:
                line += "\n  " + "\n  ".join(sub_notes)
            created.append(line)

        except Exception as e:
            failed.append(f"#{i+1} «{title}»: {e}")

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
            for v_title, v_id, v_pid, v_col in to_verify:
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
            # Subtasks: existence check (a rejected subtask must not survive
            # as a phantom «+ N подзадач» claim).
            lost_subs = [s_title for s_title, s_id in sub_verify
                         if s_id not in fresh]
            if lost_subs:
                warnings.append(
                    f"⚠️ подзадачи НЕ подтвердились ({len(lost_subs)}): "
                    + ", ".join(f"«{t}»" for t in lost_subs))

    parts = []
    if created:
        parts.append(f"Создано {len(created)}:\n" + "\n".join(created))
    if warnings:
        parts.append("Проверка назначения:\n" + "\n".join(warnings))
    if failed:
        parts.append(f"Ошибки ({len(failed)}):\n" + "\n".join(failed))
    if to_verify:
        rid = _op_journal("create", [
            {"taskId": v_id, "title": v_title,
             "expect": {"projectId": v_pid, **({"columnId": v_col} if v_col else {})}}
            for v_title, v_id, v_pid, v_col in to_verify], summary)
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
        good.append((t, pname or pid, None))

    # No project named → the SERVER thinks: per-task destination suggestions
    # via the Claude shim (sure/unsure + a clarifying question when unsure).
    if pending:
        sugs = await _run_blocking(lambda: _suggest_destinations(
            [t.get("title") for _, t in pending], names))
        for (i, t), sug in zip(pending, sugs or [{}] * len(pending)):
            if sug.get("project_id"):
                t = dict(t)
                t["project_id"] = sug["project_id"]
                good.append((t, sug["project"], sug))
            else:
                refused.append(f"#{i} «{t.get('title')}»: проект не указан "
                               "(подсказчик недоступен) — назови проект")

    # Duplicate radar: same-normalised title already open in the destination.
    open_titles: Dict[str, set] = {}
    for lt in (_open_by_id() or {}).values():
        open_titles.setdefault(lt.get("projectId") or "", set()).add(
            _norm_name(lt.get("title") or ""))

    mid = uuid.uuid4().hex[:12]
    now = time.monotonic()
    raw_items = [t for t, _, _ in good]
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
             f"_Манифест `{mid}` · ничего ещё не создано_", ""]
    for i, (t, pname, sug) in enumerate(good, 1):
        bits = [f"{i}. **«{t.get('title')}»** → **{pname}**"]
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
    if any(s and (s.get("confidence") or "unsure") != "sure" for _, _, s in good):
        lines.append("")
        lines.append("❗ _По задачам с ❓ уточни проект — можно ответить пунктами "
                     "(«2 — в Fix&Roll»), тогда план пересоберётся с явными "
                     "адресами._")
    lines.append("")
    lines.append("После явного «да» вызови "
                 f"`execute_task_creation(manifest_id=\"{mid}\", "
                 "user_reply=\"<дословная реплика пользователя>\")` · "
                 f"действует {_manifest_ttl_phrase()}, одноразово.")
    # Опциональный ТГ-фактор. При выключенном слое (дефолт) возвращает текст
    # плана БЕЗ единого изменения; при включённом — шлёт план кнопкой и
    # помечает манифест `tg_notified`, из-за чего execute-фаза начинает
    # требовать нажатие. Fail-closed: не смогли отправить — манифест гаснет,
    # наружу уходит текст ошибки вместо плана.
    return await _run_blocking(_maybe_tg_notify_plan, "create_tasks", mid,
                               "\n".join(lines))


@mcp.tool()
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
    before the press ("⏳ ждём кнопку", plan stays alive) and after it
    ("✅ сервер уже исполняет"). Calling it again changes nothing.

    Args:
        manifest_id: id from plan_task_creation
        user_reply: the user's literal reply approving the plan — REQUIRED,
            must be a genuine affirmative («да»/«ok»/…), quoted verbatim, not
            paraphrased and not made up
    """
    err = _ensure_official()
    if err:
        return err
    _prune_manifests()
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
    m["consumed"] = True
    result = await _create_tasks_impl(m.get("summary") or "Создание по манифесту",
                                      m["raw"])
    # Independent verification is NOT optional: append the server-built report
    # right here, so it reaches the user even if the model never asks for it.
    rid_m = re.search(r'operation_report\(record_id="([\w-]+)"\)', result)
    if rid_m:
        result += "\n\n" + _build_operation_report(rid_m.group(1))
    return result


@mcp.tool()
async def update_tasks(
    summary: str,
    tasks: List[Dict[str, Any]] = None,
    manifest_id: str = "",
    user_reply: str = ""
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

    IMPORTANT: always include the task's current title in each item (as "title")
    so the user knows which task is being changed.

    Supported fields per item:
      taskId (required), projectId (required for single/advanced),
      title (current title, for the dialog), new_title, content,
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

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of task change objects — required on call #1, ignored on call #2
        manifest_id: from call #1's response — pass on call #2 to actually update
        user_reply: the user's literal reply approving the plan — required on call #2

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
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
    outcome = _gate_batch("update", "update_tasks", tasks, summary,
                          manifest_id, user_reply, _describe_update_item)
    if not outcome.proceed:
        return outcome.message
    return await _update_tasks_impl(outcome.summary, outcome.tasks)


def _describe_update_item(t: Dict[str, Any]) -> str:
    title = t.get("title") or t.get("taskId") or t.get("task_id") or "?"
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
    changes = "; ".join(bits) or "(поля изменений не распознаны)"
    return f"**«{title}»** — {changes}"


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
            if g.status == "mismatch":
                results.append(f"🛑 НЕ обновил «{t.get('title')}» — {g.message}")
                continue
            if g.status == "unavailable":
                results.append(f"🛑 НЕ обновил «{shown_title}» — {g.message}")
                continue
            if g.status == "missing":
                # Not among open tasks: the official API would silently no-op
                # an update with a stale projectId — refuse instead of lying.
                results.append(f"🛑 НЕ обновил «{shown_title}» — {g.message}")
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
                    results.append(f"✗ «{shown_title}»: {task['error']}")
                    continue
                # Sub-steps (tags/column/assignee) — failures go into the RESULT
                # text, not only the log: «обновлено» must not hide a lost tag.
                sub_fails = []
                if t.get("tags") is not None and ticktick_v2:
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_tags(tid, t["tags"]))
                    except Exception as e:
                        logger.warning(f"Updated but tagging failed: {e}")
                        sub_fails.append(f"теги не применились ({e})")
                if t.get("column_id") and ticktick_v2:
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_column(tid, t["column_id"]))
                    except Exception as e:
                        logger.warning(f"Updated but column assignment failed: {e}")
                        sub_fails.append(f"раздел (column) не применился ({e})")
                if t.get("assignee") is not None and ticktick_v2:
                    try:
                        await _run_blocking(lambda: ticktick_v2.batch_update_tasks([{"taskId": tid, "assignee": t["assignee"]}]))
                    except Exception as e:
                        logger.warning(f"Updated but assignee failed: {e}")
                        sub_fails.append(f"исполнитель не назначен ({e})")
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
                if not (t.get("title") or "").strip():
                    line += " ⚠️ выполнено БЕЗ сверки названия (title не передан)"
                if sub_fails:
                    line += "\n  ⚠️ " + "; ".join(sub_fails)
                results.append(line)
                _single_updates.append(item)
            except Exception as e:
                results.append(f"✗ «{shown_title}»: {e}")
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
        ok_ids = {f["taskId"] for f in found}
        label_of = {}
        changes = []
        date_warns = {}
        for t in tasks:
            tid = t.get("taskId") or t.get("task_id")
            if tid not in ok_ids:
                continue
            label_of[tid] = t.get("title") or _lookup_task_title(tid)
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
        note = _unarmed_note(found)
        if note:
            lines.append(note)
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
        logger.error(f"Error in update_tasks: {e}")
        return f"Error updating tasks: {str(e)}"
@mcp.tool()
async def complete_tasks(summary: str, tasks: List[Dict[str, str]] = None,
                         manifest_id: str = "", user_reply: str = "") -> str:
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

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title","taskId","projectId"} objects — required on
            call #1, ignored on call #2
        manifest_id: from call #1's response — pass on call #2 to actually complete
        user_reply: the user's literal reply approving the plan — required on call #2

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_official()
    if err:
        return err
    outcome = _gate_batch(
        "complete", "complete_tasks", tasks, summary, manifest_id, user_reply,
        lambda t: f"**«{t.get('title') or t.get('taskId')}»**")
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
                if g.status == "mismatch":
                    results.append(f"🛑 НЕ завершил «{t.get('title')}» — {g.message}")
                    continue
                if g.status == "unavailable":
                    results.append(f"🛑 НЕ завершил «{title}» — {g.message}")
                    continue
                if g.status == "missing":
                    # Not among open tasks: completing would either no-op
                    # silently (stale projectId) or hit an already-closed task.
                    results.append(f"↷ «{title}» — не среди открытых "
                                   "(уже завершена/удалена/неверный id), "
                                   "пропущено")
                    continue
                pid = g.project_id or _resolve_project_id(tid, pid)
                pname = t.get("projectName") or _v2_project_names().get(pid, "")
                res = await _run_blocking(lambda: ticktick.complete_task(pid, tid))
                if 'error' in res:
                    results.append(f"✗ «{title}»: {res['error']}")
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
        logger.error(f"Error in complete_tasks: {e}")
        return f"Error completing tasks: {str(e)}"
@mcp.tool()
async def delete_tasks(summary: str, tasks: Optional[List[Dict[str, str]]] = None,
                       manifest_id: str = "", user_reply: str = "") -> str:
    """
    ⚠️ Delete one or more tasks permanently. Gated (🔴 — even a SINGLE
    deletion): this is now a two-call plan → user says yes → execute flow,
    same shape as plan_task_deletion/execute_task_deletion.

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
    «⚠️ Удаляю 5 задач из „Inbox"».

    Put the human title and project name INSIDE each task object (call #1)
    so the manifest shows what's being deleted:
    [{"title": "Buy milk", "projectName": "Groceries", "taskId": "abc",
      "projectId": "xyz"}]

    BULK (more than DIRECT_DELETE_CAP tasks) is refused here outright — use
    plan_task_deletion → execute_task_deletion instead.

    Args:
        summary: Human-readable line starting with ⚠️ (see above)
        tasks: List of {"title","projectName","taskId","projectId"} objects
            — required on call #1, ignored on call #2
        manifest_id: from call #1's response — pass on call #2 to actually delete
        user_reply: the user's literal reply approving the plan — required on call #2
    """
    err = _ensure_ready()
    if err:
        return err
    _prune_manifests()

    if manifest_id:
        m = _MANIFESTS.get(manifest_id)
        if not m or m.get("kind") != "delete":
            return _manifest_gone_msg(
                manifest_id,
                f"🛑 Манифест удаления {manifest_id} не найден/истёк/уже "
                "исполнен. Начни заново: delete_tasks(summary, tasks).")
        cr = _require_consent(action="delete", tier=2, manifest=m,
                              user_reply=user_reply,
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
        preview = [f"### 📋 Готов удалить — {len(items)}",
                  f"_Манифест `{mid}` · ничего ещё не удалено_", ""]
        for i, it in enumerate(items, 1):
            preview.append(f"{i}. **«{it['title']}»** — {it['project']} (`{it['taskId']}`)")
        preview.extend(lines)
        preview.append("")
        preview.append("Покажи это пользователю дословно и ДОЖДИСЬ его "
                       "отдельного ответа (не отвечай за него). Когда он явно "
                       "согласится, вызови "
                       f"`delete_tasks(summary=\"{summary}\", manifest_id=\"{mid}\", "
                       "user_reply=\"<дословная реплика пользователя>\")` — "
                       "НЕ в этом же ходе. Манифест одноразовый, "
                       f"действует {_manifest_ttl_phrase()}.")
        # Через поток, а не напрямую: внутри синхронный requests и сон на 429
        # (до _MAX_SEND_WAIT_S на КАЖДЫЙ кусок), а длинный план — это до
        # десятка сообщений подряд с профилактическими паузами между ними.
        # Вызванное прямо из корутины, это держит event loop на всё время
        # отправки — то есть зависший /health и заткнувшиеся MCP-сессии. Тот
        # же приём, которым уже вынесены публикация отчёта и перепроверка.
        return await _run_blocking(_maybe_tg_notify_plan, "delete_tasks", mid,
                                   "\n".join(preview))
    except Exception as e:
        logger.error(f"Error in delete_tasks: {e}")
        return f"Error deleting tasks: {str(e)}"


# ---------------------------------------------------------------------------
# Two-phase deletion (plan → approve → execute) — for agent/autonomous flows
# ---------------------------------------------------------------------------
# The identity guard stops a STALE id, but cannot stop a consistent-but-wrong
# reference (a real id of a DIFFERENT task with its own real title). The
# manifest closes that hole: plan_task_deletion resolves the request against
# live state and returns the SERVER's echo of exactly what would be deleted;
# a human approves THAT echo; execute_task_deletion then deletes exactly the
# stored manifest — the caller cannot alter the set at execution time.

_MANIFESTS: Dict[str, Dict] = {}
_MANIFEST_TTL = 3600.0  # seconds; a stale plan must be re-planned
_JOURNAL_DIR = os.environ.get("TICKTICK_DATA_DIR", "/data")


# ---------------------------------------------------------------------------
# Честная фраза о сроке жизни манифеста — раньше во ВСЕХ текстах плана была
# намертво зашита строка «действует 1 час», которая враньё, если кто-то
# сконфигурировал TTL иначе (обнаружено на живом прогоне: при TG_APPROVAL_TTL_S
# на 6 часов сообщение владельцу всё равно утверждало «1 час»). Используй эту
# функцию везде, где текст плана называет владельцу/модели срок действия
# манифеста — никогда не пиши длительность строкой руками.
# ---------------------------------------------------------------------------

def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение существительного при числительном n:
    1, 21, 31, ... → one («1 час»); 2-4, 22-24, ... → few («2 часа»);
    0, 5-20, 25-30, ... → many («5 часов», «11 часов»).
    11-14 — исключение из общего "последняя цифра решает": несмотря на то,
    что последняя цифра 1/2/3/4, «11 часов»/«12 часов», а не «11 час»/
    «12 часа» — поэтому проверяется ОТДЕЛЬНО и раньше правила по last-digit."""
    n_abs = abs(n)
    if 11 <= n_abs % 100 <= 14:
        return many
    last_digit = n_abs % 10
    if last_digit == 1:
        return one
    if 2 <= last_digit <= 4:
        return few
    return many


def _duration_ru(seconds: float) -> str:
    """Форматирует длительность в секундах человекочитаемой русской фразой
    («1 час», «2 часа», «5 часов», «30 минут», «90 секунд»). Часы — обычный
    случай (TTL по умолчанию — 3600 c = 1 час); минуты/секунды — на случай
    нестандартно короткого TTL, например при отладке (TG_APPROVAL_TTL_S=90)."""
    total = int(round(seconds))
    if total < 60:
        n = total
        word = _ru_plural(n, "секунда", "секунды", "секунд")
    elif total < 3600:
        n = total // 60
        word = _ru_plural(n, "минута", "минуты", "минут")
    else:
        n = total // 3600
        word = _ru_plural(n, "час", "часа", "часов")
    return f"{n} {word}"


def _manifest_ttl_phrase() -> str:
    """Реальный срок действия манифеста, человекочитаемо по-русски — то, что
    надо подставлять в тексты плана вместо жёстко зашитого «1 час».

    Тонкость: сроков ДВА и они НЕЗАВИСИМЫ друг от друга (подробный разбор — в
    doc-comment у _tg_auto_execute_poller_loop ниже):
      * `_MANIFEST_TTL` — жизнь манифеста в RAM этого процесса, действует
        ВСЕГДА;
      * `_TG_CFG.ttl_s` — TTL строки подтверждения в Postgres, имеет смысл
        ТОЛЬКО когда включён ТГ-слой (`_TG_CFG.enabled`); при выключенном
        слое эта строка вообще не заводится, и её TTL ни на что не влияет.
    План перестаёт быть исполнимым, как только истёк ЛЮБОЙ из двух (истёкший
    RAM-манифест не найти для исполнения, истёкшая Postgres-строка перестаёт
    подтверждаться) — поэтому честное окно для человека это MIN(), а не
    произвольно выбранное одно из двух значений."""
    ttl_s = _MANIFEST_TTL
    if _TG_CFG.enabled:
        ttl_s = min(ttl_s, float(_TG_CFG.ttl_s))
    return _duration_ru(ttl_s)


# ---------------------------------------------------------------------------
# In-chat consent gate — docs/DESIGN_approval_gate.md (v2, "no buttons").
# ---------------------------------------------------------------------------
# elicitation isn't available on claude.ai (§1 of the design doc) and Maksim
# rejected any out-of-band channel (Telegram nonce / button), so this is a
# BEHAVIOURAL gate, not a cryptographic one (§2 proves a hard gate is
# impossible under those constraints). The threat model it targets is a
# HURRIED model self-confirming (`execute` right after `plan`, using a
# password the server itself printed) — not an adversarial one fabricating
# consent outright (§3). The fix: `plan_*` no longer prints a deterministic
# password; `execute_*` instead requires `user_reply` — the user's VERBATIM
# last chat message — and every mutating tool in tiers 🟡/🔴 funnels through
# this single `_require_consent()` so the check can't be skipped by one tool
# simply lacking the reminder text.

# How long a caller must wait between a plan_* being shown and the matching
# execute_*(user_reply=...) — anti-"duplet" heuristic (§4.3.4): catches a
# model that calls plan+execute back-to-back in the same turn, without ever
# handing control back to a human. Weak but free; env-tunable per tier.
_MIN_CONSENT_GAP = float(os.environ.get("MIN_CONSENT_GAP", "2.0"))

# ===========================================================================
# === CONSENT-REPLY CLASSIFIER — BEGIN =====================================
# ===========================================================================
# ПЕРЕНОСИМЫЙ БЛОК: копировать ОТ СИХ ДО СИХ (до маркера
# "CONSENT-REPLY CLASSIFIER — END"). Внешних зависимостей у блока нет — только
# собственные словари/регулярки, стандартный `re` и typing-алиасы
# (Optional/List). Он НИЧЕГО не знает про манифесты, клиента TickTick,
# настройки, Telegram и прочее состояние сервера; это проверяется
# автоматически тестом `tests/test_consent_strict_protocol.py`
# (`test_classifier_block_has_no_server_dependencies`) — если кто-то привяжет
# классификатор к серверному объекту, тест покраснеет.
#
# ⚠ ЭТО КОПИЯ В ШЕСТИ МЕСТАХ. Тот же блок живёт в ticktick-mcp и в пяти
# Google-MCP серверах владельца (gmail / drive / calendar / docs / sheets).
# Правишь здесь — СИНХРОНИЗИРУЙ остальные пять в тот же заход. Иначе через
# месяц будет шесть разошедшихся версий и никто не будет знать, где дыра
# закрыта, а где нет.
#
# ⚠ ЧЕСТНАЯ ГРАНИЦА ПРИМЕНИМОСТИ (не переписывать в маркетинговую сторону):
# сервер физически НЕ отличает сочинённое моделью «да» от настоящего ответа
# человека — по проводу приходит одна и та же строка. Классификатор ловит
# ФОРМУЛИРОВКИ, а не намерение: он сужает то, что модель может безнаказанно
# подставить, и заставляет промахи (пересказ, оговорку, расширение плана)
# падать в отказ. Единственный настоящий ВНЕПОЛОСНЫЙ фактор — подтверждение
# КНОПКОЙ в Telegram (`tg_approval.py`), где сигнал приходит от человека по
# другому каналу. Если на принимающем сервере кнопки нет, перенос этого блока
# нельзя подавать как «дыру закрыли» — это УМЕНЬШЕНИЕ ПОВЕРХНОСТИ, а не
# устранение.
# ---------------------------------------------------------------------------
# ПРИНЦИП (смена курса 2026-08-06, fail-closed): подтверждение необратимой
# операции — это УЗКИЙ ПРОТОКОЛ, а не свободный диалог. Согласием считается
# только ответ, ЦЕЛИКОМ состоящий из понятных нам элементов (слово согласия +
# безобидные наполнители). Любая сложная структура — условие, уточнение,
# перечисление, рассуждение, незнакомое слово — НЕ согласие.
#
# Почему так, а не «перечислим слова-ограничители»: словарную гонку не выиграть.
# «ок, но только те, что просрочены» ловилось, а «делай, я передумал насчёт
# третьей» — нет, потому что «делай» есть в словаре согласий, а «передумал
# насчёт третьей» не подходило ни под один маркер. Нельзя ошибиться в разборе
# того, что не разбираешь: всё непонятное отправляется в отказ с просьбой
# ответить однозначно («да» или «нет»).
# ---------------------------------------------------------------------------

# Server-side yes/no vocabulary (§4.3.3). Deliberately NOT anything that
# looks like the old deterministic password ("DELETE 5"), a tool name, or
# JSON — those are what a self-confirming model would fabricate, not what a
# human actually types.
_CONSENT_AFFIRMATIVE_WORDS = {
    "да", "ага", "угу", "ок", "окей", "окай", "подтверждаю", "подтверждено",
    "удаляй", "удали", "давай", "го", "погнали", "делай", "применяй",
    "применить", "применяем", "конечно", "точно",
    # 2026-08-06: добавлены по итогам аудита — обычные человеческие «да»,
    # которые раньше молча давали ОТКАЗ (владелец пишет их регулярно). Взяты
    # ТОЛЬКО однозначные: «ладно»/«ну ладно»/«наверное да»/«думаю да» намеренно
    # НЕ добавлены — это неуверенность, а не согласие (fail-closed).
    "хорошо", "договорились", "принято", "валяй",
    "yes", "yep", "yeah", "sure", "confirm", "confirmed", "ok", "okay",
    "approve", "approved", "go", "+", "+1", "agreed", "proceed",
    # 2026-08-06 (fail-closed): при переходе на «согласие = ВСЕ токены
    # понятны» словарь обязан покрывать обычные живые подтверждения целиком —
    # иначе нормальный ответ владельца («да, всё верно», «подтверждаю,
    # действуй») молча станет отказом, а это ХУЖЕ закрываемой дыры. Сюда
    # берутся только слова, которые НЕ меняют набор объектов плана: чистые
    # согласия и императивы «делай то, что показал».
    "согласен", "согласна", "согласны", "действуй", "действуйте",
    "поехали", "вперёд", "вперед", "верно", "правильно", "именно",
    "утверждаю", "одобряю", "одобрено", "безусловно", "однозначно",
    "продолжай", "продолжаем", "запускай", "жми", "стартуй", "выполняй",
    # императивы конкретных операций — ровно те, что печатает человек в ответ
    # на показанный план («да, удаляй», «да, сливай», «да, отметь»)
    "сливай", "слей", "создавай", "создай", "обновляй", "обнови",
    "отметь", "отмечай", "перемещай", "перемести", "восстанавливай",
    "восстанови", "завершай", "заверши", "архивируй", "ставь", "поставь",
    "сделай", "сделайте", "сделаем", "стартуем",
    "right", "correct", "exactly", "absolutely", "definitely", "affirmative",
    "alright", "fine", "deal", "certainly", "do", "accept", "accepted", "good",
}
# Безобидные наполнители: сами по себе НЕ согласие, но и не меняют ни набор
# объектов, ни степень уверенности. Нужны, чтобы «да, только быстрее» /
# «давай, пожалуйста» / «ну давай» оставались согласием при правиле «все
# токены должны быть понятны».
#
# Чего здесь СОЗНАТЕЛЬНО нет: «и», «но», «ещё», «сначала», «я», «ты», «если»,
# «всё». Именно они несут расширение плана («да, и заодно удали ещё вон ту»),
# условие («ок, если ты уверен»), отсрочку («да, но сначала покажи») или
# неявное «удали всё» — то есть ровно то, что обязано уходить в отказ.
_CONSENT_MANNER_WORDS = {
    "быстрее", "побыстрее", "быстро", "скорее", "поскорее", "аккуратно",
    "аккуратнее", "осторожно", "осторожнее", "внимательно", "внимательнее",
    "тихо", "медленно", "спокойно", "пожалуйста", "давай", "давайте",
}
_CONSENT_FILLER_WORDS = _CONSENT_MANNER_WORDS | {
    # «только» безопасно: опасное «только <объект>» уже отсечено
    # _CONSENT_CAVEAT_RE ДО этой проверки, сюда доходит лишь «только
    # <наречие темпа/манеры>» («да, только быстрее»).
    "только",
    "ну", "же", "уж", "уже", "тогда", "сразу", "сейчас", "спасибо",
    "плиз", "please", "thanks", "thank", "you", "now", "ahead", "it",
    "sounds",
    # названия самой операции — «подтверждаю удаление», «ок, создание»: они
    # НЕ сужают и НЕ расширяют набор объектов, просто повторяют, о чём план
    "удаление", "создание", "изменение", "обновление", "перемещение",
    "завершение", "слияние", "восстановление",
}
# Устойчивые обороты, которые схлопываются в одно уже известное слово. Нужны
# там, где отдельное слово оборота само по себе опасно пускать в наполнители:
# «всё» как токен означало бы, что «удали всё» — согласие (а это расширение
# плана), зато «да, всё верно» — обычное человеческое подтверждение.
_CONSENT_SET_PHRASES = (
    (re.compile(r"\bвс[её]\s+(верно|правильно|так)\b"), r"верно"),
    (re.compile(r"\bтак\s+точно\b"), "точно"),
    (re.compile(r"\bбез\s+(проблем|вопросов|базара|разговоров|сомнений)\b"),
     "ок"),
    (re.compile(r"\b(go|move)\s+ahead\b"), "go"),
    (re.compile(r"\bof\s+course\b"), "конечно"),
)
# Страховка от абсурдно длинной фразы, целиком собранной из «понятных» слов:
# протокол подтверждения короткий по смыслу, длинное рассуждение — повод
# переспросить, а не исполнять.
_CONSENT_MAX_TOKENS = 8
_CONSENT_NEGATIVE_WORDS = {
    "нет", "неа", "не", "стоп", "отмена", "отмени", "погоди", "подожди",
    "отбой", "не надо", "cancel", "no", "nope", "stop", "wait", "don't",
    "not", "nah", "abort", "отставить", "нельзя",
}
# A reply that merely echoes the server's OWN manifest jargon back is not a
# human "yes" — it's exactly what a model that fabricates consent would type
# (the literal string it would otherwise have self-confirmed with).
_CONSENT_ECHO_ARTIFACT_RE = re.compile(
    r'^(delete|create|declutter)\s*\d+$|manifest_id|execute_\w+\s*\(|plan_\w+\s*\(|^\{.*\}$',
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Аудит 2026-08-06: три дыры В САМОМ классификаторе ответа. Они опаснее любой
# отдельно взятой дыры в отдельном туле, потому что гейт согласия расширяется
# с 2 тулов на ~25 — дефект классификатора тиражируется на все сразу.
# ---------------------------------------------------------------------------

# (A) ЧАСТИЧНОЕ согласие исполнялось как полное. Манифест исполняется ТОЛЬКО
# целиком — частичного режима у сервера физически нет, — поэтому ответ вида
# «ок, кроме последней» / «удали первые три, а последнюю не надо» раньше
# приводил к удалению В ТОМ ЧИСЛЕ того, что человек явно исключил. Любой
# маркер оговорки/исключения ⇒ это НЕ полное согласие.
#
# Про слово «только» — fail-closed с узким белым списком. «Только» по умолчанию
# считается оговоркой («ок, только молоко», «да, только вторую»), потому что по
# свободному тексту нельзя надёжно понять, сузил человек набор или нет, а цена
# ошибки несимметрична: ложный отказ стоит человеку одной лишней фразы, ложное
# согласие — удалённых данных. Исключение сделано ровно для наречий ТЕМПА и
# МАНЕРЫ («да, только быстрее», «ок, только аккуратно») — они не про объекты
# плана, а про то, как его исполнить, и это частая живая формулировка.
#
# Собирается из `_CONSENT_MANNER_WORDS` (один источник правды: те же слова
# работают и как исключение для «только», и как безобидные наполнители).
# Сортировка по убыванию длины — чтобы «быстро» не примерялось раньше
# «быстрее».
_CONSENT_MANNER_ADVERBS = "|".join(
    sorted(_CONSENT_MANNER_WORDS, key=lambda w: (-len(w), w))
)
_CONSENT_CAVEAT_RE = re.compile(
    r"\b(?:"
    r"кроме|исключая|исключи\w*|за\s+исключением|"
    r"но\s+не|а\s+не|"
    r"не\s+(?:надо|нужно|трогай|трогая|удаляй|удали|включай|бери|берём|стоит)|"
    r"оставь\w*|оставить|оставим|оставляем|оставляя|"
    r"пропусти\w*|пропустить|пропустим|пропуская|"
    r"только(?!\s+(?:" + _CONSENT_MANNER_ADVERBS + r")\b)|"
    r"без\s+(?!проблем|вопросов|базара|разговоров|сомнений|задержек|"
    r"проволочек|лишних)\w+|"
    r"except|excluding|exclude|apart\s+from|other\s+than|but\s+not|"
    r"all\s+but|everything\s+but|skip"
    r")\b",
    re.IGNORECASE,
)

# (C) Пересказ ответа человека моделью («Пользователь: да», «он сказал да»,
# «yes (по словам пользователя)») проходил как дословная реплика. Докстринги
# требуют ДОСЛОВНУЮ последнюю реплику — пересказ это самый частый честный
# промах модели, и отличить его от подделки сервер не может, поэтому отказ.
_CONSENT_PARAPHRASE_RE = re.compile(
    r"^(?:пользователь|юзер|человек|владелец|хозяин|user|the\s+user)\s*[:\-—]|"
    r"^(?:пользователь|юзер|человек|владелец|он|она|user)\s+"
    r"(?:сказал|сказала|ответил|ответила|подтвердил|подтвердила|говорит|"
    r"пишет|написал|написала)\b|"
    r"^(?:the\s+user|he|she|they)\s+(?:said|says|replied|confirmed|approved)\b|"
    r"\b(?:по|согласно)\s+словам\s+(?:пользователя|юзера|человека|владельца)\b|"
    r"\bсо\s+слов\s+(?:пользователя|юзера|человека|владельца)\b|"
    r"\bas\s+(?:the\s+)?user\s+said\b|\baccording\s+to\s+the\s+user\b",
    re.IGNORECASE,
)

# Неуверенность и безразличие — «наверное да», «думаю да», «делай что хочешь»,
# «мне всё равно». Формально там есть утвердительное слово («да», «делай»), но
# согласия человек не давал: он либо колеблется, либо самоустраняется. Для
# необратимой операции это не «да» (то же основание, по которому «ну ладно» не
# попало в словарь согласий).
_CONSENT_HEDGE_RE = re.compile(
    r"\b(?:наверн(?:ое|о)|возможно|может\s+быть|думаю|кажется|вроде(?:\s+бы)?|"
    r"не\s+уверен\w*|сомневаюсь|"
    r"как\s+(?:хочешь|хотите|знаешь|знаете|сам\w*)|"
    r"что\s+(?:хочешь|хотите)|всё\s+равно|все\s+равно|пофиг|"
    r"maybe|probably|i\s+guess|i\s+think|whatever|up\s+to\s+you|"
    r"not\s+sure|dunno"
    r")\b",
    re.IGNORECASE,
)

_CONSENT_REFUSAL_REASONS = {
    "echo": (
        "🛑 Ответ повторяет служебный жаргон самого сервера (манифест-id, имя "
        "инструмента, JSON, «DELETE 5»), а не человеческую реплику. Это ровно "
        "то, что печатает модель, подтверждающая саму себя. Ничего не сделано, "
        "план ещё активен: спроси человека и передай его ответ дословно."
    ),
    "paraphrase": (
        "🛑 Похоже на ПЕРЕСКАЗ ответа человека, а не на его дословную реплику "
        "(«Пользователь: да», «он сказал да», «yes (по словам пользователя)»). "
        "Нужна последняя реплика человека БУКВАЛЬНО, как он её написал, без "
        "твоих слов вокруг. Ничего не сделано, план ещё активен — спроси "
        "человека и передай его ответ дословно."
    ),
    "caveat": (
        "🛑 В ответе есть оговорка/исключение — это ЧАСТИЧНОЕ согласие, а "
        "сервер не умеет исполнять план частично: манифест применяется только "
        "целиком, включая то, что человек исключил. Ничего не сделано, план "
        "аннулирован. Построй план ЗАНОВО — уже под уточнённый набор "
        "объектов, — покажи его человеку и спроси подтверждение ещё раз."
    ),
    "negative": (
        "🛑 В ответе есть отрицание — это НЕ согласие, где бы оно ни стояло в "
        "фразе. Ничего не сделано, план аннулирован. Если человек хотел другой "
        "набор объектов — построй план заново и спроси подтверждение ещё раз."
    ),
    "hedge": (
        "🛑 Ответ выражает неуверенность или безразличие («наверное да», "
        "«делай что хочешь»), а не согласие. Для необратимой операции этого "
        "недостаточно. Ничего не сделано, план ещё активен: переспроси "
        "человека прямо — нужно однозначное «да» или «нет»."
    ),
    "ambiguous": (
        "🛑 Ответ не является ОДНОЗНАЧНЫМ согласием: в нём есть условие, "
        "уточнение, перечисление или рассуждение, а подтверждение необратимой "
        "операции — узкий протокол, а не свободный диалог. Сервер сознательно "
        "НЕ пытается угадать, что имелось в виду: угадав неверно, он удалит "
        "не то. Ничего не сделано, план ещё активен. Переспроси человека и "
        "попроси ответить одним словом: «да» или «нет» (если он хочет другой "
        "набор объектов — построй план заново)."
    ),
}


class ConsentReplyVerdict:
    """Разбор ответа человека одним проходом: `kind` — что это за ответ
    ("affirmative" | "caveat" | "negative" | "paraphrase" | "hedge" | "echo" |
    "empty" | "ambiguous"), `reason` — готовый обучающий текст отказа ("" для
    affirmative и для случаев, где вызывающий подставляет свой дефолт).

    "ambiguous" — сборный вид для ВСЕГО, что не разобрано однозначно (бывший
    "unrecognized" плюс всё сложное, что раньше проскакивало в согласие)."""
    __slots__ = ("kind", "reason")

    def __init__(self, kind: str, reason: str = ""):
        self.kind = kind
        self.reason = reason


def _normalize_consent_reply(reply: Optional[str]) -> str:
    return re.sub(r'\s+', ' ', (reply or "").strip().strip('.!?,;:')).lower()


def _consent_tokens(norm: str) -> List[str]:
    # Strip PER-WORD punctuation too ("да," / "го!") — _normalize_consent_reply
    # only trims the ends of the whole string, and a real chat reply like
    # "да, удаляй" or "да, сливай" commonly punctuates mid-sentence.
    return [t.strip('.,!?;:') for t in norm.split() if t.strip('.,!?;:')]


def _collapse_consent_set_phrases(norm: str) -> str:
    """Схлопывает устойчивые обороты подтверждения в одно известное слово
    («да, всё верно» → «да, верно»). Применяется ТОЛЬКО в финальной проверке
    согласия — раньше по тексту уже прошли отрицание/оговорка/неуверенность,
    и подменять им входные данные нельзя."""
    for rx, repl in _CONSENT_SET_PHRASES:
        norm = rx.sub(repl, norm)
    return norm


def _classify_consent_reply(reply: Optional[str]) -> ConsentReplyVerdict:
    """Единственное место, где решается, что означает ответ человека. Порядок
    проверок — от «ответа человека вообще нет» к «ответ есть, но он не полное
    согласие»; всё, что не распознано однозначно как согласие, — отказ
    (fail-closed)."""
    norm = _normalize_consent_reply(reply)
    if not norm:
        return ConsentReplyVerdict("empty")
    if _CONSENT_ECHO_ARTIFACT_RE.search(norm):
        return ConsentReplyVerdict("echo", _CONSENT_REFUSAL_REASONS["echo"])
    # Пересказ проверяем ДО оговорки и отрицания: если реплики человека тут
    # вообще нет, план губить незачем — модели надо просто переспросить.
    if _CONSENT_PARAPHRASE_RE.search(norm):
        return ConsentReplyVerdict("paraphrase",
                                   _CONSENT_REFUSAL_REASONS["paraphrase"])
    # Целая фраза-отказ («не надо») — это отказ, а не оговорка: проверяем до
    # маркеров оговорки, иначе «не надо» уехало бы в caveat из-за «не + надо».
    if norm in _CONSENT_NEGATIVE_WORDS:
        return ConsentReplyVerdict("negative",
                                   _CONSENT_REFUSAL_REASONS["negative"])
    if _CONSENT_CAVEAT_RE.search(norm):
        return ConsentReplyVerdict("caveat", _CONSENT_REFUSAL_REASONS["caveat"])
    tokens = _consent_tokens(norm)
    # (B) Отрицание ищем по ВСЕМ токенам, а не в окне первых четырёх: именно
    # из-за окна «да, всё верно, но подожди с третьей» считалось согласием,
    # хотя докстринг обещал обратное. Согласие по-прежнему ищем в первых 4
    # токенах, чтобы «да, и ещё сделай X» продолжало работать.
    if any(t in _CONSENT_NEGATIVE_WORDS for t in tokens) or \
            norm in _CONSENT_NEGATIVE_WORDS:
        return ConsentReplyVerdict("negative",
                                   _CONSENT_REFUSAL_REASONS["negative"])
    if _CONSENT_HEDGE_RE.search(norm):
        return ConsentReplyVerdict("hedge", _CONSENT_REFUSAL_REASONS["hedge"])
    # (D) СМЕНА ПРИНЦИПА 2026-08-06 — fail-closed вместо fail-open.
    # Было: «есть утвердительное слово среди первых четырёх токенов и не
    # поймали маркер отказа ⇒ согласие». Из-за этого «делай, я передумал
    # насчёт третьей» исполнялось: «делай» в словаре, а «передумал насчёт
    # третьей» не подходит ни под один маркер. Гонку «перечислим все
    # слова-ограничители» выиграть нельзя — ограничитель всегда можно
    # сформулировать иначе.
    # Стало: согласие ⇔ ВЕСЬ ответ состоит из понятных нам элементов —
    # минимум одно слово согласия, всё остальное из безобидных наполнителей,
    # и фраза короткая. Встретили хоть один незнакомый токен ⇒ не согласие
    # (kind="ambiguous"): нельзя ошибиться в разборе того, что не разбираешь.
    core = _consent_tokens(_collapse_consent_set_phrases(norm))
    if (core and len(core) <= _CONSENT_MAX_TOKENS
            and any(t in _CONSENT_AFFIRMATIVE_WORDS for t in core)
            and all(t in _CONSENT_AFFIRMATIVE_WORDS or t in _CONSENT_FILLER_WORDS
                    for t in core)):
        return ConsentReplyVerdict("affirmative")
    return ConsentReplyVerdict("ambiguous",
                               _CONSENT_REFUSAL_REASONS["ambiguous"])


def _consent_refusal_reason(reply: Optional[str]) -> str:
    """Конкретное объяснение, ПОЧЕМУ ответ не принят за согласие ("" — если
    принят, либо если объяснять нечего и подойдёт общий `_NO_REPLY_INSTRUCTION`
    вызывающего)."""
    return _classify_consent_reply(reply).reason


def _is_negative_reply(reply: Optional[str]) -> bool:
    """«Ответ есть, и он НЕ согласие, причём план надо аннулировать» — прямое
    отрицание в любом месте фразы ИЛИ частичное согласие с оговоркой (в обоих
    случаях исполнять показанный план целиком нельзя, его надо перестроить).
    Пересказ, эхо и неоднозначный ответ ("ambiguous") сюда НЕ входят: там
    внятной реплики человека попросту нет, план остаётся валидным и вызов
    можно повторить с дословным однозначным ответом."""
    return _classify_consent_reply(reply).kind in ("negative", "caveat")


def _is_affirmative_reply(reply: Optional[str]) -> bool:
    """True only for a real human-shaped "yes" — see docs/DESIGN_approval_gate.md
    §4.3.3. Fail-closed по построению: согласием считается ТОЛЬКО ответ,
    целиком собранный из понятных слов (слово согласия + безобидные
    наполнители, короткая фраза). Всё остальное — пустое, отрицание, эхо
    манифеста, пересказ («пользователь: да»), оговорка («ок, кроме
    последней»), неуверенность («наверное да») и любая сложная фраза («делай,
    я передумал насчёт третьей», «да, и заодно удали ещё вон ту») — НЕ
    согласие. Отрицание в любом месте фразы перевешивает: окна нет."""
    return _classify_consent_reply(reply).kind == "affirmative"


# ===========================================================================
# === CONSENT-REPLY CLASSIFIER — END =======================================
# ===========================================================================
# Конец переносимого блока (см. маркер BEGIN выше): всё, что ниже, уже завязано
# на состояние конкретного сервера (манифесты, клиент, Telegram) и НЕ копируется
# вместе с классификатором.


def _manifest_object_hash(action: str, ids: List[str]) -> str:
    """Binds a manifest to the exact object ids it was planned over (§4.3.2)
    — recomputed at consent-check time and compared to the value stored at
    plan time, so a manifest whose stored items were somehow mutated between
    plan and execute is caught rather than silently applied."""
    payload = action + "|" + ",".join(sorted(str(i) for i in ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _manifest_params_hash(action: str, params: Dict) -> str:
    """Аналог _manifest_object_hash для _gate_single-манифестов, у которых нет
    списка id объектов — привязывает манифест к точному набору параметров.

    Нужен из-за TG-кнопки: авто-исполнение по кнопке (try_auto_execute)
    сверяет `object_hash` перед тем, как что-то сделать, и БЕЗ сохранённого
    хэша эта сверка просто пропускается — кнопка оказалась бы слабее
    чат-подтверждения. Чат-путь при этом не меняется: `_require_consent`
    трогает `object_hash` только когда ему передали `object_ids`, а
    `_gate_single` их не передаёт."""
    payload = action + "|" + json.dumps(params or {}, sort_keys=True,
                                        ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConsentResult:
    __slots__ = ("ok", "reason")

    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ok


def _maybe_tg_notify_plan(tool: str, manifest_id: str, preview_text: str) -> str:
    """Зовётся ПОСЛЕ создания манифеста (`_MANIFESTS[mid] = {...}`), ПЕРЕД
    возвратом превью-текста моделью — портирует поведение gmail-mcp's
    requireConsent's "фаза плана" ветки на архитектуру ticktick-mcp, где
    plan_*/аналоги — ОТДЕЛЬНЫЕ функции от _require_consent() (не единый
    dual-mode вызов, как в TS). Fail-closed (та же дисциплина, что в TS): если
    отправка в Telegram упала, манифест ИНВАЛИДИРУЕТСЯ, а не остаётся
    доступным через голое user_reply без второго фактора."""
    if not (tool and tg_approval.enabled_for(_TG_CFG, tool)):
        return preview_text
    ok, err = tg_approval.notify_plan(_TG_CFG, manifest_id, preview_text, tool)
    if not ok:
        m = _MANIFESTS.get(manifest_id)
        if m is not None:
            m["consumed"] = True
        return (f"🛑 Не смог отправить запрос подтверждения в Telegram ({err}). "
                "Действие НЕ запланировано, ничего не изменено. Проверьте "
                "бота/настройки Telegram-подтверждения и попробуйте снова.")
    # УСПЕШНО отправлено → ПОМЕЧАЕМ САМ МАНИФЕСТ (2026-08-06, фикс
    # недетерминированного гейта). До этой правки факт «план ушёл в Telegram»
    # нигде не сохранялся: execute-фаза заново решала, нужен ли ТГ-фактор, по
    # аргументу `tool`, который один путь передавал (delete_tasks), а другой
    # (execute_task_deletion) — забывал, из-за чего ОДНА И ТА ЖЕ операция на
    # одном сервере то требовала кнопку, то нет. Флаг переносит решение туда,
    # где оно фактически принято — в фазу плана; execute-фаза больше не
    # угадывает. Fail-closed: если план ушёл в TG, «да» в чате БЕЗ approved-
    # строки не проходит НИКОГДА, чем бы ни был вызов execute.
    m = _MANIFESTS.get(manifest_id)
    if m is not None:
        m["tg_notified"] = True
        m["_tg_tool"] = tool
        m["_tg_manifest_id"] = manifest_id
    else:
        # Не должно случаться (все вызывающие создают манифест ДО отправки).
        # Не fail-closed-отказ: кнопка в TG всё равно ни к чему не приведёт —
        # ни поллер, ни execute не найдут манифеста, — но это ошибка порядка
        # вызовов, и она должна быть видна в логе.
        logger.warning(f"TG approval: план {manifest_id} отправлен в Telegram, "
                       f"но манифест не найден в _MANIFESTS (tool={tool})")
    # Приписка честно отражает, что произойдёт ДАЛЬШЕ, а не то, что было
    # написано здесь до 2026-08-06 («…затем ответьте «да» здесь»). Для планов,
    # которые сервер умеет исполнить сам по нажатию (есть авто-исполнитель),
    # текстовый путь ЗАКРЫТ (см. `_tg_button_only`), и звать инструмент второй
    # раз не нужно — прежняя формулировка подталкивала и владельца, и модель
    # ровно к тому действию, которое теперь отвергается. Для двух планов без
    # авто-исполнителя (delete_project / rename_tag-слияние) повторный вызов
    # ПО-ПРЕЖНЕМУ нужен — им нельзя обещать «выполнится само».
    if _resolve_auto_executor(_auto_execute_tool_of(m or {}), m or {}) is not None:
        return (preview_text + "\n\n_⏳ Запрос на подтверждение отправлен в "
                "Telegram. Подтвердите кнопкой ✅ в боте — операция выполнится "
                "автоматически: короткая сводка придёт в то же сообщение, "
                "полный отчёт — в группу-архив «MCP Отчёты». Повторно "
                "вызывать этот инструмент НЕ нужно, текстовое «да» для этого "
                "плана не принимается._")
    return (preview_text + "\n\n_⏳ Запрос на подтверждение отправлен в "
            "Telegram. Подтвердите кнопкой ✅ в боте — этот план сервер сам не "
            "исполняет, после нажатия повторите вызов инструмента._")


_TG_BUTTON_ONLY_PENDING_MSG = (
    "⏳ Этот план подтверждается ТОЛЬКО кнопкой в Telegram — текстовое "
    "подтверждение для него отключено, что бы пользователь ни написал в чате. "
    "Нажмите ✅ в боте: операция выполнится сама, отчёт придёт в то же "
    "сообщение с кнопками. Повторно вызывать этот инструмент НЕ нужно — "
    "просто скажи пользователю, что ждёшь его нажатия. План активен."
)

_TG_BUTTON_ONLY_APPROVED_MSG = (
    "✅ Уже подтверждено кнопкой в Telegram — сервер исполняет эту операцию "
    "САМ (фоновый поллер, обычно в течение ~10 секунд). Ничего повторять не "
    "надо и этот инструмент больше звать не надо: результат будет вписан в то "
    "же сообщение Telegram, где были кнопки."
)


def _tg_button_only(manifest: Optional[Dict]) -> bool:
    """Закрыт ли для ЭТОГО плана текстовый путь исполнения (2026-08-06).

    Два условия, оба обязательны:
      1. План реально ушёл в Telegram — пометку `tg_notified` ставит ТОЛЬКО
         `_maybe_tg_notify_plan`, и только при УСПЕШНОЙ отправке сообщения с
         кнопками. Решение принимается по СОСТОЯНИЮ МАНИФЕСТА, а не по
         текущему значению `TG_APPROVAL_ENABLED`: выключить настройку между
         планом и исполнением и тем «растворить» требование кнопки нельзя.
      2. У плана есть авто-исполнитель (`_resolve_auto_executor`), то есть
         нажатие кнопки действительно приведёт к исполнению фоновым поллером.
         Без этого условия закрытие текстового пути означало бы, что
         операцию нельзя выполнить ВООБЩЕ никак. Сегодня мимо проходят ровно
         два плана — `delete_project` и слияние в `rename_tag` (их манифесты
         помечены собственными `_gate`, поллер их не исполняет; так и написано
         в их докстрингах). Для них остаётся прежняя двухфакторная схема:
         кнопка ОБЯЗАТЕЛЬНА, но исполняет повторный вызов инструмента. Как
         только у такого плана появится авто-исполнитель, он автоматически
         перейдёт в button-only — отдельного списка тулов здесь намеренно нет.

    Возвращает False для всего остального (слой выключен, план в Telegram не
    уходил, вызов без манифеста) → путь через chat-«да» работает как раньше.
    """
    if not (manifest or {}).get("tg_notified"):
        return False
    return _resolve_auto_executor(_auto_execute_tool_of(manifest), manifest) is not None


_NO_REPLY_INSTRUCTION = (
    "🛑 Нужно подтверждение пользователя, а не самого себя: этот инструмент "
    "физически необратим/массовый, поэтому вызывай его ТОЛЬКО после того, "
    "как человек в чате явно ответил на показанный план. Передай "
    "user_reply=<дословная последняя реплика пользователя> — не сочиняй её "
    "и не вызывай execute в этом же ходе, где ты только что показал план: "
    "дождись отдельного сообщения от человека. Ничего не сделано."
)


def _require_consent(
    *, action: str, tier: int, manifest: Optional[Dict] = None,
    user_reply: Optional[str] = None, automation_key: str = "",
    object_ids: Optional[List[str]] = None, min_gap: Optional[float] = None,
    tool: str = "", manifest_id: str = "",
) -> ConsentResult:
    """The single gate every mutating tool in tiers 🟡(1)/🔴(2) must pass
    before touching live state or writing an "approved" decision — see
    docs/DESIGN_approval_gate.md §4.3/§6.1. Tier 🟢(0) always passes (nothing
    to gate). `manifest`, when given, is the ALREADY-looked-up plan_*
    manifest for this call (kind/existence already checked by the caller) —
    used here only for the one-shot/TTL/binding/timing checks. When None,
    this is an inline (non-manifest) 🔴 check (e.g. rename_tag's merge
    branch, or a sheet-backed declutter row) — the binding/timing checks are
    skipped (there is no plan_shown_at to compare against) but the
    affirmative-reply check still fully applies. Never mutates `manifest`
    except to invalidate it on an explicit "no"."""
    if SECRET and automation_key and hmac.compare_digest(automation_key, SECRET):
        return ConsentResult(True, "automation_key")
    if tier <= 0:
        return ConsentResult(True, "tier0-no-gate")

    if manifest is not None:
        if manifest.get("consumed"):
            # `_manifest_gone_msg` различает «погашен поллером по кнопке» и
            # «просто протух»: без него модель, позвавшая execute сразу после
            # нажатия кнопки (пока `_prune_manifests` ещё не выбросил запись),
            # получала безликое «протух» и не понимала, исполнено ли что-то.
            return ConsentResult(False, _manifest_gone_msg(
                manifest_id or manifest.get("_tg_manifest_id", ""),
                "🛑 Манифест уже исполнен (one-shot) — план протух. Вызови "
                "plan_* заново."))
        created = manifest.get("created")
        if created is not None and time.monotonic() - created > _MANIFEST_TTL:
            manifest["consumed"] = True
            return ConsentResult(False, "🛑 Манифест истёк (>1ч с момента "
                                  "плана) — вызови plan_* заново.")
        stored_hash = manifest.get("object_hash")
        if stored_hash and object_ids is not None:
            if _manifest_object_hash(action, object_ids) != stored_hash:
                return ConsentResult(False, "🛑 Манифест не совпадает с текущим "
                                      "набором объектов (изменился между "
                                      "планом и подтверждением) — вызови "
                                      "plan_* заново.")

    # Текст отказа — КОНКРЕТНЫЙ, а не общий: `_consent_refusal_reason` знает,
    # ЧЕМ именно ответ не является согласием (прямое отрицание / оговорка вида
    # «ок, кроме последней» / пересказ вместо дословной реплики / неуверенность
    # / эхо манифестного жаргона) и объясняет модели, что делать дальше. Без
    # этой подстановки классификатор различал бы случаи внутри себя, а наружу
    # отдавал одно и то же безликое «не подтвердил» — и модель, получив отказ
    # на «ок, кроме последней», не понимала бы, что от неё хотят перепланировать
    # набор, а не переспросить то же самое. Пустая строка (объяснять нечего) →
    # прежние общие формулировки, поведение не меняется.
    if _is_negative_reply(user_reply):
        if manifest is not None:
            manifest["consumed"] = True
        detail = _consent_refusal_reason(user_reply)
        # Ведущая фраза «Пользователь НЕ подтвердил» сохранена дословно: на неё
        # опираются существующие тесты и, возможно, внешние интеграции, читающие
        # ответ гейта. Конкретика классификатора идёт ПОСЛЕ неё, а не вместо.
        return ConsentResult(False, "🛑 Пользователь НЕ подтвердил — ничего не "
                             "сделано, план аннулирован. " + (
                                 detail.lstrip("🛑 ") if detail else
                                 "Ответ похож на отказ/отмену; при необходимости "
                                 "перепланируй."))

    # ─────────── BUTTON-ONLY: текстовый путь исполнения ЗАКРЫТ ───────────
    # Максим, 2026-08-06: «зачем вообще ждать текстовое да? нужно убрать для
    # Клода возможность видеть и тыкать методы экзекют».
    #
    # Почему это не «ещё одна проверка формулировок», а закрытие класса дыр:
    # сервер физически НЕ может отличить сочинённое моделью «да» от реального
    # ответа человека — он видит только строку. Всю ночь до этого затыкались
    # формулировки («делай, я передумал насчёт третьей» регуляркой не
    # ловится). Если план УЖЕ ушёл в Telegram и сервер умеет исполнить его сам
    # по нажатию, то текстовому пути исполнения просто нечего здесь делать:
    # исполняет фоновый поллер по факту нажатия, а любой вызов execute с
    # текстом отвергается — независимо от того, что в этом тексте написано.
    # Дыра не уменьшается, а исчезает: способа исполнить операцию текстом
    # больше не существует.
    #
    # Стоит ДО проверки `_is_affirmative_reply` намеренно: для такого плана
    # содержание реплики уже ни на что не влияет, и требовать «пришли
    # дословное да» (`_NO_REPLY_INSTRUCTION`) было бы враньём — модель пошла
    # бы добывать текст, который всё равно ничего не откроет. Проверка
    # ОТРИЦАНИЯ выше при этом сохранена: «нет» в чате по-прежнему гасит план,
    # не дожидаясь кнопки.
    if _tg_button_only(manifest):
        mid_for_lookup = manifest_id or (manifest or {}).get("_tg_manifest_id", "")
        approval = tg_approval.check_approval(mid_for_lookup)
        if approval == "approved":
            # Кнопка нажата, поллер ещё не добрался (интервал ~10 c). НЕ
            # исполняем здесь и НЕ гасим манифест — иначе поллер найдёт его
            # уже погашенным и операция не произойдёт вовсе.
            return ConsentResult(False, _TG_BUTTON_ONLY_APPROVED_MSG)
        if approval == "rejected":
            if manifest is not None:
                manifest["consumed"] = True
            return ConsentResult(False, "🛑 Отклонено кнопкой в Telegram. План отменён, "
                                  "ничего не сделано. Чтобы повторить — построй план заново.")
        if approval == "none":
            return ConsentResult(False, "🛑 Запрос подтверждения в Telegram не найден или "
                                  "истёк по TTL. Построй план заново.")
        return ConsentResult(False, _TG_BUTTON_ONLY_PENDING_MSG)

    if not _is_affirmative_reply(user_reply):
        return ConsentResult(False,
                             _consent_refusal_reason(user_reply) or _NO_REPLY_INSTRUCTION)

    # Опциональный внеполосный ТГ-фактор (см. tg_approval.py) — ВЫКЛ по
    # умолчанию (TG_APPROVAL_ENABLED=false). Встаёт ПОСЛЕ дешёвой проверки
    # user_reply, ПЕРЕД таймером/consume — та же позиция, что в gmail-mcp's
    # requireConsent.
    #
    # ДВА независимых основания требовать кнопку (2026-08-06):
    #   (а) манифест ПОМЕЧЕН `tg_notified` — план этой самой операции реально
    #       ушёл в Telegram (пометку ставит `_maybe_tg_notify_plan` и только
    #       он). Это ЖЁСТКОЕ, приоритетное основание: оно не зависит от того,
    #       передал ли конкретный execute-путь аргумент `tool`, — именно
    #       забытый `tool=` в `execute_task_deletion` делал самую опасную
    #       операцию недетерминированной (chat-«да» без кнопки исполнял
    #       удаление по-настоящему);
    #   (б) старое основание `tool and enabled_for(tool)` — сохранено для
    #       путей БЕЗ манифеста (inline-🔴), где помечать нечего.
    # Обратная совместимость: без `TG_APPROVAL_ENABLED=true` пометка не
    # ставится НИКОГДА и `enabled_for` всегда False → поведение побайтово
    # прежнее. Пути, чей план в Telegram не уходит (_gate_batch/_gate_single),
    # намеренно НЕ передают `tool`: иначе `check_approval` вернул бы "none"
    # (строки-то нет) и тулы отказывали бы навсегда даже на честное «да».
    #
    # ЧТО СЮДА ЕЩЁ ДОХОДИТ ПОСЛЕ button-only (блок выше). Планы, у которых
    # есть авто-исполнитель, до этой строки не добираются вовсе — им текстовый
    # путь закрыт целиком. Здесь остаются:
    #   • `delete_project` и слияние в `rename_tag` — помеченные, но НЕ
    #     авто-исполняемые: кнопка обязательна (иначе "pending"/"none"), а
    #     исполняет повторный вызов инструмента с «да». Единственные, для кого
    #     ниже возможен проход при approved;
    #   • непомеченные манифесты с переданным `tool` (план в Telegram не
    #     уходил — слой был выключен или тул вне allowlist): здесь
    #     `check_approval` вернёт "none", то есть отказ, ровно как и раньше.
    tg_required = bool((manifest or {}).get("tg_notified")) or bool(
        tool and tg_approval.enabled_for(_TG_CFG, tool))
    if tg_required:
        approval = tg_approval.check_approval(manifest_id or (manifest or {}).get("_tg_manifest_id", ""))
        if approval == "pending":
            return ConsentResult(False, "⏳ Подтвердите кнопкой в Telegram-боте, затем "
                                  "повторите. План ещё активен.")
        if approval == "rejected":
            if manifest is not None:
                manifest["consumed"] = True
            return ConsentResult(False, "🛑 Отклонено кнопкой в Telegram. План отменён, "
                                  "ничего не сделано. Чтобы повторить — построй план заново.")
        if approval == "none":
            return ConsentResult(False, "🛑 Запрос подтверждения в Telegram не найден или "
                                  "истёк по TTL. Построй план заново.")
        # approval == "approved" → идём дальше.

    gap = _MIN_CONSENT_GAP if min_gap is None else min_gap
    if manifest is not None and gap > 0:
        shown_at = manifest.get("plan_shown_at", manifest.get("created"))
        if shown_at is not None and time.monotonic() - shown_at < gap:
            return ConsentResult(False, "🛑 План и «да» пришли слишком быстро "
                                  f"подряд (< {gap:.0f}с) — похоже на "
                                  "самоподтверждение в один ход, без реального "
                                  "ответа человека. Покажи план пользователю, "
                                  "дождись ЕГО отдельного сообщения, потом "
                                  "повтори execute с user_reply.")

    return ConsentResult(True, "user_reply")


# Какой манифест исполняется ПРЯМО СЕЙЧАС по нажатой в Telegram кнопке
# (устанавливается только в `_tg_auto_execute_tick` вокруг вызова исполнителя,
# см. там же). Пусто во всех остальных случаях — на чат-пути ничего не
# меняется.
#
# Зачем (2026-08-06, найдено аудитом слияния): независимая перепроверка
# автоисполнения зовёт `_build_operation_report(manifest_id)`, а тот ищет
# записи журнала по `record`/`manifest`. Совпадение с manifest_id было ТОЛЬКО
# у delete_tasks (его `_execute_task_deletion_impl` кладёт "manifest": mid) —
# все остальные исполнители журналируют через `_op_journal`, который пишет
# собственный "<op>-<hex>". После расширения кнопки на 22 инструмента это
# значило, что у всех НОВЫХ тулов перепроверка возвращала «В журнале нет
# записей по …» и вердикт был вечным "unverified": отчёт в группу уходил, но
# доказывать в нём было нечего. Метка проставляется в ОДНОМ месте
# (`_journal_write` — единственная дверь в журнал), поэтому новый исполнитель
# получает её автоматически, без правок в своём коде.
_TG_AUTO_EXECUTE_MANIFEST: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "tg_auto_execute_manifest", default="")


def _journal_write(record: Dict) -> str:
    """Append a JSON record to the mutation journal (best-effort). Returns the
    journal path or '' if unwritable. The journal holds FULL task snapshots so
    anything mutated by mistake can be reconstructed by hand."""
    mid = _TG_AUTO_EXECUTE_MANIFEST.get()
    if mid and not record.get("tg_manifest"):
        # Копия, а не мутация аргумента: вызывающие собирают record инлайном,
        # но полагаться на это нельзя.
        record = {**record, "tg_manifest": mid}
    try:
        os.makedirs(_JOURNAL_DIR, exist_ok=True)
        path = os.path.join(_JOURNAL_DIR, "deletion_journal.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path
    except Exception as e:
        logger.warning(f"mutation journal unwritable: {e}")
        return ""


def _snapshot_of(live: Optional[Dict]) -> Dict:
    """Compact snapshot of a live task for the journal."""
    return {k: (live or {}).get(k) for k in
            ("title", "content", "desc", "dueDate", "startDate", "priority",
             "tags", "projectId", "parentId", "columnId", "isAllDay")
            if (live or {}).get(k) is not None}


def _op_journal(op: str, items: List[Dict], summary: str = "") -> str:
    """Record a mutation operation: op ∈ create/update/complete/delete/move/
    tags/parent/abandon. Each item: {taskId, title, snapshot?, expect?}.
    Returns the record id ("<op>-<hex>") to hand to operation_report, or ''
    when the journal is unavailable (report then impossible — say so)."""
    rid = f"{op}-{uuid.uuid4().hex[:8]}"
    path = _journal_write({
        "ts": datetime.now(timezone.utc).isoformat(),
        "record": rid, "op": op, "summary": summary, "items": items,
    })
    return rid if path else ""


def _report_line(rid: str) -> str:
    """Standard footer pointing at the independent post-check."""
    if not rid:
        return "🧾 Журнал недоступен — независимая проверка невозможна."
    return f"🧾 Независимая проверка: operation_report(record_id=\"{rid}\")."


def _prune_manifests() -> None:
    now = time.monotonic()
    for mid in [m for m, v in _MANIFESTS.items()
                if now - v["created"] > _MANIFEST_TTL or v.get("consumed")]:
        _MANIFESTS.pop(mid, None)


# «Надгробия» (tombstones) исполненных манифестов — крошечная память о том,
# ПОЧЕМУ манифеста больше нет. Нужна ровно для одного человеческого случая:
# Максим нажал кнопку в Telegram, фоновый поллер (_tg_auto_execute_tick) уже
# всё исполнил и погасил манифест, а модель следом честно зовёт
# execute_task_deletion — и раньше получала неотличимое «не найден/истёк/уже
# исполнен», из которого нельзя понять, удалено ли что-то на самом деле.
# Хранится только id + причина + время, без содержимого плана; кап на размер,
# чтобы это не превращалось в утечку памяти.
_MANIFEST_TOMBSTONES: "collections.OrderedDict[str, Dict]" = collections.OrderedDict()
_MANIFEST_TOMBSTONE_CAP = 200


def _tombstone_manifest(manifest_id: str, reason: str) -> None:
    _MANIFEST_TOMBSTONES.pop(manifest_id, None)
    _MANIFEST_TOMBSTONES[manifest_id] = {
        "reason": reason, "ts": datetime.now(timezone.utc).isoformat()}
    while len(_MANIFEST_TOMBSTONES) > _MANIFEST_TOMBSTONE_CAP:
        _MANIFEST_TOMBSTONES.popitem(last=False)


def _manifest_gone_msg(manifest_id: str, default: str) -> str:
    """Внятный ответ вместо общего «не найден/истёк»: различает «уже исполнено
    по кнопке в Telegram» и «истёк/не существовал»."""
    t = _MANIFEST_TOMBSTONES.get(manifest_id)
    if t and t.get("reason") == "tg_auto_executed":
        return (f"✅ Этот план ({manifest_id}) УЖЕ исполнен — вы подтвердили его "
                "кнопкой в Telegram, и сервер выполнил его сам "
                f"({t.get('ts')}). Повторять нечего, ничего не потеряно: отчёт "
                "об исполнении вписан в то же сообщение Telegram, где были "
                "кнопки. Для новой операции построй план заново.")
    return default


# ---------------------------------------------------------------------------
# Shared tier-🟡 gate for the batch-mutation tools (update_tasks/
# complete_tasks/move_tasks/set_task_parent/set_task_tags/restore_tasks) —
# docs/DESIGN_approval_gate.md §4/§5. Same "one function, two calls" shape as
# delete_tasks (above): call #1 (manifest_id omitted) stores the caller's
# `tasks` VERBATIM in a one-shot manifest and returns a preview — nothing
# runs. Call #2 (manifest_id + user_reply) is checked by
# _require_consent(tier=1, ...) and, on success, hands back the manifest's
# STORED tasks — never the `tasks` argument of call #2 — so the set can't be
# swapped between plan and execute. This is the SAME public tool name in both
# calls; no new tools are introduced.
class _GateOutcome:
    __slots__ = ("proceed", "tasks", "summary", "message", "extra")

    def __init__(self, proceed: bool, tasks: Optional[List[Dict]] = None,
                 summary: Optional[str] = None, message: Optional[str] = None,
                 extra: Optional[Dict] = None):
        self.proceed = proceed
        self.tasks = tasks
        self.summary = summary
        self.message = message
        self.extra = extra or {}


def _gate_batch(kind: str, tool_name: str, tasks: Optional[List[Dict]],
                summary: str, manifest_id: str, user_reply: str,
                describe_item, extra: Optional[Dict] = None) -> _GateOutcome:
    """Runs the two-call consent gate. Returns a _GateOutcome: when
    `.proceed` is True, the caller must actually run the mutation using
    `.tasks`/`.summary`/`.extra`; when False, `.message` is the full response
    to return as-is (nothing was touched). `extra` (call #1 only) is fixed
    context beyond the per-task list (e.g. set_task_parent's target parent,
    move_tasks's destination project) — stored verbatim in the manifest and
    handed back unchanged on call #2, same one-shot/no-swap guarantee as
    `tasks`."""
    _prune_manifests()
    if manifest_id:
        m = _MANIFESTS.get(manifest_id)
        if not m or m.get("kind") != kind:
            return _GateOutcome(False, message=_manifest_gone_msg(manifest_id, (
                f"🛑 Манифест {manifest_id} не найден/истёк/уже исполнен. "
                f"Начни заново: {tool_name}(summary, tasks, ...) без "
                "manifest_id.")))
        stored = m.get("tasks") or []
        ids = [str(t.get("taskId") or t.get("task_id") or "") for t in stored]
        # tier-🟡 per docs/DESIGN_approval_gate.md §5: no anti-duplet gap —
        # only 🔴 tools time-gate plan vs. execute.
        # `tool=` здесь НАМЕРЕННО не передаётся, хотя с 2026-08-06 план ЭТИХ
        # тулов тоже уходит в Telegram (см. _maybe_tg_notify_plan в конце
        # ветки call #1). Причина прежняя: `tool=` включал бы ТГ-проверку
        # БЕЗУСЛОВНО — в том числе для манифестов, чей план в Telegram по
        # какой-то причине НЕ ушёл (тул вне TG_APPROVAL_TOOLS, слой выключен),
        # и тогда check_approval вернул бы "none" → вечный отказ даже на
        # честное «да». Решает пометка `tg_notified` в САМОМ манифесте: она
        # стоит тогда и только тогда, когда сообщение с кнопками реально
        # отправлено — то есть требование кнопки включается ровно вместе с
        # существованием кнопки.
        cr = _require_consent(action=kind, tier=1, manifest=m,
                              user_reply=user_reply, object_ids=ids, min_gap=0,
                              manifest_id=manifest_id)
        if not cr.ok:
            return _GateOutcome(False, message=cr.reason)
        m["consumed"] = True
        return _GateOutcome(True, tasks=stored, summary=m.get("summary") or summary,
                            extra=m.get("extra") or {})

    if not tasks:
        return _GateOutcome(False, message="Пустой список — нечего делать.")
    mid = uuid.uuid4().hex[:12]
    now = time.monotonic()
    ids = [str(t.get("taskId") or t.get("task_id") or "") for t in tasks]
    # `tool`/`_gate` (2026-08-06) — то, что нужно фоновому TG-поллеру, чтобы
    # ИСПОЛНИТЬ этот план по нажатию кнопки, не заводя по ручной регистрации
    # на каждый тул: `tool` даёт имя `_<tool>_impl`, `_gate` — форму вызова
    # (batch: impl(summary, tasks, **extra)). См. _generic_gate_auto_execute.
    _MANIFESTS[mid] = {"kind": kind, "tool": tool_name, "_gate": "batch",
                       "tasks": tasks, "summary": summary,
                       "created": now, "plan_shown_at": now, "consumed": False,
                       "object_hash": _manifest_object_hash(kind, ids),
                       "extra": extra or {}}
    lines = [f"### 📋 План — {summary}",
             f"_Манифест `{mid}` · ничего ещё не изменено_", ""]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {describe_item(t)}")
    lines.append("")
    lines.append(
        "Покажи это пользователю дословно и ДОЖДИСЬ его отдельного ответа "
        "(не отвечай за него). Когда он явно согласится, вызови этот же "
        f'инструмент снова: {tool_name}(summary="{summary}", tasks=[...], '
        f'manifest_id="{mid}", user_reply="<дословная реплика пользователя>") '
        "— НЕ в этом же ходе (сам список tasks можно повторить как есть, на "
        "2-м вызове он игнорируется — используются данные из манифеста). "
        f"Манифест одноразовый, действует {_manifest_ttl_phrase()}.")
    return _GateOutcome(False, message=_maybe_tg_notify_plan(
        tool_name, mid, "\n".join(lines)))


# ---------------------------------------------------------------------------
# Shared tier-🟢→🟡 gate for the SINGLE-object mutation tools that used to be
# exempt from _require_consent altogether (create_project, create_tag,
# checkin_habit, unset_task_parent, ...) — Maksim, 2026-08-05: "создание тоже
# должно быть через флоу план→экзек, всё что не только читает — через этот
# флоу" (no more tier exemption by "it's just a create/reversible edit").
# Same "one tool, two calls" shape as _gate_batch above, but for a single
# mutating call carrying arbitrary keyword params instead of a list of task
# dicts (there is no natural per-item list to preview here). tier=1 ("light
# confirmation" — create/reversible edits, not delete) per
# docs/DESIGN_write_tool_taxonomy.md's former 🟢 tier.
def _gate_single(kind: str, tool_name: str, params: Optional[Dict],
                 manifest_id: str, user_reply: str, describe_fn,
                 automation_key: str = "") -> _GateOutcome:
    """Call #1 (manifest_id omitted): stores `params` VERBATIM in a one-shot
    manifest and returns a preview built by describe_fn(params) — nothing is
    mutated. Call #2 (manifest_id + user_reply): _require_consent(tier=1, ...)
    is checked (a valid `automation_key` short-circuits the user_reply check,
    same headless-bypass contract as create_tasks/_gate_batch — see
    references/automation-secrets.md §8), then the STORED params (never call
    #2's own arguments) are handed back via `.extra` — same no-swap contract
    as _gate_batch. The stored `object_hash` here is a hash of the PARAMS
    (_manifest_params_hash) — there is no list of object ids to bind to (many
    of these tools create brand-new objects that don't exist yet). It is NOT
    consulted on the chat path: _require_consent only compares object_hash
    when the caller passes `object_ids`, and this gate never does. It exists
    for the Telegram-button path, whose auto-executor re-hashes the manifest
    before touching anything and would otherwise skip that check entirely.

    NOTE on one-shot: this function marks `consumed = True` itself right
    here on the SUCCESS path, matching docs/DESIGN_approval_gate.md §4.3.3
    item 4 ("after success, consumed = True") literally. _gate_batch does
    the same — see its own SUCCESS-path branch — so both functions are
    genuinely one-shot on their own, not just "accidentally" so via a retry
    that happens to also trip the identity-guard."""
    _prune_manifests()
    if manifest_id:
        m = _MANIFESTS.get(manifest_id)
        if not m or m.get("kind") != kind:
            return _GateOutcome(False, message=_manifest_gone_msg(manifest_id, (
                f"🛑 Манифест {manifest_id} не найден/истёк/уже исполнен. "
                f"Начни заново: {tool_name}(...) без manifest_id.")))
        # `tool=` не передаётся по той же причине, что и в _gate_batch выше:
        # ТГ-фактор включает пометка `tg_notified` в самом манифесте (её
        # ставит _maybe_tg_notify_plan только при реально отправленном
        # сообщении с кнопками), а не аргумент этого вызова.
        cr = _require_consent(action=kind, tier=1, manifest=m,
                              user_reply=user_reply, min_gap=0,
                              automation_key=automation_key,
                              manifest_id=manifest_id)
        if not cr.ok:
            return _GateOutcome(False, message=cr.reason)
        m["consumed"] = True
        return _GateOutcome(True, extra=m.get("params") or {})

    if not params:
        return _GateOutcome(False, message="Нечего делать — пустые параметры.")
    mid = uuid.uuid4().hex[:12]
    now = time.monotonic()
    # `tool`/`_gate` — см. тот же комментарий в _gate_batch (single:
    # impl(**params)). `object_hash` здесь считается по ПАРАМЕТРАМ
    # (_manifest_params_hash): у single-плана нет списка id объектов, но
    # авто-исполнению по TG-кнопке всё равно нужна привязка к тому, ЧТО
    # именно было показано человеку. На чат-путь это не влияет —
    # `_require_consent` сверяет object_hash только когда ему передан
    # `object_ids`, а этот гейт его не передаёт.
    _MANIFESTS[mid] = {"kind": kind, "tool": tool_name, "_gate": "single",
                       "params": params, "created": now,
                       "plan_shown_at": now, "consumed": False,
                       "object_hash": _manifest_params_hash(kind, params)}
    lines = [f"### 📋 План — {describe_fn(params)}",
             f"_Манифест `{mid}` · ничего ещё не изменено_", "",
             "Покажи это пользователю дословно и ДОЖДИСЬ его отдельного "
             "ответа (не отвечай за него). Когда он явно согласится, вызови "
             f"{tool_name} СНОВА с теми же аргументами и добавь "
             f'manifest_id="{mid}", user_reply="<дословная реплика '
             'пользователя>" — НЕ в этом же ходе (сами аргументы можно '
             "повторить как есть, на 2-м вызове они игнорируются — "
             "используются данные из манифеста). Манифест одноразовый, "
             f"действует {_manifest_ttl_phrase()}."]
    return _GateOutcome(False, message=_maybe_tg_notify_plan(
        tool_name, mid, "\n".join(lines)))


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
            "snapshot": {k: (live or {}).get(k) for k in
                         ("title", "content", "desc", "dueDate", "startDate",
                          "priority", "tags", "projectId", "parentId", "isAllDay")
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
             f"_Манифест `{mid}` · ничего ещё не удалено_", ""]
    for i, it in enumerate(items, 1):
        mark = "↳ " * it.get("depth", 0)
        lines.append(f"{i}. {mark}**«{it['title']}»** — {it['project']} (`{it['taskId']}`)")
    if mismatch:
        lines.append(_mismatch_report(mismatch, "включил в план"))
    if missing:
        lines.append(f"↷ Исключены (не среди открытых) {len(missing)}: "
                     + ", ".join(f"«{m['title']}»" for m in missing))
    lines.append("")
    lines.append("Покажи этот план пользователю дословно и ДОЖДИСЬ его "
                 "отдельного ответа (не отвечай за него). Когда он явно "
                 "согласится, вызови "
                 f"`execute_task_deletion(manifest_id=\"{mid}\", "
                 "user_reply=\"<дословная реплика пользователя>\")` — НЕ в "
                 "этом же ходе. Манифест одноразовый, "
                 f"действует {_manifest_ttl_phrase()}.")
    # В поток по той же причине, что и в delete_tasks выше: синхронная
    # отправка нескольких сообщений с паузами не должна держать event loop.
    return await _run_blocking(_maybe_tg_notify_plan, "delete_tasks", mid,
                               "\n".join(lines))


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
        m["consumed"] = True
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
            if not planned_title:
                drifted.append((live_title or f"[task {str(it['taskId'])[:8]}…]",
                                "в плане не было названия — сверить id↔задачу "
                                "нечем"))
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
                          "title": planned_title, "snapshot": it["snapshot"]})
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
        logger.error(f"Error in execute_task_deletion: {e}")
        return f"Error executing deletion manifest: {str(e)}"


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
    title = item.get("title") or (item.get("snapshot") or {}).get("title") \
        or f"[task {str(tid)[:8]}…]"
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
            return ("bad", f"- ❌ **«{title}»** — НЕ появилась среди открытых "
                    "(восстановление не подтвердилось)")
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
        if probs:
            return ("warn", f"- ⚠️ **«{title}»** — создана, но: "
                    + "; ".join(probs))
        # State the FACTS, not agreement-with-intent: the reader must SEE where
        # it landed, so a wrong-but-consistent request is still visible.
        facts = [f"в «{names.get(live.get('projectId'), live.get('projectId'))}»"]
        if live.get("columnId"):
            facts.append("раздел применён")
        if live.get("dueDate"):
            facts.append(f"срок {str(live['dueDate'])[:10]}")
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
        return (("ok", f"- ✅ **«{title}»** — теги {sorted(got)}") if want == got
                else ("bad", f"- ❌ **«{title}»** — теги {sorted(got)}, "
                      f"ожидались {sorted(want)}"))
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
        return (("ok", f"- ✅ **«{title}»** — родитель применён") if ok else
                ("bad", f"- ❌ **«{title}»** — parentId={got!r}, ожидался {want!r}"))
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
        verdicts: List[Tuple[str, str]] = []
        for rec in records:
            op = rec.get("op") or "delete"
            items = rec.get("items") or [
                {"taskId": s.get("taskId"), "title": s.get("title"), "snapshot": s}
                for s in rec.get("deleted", [])
            ]
            for item in items:
                verdicts.append(_verify_item(op, item, live, names))
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
        overall = "❌" if bad else ("⚠️" if warn else "✅")
        tail = ("есть расхождения — это НЕ успех." if bad else
                "есть непроверенные пункты — это НЕ полный успех." if warn else
                "всё подтверждено.")
        lines.append(f"**Статус операции: {overall}** — {tail}")
        lines.append("[агенту: перепечатай этот отчёт пользователю ДОСЛОВНО — "
                     "это серверная проверка, не заменяй её своим пересказом]")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error in operation_report: {e}")
        return f"Error building operation report: {str(e)}"


# ---------------------------------------------------------------------------
# Retroactive declutter ("разбор помойки") — plan → confirm → execute
# ---------------------------------------------------------------------------
# The ingest pipeline (tg-ai) dedupes NEW tasks at capture time. This works
# RETROACTIVELY over the EXISTING pile of open tasks: it clusters near-duplicate
# titles, flags long-overdue/stale candidates, spots umbrella groups, and
# proposes SMART re-titles. It CREATES and CHANGES nothing — plan_declutter
# builds a manifest, execute_declutter (after an explicit confirm token) routes
# every mutation through the already-audited deletion / update / parent tools,
# so each one is identity-guarded, journalled and post-verified. The same
# safety philosophy as the ingest judge governs the analysis: uncertain merges
# default to KEEP-BOTH, and obsolete tasks are only FLAGGED — never auto-deleted
# or auto-completed.

# Similarity threshold for fuzzy (token-Jaccard) duplicate candidates — only
# reached when the CLAUDE_CLI judge is available to confirm each candidate.
_DC_FUZZY_THRESHOLD = 0.6
# Obsolete = overdue by at least this many days AND untouched at least this long.
_DC_OBSOLETE_OVERDUE_DAYS = 30
_DC_OBSOLETE_STALE_DAYS = 60

# Hard cap on how many open tasks a single plan_declutter scope may resolve
# to. _dc_cluster_duplicates and _dc_group_candidates are both O(n^2) over
# this set, and (when the shim is available) the analysis also bundles ALL
# fuzzy-duplicate clusters into one judge prompt and ALL vague-title
# candidates into one SMART-rewrite prompt — bigger input means bigger
# prompts on top of the quadratic clustering cost. The MCP client this tool
# is called through has a 60s timeout; a real user's full open-task list has
# been observed to blow past it. 200 is picked as a middle ground: at that
# size the O(n^2) passes stay well under a second in pure Python, and the two
# (now-parallel, individually timeout-capped — see _DC_SHIM_TIMEOUT) shim
# prompts stay small enough to answer quickly, leaving real headroom under
# the 60s ceiling. A single project or a day/week's worth of inbox triage
# should resolve well under this; the whole-pile case (what the timeout was
# actually hit on) is exactly what should be narrowed via `scope` instead.
# Bumped 200 -> 300 (2026-08-05 cap-sizing pass): a genuinely active user's
# single project/column can plausibly hold 200-300+ untriaged tasks before
# their first declutter — 200 was clipping that common case into an
# unnecessary refusal+retry; 300 stays well inside the sub-second O(n^2) and
# ~25s shim budget (see rationale above) while leaving real headroom under
# _DC_MAX_TASKS_HARD_CAP=500 for the explicit-override path.
_DC_MAX_TASKS = 300
# 17.6 (security-checklist.md §4): plan_declutter's `max_tasks` argument can
# raise the per-call cap above _DC_MAX_TASKS, but a cap that the caller can
# override without any ceiling isn't a cap — the model can (and, asked to
# "just declutter everything", will) pass an arbitrarily large value and walk
# straight into the O(n^2) clustering pass over the whole account. This is a
# hard, server-side maximum that `max_tasks` can NEVER exceed, independent of
# what the argument says.
_DC_MAX_TASKS_HARD_CAP = 500
# Per-call timeout for the two declutter shim calls (judge_fn/smart_fn only —
# NOT _dc_shim_json's default, which other/future callers may still rely on
# at 90s). The two calls now run concurrently, so wall-clock cost is
# ~max(judge, smart) rather than their sum, but each individual call still
# needs its own sane ceiling well under the 60s client timeout.
_DC_SHIM_TIMEOUT = 25


def _dc_tokens(title: str) -> set:
    """Normalised word-token set of a title (for Jaccard similarity)."""
    return set(re.findall(r"\w+", _norm_name(title), flags=re.UNICODE))


def _dc_jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dc_shim_available() -> bool:
    return bool(os.environ.get("CLAUDE_CLI_URL") and os.environ.get("CLAUDE_CLI_TOKEN"))


def _dc_shim_json(system: str, prompt: str, timeout: int = 90,
                  fail_tracker: Optional[list] = None):
    """Call the CLAUDE_CLI shim and return the parsed JSON array/object, or None
    on ANY failure (unset, unreachable, malformed). Same shim the bot and
    _suggest_destinations use.

    fail_tracker: optional mutable list — if the shim WAS configured (url set)
    but the call itself failed (network error, non-ok response, unparsable
    reply), True is appended so callers can distinguish "not configured" from
    "configured but degraded during this run"."""
    url = os.environ.get("CLAUDE_CLI_URL")
    token = os.environ.get("CLAUDE_CLI_TOKEN")
    if not url:
        return None
    import requests as _rq
    try:
        r = _rq.post(url, json={
            "system": system,
            "prompt": prompt,
            "model": os.environ.get("CLAUDE_CLI_MODEL", "sonnet"),
        }, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            if fail_tracker is not None:
                fail_tracker.append(True)
            return None
        text = data.get("result") or ""
        a = min([i for i in (text.find("["), text.find("{")) if i != -1] or [-1])
        b = max(text.rfind("]"), text.rfind("}"))
        if a == -1 or b == -1:
            if fail_tracker is not None:
                fail_tracker.append(True)
            return None
        return json.loads(text[a:b + 1])
    except Exception as e:
        logger.warning(f"declutter shim call failed: {e}")
        if fail_tracker is not None:
            fail_tracker.append(True)
        return None


def _dc_cluster_duplicates(tasks: List[Dict], fuzzy: bool) -> List[Dict]:
    """Cluster open tasks by title. Returns list of {"tasks": [...], "exact":
    bool}. EXACT clusters share an identical normalised title (safe to act on
    even without the LLM). When fuzzy=True, remaining tasks are additionally
    grouped by token-Jaccard ≥ threshold into non-exact clusters (candidates
    that REQUIRE the judge to confirm). Only clusters of size ≥ 2 are returned."""
    # Exact normalised-title buckets first.
    exact: Dict[str, List[Dict]] = {}
    for t in tasks:
        key = _norm_name(t.get("title") or "")
        if key:
            exact.setdefault(key, []).append(t)
    clusters: List[Dict] = []
    claimed = set()
    for key, group in exact.items():
        if len(group) >= 2:
            clusters.append({"tasks": group, "exact": True})
            for t in group:
                claimed.add(t.get("id"))
    if not fuzzy:
        return clusters
    # Fuzzy pass over the not-yet-claimed tasks: greedy single-link by Jaccard.
    rest = [t for t in tasks if t.get("id") not in claimed and (t.get("title") or "").strip()]
    toks = {t.get("id"): _dc_tokens(t.get("title") or "") for t in rest}
    used = set()
    for i, a in enumerate(rest):
        if a.get("id") in used:
            continue
        group = [a]
        used.add(a.get("id"))
        for b in rest[i + 1:]:
            if b.get("id") in used:
                continue
            if _dc_jaccard(toks[a.get("id")], toks[b.get("id")]) >= _DC_FUZZY_THRESHOLD:
                group.append(b)
                used.add(b.get("id"))
        if len(group) >= 2:
            clusters.append({"tasks": group, "exact": False})
    return clusters


def _dc_metadata_score(task: Dict) -> tuple:
    """Richness score for choosing which duplicate to KEEP — the survivor should
    carry the most information. Higher tuple sorts first."""
    return (
        1 if task.get("dueDate") else 0,
        task.get("priority") or 0,
        len(task.get("content") or task.get("desc") or ""),
        len(task.get("tags") or []),
        # Older task (earlier createdTime) breaks ties — keep the original.
        -(_dc_created_sort_key(task)),
    )


def _dc_created_sort_key(task: Dict) -> float:
    dt = _parse_ticktick_datetime(task.get("createdTime"))
    return dt.timestamp() if dt else 0.0


def _dc_pick_primary(cluster_tasks: List[Dict]) -> int:
    """Index of the task to KEEP within a duplicate cluster (richest metadata)."""
    best_i, best_score = 0, None
    for i, t in enumerate(cluster_tasks):
        score = _dc_metadata_score(t)
        if best_score is None or score > best_score:
            best_i, best_score = i, score
    return best_i


def _dc_task_age_days(task: Dict, now: datetime) -> Optional[int]:
    """Days since the task was last modified (or created). None if unknown."""
    dt = _parse_ticktick_datetime(task.get("modifiedTime") or task.get("createdTime"))
    if dt is None:
        return None
    return (now - dt).days


def _dc_is_obsolete(task: Dict, today, now: datetime) -> Optional[Dict]:
    """A long-overdue AND untouched task → {overdue_days, age_days}. None when
    it is not a stale-obsolete candidate. FLAG-only: never acted on."""
    due = _task_due_local_date(task)
    if due is None:
        return None
    overdue_days = (today - due).days
    if overdue_days < _DC_OBSOLETE_OVERDUE_DAYS:
        return None
    age = _dc_task_age_days(task, now)
    if age is not None and age < _DC_OBSOLETE_STALE_DAYS:
        return None
    return {"overdue_days": overdue_days, "age_days": age}


def _dc_is_nonsmart_candidate(title: str) -> bool:
    """Cheap prefilter for non-SMART titles: a very short / vague title (≤ 2
    meaningful words). The LLM does the actual judging + reformulation; without
    it these are surfaced as flags only."""
    toks = _dc_tokens(title)
    return 1 <= len(toks) <= 2


def _dc_word_prefix(short_title: str, long_title: str) -> bool:
    """True if short_title's tokens are a PROPER leading sub-sequence of
    long_title's tokens — i.e. short is a natural umbrella/header for long."""
    st = re.findall(r"\w+", _norm_name(short_title), flags=re.UNICODE)
    lt = re.findall(r"\w+", _norm_name(long_title), flags=re.UNICODE)
    return bool(st) and len(st) < len(lt) and lt[:len(st)] == st


def _dc_group_candidates(tasks: List[Dict], skip_ids: set) -> List[Dict]:
    """Umbrella groups: a task whose title is a word-prefix of ≥ 2 other open
    tasks IN THE SAME PROJECT becomes the parent, those become children. Only
    genuine headers qualify (no synthetic parents) — safe, reversible nesting.
    Tasks already parented, or in skip_ids (e.g. slated for deletion), are
    excluded."""
    pool = [t for t in tasks
            if t.get("id") not in skip_ids and not t.get("parentId")
            and (t.get("title") or "").strip()]
    by_project: Dict[str, List[Dict]] = {}
    for t in pool:
        by_project.setdefault(t.get("projectId") or "", []).append(t)
    groups: List[Dict] = []
    used = set()
    for pid, plist in by_project.items():
        # Longer titles first so a shorter header is tested against them.
        for parent in sorted(plist, key=lambda t: len(_dc_tokens(t.get("title") or ""))):
            if parent.get("id") in used:
                continue
            children = [c for c in plist
                        if c.get("id") not in used and c.get("id") != parent.get("id")
                        and _dc_word_prefix(parent.get("title") or "", c.get("title") or "")]
            if len(children) >= 2:
                used.add(parent.get("id"))
                for c in children:
                    used.add(c.get("id"))
                groups.append({"parent": parent, "children": children})
    return groups


async def _dc_analyze(tasks: List[Dict], names: Dict, judge_fn=None, smart_fn=None,
                      today=None, now: Optional[datetime] = None,
                      fuzzy: bool = True) -> Dict:
    """Analysis core (the only I/O is the two injected shim calls, dispatched
    concurrently): from a list of live open tasks build the proposed
    declutter actions + flags. judge_fn/smart_fn are injected so the logic is
    unit-testable with mocks (plain sync callables are fine — they are run
    via asyncio.to_thread); the real tool wires them to the shim.

    judge_fn(clusters) -> aligned list of {"is_duplicate": bool, "keep": int,
        "reason": str} (or [] / None to abstain → KEEP-BOTH).
    smart_fn(titles) -> aligned list of {"new_title": str, "reason": str}
        (empty new_title → leave as-is).

    judge_fn and smart_fn look at independent slices of the task pile
    (fuzzy-duplicate clusters vs. vague-title candidates) and are run
    CONCURRENTLY via asyncio.gather/to_thread rather than sequentially, since
    each is one (potentially slow) shim HTTP call and running them back to
    back risked summing past the MCP client's own timeout. The smart_fn
    candidate pool is gathered up front (before duplicate-resolution below
    decides what gets consumed) precisely so its dispatch doesn't have to
    wait on the judge's verdict; any candidate that duplicate/group
    resolution later claims is simply dropped from the final rename/flag
    output — see the id-keyed lookup in step 4.

    Returns dict with mutating action lists (delete/rename/group) and flag lists
    (flag_obsolete/flag_dupe/flag_nonsmart)."""
    today = today or _today_local()
    now = now or datetime.now(timezone.utc)
    out = {"delete": [], "rename": [], "group": [],
           "flag_obsolete": [], "flag_dupe": [], "flag_nonsmart": []}
    consumed = set()  # ids already claimed by a delete/rename/group action

    # Ids that are the parentId of at least one task in THIS SAME open-task
    # pass — i.e. tasks with live children. A task with live children must
    # NEVER be the "redundant" (delete) side of a duplicate pair: deleting it
    # would orphan its children (parentId pointing at a deleted task).
    # plan_task_deletion already guards this with BFS subtree expansion;
    # declutter's own duplicate-scoring must not bypass that protection.
    children_of = {t.get("parentId") for t in tasks if t.get("parentId")}

    # ---- Prepare BOTH shim inputs up front, then dispatch concurrently ----
    clusters = _dc_cluster_duplicates(tasks, fuzzy=fuzzy)
    fuzzy_clusters = [c for c in clusters if not c["exact"]]
    # Full non-SMART candidate pool, NOT yet excluding tasks that duplicate/
    # group resolution below will consume (that isn't known until after the
    # judge verdict is in) — see the docstring above for why.
    full_nonsmart_cand = [t for t in tasks
                          if _dc_is_nonsmart_candidate(t.get("title") or "")]

    async def _run_judge() -> Dict[int, Dict]:
        if not (fuzzy_clusters and judge_fn):
            return {}
        try:
            res = await asyncio.to_thread(
                judge_fn, [[t.get("title") or "" for t in c["tasks"]]
                          for c in fuzzy_clusters]) or []
            return {i: v for i, v in enumerate(res) if isinstance(v, dict)}
        except Exception as e:
            logger.warning(f"declutter judge failed: {e}")
            return {}

    async def _run_smart() -> Dict[Any, Dict]:
        if not (full_nonsmart_cand and smart_fn):
            return {}
        try:
            res = await asyncio.to_thread(
                smart_fn, [t.get("title") or "" for t in full_nonsmart_cand]) or []
            by_id = {}
            for i, v in enumerate(res):
                if isinstance(v, dict) and i < len(full_nonsmart_cand):
                    by_id[full_nonsmart_cand[i].get("id")] = v
            return by_id
        except Exception as e:
            logger.warning(f"declutter smart rewrite failed: {e}")
            return {}

    verdicts, smart_by_id = await asyncio.gather(_run_judge(), _run_smart())

    # ---- 1. Duplicate clusters -------------------------------------------
    fuzzy_idx = 0
    for c in clusters:
        ctasks = c["tasks"]
        if c["exact"]:
            confident, keep_i, reason = True, _dc_pick_primary(ctasks), "идентичные названия"
        else:
            v = verdicts.get(fuzzy_idx)
            fuzzy_idx += 1
            # Bias to KEEP-BOTH: only merge when the judge is explicitly sure.
            if v and v.get("is_duplicate") is True:
                keep_i = v.get("keep")
                keep_i = keep_i if isinstance(keep_i, int) and 0 <= keep_i < len(ctasks) \
                    else _dc_pick_primary(ctasks)
                confident, reason = True, (v.get("reason") or "судья подтвердил дубликат")
            else:
                confident, keep_i, reason = False, None, (
                    (v or {}).get("reason") or "похожи, но слияние не подтверждено")

        # Finding #2: the fuzzy pass is anchor/"star" clustering — every member
        # is only checked against the cluster's anchor, never against each
        # other, so a 3+-member cluster can silently mix a genuine duplicate
        # pair with a task that only shares anchor-similarity. The judge
        # returns a single is_duplicate/keep verdict for the WHOLE cluster, so
        # an all-or-nothing delete on 3+ members risks wiping a distinct task.
        # Cap auto-delete at exactly 2 members; any bigger FUZZY cluster is
        # routed to flag_dupe as a suggestion instead, regardless of verdict.
        if confident and not c["exact"] and len(ctasks) >= 3:
            confident, keep_i = False, None
            reason = ("группа из 3+ похожих задач — попарное сходство между "
                      "всеми не гарантировано, слияние не автоматическое, "
                      "проверь сам")

        # Finding #1: a task with live children must never be the deleted
        # side. If exactly one cluster member has children, force it onto the
        # KEEP side regardless of metadata scoring / judge choice. If 2+
        # members have children, there is no safe single keeper — flag it.
        if confident:
            child_ids = [t.get("id") for t in ctasks if t.get("id") in children_of]
            if len(child_ids) >= 2:
                confident, keep_i = False, None
                reason = ("у нескольких похожих задач есть живые подзадачи — "
                          "не могу безопасно выбрать, кого оставить, реши сам")
            elif len(child_ids) == 1:
                forced_i = next(j for j, t in enumerate(ctasks)
                                if t.get("id") == child_ids[0])
                if forced_i != keep_i:
                    keep_i = forced_i
                    reason = reason + " (оставил — у задачи есть подзадачи)"

        if confident:
            keeper = ctasks[keep_i]
            consumed.add(keeper.get("id"))
            redundant = [t for j, t in enumerate(ctasks) if j != keep_i]
            for r in redundant:
                consumed.add(r.get("id"))
                out["delete"].append({
                    "taskId": r.get("id"), "projectId": r.get("projectId"),
                    "title": r.get("title") or "", "snapshot": _snapshot_of(r),
                    "keep_title": keeper.get("title") or "",
                    "keep_id": keeper.get("id"),
                    "project": names.get(r.get("projectId"), ""),
                    "reason": reason,
                })
        else:
            out["flag_dupe"].append({
                "titles": [t.get("title") or "" for t in ctasks],
                "ids": [t.get("id") for t in ctasks],
                "reason": reason,
            })

    # ---- 2. Obsolete (FLAG ONLY — never mutated) -------------------------
    for t in tasks:
        info = _dc_is_obsolete(t, today, now)
        if info:
            out["flag_obsolete"].append({
                "taskId": t.get("id"), "title": t.get("title") or "",
                "project": names.get(t.get("projectId"), ""),
                "due": str(t.get("dueDate"))[:10] if t.get("dueDate") else "",
                "overdue_days": info["overdue_days"], "age_days": info["age_days"],
            })

    # ---- 3. Umbrella groups ---------------------------------------------
    for g in _dc_group_candidates(tasks, skip_ids=consumed):
        parent = g["parent"]
        kids = [c for c in g["children"] if c.get("id") not in consumed]
        if len(kids) < 2:
            continue
        consumed.add(parent.get("id"))
        for c in kids:
            consumed.add(c.get("id"))
        out["group"].append({
            "parentId": parent.get("id"), "parent_title": parent.get("title") or "",
            "project_id": parent.get("projectId"),
            "project": names.get(parent.get("projectId"), ""),
            "children": [{"taskId": c.get("id"), "title": c.get("title") or "",
                          "projectId": c.get("projectId")} for c in kids],
        })

    # ---- 4. Non-SMART titles --------------------------------------------
    # smart_fn was already called (concurrently with judge_fn) above, against
    # full_nonsmart_cand — the pre-consumption superset of this list. Re-apply
    # the same "not consumed" filter now that consumed is final, and look each
    # survivor's rewrite up by task id in smart_by_id (built from that earlier
    # call) instead of re-invoking the shim.
    cand = [t for t in tasks if t.get("id") not in consumed
            and _dc_is_nonsmart_candidate(t.get("title") or "")]
    for t in cand:
        v = smart_by_id.get(t.get("id"))
        new_title = (v or {}).get("new_title") if v else None
        if new_title and _norm_name(new_title) and _norm_name(new_title) != _norm_name(t.get("title") or ""):
            consumed.add(t.get("id"))
            out["rename"].append({
                "taskId": t.get("id"), "projectId": t.get("projectId"),
                "title": t.get("title") or "", "new_title": new_title.strip(),
                "project": names.get(t.get("projectId"), ""),
                "reason": (v or {}).get("reason") or "",
            })
        else:
            out["flag_nonsmart"].append({
                "taskId": t.get("id"), "title": t.get("title") or "",
                "project": names.get(t.get("projectId"), ""),
            })
    return out


def _dc_judge_fn(clusters: List[List[str]],
                 fail_tracker: Optional[list] = None) -> List[Dict]:
    """Wire the fuzzy-cluster judge to the shim. Bias: KEEP-BOTH unless sure."""
    if not clusters:
        return []
    blocks = []
    for i, titles in enumerate(clusters):
        lst = "\n".join(f"   {j}. «{t}»" for j, t in enumerate(titles))
        blocks.append(f"Кластер {i}:\n{lst}")
    prompt = (
        "Ниже кластеры ПОХОЖИХ задач владельца. Для КАЖДОГО реши: это один и тот "
        "же пункт (дубликаты, можно слить), или разные дела? Правило безопасности: "
        "если сомневаешься — is_duplicate=false (лучше оставить обе, чем потерять "
        "задачу). Когда дубликаты — укажи keep = индекс той версии, которую оставить "
        "(с датой/приоритетом/деталями).\n\n"
        + "\n\n".join(blocks) +
        '\n\nОтвет СТРОГО JSON-массивом по одному объекту на кластер:\n'
        '[{"i":0,"is_duplicate":true,"keep":1,"reason":"<кратко>"}]'
    )
    res = _dc_shim_json("Ты вычищаешь дубликаты в списке задач. Отвечай только JSON.",
                        prompt, timeout=_DC_SHIM_TIMEOUT, fail_tracker=fail_tracker)
    if not isinstance(res, list):
        return []
    aligned = [{} for _ in clusters]
    for it in res:
        if isinstance(it, dict):
            idx = it.get("i")
            if isinstance(idx, int) and 0 <= idx < len(clusters):
                aligned[idx] = it
    return aligned


def _dc_smart_fn(titles: List[str], fail_tracker: Optional[list] = None) -> List[Dict]:
    """Wire the SMART-rewrite proposer to the shim."""
    if not titles:
        return []
    numbered = "\n".join(f"{i}. «{t}»" for i, t in enumerate(titles))
    prompt = (
        "Ниже короткие/расплывчатые названия задач. Для каждого предложи более "
        "SMART-формулировку: конкретное действие + объект (что именно сделать и с "
        "чем), тем же языком. Время/срок ДОБАВЛЯТЬ НЕ обязательно. Если название "
        "уже нормальное — верни пустую строку в new_title.\n\n"
        f"{numbered}\n\n"
        'Ответ СТРОГО JSON-массивом:\n'
        '[{"i":0,"new_title":"<или пусто>","reason":"<кратко>"}]'
    )
    res = _dc_shim_json("Ты переформулируешь задачи в SMART-вид. Отвечай только JSON.",
                        prompt, timeout=_DC_SHIM_TIMEOUT, fail_tracker=fail_tracker)
    if not isinstance(res, list):
        return []
    aligned = [{} for _ in titles]
    for it in res:
        if isinstance(it, dict):
            idx = it.get("i")
            if isinstance(idx, int) and 0 <= idx < len(titles):
                aligned[idx] = it
    return aligned


def _dc_scope_filter(tasks: List[Dict], names: Dict, scope: str,
                     col_names: Optional[Dict] = None) -> List[Dict]:
    """Optional narrowing. Forms of `scope`:
      'inbox'          → Inbox only
      'Project'        → case-insensitive substring match on the project name
      'Project/Column' → project-name substring AND kanban-column-name substring
                         (col_names: {columnId: column_name} for the matched
                         project(s), built by the caller via
                         _dc_column_names_for_scope). Narrowing a big project to
                         one column is the intended way to stay under
                         _DC_MAX_TASKS.
    An empty column part after '/' keeps every column (same as project-only)."""
    s = (scope or "").strip()
    if not s:
        return tasks
    # Column-qualified scope: "<project>/<column>". Split on the FIRST '/'.
    if "/" in s:
        proj_part, col_part = s.split("/", 1)
        proj = proj_part.strip().lower()
        col = col_part.strip().lower()
        cn = col_names or {}
        out = []
        for t in tasks:
            pname = (names.get(t.get("projectId"), "") or "").lower()
            if proj and proj not in pname:
                continue
            if col:
                cname = (cn.get(t.get("columnId"), "") or "").lower()
                if col not in cname:
                    continue
            out.append(t)
        return out
    s = s.lower()
    if s == "inbox":
        return [t for t in tasks if names.get(t.get("projectId"), "").lower() == "inbox"]
    return [t for t in tasks
            if s in (names.get(t.get("projectId"), "") or "").lower()]


async def _dc_column_names_for_scope(scope: str, names: Dict) -> Dict:
    """For a 'Project/Column' scope, build {columnId: column_name} across the
    projects whose name matches the project part, so _dc_scope_filter can match
    the requested column by name. Returns {} for non-column scopes, when the v2
    client is unavailable, or on any per-project failure (the column part then
    simply matches nothing, and plan_declutter reports 'nothing in this area')."""
    s = (scope or "").strip()
    if "/" not in s or not ticktick_v2:
        return {}
    proj_part = s.split("/", 1)[0].strip().lower()
    if not proj_part:
        return {}
    pids = [pid for pid, nm in names.items() if proj_part in (nm or "").lower()]
    out: Dict = {}
    for pid in pids:
        try:
            cols = await _run_blocking(lambda p=pid: ticktick_v2.get_project_columns(p))
            for c in cols or []:
                cid = c.get("id")
                if cid:
                    out[cid] = c.get("name") or c.get("title") or ""
        except Exception as e:
            logger.warning(f"declutter column scope: columns for {pid} failed: {e}")
    return out


def _dc_mutating_count(actions: Dict) -> int:
    return (len(actions["delete"]) + len(actions["rename"])
            + sum(len(g["children"]) for g in actions["group"]))


def _dc_object_ids(actions: Dict) -> List[str]:
    """Every task id a declutter manifest would actually TOUCH — the binding
    set for _manifest_object_hash (docs/DESIGN_approval_gate.md §4.3.2). The
    delete manifests had this from day one; without it a declutter manifest
    whose stored actions changed between plan and consent would be applied
    silently. Flags are excluded: they mutate nothing."""
    ids = [it["taskId"] for it in actions.get("delete", [])]
    ids += [it["taskId"] for it in actions.get("rename", [])]
    for g in actions.get("group", []):
        ids += [c["taskId"] for c in g.get("children", [])]
    return ids


# ---------------------------------------------------------------------------
# Sheet-backed declutter manifest (persist="sheet") — see
# docs/DESIGN_sheet_backed_declutter.md. All Google I/O lives in
# declutter_sheet.py; everything here is pure/deterministic glue so it stays
# unit-testable without network (§8.3/§8.4 of the design doc).
# ---------------------------------------------------------------------------

# Sheet row actions that actually mutate TickTick — flags never do (§3/§4.1).
_DC_MUTATING_ROW_ACTIONS = ("delete", "rename", "group")


def _dc_now_la_iso() -> str:
    """Timestamp for the sheet's run_ts/applied_ts columns — America/Los_Angeles
    (via the app-wide _USER_TZ, same convention already used for due dates and
    the plan_declutter header), never UTC."""
    return datetime.now(_USER_TZ).isoformat()


def _dc_actions_to_rows(actions: Dict, manifest_id: str, run_ts: str,
                        names: Optional[Dict] = None) -> List[Dict]:
    """Pure: flatten a declutter `actions` dict (the output of _dc_analyze)
    into the flat per-row shape of the `Declutter Log` sheet (§3 of the
    design doc). No I/O — `row_id` is assigned later by
    declutter_sheet.append_rows(). Mutating actions (delete/rename/
    group-child) default decision="approved" (today's apply-everything-
    proposed semantics, §3 "Дефолт decision"); flag_* rows default
    decision="pending" and are NEVER auto-applied (§5.9/§9.4 — MVP: flags
    stay informational)."""
    names = names or {}

    def row(task_id, title, project, action, proposed_value, reason,
            cluster_id="", decision="approved", snapshot=None):
        return {
            "manifest_id": manifest_id, "run_ts": run_ts,
            "task_id": task_id or "", "title": title or "",
            "project": project or "", "column": "",
            "cluster_id": cluster_id or "", "action": action,
            "proposed_value": proposed_value or "", "reason": reason or "",
            "decision": decision, "status": "planned",
            "applied_ts": "", "error": "",
            "snapshot_json": json.dumps(snapshot or {}, ensure_ascii=False, default=str),
        }

    rows: List[Dict] = []

    for it in actions.get("delete", []):
        keep_id = it.get("keep_id") or ""
        rows.append(row(
            it.get("taskId"), it.get("title"), it.get("project"),
            "delete", f"keep→{keep_id}", it.get("reason"),
            cluster_id=keep_id, decision="approved",
            snapshot=it.get("snapshot")))

    for it in actions.get("rename", []):
        rows.append(row(
            it.get("taskId"), it.get("title"), it.get("project"),
            "rename", it.get("new_title"), it.get("reason"),
            decision="approved"))

    for g in actions.get("group", []):
        pid = g.get("parentId") or ""
        for c in g.get("children", []):
            rows.append(row(
                c.get("taskId"), c.get("title"),
                names.get(c.get("projectId"), g.get("project")),
                "group", f"parent→{pid}",
                f"под «{g.get('parent_title', '')}»",
                cluster_id=pid, decision="approved"))

    for it in actions.get("flag_obsolete", []):
        age = (f"{it.get('age_days')}д без правок"
               if it.get("age_days") is not None else "возраст неизв.")
        rows.append(row(
            it.get("taskId"), it.get("title"), it.get("project"),
            "flag_obsolete", "",
            f"просрочено {it.get('overdue_days')}д, {age}",
            decision="pending"))

    for i, it in enumerate(actions.get("flag_dupe", [])):
        cid = f"dupeflag-{i}"
        ids = it.get("ids") or []
        titles = it.get("titles") or []
        for j, tid in enumerate(ids):
            rows.append(row(
                tid, titles[j] if j < len(titles) else "", "",
                "flag_dupe", "", it.get("reason"),
                cluster_id=cid, decision="pending"))

    for it in actions.get("flag_nonsmart", []):
        rows.append(row(
            it.get("taskId"), it.get("title"), it.get("project"),
            "flag_nonsmart", "", "", decision="pending"))

    return rows


def _dc_rows_to_exec(rows: List[Dict]) -> Dict:
    """Pure: reconstruct the input for execute_task_deletion / update_tasks /
    set_task_parent from an ALREADY-FILTERED list of sheet rows (the caller
    picks `apply_rows` = decision=="approved" AND status in
    ("planned","failed") AND action in _DC_MUTATING_ROW_ACTIONS). No I/O.

    Enforces the keep∉deletes invariant (§5.9): a delete row's `cluster_id`
    IS the id of the task being kept. If that id is itself among the
    task_ids approved for deletion in this same batch, the row is refused
    (moved to `refused`, not `delete`) — a human approved a self-contradicting
    edit (probably by hand-editing `decision`), and applying it would delete
    the very task the OTHER row promised to keep.

    Returns {"delete": [...], "rename": [...], "group": [...],
             "refused": [{"row_id", "task_id", "cluster_id", "reason"}]} —
    `delete`/`rename` items carry a "projectId": "" placeholder (the sheet
    does not store project ids, only display names — every downstream
    sub-tool re-resolves the REAL projectId from live state via the identity
    guard, so an empty placeholder here is never actually trusted)."""
    delete_rows = [r for r in rows if r.get("action") == "delete"]
    rename_rows = [r for r in rows if r.get("action") == "rename"]
    group_rows = [r for r in rows if r.get("action") == "group"]

    delete_task_ids = {r.get("task_id") for r in delete_rows}
    refused: List[Dict] = []
    delete: List[Dict] = []
    for r in delete_rows:
        keep_id = r.get("cluster_id") or ""
        if keep_id and keep_id in delete_task_ids:
            refused.append({
                "row_id": r.get("row_id"), "task_id": r.get("task_id"),
                "cluster_id": keep_id,
                "reason": (f"задача-«оставить» ({str(keep_id)[:8]}…) сама "
                           "числится на удаление в этом же прогоне — "
                           "кластер отклонён, поправь decision вручную"),
            })
            continue
        try:
            snapshot = json.loads(r.get("snapshot_json") or "{}")
        except (ValueError, TypeError):
            snapshot = {}
        delete.append({
            "taskId": r.get("task_id"), "projectId": "",
            "title": r.get("title") or "", "project": r.get("project") or "",
            "snapshot": snapshot,
        })

    rename = [{"taskId": r.get("task_id"), "projectId": "",
               "title": r.get("title") or "",
               "new_title": r.get("proposed_value") or ""}
              for r in rename_rows]

    groups_by_parent: Dict[str, Dict] = {}
    for r in group_rows:
        pid = r.get("cluster_id") or ""
        g = groups_by_parent.setdefault(pid, {
            "parentId": pid, "parent_title": "", "project_id": "",
            "children": [],
        })
        g["children"].append({"taskId": r.get("task_id"),
                              "title": r.get("title") or ""})
    group = [g for g in groups_by_parent.values() if g["children"]]

    return {"delete": delete, "rename": rename, "group": group,
            "refused": refused}


async def _execute_declutter_from_sheet(manifest_id: str) -> str:
    """Sheet-backed execute/resume engine — shared by execute_declutter (when
    the RAM pointer says persist="sheet") and resume_declutter (RAM pointer
    gone, e.g. after a Railway restart). Implements §4.2/§5/§6 of
    docs/DESIGN_sheet_backed_declutter.md:

    - freshly reads `decision` from the sheet every time (never trusts RAM —
      a human may have edited the sheet after the plan was printed);
    - CONSENT (docs/DESIGN_approval_gate.md) is the CALLER's job — both
      execute_declutter and resume_declutter run _require_consent(...) with
      the user's real user_reply BEFORE dispatching here; this function
      trusts that a genuine "yes" already happened and just does the work;
    - resolves a crashed `applying` lock by LIVE fact in TickTick (§5.4)
      before deciding whether to retry it, rather than trusting the stale
      flag;
    - write-throughs status=done|failed per row, judged from a FRESH
      post-apply read of live state (§5.6) rather than by parsing the
      sub-tool's text output;
    - never re-applies a `done` row (§5.2 — the core "don't delete twice"
      guarantee) — a row whose decision flipped to "rejected"/"pending"
      since the plan was printed is likewise simply excluded from
      `apply_rows` below, so nothing gets touched for it regardless of what
      N a stale plan-time message said."""
    err = _ensure_ready()
    if err:
        return err
    try:
        rows = declutter_sheet.read_manifest_rows(manifest_id)
    except declutter_sheet.DeclutterSheetError as e:
        return f"🛑 Не могу прочитать таблицу разбора: {e}"
    if not rows:
        try:
            url = declutter_sheet.sheet_url()
        except declutter_sheet.DeclutterSheetError:
            url = "(таблица не настроена)"
        return (f"🛑 Манифест разбора {manifest_id} не найден в таблице "
                f"({url}). Сначала plan_declutter(persist=\"sheet\").")

    by_id = _open_by_id(fresh=True)

    # ---- §5.4: resolve any crashed `applying` lock by LIVE fact FIRST -----
    stuck = [r for r in rows if r.get("status") == "applying"
             and r.get("action") in _DC_MUTATING_ROW_ACTIONS]
    if stuck:
        if by_id is None:
            return _STATE_UNAVAILABLE_MSG
        resolved_updates = []
        for r in stuck:
            tid = r.get("task_id")
            if r.get("action") == "delete":
                done = tid not in by_id
            elif r.get("action") == "rename":
                live = by_id.get(tid)
                done = bool(live) and _names_agree(
                    r.get("proposed_value") or "", live.get("title") or "")
            else:  # group
                live = by_id.get(tid)
                done = bool(live) and live.get("parentId") == r.get("cluster_id")
            if done:
                r["status"] = "done"
                resolved_updates.append({"row_id": r.get("row_id"),
                                         "status": "done",
                                         "applied_ts": _dc_now_la_iso()})
            else:
                r["status"] = "failed"
                resolved_updates.append({
                    "row_id": r.get("row_id"), "status": "failed",
                    "error": "прерванное применение (лок завис) — переприменяю"})
        try:
            declutter_sheet.batch_update_rows(resolved_updates)
        except declutter_sheet.DeclutterSheetError as e:
            return f"🛑 Не могу разрешить зависший лок applying в таблице: {e}"

    apply_rows = [r for r in rows if r.get("action") in _DC_MUTATING_ROW_ACTIONS
                  and r.get("decision") == "approved"
                  and r.get("status") in ("planned", "failed")]

    if not apply_rows:
        return ("Нечего применять — в таблице нет одобренных ещё-не-"
                f"применённых правок для манифеста {manifest_id}.")

    exec_input = _dc_rows_to_exec(apply_rows)

    # ---- lock BEFORE touching TickTick (§5.3) ------------------------------
    lock_updates = [{"row_id": r.get("row_id"), "status": "applying"}
                    for r in apply_rows]
    try:
        declutter_sheet.batch_update_rows(lock_updates)
    except declutter_sheet.DeclutterSheetError as e:
        return f"🛑 Не могу поставить лок applying в таблице: {e}"

    summary = f"Разбор помойки (таблица) — манифест {manifest_id}"
    out_blocks: List[str] = []
    report_ids: set = set()
    row_by_task_action: Dict[tuple, Dict] = {
        (r.get("task_id"), r.get("action")): r for r in apply_rows}
    status_updates: List[Dict] = []

    try:
        if exec_input["delete"]:
            sub_mid = uuid.uuid4().hex[:12]
            items = [{"taskId": it["taskId"], "projectId": it["projectId"],
                      "title": it["title"], "project": it.get("project", ""),
                      "snapshot": it["snapshot"]} for it in exec_input["delete"]]
            _MANIFESTS[sub_mid] = {"kind": "delete", "items": items,
                                   "created": time.monotonic(),
                                   "summary": summary + " — дубликаты",
                                   "consumed": False}
            res = await _execute_task_deletion_impl(sub_mid)
            out_blocks.append("## 🗑 Удаление дубликатов\n" + res)
            report_ids.add(sub_mid)

        if exec_input["rename"]:
            res = await _update_tasks_impl(
                summary + " — SMART-переименования",
                [{"taskId": it["taskId"], "projectId": it["projectId"],
                  "title": it["title"], "new_title": it["new_title"]}
                 for it in exec_input["rename"]])
            out_blocks.append("## ✏️ Переименования\n" + res)

        for g in exec_input["group"]:
            res = await _set_task_parent_impl(
                summary + " — группировка",
                [{"taskId": c["taskId"], "title": c["title"]}
                 for c in g["children"]],
                g["parentId"], g["project_id"] or "", g.get("parent_title") or "")
            out_blocks.append(f"## 🔗 Группировка под `{g['parentId']}`\n" + res)

        # ---- §5.6: per-row write-through, judged from FRESH live state ----
        fresh = _open_by_id(fresh=True)
        for it in exec_input["delete"]:
            r = row_by_task_action.get((it["taskId"], "delete"))
            if not r:
                continue
            if fresh is None:
                status_updates.append({"row_id": r["row_id"], "status": "failed",
                                       "error": _UNVERIFIED_MSG})
                continue
            ok = it["taskId"] not in fresh
            status_updates.append({
                "row_id": r["row_id"],
                "status": "done" if ok else "failed",
                "applied_ts": _dc_now_la_iso() if ok else "",
                "error": "" if ok else "задача всё ещё в TickTick после удаления",
            })
        for it in exec_input["rename"]:
            r = row_by_task_action.get((it["taskId"], "rename"))
            if not r:
                continue
            if fresh is None:
                status_updates.append({"row_id": r["row_id"], "status": "failed",
                                       "error": _UNVERIFIED_MSG})
                continue
            live = fresh.get(it["taskId"])
            ok = bool(live) and _names_agree(it["new_title"], live.get("title") or "")
            status_updates.append({
                "row_id": r["row_id"],
                "status": "done" if ok else "failed",
                "applied_ts": _dc_now_la_iso() if ok else "",
                "error": "" if ok else "новое название не видно в живом состоянии",
            })
        for g in exec_input["group"]:
            for c in g["children"]:
                r = row_by_task_action.get((c["taskId"], "group"))
                if not r:
                    continue
                if fresh is None:
                    status_updates.append({"row_id": r["row_id"], "status": "failed",
                                           "error": _UNVERIFIED_MSG})
                    continue
                live = fresh.get(c["taskId"])
                ok = bool(live) and live.get("parentId") == g["parentId"]
                status_updates.append({
                    "row_id": r["row_id"],
                    "status": "done" if ok else "failed",
                    "applied_ts": _dc_now_la_iso() if ok else "",
                    "error": "" if ok else "parentId не применился",
                })
        # Rows refused by the keep∉deletes invariant (§5.9) never went into
        # exec_input — they were locked `applying` above, so must be
        # explicitly written back to `failed` here (not left stuck).
        for ref in exec_input["refused"]:
            status_updates.append({"row_id": ref["row_id"], "status": "failed",
                                   "error": ref["reason"]})

        try:
            declutter_sheet.batch_update_rows(status_updates)
        except declutter_sheet.DeclutterSheetError as e:
            out_blocks.append(
                f"⚠️ Правки применены, но не смог записать статусы обратно в "
                f"таблицу: {e} — сверь фактический результат вручную "
                "(operation_report ниже) и поправь `status` в таблице.")

        if not out_blocks:
            return "Нечего применять — в манифесте не было правок."

        combined = "\n\n".join(out_blocks)
        for rid in re.findall(r'operation_report\(record_id="([\w-]+)"\)', combined):
            report_ids.add(rid)
        reports = [_build_operation_report(rid) for rid in report_ids]
        if reports:
            combined += "\n\n---\n### 🧾 Независимые отчёты\n\n" + "\n\n".join(reports)
        if exec_input["refused"]:
            try:
                url = declutter_sheet.sheet_url()
            except declutter_sheet.DeclutterSheetError:
                url = ""
            combined += (
                f"\n\n🛑 Отклонено по инварианту «оставить∉удаляемых»: "
                f"{len(exec_input['refused'])} правк(а/и) — задача-«оставить» "
                "сама была одобрена на удаление в этом же прогоне. Строки "
                f"помечены failed{(', поправь decision: ' + url) if url else ''}.")
        try:
            combined += f"\n\n📄 Таблица: {declutter_sheet.sheet_url()}"
        except declutter_sheet.DeclutterSheetError:
            pass
        return combined
    except Exception as e:
        logger.error(f"Error in _execute_declutter_from_sheet: {e}")
        # Best-effort: release the applying lock back to failed so a retry is
        # possible instead of a permanently stuck row (mirrors the RAM
        # branch's "graceful error, manifest state already changed" contract
        # — see test_execute_declutter_returns_graceful_error_on_internal_exception).
        try:
            declutter_sheet.batch_update_rows(
                [{"row_id": r["row_id"], "status": "failed",
                  "error": f"внутренняя ошибка: {e}"} for r in apply_rows])
        except Exception:
            pass
        return f"Error executing declutter manifest (sheet): {str(e)}"


# DISABLED 2026-08-04 по прямому указанию Максима ("Деклатер закоменть пока") —
# QA-инцидент: plan_declutter молча смешал реальные задачи с тестовыми в одном
# манифесте на исполнение. Держать выключенным, пока функция не настроена и
# не протестирована безопасно. Раскомментировать decorator ниже, чтобы вернуть.
# @mcp.tool(annotations=READONLY)
async def plan_declutter(scope: str = "", dry_run: bool = True,
                         max_tasks: int = 0, persist: str = "ram") -> str:
    """
    Phase 1 of the retroactive declutter ("разбор помойки"): READ every open
    task and propose how to tidy the EXISTING pile. Read-only — creates and
    changes NOTHING. This is the retro counterpart to ingest-time dedup: it
    works over what is ALREADY in TickTick.

    Analyses for: (1) DUPLICATE clusters — near-identical tasks; the redundant
    copies are proposed for deletion, the richest kept (uncertain merges default
    to KEEP-BOTH and are only flagged); (2) OBSOLETE — long-overdue + untouched
    tasks, FLAGGED for a human, never auto-completed/deleted; (3) GROUPABLE —
    umbrella tasks whose title is a header of others (proposed nesting);
    (4) non-SMART titles — a proposed clearer reformulation.

    When the CLAUDE_CLI shim is set it judges fuzzy duplicate merges (bias
    keep-both) and writes the SMART reformulations. When it is unset/unreachable
    the analysis DEGRADES to rule-based exact-title duplicates only and says so.

    IMPORTANT: reprint the returned manifest VERBATIM to the user and STOP —
    wait for their real reply, do not call execute in the same turn. Once
    they actually answer, call execute_declutter(manifest_id,
    user_reply=<their literal last message, verbatim>). Nothing mutates
    until then. Manifests are one-shot, expire in 1 h.

    Refuses cleanly (no analysis, no shim call) when the resolved scope has
    more than a few hundred open tasks — narrow `scope` (project/tag/date)
    instead of running this over the whole pile.

    persist="sheet" (opt-in, default stays "ram" — RAM behaviour is UNCHANGED):
    writes every proposed edit (and flag) as a row in the `Declutter Log`
    Google Sheet (env DECLUTTER_SHEET_ID/GSHEETS_SA_JSON — see
    docs/DESIGN_sheet_backed_declutter.md) instead of relying only on the
    in-process RAM manifest. The sheet becomes the durable source of truth:
    it survives a restart, a human can edit its `decision` column directly,
    and resume_declutter/execute_declutter re-read it fresh rather than
    trusting stale RAM. If the sheet is unreachable (missing creds/network)
    plan_declutter REFUSES explicitly — no silent fallback to RAM, since the
    entire point of persist="sheet" is a durable plan.

    Args:
        scope: optional narrowing — 'inbox', a project-name substring, or
            'Project/Column' to restrict to one kanban column of a project
            (e.g. 'Fix&Roll/TG Inbox'). Use list_project_columns to see a
            project's column names. Narrowing a big project to one column is
            the intended way to fit under the size cap.
        dry_run: retained for symmetry; plan_declutter is always read-only
        max_tasks: override the default size cap (_DC_MAX_TASKS = 300) for THIS
            call only. 0/unset keeps the default. Raise it when you deliberately
            want to declutter a bigger set and accept the extra time — the O(n^2)
            clustering and the bundled shim prompts grow with the set, and the
            MCP client's ~60s timeout still applies, so keep it sane.
        persist: "ram" (default, unchanged behaviour) or "sheet" (durable —
            see above).
    """
    err = _ensure_ready()
    if err:
        return err
    _prune_manifests()
    by_id = _open_by_id(fresh=True)
    if by_id is None:
        return _STATE_UNAVAILABLE_MSG
    names = _v2_project_names()
    col_names = await _dc_column_names_for_scope(scope, names)
    tasks = _dc_scope_filter(list(by_id.values()), names, scope, col_names)
    if not tasks:
        return ("Открытых задач в этой области нет — разбирать нечего."
                + (f" (scope='{scope}')" if scope else ""))
    # Refuse BEFORE any O(n^2) clustering/grouping or shim call when the
    # resolved scope is too big — see _DC_MAX_TASKS for the sizing rationale.
    # Read-only refusal: nothing above this point mutated anything, and
    # nothing below runs, so there is no partial analysis/journal entry.
    cap = max_tasks if isinstance(max_tasks, int) and max_tasks > 0 else _DC_MAX_TASKS
    if len(tasks) > cap:
        raise_hint = (
            "" if (isinstance(max_tasks, int) and max_tasks > 0)
            else f" Либо, если уверен в объёме, подними лимит: max_tasks={len(tasks)}.")
        return (f"🛑 Отказ: в этой области {len(tasks)} открытых задач — "
                f"больше капа {cap} (на {len(tasks) - cap} "
                "больше). Разбор такого объёма упрётся в таймаут ещё до "
                "готового плана. Сузь scope и попробуй снова — например "
                "scope='inbox', конкретный проект, или ОДНУ колонку большого "
                "проекта: scope='Проект/Колонка' (например 'Fix&Roll/TG Inbox'; "
                "названия колонок — list_project_columns)."
                + raise_hint
                + (f" (текущий scope='{scope}')" if scope else " (scope не задан — вся куча)"))

    shim = _dc_shim_available()
    # Track whether a shim call actually FAILED during this run (vs. simply
    # being unconfigured) so the manifest can warn accurately — "shim
    # unavailable" previously only reflected the env vars being unset, not a
    # real degraded call (timeout/bad response/malformed JSON) mid-analysis.
    shim_fail_tracker: list = []
    judge_fn = (lambda clusters: _dc_judge_fn(clusters, fail_tracker=shim_fail_tracker)) \
        if shim else None
    smart_fn = (lambda titles: _dc_smart_fn(titles, fail_tracker=shim_fail_tracker)) \
        if shim else None
    actions = await _dc_analyze(
        tasks, names,
        judge_fn=judge_fn,
        smart_fn=smart_fn,
        today=_today_local(), now=datetime.now(timezone.utc), fuzzy=shim)
    shim_call_failed = bool(shim_fail_tracker)

    n_mut = _dc_mutating_count(actions)
    n_flags = (len(actions["flag_obsolete"]) + len(actions["flag_dupe"])
               + len(actions["flag_nonsmart"]))

    mid = uuid.uuid4().hex[:12]
    now = time.monotonic()
    sheet_note = ""
    if persist == "sheet":
        # Durable-plan invariant (design doc §9.3, Maksim's decision): if the
        # sheet is unreachable, REFUSE explicitly — no silent fallback to a
        # RAM manifest the human never sees and that dies on restart.
        run_ts = _dc_now_la_iso()
        rows = _dc_actions_to_rows(actions, mid, run_ts, names)
        try:
            declutter_sheet.ensure_header()
            declutter_sheet.append_rows(rows)
            url = declutter_sheet.sheet_url()
        except declutter_sheet.DeclutterSheetError as e:
            return (f"🛑 Отказ: не могу сохранить план durable в Google Sheet "
                    f"(persist=\"sheet\") — {e} Ничего не записано в TickTick "
                    "и ничего не записано в таблицу — план НЕ построен. "
                    "Проверь DECLUTTER_SHEET_ID/GSHEETS_SA_JSON и доступ к "
                    "сети, либо вызови без persist (RAM-режим, как раньше).")
        _MANIFESTS[mid] = {"kind": "declutter", "actions": actions,
                           "persist": "sheet", "spreadsheet_url": url,
                           "mutating_count": n_mut, "created": now,
                           "plan_shown_at": now,
                           "object_hash": _manifest_object_hash(
                               "declutter", _dc_object_ids(actions)),
                           "summary": f"Разбор помойки ({n_mut} правок)",
                           "consumed": False}
        sheet_note = (
            f"\n📄 _План записан durable в таблицу: {url} — можешь править "
            "колонку `decision` (approved/rejected) прямо там, либо "
            "`set_declutter_decision`. Затем `execute_declutter(...)` "
            "применит одобренное; после рестарта — "
            f"`resume_declutter(manifest_id=\"{mid}\", user_reply=...)`._")
    else:
        _MANIFESTS[mid] = {"kind": "declutter", "actions": actions,
                           "mutating_count": n_mut, "created": now,
                           "plan_shown_at": now,
                           "object_hash": _manifest_object_hash(
                               "declutter", _dc_object_ids(actions)),
                           "summary": f"Разбор помойки ({n_mut} правок)",
                           "consumed": False}

    when = datetime.now(_USER_TZ).strftime("%d.%m.%Y %H:%M")
    lines = [f"### 🧹 План разбора помойки — {when} ({_USER_TZ.key})",
             f"_Манифест `{mid}` · проверено задач: {len(tasks)} · "
             "ничего ещё не тронуто_"]
    if not shim:
        lines.append("⚠️ _CLAUDE_CLI shim недоступен → только точные дубликаты "
                     "(по идентичному названию), без судьи слияний и без "
                     "SMART-переформулировок._")
    elif shim_call_failed:
        lines.append("⚠️ _CLAUDE_CLI shim настроен, но хотя бы один вызов во "
                     "время этого разбора не удался (сеть/таймаут/некорректный "
                     "ответ) — часть спорных дублей/названий могла остаться "
                     "без вердикта судьи и уйти в «на заметку»._")
    lines.append("")

    if actions["delete"]:
        lines.append(f"#### 🗑 Дубликаты на удаление — {len(actions['delete'])}")
        for it in actions["delete"]:
            lines.append(
                f"- **«{it['title']}»** — {it['project']} (`{it['taskId']}`) → "
                f"удалить, оставить **«{it['keep_title']}»** _({it['reason']})_")
        lines.append("")
    if actions["rename"]:
        lines.append(f"#### ✏️ SMART-переформулировки — {len(actions['rename'])}")
        for it in actions["rename"]:
            lines.append(
                f"- «{it['title']}» → **«{it['new_title']}»** — {it['project']} "
                f"(`{it['taskId']}`)"
                + (f" _({it['reason']})_" if it['reason'] else ""))
        lines.append("")
    if actions["group"]:
        total_kids = sum(len(g["children"]) for g in actions["group"])
        lines.append(f"#### 🔗 Группировка (родитель+подзадачи) — "
                     f"{len(actions['group'])} групп / {total_kids} задач")
        for g in actions["group"]:
            lines.append(f"- под **«{g['parent_title']}»** ({g['project']}, "
                         f"`{g['parentId']}`):")
            for c in g["children"]:
                lines.append(f"    - «{c['title']}» (`{c['taskId']}`)")
        lines.append("")

    if n_flags:
        lines.append("#### 🚩 Только на заметку (НЕ трогаю автоматически)")
        if actions["flag_obsolete"]:
            lines.append(f"- ⏳ Похоже на протухшие — просрочены и давно без "
                         f"движения ({len(actions['flag_obsolete'])}): "
                         "реши сам, добить или отпустить:")
            for it in actions["flag_obsolete"]:
                age = f"{it['age_days']}д без правок" if it['age_days'] is not None else "возраст неизв."
                lines.append(f"    - «{it['title']}» — {it['project']} · срок "
                             f"{it['due']} (просрочено {it['overdue_days']}д, {age})")
        if actions["flag_dupe"]:
            lines.append(f"- 🤔 Похожи, но слить не уверен — оставил обе "
                         f"({len(actions['flag_dupe'])}):")
            for it in actions["flag_dupe"]:
                lines.append("    - " + " / ".join(f"«{t}»" for t in it["titles"])
                             + f" _({it['reason']})_")
        if actions["flag_nonsmart"]:
            lines.append(f"- ✏️ Расплывчатые названия без готовой переформулировки "
                         f"({len(actions['flag_nonsmart'])}): "
                         + ", ".join(f"«{it['title']}»" for it in actions["flag_nonsmart"]))
        lines.append("")

    if n_mut == 0:
        lines.append("**Правок для применения нет** — либо всё чисто, либо всё "
                     "спорное ушло в «на заметку».")
        return "\n".join(lines) + sheet_note

    lines.append(f"**Итого к применению: {n_mut} правок** "
                 f"(🗑 {len(actions['delete'])} · ✏️ {len(actions['rename'])} · "
                 f"🔗 {sum(len(g['children']) for g in actions['group'])}). "
                 "Протухшие и спорные НЕ входят.")
    lines.append("")
    lines.append("Покажи этот план пользователю дословно и ДОЖДИСЬ его "
                 "отдельного ответа. Когда он явно согласится, вызови "
                 f"`execute_declutter(manifest_id=\"{mid}\", "
                 "user_reply=\"<дословная реплика пользователя>\")` — НЕ в "
                 "этом же ходе. Действует 1 час, одноразово. Каждая правка "
                 "пройдёт через штатные удаление/обновление/вложение "
                 "(guard + журнал + сверка).")
    return _maybe_tg_notify_plan("execute_declutter", mid, "\n".join(lines) + sheet_note)


# DISABLED 2026-08-04 — см. пометку у plan_declutter выше.
# @mcp.tool()
async def execute_declutter(manifest_id: str, user_reply: str = "") -> str:
    """
    Phase 2 of the declutter: apply EXACTLY the mutating actions the manifest
    proposed and the user approved. Gated (🔴 docs/DESIGN_approval_gate.md):
    `user_reply` must be the user's VERBATIM last chat message, given ONLY
    after they actually saw the plan and replied — do not paraphrase or
    invent it, and do not call this in the same turn where you printed the
    plan.

    Nothing here is a fresh decision — every action is routed through the
    already-audited tools (the deletion engine / update_tasks /
    set_task_parent), so each mutation is identity-guarded, journalled and
    post-verified. Obsolete and uncertain-duplicate FLAGS are never touched.
    One-shot. Afterwards the independent operation reports are appended.

    For a manifest planned with persist="sheet": this dispatches into the
    sheet-backed engine instead — `decision`/`status` are freshly re-read
    from the `Declutter Log` sheet (never trusted from RAM — see
    docs/DESIGN_sheet_backed_declutter.md §6), and per-row results are
    written back to the sheet. If the RAM pointer from plan_declutter is gone
    (e.g. a restart) but the manifest_id still has rows in the sheet, use
    resume_declutter instead — same engine, explicit entry point.

    Args:
        manifest_id: id from plan_declutter
        user_reply: the user's literal last message approving the plan
    """
    err = _ensure_ready()
    if err:
        return err
    _prune_manifests()
    m = _MANIFESTS.get(manifest_id)
    if m and m.get("kind") == "declutter" and m.get("persist") == "sheet":
        cr = _require_consent(action="declutter", tier=2, manifest=m,
                              user_reply=user_reply,
                              object_ids=_dc_object_ids(m.get("actions") or {}),
                              tool="execute_declutter", manifest_id=manifest_id)
        if not cr.ok:
            return cr.reason
        return await _execute_declutter_from_sheet(manifest_id)
    if not m or m.get("kind") != "declutter":
        # RAM pointer missing/expired — it may still be a sheet-backed
        # manifest from an earlier process (Railway restart killed RAM but
        # the sheet is durable). Only probe the sheet when it's actually
        # configured, so a genuinely bad/expired id keeps today's exact text.
        if declutter_sheet.sheet_configured():
            try:
                if declutter_sheet.read_manifest_rows(manifest_id):
                    cr = _require_consent(action="declutter", tier=2,
                                          manifest=None, user_reply=user_reply,
                                          tool="execute_declutter", manifest_id=manifest_id)
                    if not cr.ok:
                        return cr.reason
                    return await _execute_declutter_from_sheet(manifest_id)
            except declutter_sheet.DeclutterSheetError:
                pass
        return (f"🛑 Манифест разбора {manifest_id} не найден/истёк/уже "
                "исполнен. Сначала plan_declutter.")
    cr = _require_consent(action="declutter", tier=2, manifest=m,
                          user_reply=user_reply,
                          object_ids=_dc_object_ids(m.get("actions") or {}),
                          tool="execute_declutter", manifest_id=manifest_id)
    if not cr.ok:
        return cr.reason
    return await _execute_declutter_ram_impl(manifest_id, m)


async def _execute_declutter_ram_impl(manifest_id: str, m: Dict) -> str:
    """Shared declutter-application engine for the RAM-manifest branch (both
    plain RAM plans and persist="sheet" plans whose RAM pointer is still
    alive — the sheet-gone-after-restart case is handled separately by
    _execute_declutter_from_sheet, called by the resume_declutter tool and by
    execute_declutter's own sheet-persist/RAM-missing branches above).
    Factored out of execute_declutter() 2026-08-05 so the TG auto-execute
    poller (server.py's _AUTO_EXECUTORS registry) can call the exact same
    mutation path a manually-confirmed «да» would have run, once consent has
    already been granted by the caller (_require_consent, or the poller's
    try_auto_execute — see tg_approval.py)."""
    m["consumed"] = True
    try:
        actions = m["actions"]
        summary = m.get("summary") or "Разбор помойки"
        out_blocks: List[str] = []
        report_ids: set = set()

        # ---- Deletions: reuse the audited deletion manifest engine --------
        if actions["delete"]:
            sub_mid = uuid.uuid4().hex[:12]
            items = [{"taskId": it["taskId"], "projectId": it["projectId"],
                      "title": it["title"], "project": it.get("project", ""),
                      "snapshot": it["snapshot"]} for it in actions["delete"]]
            _MANIFESTS[sub_mid] = {"kind": "delete", "items": items,
                                   "created": time.monotonic(),
                                   "summary": summary + " — дубликаты",
                                   "consumed": False}
            res = await _execute_task_deletion_impl(sub_mid)
            out_blocks.append("## 🗑 Удаление дубликатов\n" + res)
            report_ids.add(sub_mid)

        # ---- Renames: reuse update_tasks (guard + journal + post-verify) --
        if actions["rename"]:
            res = await _update_tasks_impl(
                summary + " — SMART-переименования",
                [{"taskId": it["taskId"], "projectId": it["projectId"],
                  "title": it["title"], "new_title": it["new_title"]}
                 for it in actions["rename"]])
            out_blocks.append("## ✏️ Переименования\n" + res)

        # ---- Groups: reuse set_task_parent (guard + journal + post-verify) -
        for g in actions["group"]:
            res = await _set_task_parent_impl(
                summary + f" — под «{g['parent_title']}»",
                [{"taskId": c["taskId"], "title": c["title"]} for c in g["children"]],
                g["parentId"], g["project_id"], g["parent_title"])
            out_blocks.append(f"## 🔗 Группировка под «{g['parent_title']}»\n" + res)

        if not out_blocks:
            return "Нечего применять — в манифесте не было правок."

        combined = "\n\n".join(out_blocks)
        # Consolidated independent check: pull every journalled record id the
        # sub-tools referenced and append the server-built report for each.
        for rid in re.findall(r'operation_report\(record_id="([\w-]+)"\)', combined):
            report_ids.add(rid)
        reports = [_build_operation_report(rid) for rid in report_ids]
        if reports:
            combined += "\n\n---\n### 🧾 Независимые отчёты\n\n" + "\n\n".join(reports)
        return combined
    except Exception as e:
        logger.error(f"Error in execute_declutter: {e}")
        return f"Error executing declutter manifest: {str(e)}"


# DISABLED 2026-08-04 — см. пометку у plan_declutter выше.
# @mcp.tool()
async def resume_declutter(manifest_id: str, user_reply: str = "") -> str:
    """
    Resume a sheet-backed declutter manifest (plan_declutter(persist="sheet"))
    after a restart, timeout, or partial application — when the in-process RAM
    pointer plan_declutter left behind is gone. The Google Sheet IS the durable
    state; RAM was only ever a same-process cache (see
    docs/DESIGN_sheet_backed_declutter.md).

    Reconstructs the remaining work straight from the `Declutter Log` rows for
    this manifest_id: applies rows with decision="approved" AND status in
    (planned, failed) — a row already `done` is NEVER re-applied (so this is
    safe to call again after a partial run — it won't delete anything twice),
    and a row left `applying` by a crashed run is resolved against LIVE
    TickTick state first (task already gone → done; title already matches →
    done; otherwise → retried) rather than trusted at face value.

    Gated (🔴 docs/DESIGN_approval_gate.md): the RAM manifest is gone by
    definition here, so there is no plan_shown_at to time against — but
    `user_reply` must still be the user's VERBATIM real reply confirming they
    want the remaining approved rows applied NOW (not fabricated by you).
    If `decision` was edited in the sheet (or via set_declutter_decision)
    since the original plan, that's reflected automatically — this always
    re-reads `decision`/`status` fresh from the sheet.

    Args:
        manifest_id: id from the ORIGINAL plan_declutter(persist="sheet") call
        user_reply: the user's literal message confirming "yes, apply now"
    """
    err = _ensure_ready()
    if err:
        return err
    if not declutter_sheet.sheet_configured():
        return ("🛑 Sheet-режим declutter не настроен (нужны env "
                "DECLUTTER_SHEET_ID и GSHEETS_SA_JSON) — resume_declutter "
                "работает только для планов, сохранённых с persist=\"sheet\".")
    cr = _require_consent(action="declutter", tier=2, manifest=None,
                          user_reply=user_reply,
                          tool="resume_declutter", manifest_id=manifest_id)
    if not cr.ok:
        return cr.reason
    return await _execute_declutter_from_sheet(manifest_id)


# DISABLED 2026-08-04 — см. пометку у plan_declutter выше.
# @mcp.tool()
async def set_declutter_decision(manifest_id: str, row_ids: List[int],
                                 decision: str, user_reply: str = "") -> str:
    """
    Set the `decision` column (approved/rejected) for specific rows of a
    sheet-backed declutter manifest (plan_declutter(persist="sheet")) —
    instead of the human editing the Google Sheet by hand, Claude
    proposes/relays a decision here and the CODE writes it, so the row's
    task_id/decision never has to round-trip through the model's own context.

    WARNING: writing decision="approved" is EQUIVALENT to authorizing
    whatever execute_declutter/resume_declutter will do with those rows next
    (delete/rename/group) — it is NOT a harmless bookkeeping note. Gated
    (🔴 docs/DESIGN_approval_gate.md) for that reason: decision="approved"
    REQUIRES user_reply = the user's VERBATIM message approving exactly these
    rows — you may NOT set "approved" on your own judgement. decision=
    "rejected" is safe (it only prevents action) and is NOT gated.

    Does NOT touch TickTick and does NOT change `status` — purely an
    approval-bookkeeping write to column L. execute_declutter/resume_declutter
    read the freshly-written `decision` afterwards, as always.

    Args:
        manifest_id: id from plan_declutter(persist="sheet")
        row_ids: sheet row_id values (column A, printed in the plan / visible
            in the sheet) to update — NOT task ids
        decision: "approved" or "rejected"
        user_reply: REQUIRED when decision="approved" — the user's literal
            message approving these specific rows
    """
    if decision not in ("approved", "rejected"):
        return "🛑 decision должен быть \"approved\" или \"rejected\" — получено " \
               f"{decision!r}. Ничего не тронул."
    if not row_ids:
        return "🛑 Не передано ни одного row_id."
    if decision == "approved":
        cr = _require_consent(action="declutter_decision", tier=2,
                              manifest=None, user_reply=user_reply)
        if not cr.ok:
            return cr.reason
    if not declutter_sheet.sheet_configured():
        return ("🛑 Sheet-режим declutter не настроен (нужны env "
                "DECLUTTER_SHEET_ID и GSHEETS_SA_JSON).")
    try:
        rows = declutter_sheet.read_manifest_rows(manifest_id)
    except declutter_sheet.DeclutterSheetError as e:
        return f"🛑 Не могу прочитать таблицу разбора: {e}"
    if not rows:
        return f"🛑 Манифест разбора {manifest_id} не найден в таблице."
    valid_ids = {int(r["row_id"]) for r in rows
                if str(r.get("row_id") or "").strip().isdigit()}
    apply_ids = [rid for rid in row_ids if rid in valid_ids]
    unknown = [rid for rid in row_ids if rid not in valid_ids]
    if not apply_ids:
        return (f"🛑 Ни один row_id не найден в манифесте {manifest_id}: "
                f"{row_ids}. Ничего не тронул.")
    try:
        declutter_sheet.batch_update_rows(
            [{"row_id": rid, "decision": decision} for rid in apply_ids])
    except declutter_sheet.DeclutterSheetError as e:
        return f"🛑 Не удалось записать decision в таблицу: {e}"
    msg = f"✅ decision=\"{decision}\" проставлен для {len(apply_ids)} строк: {apply_ids}"
    if unknown:
        msg += f"\n⚠️ Не найдены в манифесте {manifest_id} (пропущены): {unknown}"
    return msg


@mcp.tool()
async def delete_task_with_subtasks(
    summary: str,
    task_id: str,
    project_id: str,
    task_title: str = None,
    project_name: str = None,
) -> str:
    """
    DEPRECATED / always refuses. Subtree deletion is NOT performed here —
    this tool exists only to catch old callers and redirect them. It always
    returns a refusal pointing to plan_task_deletion with {"taskId", "title",
    "with_subtasks": true}, which expands the ENTIRE open subtree into a
    manifest for approval (already gated 🔴 — plan_task_deletion →
    execute_task_deletion(manifest_id, user_reply=...)). No argument below
    has any effect; nothing is ever deleted by THIS tool, regardless of what
    you pass.

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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        name: Project name (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        color: Color code (hex format) (optional)
        view_mode: View mode - one of list, kanban, or timeline (optional)
        manifest_id: from call #1's response — pass on call #2 to actually create
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_official()
    if err:
        return err

    # Validate view_mode up front (cheap, applies to both calls).
    if view_mode not in ["list", "kanban", "timeline"]:
        return "Invalid view_mode. Must be one of: list, kanban, timeline."

    params = {"name": name, "color": color, "view_mode": view_mode}
    outcome = _gate_single("create_project", "create_project",
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
            return f"### ❌ Проект «{name}» НЕ создан\n\nTickTick отклонил: {project['error']}"
        pid = project.get('id')
    except Exception as e:
        logger.error(f"Error in create_project: {e}")
        return f"### ❌ Проект «{name}» НЕ создан\n\nОшибка: {str(e)}"

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
                f"⚠️ {_UNVERIFIED_MSG} ({e})")

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
    refuse = _guard_project(project_id, project_name, fresh=True,
                            require_known=True)
    if refuse:
        return refuse
    live_name = _v2_project_names().get(project_id, project_name)

    # Blast-radius disclosure: read the project's CURRENT contents fresh on
    # every call (no stored plan/manifest) — the compare below is always
    # against what's live right now, so drift between preview and confirm
    # naturally re-triggers a fresh preview instead of deleting stale counts.
    try:
        data = await _run_blocking(lambda: ticktick.get_project_with_data(project_id))
    except Exception as e:
        logger.error(f"Error in delete_project (fetching contents): {e}")
        return (f"🛑 Не смог прочитать содержимое проекта «{live_name}» — отказ "
                f"(не удаляю вслепую): {str(e)}")
    if 'error' in data:
        return (f"🛑 Не смог прочитать содержимое проекта «{live_name}» — отказ "
                f"(не удаляю вслепую): {data['error']}")
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
            lines.append(cr.reason)
            return "\n".join(lines)
        lines.append(
            "Ничего не удалено. Покажи это пользователю дословно и "
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
        return _maybe_tg_notify_plan("delete_project", new_mid, "\n".join(lines))

    if m is not None:
        m["consumed"] = True  # one-shot: план сгорел вместе с исполнением

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
                    f"{result['error']}\n{_report_line(record_id)}")

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
        logger.error(f"Error in delete_project: {e}")
        return f"Error deleting project: {str(e)}"
    

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
    """Check if a task matches the search term (case-insensitive)."""
    search_term = search_term.lower()
    
    # Search in title
    title = task.get('title', '').lower()
    if search_term in title:
        return True
    
    # Search in content
    content = task.get('content', '').lower()
    if search_term in content:
        return True
    
    # Search in subtasks
    items = task.get('items', [])
    for item in items:
        item_title = item.get('title', '').lower()
        if search_term in item_title:
            return True
    
    return False

def _get_project_tasks_by_filter(filter_func, filter_name: str) -> str:
    """
    Helper function to filter tasks across all projects.

    Args:
        filter_func: Function that takes a task and returns True if it matches the filter
        filter_name: Name of the filter for output formatting

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
            out = f"Tasks that are '{filter_name}' ({len(matched)}):\n"
            return out + format_task_tree(matched)
        except Exception as e:
            logger.warning(f"v2 task pool failed, falling back to official API: {e}")

    # Official-API fallback: fetch the project list only now that we need it.
    projects = ticktick.get_projects()
    if 'error' in projects:
        return f"Error fetching projects: {projects['error']}"
    if not projects:
        return "No projects found."

    result = f"Found {len(projects)} projects:\n\n"

    for i, project in enumerate(projects, 1):
        if project.get('closed'):
            continue

        project_id = project.get('id', 'No ID')
        project_data = ticktick.get_project_with_data(project_id)
        tasks = project_data.get('tasks', [])
        
        if not tasks:
            result += f"Project {i}:\n{format_project(project)}"
            result += f"With 0 tasks that are to be '{filter_name}' in this project :\n\n\n"
            continue
        
        # Filter tasks using the provided function
        filtered_tasks = [(t, task) for t, task in enumerate(tasks, 1) if filter_func(task)]
        
        result += f"Project {i}:\n{format_project(project)}"
        result += f"With {len(filtered_tasks)} tasks that are to be '{filter_name}' in this project :\n"
        
        for t, task in filtered_tasks:
            result += f"Task {t}:\n{format_task(task)}\n"
        
        result += "\n\n"
    
    return result

# New MCP Tools for Tasks

@mcp.tool(annotations=READONLY)
async def get_all_tasks() -> str:
    """
    Get ALL open tasks across every project and the Inbox in one fast call.

    Preferred over get_project_tasks when you need a full picture — this uses
    the v2 sync state (single request, includes Inbox) when available, falling
    back to the official API otherwise.
    """
    err = _ensure_official()
    if err:
        return err

    try:
        if ticktick_v2:
            tasks = await _run_blocking(lambda: ticktick_v2.get_open_tasks())
            if not tasks:
                return "No tasks found."
            names = _v2_project_names()
            by_project: Dict[str, list] = {}
            for t in tasks:
                pid = t.get("projectId", "")
                by_project.setdefault(pid, []).append(t)
            out = f"All open tasks ({len(tasks)}):\n\n"
            for pid, ptasks in by_project.items():
                pname = names.get(pid, pid or "Inbox")
                top = [t for t in ptasks if not t.get("parentId")]
                out += f"── {pname} ({len(top)} tasks) ──\n"
                out += format_task_tree(top, 500)
                out += "\n"
            return out

        # Fallback: official API per project (projects fetched inside helper)
        return _get_project_tasks_by_filter(lambda t: True, "included")

    except Exception as e:
        logger.error(f"Error in get_all_tasks: {e}")
        return f"Error retrieving tasks: {str(e)}"

@mcp.tool(annotations=READONLY)
async def get_tasks_by_priority(priority_id: int) -> str:
    """
    Get all tasks from TickTick by priority. Ignores closed projects.

    Args:
        priority_id: Priority of tasks to retrieve {0: "None", 1: "Low", 3: "Medium", 5: "High"}
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
        return _get_project_tasks_by_filter(priority_filter, f"priority '{priority_name}'")

    except Exception as e:
        logger.error(f"Error in get_tasks_by_priority: {e}")
        return f"Error retrieving tasks by priority: {str(e)}"

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
        logger.error(f"Error in get_tasks_due_today: {e}")
        return f"Error retrieving tasks due today: {str(e)}"

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
        logger.error(f"Error in get_overdue_tasks: {e}")
        return f"Error retrieving overdue tasks: {str(e)}"

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
        logger.error(f"Error in get_tasks_due_tomorrow: {e}")
        return f"Error retrieving tasks due tomorrow: {str(e)}"
    
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
        logger.error(f"Error in get_tasks_due_in_days: {e}")
        return f"Error retrieving tasks due in days: {str(e)}"

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
        logger.error(f"Error in get_tasks_due_this_week: {e}")
        return f"Error retrieving tasks due this week: {str(e)}"

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
        logger.error(f"Error in search_tasks: {e}")
        return f"Error searching tasks: {str(e)}"

@mcp.tool(annotations=READONLY)
async def get_recurring_tasks(search_term: str = "") -> str:
    """
    Get all tasks that have a recurrence rule (repeatFlag set), i.e. repeating tasks.
    Optionally filter by title/content search term.

    Do NOT call this in a loop — it already scans all open tasks at once.

    Args:
        search_term: Optional text to further filter by title/content (case-insensitive).
                     Leave empty to return all recurring tasks.
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
                return f"Error fetching projects: {projects['error']}"
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
        logger.error(f"Error in get_recurring_tasks: {e}")
        return f"Error retrieving recurring tasks: {str(e)}"

# New MCP Tools for Getting things done framework (Priority / Due Dates)

@mcp.tool(annotations=READONLY)
async def get_engaged_tasks() -> str:
    """
    Get all tasks from TickTick that are "Engaged".
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
        logger.error(f"Error in get_engaged_tasks: {e}")
        return f"Error retrieving engaged tasks: {str(e)}"

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
        logger.error(f"Error in get_next_tasks: {e}")
        return f"Error retrieving next tasks: {str(e)}"

def _describe_create_subtask(p: Dict) -> str:
    return (f'Создаю подзадачу «{p.get("subtask_title")}» под «'
            f'{p.get("parent_task_title")}»')


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        parent_task_title: Title of the parent task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        subtask_title: Title of the new subtask
        parent_task_id: ID of the parent task
        project_id: ID of the project (must be same for both parent and subtask)
        content: Optional content/description for the subtask
        priority: Priority level (0: None, 1: Low, 3: Medium, 5: High) (optional)
        manifest_id: from call #1's response — pass on call #2 to actually create
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_official()
    if err:
        return err

    # Validate priority up front (cheap, applies to both calls).
    if priority not in [0, 1, 3, 5]:
        return "Invalid priority. Must be 0 (None), 1 (Low), 3 (Medium), or 5 (High)."

    params = {"parent_task_title": parent_task_title, "subtask_title": subtask_title,
              "parent_task_id": parent_task_id, "project_id": project_id,
              "content": content, "priority": priority}
    outcome = _gate_single("create_subtask", "create_subtask",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_create_subtask,
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
    if g.status == "unavailable":
        return g.message
    if g.status == "mismatch":
        return (f"🛑 НЕ создал подзадачу — родитель по id это «{g.title}», а НЕ "
                f"«{parent_task_title}». Ничего не тронул.")
    if g.status == "missing":
        return (f"🛑 НЕ создал подзадачу — родитель «{parent_task_title}» не "
                "среди открытых задач (завершён/удалён/неверный id). Ничего не тронул.")
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
            return f"Error creating subtask: {subtask['error']}"

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
        logger.error(f"Error in create_subtask: {e}")
        return f"Error creating subtask: {str(e)}"

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
        return out + format_task_list(tasks)
    except Exception as e:
        logger.error(f"Error in get_completed_tasks: {e}")
        return f"Error fetching completed tasks: {str(e)}"


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
        logger.error(f"Error in list_tags: {e}")
        return f"Error fetching tags: {str(e)}"


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
        logger.error(f"Error in get_tasks_by_tag: {e}")
        return f"Error fetching tasks by tag: {str(e)}"


@mcp.tool(annotations=READONLY)
async def get_inbox_tasks() -> str:
    """Get open tasks in the Inbox (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        tasks = await _run_blocking(lambda: ticktick_v2.get_inbox_tasks())
        if not tasks:
            return "No open tasks in the Inbox."
        out = f"Inbox tasks ({len(tasks)}):\n\n"
        return out + format_task_tree(tasks)
    except Exception as e:
        logger.error(f"Error in get_inbox_tasks: {e}")
        return f"Error fetching inbox tasks: {str(e)}"


@mcp.tool()
async def move_tasks(summary: str, tasks: List[Dict[str, str]] = None,
                     to_project_id: str = "", to_project_name: str = None,
                     manifest_id: str = "", user_reply: str = "") -> str:
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

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title": "...", "taskId": "..."} objects — required
            on call #1, ignored on call #2
        to_project_id: Destination project/list ID for ALL tasks — required
            on call #1, ignored on call #2
        to_project_name: Destination list name (shown in the dialog)
        manifest_id: from call #1's response — pass on call #2 to actually move
        user_reply: the user's literal reply approving the plan — required on call #2

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    outcome = _gate_batch(
        "move", "move_tasks", tasks, summary, manifest_id, user_reply,
        lambda t: f"**«{t.get('title') or t.get('taskId')}»** → {to_project_name or to_project_id}",
        extra={"to_project_id": to_project_id, "to_project_name": to_project_name})
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
        refuse = _guard_project(to_project_id, to_project_name or "",
                                fresh=True, require_known=True)
        if refuse:
            return refuse
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
            resp = await _run_blocking(lambda: ticktick_v2.batch_move_tasks(
                [f["taskId"] for f in found], to_project_id))
            api_fail = id2error_failures(resp, [f["taskId"] for f in found])
            fresh = _open_by_id(fresh=True)  # verify the tasks actually landed
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
        logger.error(f"Error in move_tasks: {e}")
        return f"Error moving tasks: {str(e)}"


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
        out = f"Habits ({len(habits)}):\n\n"
        for h in habits:
            out += (f"- {h.get('name','?')}  (id: {h.get('id')})\n"
                    f"    goal: {h.get('goal')} {h.get('unit','')} | type: {h.get('type')} | "
                    f"total check-ins: {h.get('totalCheckIns', 0)}\n"
                    f"    repeat: {h.get('repeatRule','')}\n")
        return out
    except Exception as e:
        logger.error(f"Error in get_habits: {e}")
        return f"Error fetching habits: {str(e)}"


_HABIT_STATUS_LABELS = {2: "выполнено", 1: "провалено", 0: "не выполнено"}


def _describe_checkin_habit(p: Dict) -> str:
    label = _HABIT_STATUS_LABELS.get(p.get("status"), p.get("status"))
    when = p.get("date") or "сегодня"
    return f'Отмечаю привычку «{p.get("habit_name")}» на {when}: {label}'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

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
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
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
    outcome = _gate_single("checkin_habit", "checkin_habit",
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
                    f"- ⚠️ независимое перечитывание (`get_habit_checkins`) упало с ошибкой: {e} — "
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
        logger.error(f"Error in checkin_habit: {e}")
        return f"Error checking in habit: {str(e)}"


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
        lines = [f"- {e.get('checkinStamp')}: {labels.get(e.get('status'), e.get('status'))} "
                 f"(value {e.get('value')}/{e.get('goal')})" for e in entries]
        return f"Check-ins for '{habit_name}' ({len(entries)}):\n" + "\n".join(lines)
    except Exception as e:
        logger.error(f"Error in get_habit_checkins: {e}")
        return f"Error fetching habit check-ins: {str(e)}"


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
        logger.error(f"Error in list_filters: {e}")
        return f"Error fetching filters: {str(e)}"


# ---------------------------------------------------------------------------
# Subtasks (v2)
# ---------------------------------------------------------------------------

@mcp.tool()
async def set_task_parent(summary: str, tasks: List[Dict[str, str]] = None,
                          parent_task_id: str = "", project_id: str = "",
                          parent_task_title: str = None,
                          manifest_id: str = "", user_reply: str = "") -> str:
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
        user_reply: the user's literal reply approving the plan — required on call #2

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    outcome = _gate_batch(
        "parent", "set_task_parent", tasks, summary, manifest_id, user_reply,
        lambda t: f"**«{t.get('title') or t.get('taskId')}»** → под «{parent_task_title or parent_task_id}»",
        extra={"parent_task_id": parent_task_id, "project_id": project_id,
               "parent_task_title": parent_task_title})
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
        if pg.status == "mismatch":
            return (f"🛑 НЕ вложил — родитель по id это «{pg.title}», а НЕ "
                    f"«{parent_task_title}». Ничего не тронул.")
        if pg.status == "missing":
            return (f"🛑 НЕ вложил — родитель «{parent_task_title or parent_task_id}» "
                    "не среди открытых задач (завершён/удалён/неверный id) — "
                    "вложение под мёртвого родителя осиротит задачи. Ничего не тронул.")
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
            fresh = _open_by_id(fresh=True)
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
        logger.error(f"Error in set_task_parent: {e}")
        return f"Error nesting tasks: {str(e)}"

def _describe_unset_task_parent(p: Dict) -> str:
    return (f'Отцепляю «{p.get("task_title")}» от родителя '
            f'«{p.get("parent_task_title")}»')


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        task_title: Title of the subtask being detached (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        parent_task_title: Title of its current parent task
        task_id: ID of the subtask to detach
        parent_task_id: ID of its current parent
        project_id: ID of the project both tasks live in
        manifest_id: from call #1's response — pass on call #2 to actually detach
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"task_title": task_title, "parent_task_title": parent_task_title,
              "task_id": task_id, "parent_task_id": parent_task_id,
              "project_id": project_id}
    outcome = _gate_single("unset_task_parent", "unset_task_parent",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_unset_task_parent,
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
        if g.status == "mismatch":
            return (f"🛑 НЕ отцепил — id это «{g.title}», а НЕ «{task_title}». "
                    "Ничего не тронул.")
        if g.status == "missing":
            return (f"🛑 НЕ отцепил — «{task_title}» не среди открытых задач "
                    "(завершена/удалена/неверный id). Ничего не тронул.")
        live_parent = (by_id.get(task_id) or {}).get("parentId")
        if not live_parent:
            return (f"↷ «{task_title}» и так не является подзадачей — "
                    "отцеплять нечего. Ничего не тронул.")
        if live_parent != parent_task_id:
            real_pname = (by_id.get(live_parent) or {}).get("title") or live_parent
            return (f"🛑 НЕ отцепил — «{task_title}» является подзадачей "
                    f"«{real_pname}», а НЕ «{parent_task_title}». Ничего не тронул.")
        resp = await _run_blocking(lambda: ticktick_v2.unset_task_parent(
            task_id, live_parent, g.project_id or project_id))
        api_err = id2error_failures(resp, [task_id]).get(task_id)
        rid = _op_journal("parent", [{"taskId": task_id, "title": task_title,
                                      "expect": {"parentId": None}}],
                          f"Отцепить «{task_title}»")
        # Post-verify: the live parentId must actually be gone.
        fresh = _open_by_id(fresh=True)
        if api_err:
            return (f"❌ НЕ отцепил «{task_title}» — TickTick отклонил: {api_err}\n"
                    + _report_line(rid))
        if fresh is None:
            return (f"Отцепление «{task_title}» отправлено, но {_UNVERIFIED_MSG}\n"
                    + _report_line(rid))
        if (fresh.get(task_id) or {}).get("parentId"):
            return (f"❌ НЕ отцепил «{task_title}» — parentId всё ещё стоит.\n"
                    + _report_line(rid))
        return (f"✓ «{task_title}» отцеплена от «{parent_task_title}» (проверено).\n"
                + _report_line(rid))
    except Exception as e:
        logger.error(f"Error in unset_task_parent: {e}")
        return f"Error detaching subtask: {str(e)}"


@mcp.tool()
async def set_task_tags(summary: str, tasks: List[Dict[str, Any]] = None,
                        manifest_id: str = "", user_reply: str = "") -> str:
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

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title","taskId","tags"} objects — required on call
            #1, ignored on call #2
        manifest_id: from call #1's response — pass on call #2 to actually retag
        user_reply: the user's literal reply approving the plan — required on call #2

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
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
    existing_tags: set = set()
    if not manifest_id and tasks:
        try:
            existing_tags = set(await _live_tag_names(force=True))
        except Exception as e:
            logger.warning(f"set_task_tags: не удалось прочитать список тегов "
                           f"для превью плана ({e}) — пометка «будет создан» "
                           "в этом плане не показывается")

    def _describe_tags(t: Dict) -> str:
        wanted = t.get("tags") or []
        parts = []
        for tag in wanted:
            bare = tag.lstrip("#").lower()
            if bare and bare not in existing_tags:
                parts.append(f"{tag} (тег не существует — будет создан)")
            else:
                parts.append(tag)
        return (f"**«{t.get('title') or t.get('taskId')}»** → теги: "
                + (", ".join(parts) or "(пусто)"))

    outcome = _gate_batch(
        "tags", "set_task_tags", tasks, summary, manifest_id, user_reply,
        _describe_tags)
    if not outcome.proceed:
        return outcome.message
    return await _set_task_tags_impl(outcome.summary, outcome.tasks)


async def _set_task_tags_impl(summary: str, tasks: List[Dict[str, Any]]) -> str:
    """Pure mutation logic for set_task_tags — no consent gate. Called
    only by the public gated set_task_tags() below."""
    err = _ensure_ready()
    if err:
        return err
    try:
        by_id = _open_by_id(fresh=True)
        if by_id is None:
            return _STATE_UNAVAILABLE_MSG
        found, mismatch, missing = _split_tasks_by_state(tasks, by_id=by_id)
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
        # force=False: _open_by_id(fresh=True) just above already forced a
        # fresh sync-state fetch (tags included) within the TTL window — no
        # need for a second network round-trip for the same snapshot.
        existing_tags = set(await _live_tag_names(force=False))
        to_register = [t for t in requested if t not in existing_tags]
        for tag_name in to_register:
            try:
                await _run_blocking(lambda tn=tag_name: ticktick_v2.create_tag(tn))
            except Exception as e:
                logger.error(
                    f"set_task_tags: auto-registration of tag '{tag_name}' "
                    f"raised: {e}")
        after_create = (set(await _live_tag_names(force=True))
                        if to_register else existing_tags)
        registered = [t for t in to_register if t in after_create]
        failed_register = [t for t in to_register if t not in after_create]

        # Fail closed for exactly the tags that couldn't be registered: drop
        # the WHOLE per-task change rather than send a truncated tag list —
        # set_task_tags REPLACES all tags on a task, so silently stripping
        # just the bad tag from a change could wipe tags the user never
        # asked to touch. The task is left completely untouched instead.
        skipped_tasks = []
        if failed_register:
            bad_set = set(failed_register)
            kept = []
            for c in changes:
                bad = set(c["tags"]) & bad_set
                if bad:
                    skipped_tasks.append((ok[c["taskId"]]["title"], sorted(bad)))
                else:
                    kept.append(c)
            changes = kept

        api_fail = {}
        if changes:
            resp = await _run_blocking(
                lambda: ticktick_v2.batch_update_tasks(changes))
            api_fail = id2error_failures(resp, [c["taskId"] for c in changes])
        # Inline post-verify: live tags must equal the requested set, AND any
        # newly-registered tag must be visible in the account's own tag list
        # (list_tags) — this is the proof that (b) actually closed the
        # orphan hole, not just moved it.
        tags_by_id = {c["taskId"]: c["tags"] for c in changes}
        applied, failed = [], []
        unverified = False
        if changes:
            fresh = _open_by_id(fresh=True)
            if fresh is None:
                unverified = True
            else:
                for f in found:
                    if f["taskId"] not in tags_by_id:
                        continue  # skipped above — never sent, don't verify
                    want = set(tags_by_id.get(f["taskId"], []))
                    got = set((fresh.get(f["taskId"]) or {}).get("tags") or [])
                    ok_item = want == got and f["taskId"] not in api_fail
                    (applied if ok_item else failed).append(f["title"])
        lines = []
        if applied:
            lines.append(f"🏷 Теги обновлены у {len(applied)} (проверено): "
                         + ", ".join(f"«{t}»" for t in applied))
        if registered:
            lines.append(
                f"🆕 Новые теги зарегистрированы в аккаунте (проверено — видны "
                f"в list_tags, удаляются delete_tag), {len(registered)}: "
                + ", ".join(f"«{t}»" for t in registered))
        if failed_register:
            lines.append(
                f"⚠️ Не удалось зарегистрировать в аккаунте {len(failed_register)} "
                "тег(ов) — они НЕ проставлены ни на одну задачу (во избежание "
                "тега-сироты): " + ", ".join(f"«{t}»" for t in failed_register))
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
        logger.error(f"Error in set_task_tags: {e}")
        return f"Error setting tags: {str(e)}"# ---------------------------------------------------------------------------
# Batch operations (v2)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Builder helpers (no API call — produce strings for create_task/update_task)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READONLY)
async def build_recurrence_rule(frequency: str, interval: int = 1,
                                by_day: List[str] = None, count: int = None,
                                until: str = None) -> str:
    """
    Build an RRULE recurrence string to pass as repeat_flag in create_task/update_task.

    Args:
        frequency: DAILY, WEEKLY, MONTHLY, or YEARLY
        interval: Repeat every N units (default 1)
        by_day: For weekly rules, days like ["MO","WE","FR"] (optional)
        count: Stop after this many occurrences (optional)
        until: Stop on this date YYYY-MM-DD (optional)
    """
    freq = frequency.upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return "Invalid frequency. Use DAILY, WEEKLY, MONTHLY, or YEARLY."
    parts = [f"FREQ={freq}", f"INTERVAL={max(1, interval)}"]
    if by_day:
        parts.append("BYDAY=" + ",".join(d.upper() for d in by_day))
    if count:
        parts.append(f"COUNT={count}")
    if until:
        parts.append("UNTIL=" + until.replace("-", "") + "T000000Z")
    return "RRULE:" + ";".join(parts)


@mcp.tool(annotations=READONLY)
async def build_reminder(minutes_before: int = 0) -> str:
    """
    Build a reminder TRIGGER string to pass in the reminders list of create_task/update_task.

    Args:
        minutes_before: Minutes before the due time to remind. 0 = at the time of the event.
    """
    if minutes_before <= 0:
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
async def run_filter(filter: str) -> str:
    """
    Run a saved smart-list filter and return the open tasks it matches (requires v2 API).

    Args:
        filter: Filter name or ID (from list_filters)
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        tasks = await _run_blocking(lambda: ticktick_v2.run_filter(filter))
        if not tasks:
            return f"Filter '{filter}' matched no open tasks."
        out = f"Filter '{filter}' — {len(tasks)} task(s):\n\n"
        return out + format_task_tree(tasks)
    except Exception as e:
        logger.error(f"Error in run_filter: {e}")
        return f"Error running filter: {str(e)}"


# ---------------------------------------------------------------------------
# Project groups / folders (v2)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READONLY)
async def list_project_groups() -> str:
    """List project groups (folders) (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        groups = await _run_blocking(lambda: ticktick_v2.list_project_groups())
        groups = [g for g in groups if not g.get("deleted")]
        if not groups:
            return "No project groups found."
        return f"Project groups ({len(groups)}):\n\n" + "\n".join(
            f"- {g.get('name','?')}  (id: {g.get('id')})" for g in groups)
    except Exception as e:
        logger.error(f"Error in list_project_groups: {e}")
        return f"Error fetching project groups: {str(e)}"


async def _live_groups(fresh: bool = True) -> List[Dict]:
    """Non-deleted project groups from the (optionally force-fresh) v2 state."""
    if fresh:
        await _run_blocking(lambda: ticktick_v2.get_state(force=True))
    groups = await _run_blocking(lambda: ticktick_v2.list_project_groups())
    return [g for g in groups if not g.get("deleted")]


def _describe_create_project_group(p: Dict) -> str:
    return f'Создаю папку проектов «{p.get("name")}»'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        name: Name of the new group (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        manifest_id: from call #1's response — pass on call #2 to actually create
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"name": name}
    outcome = _gate_single("create_project_group", "create_project_group",
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
        return f"❌ Группа «{name}» НЕ создана — TickTick отклонил: {e}"
    except Exception as e:
        logger.error(f"Error in create_project_group: {e}")
        return f"Error creating project group: {str(e)}"
    # Post-verify: the new group must appear in the force-refreshed list.
    try:
        groups = await _live_groups()
        if not any(g.get("id") == gid for g in groups):
            return (f"❌ Группа «{name}» НЕ подтвердилась — её нет в списке "
                    "групп после создания, проверь вручную.")
    except Exception as e:
        return f"Группа «{name}» отправлена (id: {gid}), но {_UNVERIFIED_MSG} ({e})"
    return f"Группа проектов «{name}» создана (проверено). (id: {gid})"


def _describe_delete_project_group(p: Dict) -> str:
    return (f'Удаляю папку проектов «{p.get("group_name")}» (сами проекты '
            "останутся, просто без папки)")


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        group_name: Name of the group (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        group_id: ID of the group
        manifest_id: from call #1's response — pass on call #2 to actually delete
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"group_name": group_name, "group_id": group_id}
    outcome = _gate_single("delete_project_group", "delete_project_group",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_delete_project_group,
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
        return f"Project group '{real}' deleted (проверено; проекты остались, просто без папки)."
    except Exception as e:
        logger.error(f"Error in delete_project_group: {e}")
        return f"Error deleting project group: {str(e)}"


def _describe_move_project_to_group(p: Dict) -> str:
    dest = "без папки" if p.get("group_id") == "NONE" else f'в папку id:{p.get("group_id")}'
    return f'Перемещаю проект «{p.get("project_name")}» {dest}'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        project_name: Name of the project (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        project_id: ID of the project to move
        group_id: ID of the destination group, or "NONE" to ungroup
        manifest_id: from call #1's response — pass on call #2 to actually move
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"project_name": project_name, "project_id": project_id, "group_id": group_id}
    outcome = _gate_single("move_project_to_group", "move_project_to_group",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_move_project_to_group,
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
        refuse = _guard_project(project_id, project_name, fresh=True,
                                require_known=True)
        if refuse:
            return refuse
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
        # Post-verify: the project's live groupId must equal the target.
        await _run_blocking(lambda: ticktick_v2.get_state(force=True))
        projs = await _run_blocking(lambda: ticktick_v2.list_projects())
        proj = next((p for p in projs if p.get("id") == project_id), None)
        got = (proj or {}).get("groupId")
        want = None if group_id == "NONE" else group_id
        dest = "без папки (ungrouped)" if group_id == "NONE" else f"папку «{dest_name}»"
        if proj is None:
            return (f"Проект «{live_pname}» отправлен в {dest}, но "
                    f"{_UNVERIFIED_MSG}")
        if (got or None) != want:
            return (f"❌ Проект «{live_pname}» НЕ переместился — живой groupId "
                    f"{got!r}, ожидался {want!r}.")
        return f"Проект «{live_pname}» перемещён в {dest} (проверено)."
    except Exception as e:
        logger.error(f"Error in move_project_to_group: {e}")
        return f"Error moving project: {str(e)}"


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
        logger.error(f"Error in get_task_comments: {e}")
        return f"Error fetching comments: {str(e)}"


def _describe_add_task_comment(p: Dict) -> str:
    return f'Добавляю комментарий к «{p.get("task_title")}»: «{p.get("text")}»'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        task_title: Title of the task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        text: Comment text
        project_id: ID of the project
        task_id: ID of the task
        manifest_id: from call #1's response — pass on call #2 to actually add
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"task_title": task_title, "text": text, "project_id": project_id,
              "task_id": task_id}
    outcome = _gate_single("add_task_comment", "add_task_comment",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_add_task_comment,
                           automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _add_task_comment_impl(**outcome.extra)


async def _add_task_comment_impl(task_title: str, text: str, project_id: str,
                                 task_id: str) -> str:
    """Pure mutation logic for add_task_comment — no consent gate. Called
    only by the gated add_task_comment() above once the plan is approved."""
    try:
        g = _guard_task(task_id, task_title or "", project_id)
        if g.status == "unavailable":
            return g.message
        if g.status == "mismatch":
            return (f"🛑 НЕ добавил комментарий — id это «{g.title}», а НЕ "
                    f"«{task_title}». Ничего не тронул.")
        warn = ""
        if g.status == "missing":
            # Commenting a completed task is legitimate, but the id↔title
            # check could not run — say so instead of implying it did.
            warn = ("\n⚠️ id не среди открытых задач (возможно, завершена) — "
                    "название НЕ проверено.")
        await _run_blocking(lambda: ticktick_v2.add_task_comment(
            g.project_id or project_id, task_id, text))
        return f"Comment added to '{task_title}'.{warn}"
    except Exception as e:
        logger.error(f"Error in add_task_comment: {e}")
        return f"Error adding comment: {str(e)}"


# ---------------------------------------------------------------------------
# Statistics & trash (v2)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READONLY)
async def get_statistics() -> str:
    """Get productivity statistics: achievement score/level and completion counts (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        s = await _run_blocking(lambda: ticktick_v2.get_statistics())
        if not s:
            return "No statistics available."
        return (
            f"Achievement score: {s.get('score')}  |  Level: {s.get('level')}\n"
            f"Completed today: {s.get('todayCompleted')}  |  "
            f"yesterday: {s.get('yesterdayCompleted')}  |  "
            f"total: {s.get('totalCompleted')}"
        )
    except Exception as e:
        logger.error(f"Error in get_statistics: {e}")
        return f"Error fetching statistics: {str(e)}"


@mcp.tool(annotations=READONLY)
async def get_trash(limit: int = 50) -> str:
    """
    List recently deleted (trashed) tasks (requires v2 API). Each line carries
    the task's id and original project — restore_tasks needs both: the id/title
    pair to identify the task (title is checked against the live trash entry
    before restoring), and to_project_id only if you want to override the
    original list it restored to.

    Args:
        limit: Maximum number of trashed tasks to return (default 50, max 500)
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        tasks = await _run_blocking(lambda: ticktick_v2.get_trash(limit))
        if not tasks:
            return "Trash is empty."
        out = f"Trashed tasks ({len(tasks)}):\n\n"
        return out + format_task_list(tasks)
    except Exception as e:
        logger.error(f"Error in get_trash: {e}")
        return f"Error fetching trash: {str(e)}"


@mcp.tool()
async def restore_tasks(summary: str, tasks: List[Dict[str, str]] = None,
                        to_project_id: str = None,
                        manifest_id: str = "", user_reply: str = "") -> str:
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
        user_reply: the user's literal reply approving the plan — required on call #2

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    outcome = _gate_batch(
        "restore", "restore_tasks", tasks, summary, manifest_id, user_reply,
        lambda t: f"**«{t.get('title') or t.get('taskId')}»**",
        extra={"to_project_id": to_project_id})
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
            refuse = _guard_project(to_project_id, "", fresh=True,
                                    require_known=True)
            if refuse:
                return refuse
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
        wrong_project = []  # [(title, want_pid, got_pid)] — так и не туда даже после фикса
        unverified = False
        if ok_items:
            fresh = _open_by_id(fresh=True)
            if fresh is None:
                unverified = True
            else:
                to_fix = []
                for i in ok_items:
                    live = fresh.get(i["taskId"])
                    if live is None or i["taskId"] in api_fail:
                        failed.append(i["title"])
                    elif not i["want_pid"]:
                        # Не удалось определить исходный список из записи в
                        # корзине — честно сказать об этом, а не молча
                        # засчитать как успех, который на деле не проверен.
                        unknown_dest.append(i["title"])
                    elif live.get("projectId") != i["want_pid"]:
                        to_fix.append(i)
                    else:
                        restored.append(i["title"])
                if to_fix:
                    fix_by_pid: Dict[str, List[str]] = {}
                    for i in to_fix:
                        fix_by_pid.setdefault(i["want_pid"], []).append(i["taskId"])
                    fix_api_fail: Dict[str, str] = {}
                    for pid, ids in fix_by_pid.items():
                        try:
                            mresp = await _run_blocking(
                                lambda pid=pid, ids=ids:
                                    ticktick_v2.batch_move_tasks(ids, pid))
                            fix_api_fail.update(id2error_failures(mresp, ids))
                        except Exception as e:
                            logger.error("restore_tasks: corrective move to "
                                        f"{pid} failed: {e}")
                            for tid2 in ids:
                                fix_api_fail.setdefault(tid2, str(e))
                    fresh2 = _open_by_id(fresh=True)
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
        logger.error(f"Error in restore_tasks: {e}")
        return f"Error restoring tasks: {str(e)}"


def _describe_attach_file_to_task(p: Dict) -> str:
    name = p.get("filename") or (p.get("url") or "").split("?")[0].rstrip("/").split("/")[-1] or "файл"
    return f'Прикрепляю «{name}» к задаче «{p.get("task_title")}»'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        task_title: Title of the task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        task_id: ID of the task
        project_id: ID of the task's project (auto-corrected if stale)
        url: Public/direct URL to download the file from (optional)
        content_base64: Base64-encoded file content (optional, alternative to url)
        filename: File name to store it as (optional; inferred from url if omitted)
        manifest_id: from call #1's response — pass on call #2 to actually attach
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    if not url and not content_base64:
        return "Provide either a url or content_base64 for the file."
    params = {"task_title": task_title, "task_id": task_id, "project_id": project_id,
              "url": url, "content_base64": content_base64, "filename": filename}
    outcome = _gate_single("attach_file_to_task", "attach_file_to_task",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_attach_file_to_task,
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
    g = _guard_task(task_id, task_title or "", project_id, by_id=pre)
    if g.status == "mismatch":
        return (f"🛑 НЕ прикрепил — id это «{g.title}», а НЕ «{task_title}». "
                "Ничего не тронул.")
    warn = ""
    if g.status == "missing":
        warn = ("\n⚠️ id не среди открытых задач (возможно, завершена) — "
                "название НЕ проверено.")
    try:
        pid = g.project_id or _resolve_project_id(task_id, project_id)
        pre_count = len((pre.get(task_id) or {}).get("attachments") or [])
        att = await _run_blocking(lambda: ticktick_v2.upload_attachment(
            pid, task_id, url=url, content_base64=content_base64, filename=filename))
        # The endpoint can return a 2xx with an empty body — don't fabricate
        # details from {}; post-verify against the task's attachment list.
        shown_name = att.get("fileName") or filename or \
            ((url or "").split("?")[0].rstrip("/").split("/")[-1] or "attachment")
        size = att.get("size")
        size_str = f"{size} bytes" if size is not None else "размер неизвестен"
        post = _open_by_id(fresh=True)
        if post is None:
            verify = f" {_UNVERIFIED_MSG}"
        elif task_id in post:
            post_count = len((post.get(task_id) or {}).get("attachments") or [])
            verify = (" (проверено: вложение видно на задаче)"
                      if post_count > pre_count else
                      " ⚠️ вложение НЕ видно на задаче — проверь вручную")
        else:
            verify = " (задача не среди открытых — вложение не проверить)"
        return f"Attached '{shown_name}' ({size_str}) to '{title}'{verify}{warn}"
    except Exception as e:
        logger.error(f"Error in attach_file_to_task: {e}")
        return f"Error attaching file: {str(e)}"


# Base64-encoded response payloads are ~4/3 the raw byte size, plus the MCP
# transport itself has overhead — cap well under upload's 20 MB so a giant
# attachment doesn't blow up the tool response instead of failing cleanly.
DOWNLOAD_ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024  # 15 MB


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


@mcp.tool(annotations=READONLY)
async def list_task_attachments(task_id: str, project_id: str = None) -> str:
    """
    List a task's file attachments (requires v2 API): filename, id (needed by
    download_task_attachment), and size when known. Combines the structured
    attachment metadata with ids/filenames parsed out of the task's own
    content (TickTick embeds them there as ![file](id/name) tokens), since
    not every account's attachment entries carry an id field directly.

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
        logger.error(f"Error in list_task_attachments: {e}")
        return f"Error listing attachments: {str(e)}"


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
    pid = project_id or _resolve_project_id(task_id, project_id)
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
    Download a file attachment from a task (requires v2 API) and return its
    content as base64, so it can be re-saved elsewhere (e.g. uploaded to
    Google Drive). Identify the attachment by ONE of: attachment_id (from
    list_task_attachments), filename (exact match), or index (1-based, as
    shown by list_task_attachments). Refuses files over 15 MB (base64 bloats
    the response) — for those (and whenever the user just wants the file on
    their phone/computer) use get_attachment_download_url instead: it hands
    back a short-lived link and the bytes never enter this conversation.

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
            return (f"'{name}' is {len(data) // (1024*1024)} MB — over the "
                    f"{DOWNLOAD_ATTACHMENT_MAX_BYTES // (1024*1024)} MB base64-response "
                    "limit. Not downloaded.")

        b64 = base64.b64encode(data).decode("ascii")
        return (f"filename: {name}\n"
                f"mime: {mime}\n"
                f"size_bytes: {len(data)}\n"
                f"content_base64: {b64}")
    except Exception as e:
        logger.error(f"Error in download_task_attachment: {e}")
        return f"Error downloading attachment: {str(e)}"


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
    Get a temporary direct download LINK for a task attachment (requires v2
    API) instead of pulling the file through this conversation. Use this for
    anything big, and whenever the user actually wants the file on their phone
    or computer — download_task_attachment base64-encodes into the answer and
    refuses over 15 MB, this has no such limit and costs no tokens.

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
        logger.error(f"Error in get_attachment_download_url: {e}")
        return f"Error building download link: {str(e)}"


def _describe_create_attachment_upload_url(p: Dict) -> str:
    # НИ ОДНОГО фрагмента будущей ссылки/токена здесь быть не может: токен
    # подписывается только в `_impl`, уже ПОСЛЕ подтверждения. Иначе пропуск
    # утёк бы в превью (и в сообщение Telegram) ещё до согласия человека.
    name = p.get("filename") or "файл без имени"
    return (f'Выдаю ссылку на загрузку «{name}» в задачу {p.get("task_id")} '
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

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
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
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
    outcome = _gate_single("create_attachment_upload_url",
                           "create_attachment_upload_url",
                           params if not manifest_id else None,
                           manifest_id, user_reply,
                           _describe_create_attachment_upload_url,
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
    try:
        pid = project_id or _resolve_project_id(task_id, project_id)
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
        logger.error(f"Error in create_attachment_upload_url: {e}")
        return f"Error building upload link: {str(e)}"


# ---------------------------------------------------------------------------
# Tag write operations (v2)
# ---------------------------------------------------------------------------

def _describe_create_tag(p: Dict) -> str:
    return f'Создаю тег «{p.get("name")}»' + (f' (цвет {p["color"]})' if p.get("color") else "")


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        name: Tag name (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        color: Optional hex color like '#FF6161'
        manifest_id: from call #1's response — pass on call #2 to actually create
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"name": name, "color": color}
    outcome = _gate_single("create_tag", "create_tag",
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
        logger.error(f"Error in create_tag: {e}")
        return f"Error creating tag: {str(e)}"


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
                msg = (f"🛑 Тег «{new_name}» уже существует — это будет СЛИЯНИЕ "
                       f"тегов «{old_name}» и «{new_name}» (необратимо: какие "
                       "задачи носили какой тег — потеряется). Покажи это "
                       "пользователю дословно и, ТОЛЬКО после его явного "
                       "согласия, повтори с allow_merge=true и "
                       "user_reply=<дословная реплика пользователя>. Ничего "
                       "не тронул." + (f" ({cr.reason})"
                                       if (not cr.ok and cr.reason) else ""))
                if m is not None or _is_negative_reply(user_reply):
                    # План уже есть (возможно, уже висит кнопкой в Telegram)
                    # либо человек отказался — второй раз не шлём.
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
                return _maybe_tg_notify_plan("rename_tag", new_mid, msg)
            if m is not None:
                m["consumed"] = True  # one-shot
        return await _rename_tag_impl(old_name, new_name,
                                      merged=bool(allow_merge and will_merge))
    except Exception as e:
        logger.error(f"Error in rename_tag: {e}")
        return f"Error renaming tag: {str(e)}"


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
    return f"Tag '{old_name}' renamed to '{new_name}' (проверено){note}."


# Конфликт слияния 2026-08-06, разрешён сохранением ОБЕИХ сторон: эта ветка
# добавляла сюда `_rename_tag_impl` (исполнитель для кнопки), а пришедшая из
# #12 — `_describe_delete_tag` (описатель плана удаления тега). Обе нужны и
# друг с другом никак не связаны — совпало только место вставки, сразу после
# `rename_tag`.
def _describe_delete_tag(p: Dict) -> str:
    return f'Удаляю тег «{p.get("name")}»'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        name: Tag name (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        manifest_id: from call #1's response — pass on call #2 to actually delete
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"name": name}
    outcome = _gate_single("delete_tag", "delete_tag",
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
        logger.error(f"Error in delete_tag: {e}")
        return f"Error deleting tag: {str(e)}"




# ---------------------------------------------------------------------------
# Won't-do / duplicate (v2)
# ---------------------------------------------------------------------------

def _describe_abandon_task(p: Dict) -> str:
    return p.get("summary") or (
        f'Отмечаю «не буду делать» задачу «{p.get("task_title") or p.get("task_id")}»')


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's), e.g. «Отмечаю «не буду делать»
    задачу „Купить молоко"».

    Args:
        summary: Human-readable confirmation line (see above)
        task_id: ID of the task
        task_title: Title of the task (optional but recommended)
        manifest_id: from call #1's response — pass on call #2 to actually mark it
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"summary": summary, "task_id": task_id, "task_title": task_title}
    outcome = _gate_single("abandon_task", "abandon_task",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_abandon_task,
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
    if g.status == "unavailable":
        return g.message
    if g.status == "mismatch":
        return (f"🛑 НЕ отметил — id это «{g.title}», а НЕ «{task_title}». "
                "Ничего не тронул.")
    if g.status == "missing":
        return (f"🛑 НЕ отметил — «{title}» не среди открытых задач "
                "(завершена/удалена/неверный id). Ничего не тронул.")
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
        logger.error(f"Error in abandon_task: {e}")
        return f"Error abandoning task: {str(e)}"


def _describe_duplicate_task(p: Dict) -> str:
    return p.get("summary") or f'Дублирую задачу «{p.get("task_title") or p.get("task_id")}»'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the summary you show the user (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's), e.g. «Дублирую задачу „Купить молоко"».

    Args:
        summary: Human-readable confirmation line (see above)
        task_id: ID of the task
        task_title: Title of the task (optional but recommended for confirmation)
        manifest_id: from call #1's response — pass on call #2 to actually duplicate
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"summary": summary, "task_id": task_id, "task_title": task_title}
    outcome = _gate_single("duplicate_task", "duplicate_task",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_duplicate_task,
                           automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _duplicate_task_impl(**outcome.extra)


async def _duplicate_task_impl(summary: str, task_id: str, task_title: str = None) -> str:
    """Pure mutation logic for duplicate_task — no consent gate. Called only
    by the gated duplicate_task() above once the plan is approved."""
    title = task_title or _lookup_task_title(task_id)
    g = _guard_task(task_id, task_title or "")
    if g.status == "unavailable":
        return g.message
    if g.status == "mismatch":
        return (f"🛑 НЕ дублировал — id это «{g.title}», а НЕ «{task_title}». "
                "Ничего не тронул.")
    if g.status == "missing":
        return (f"🛑 НЕ дублировал — «{title}» не среди открытых задач "
                "(завершена/удалена/неверный id). Ничего не тронул.")
    try:
        copy = await _run_blocking(lambda: ticktick_v2.duplicate_task(task_id))
        cid = copy.get("id")
        rid = _op_journal("create", [
            {"taskId": cid, "title": copy.get("title") or title,
             "expect": {"projectId": copy.get("projectId")}}],
            summary)
        # Post-verify: the copy must actually exist in fresh open state.
        fresh = _open_by_id(fresh=True)
        if fresh is None:
            verdict = f"Дублирование отправлено, но {_UNVERIFIED_MSG}"
        elif cid not in fresh:
            verdict = ("❌ Копия НЕ подтвердилась — её нет среди открытых "
                       "задач, проверь вручную.")
        else:
            verdict = (f"✅ Дублировано (проверено): «{title}» → копия "
                       f"«{copy.get('title') or title}»")
        return (verdict + "\n⚠️ В копию НЕ переносятся: чек-лист (items), "
                "kanban-раздел (column) и привязка к родителю.\n"
                + _report_line(rid))
    except Exception as e:
        logger.error(f"Error in duplicate_task: {e}")
        return f"Error duplicating task: {str(e)}"


# ---------------------------------------------------------------------------
# Comment edit/delete (v2)
# ---------------------------------------------------------------------------

def _describe_update_task_comment(p: Dict) -> str:
    return f'Правлю комментарий на «{p.get("task_title")}»: новый текст «{p.get("text")}»'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        task_title: Title of the task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        text: New comment text
        project_id: ID of the project
        task_id: ID of the task
        comment_id: ID of the comment to edit
        manifest_id: from call #1's response — pass on call #2 to actually edit
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"task_title": task_title, "text": text, "project_id": project_id,
              "task_id": task_id, "comment_id": comment_id}
    outcome = _gate_single("update_task_comment", "update_task_comment",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_update_task_comment,
                           automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _update_task_comment_impl(**outcome.extra)


async def _update_task_comment_impl(task_title: str, text: str, project_id: str,
                                    task_id: str, comment_id: str) -> str:
    """Pure mutation logic for update_task_comment — no consent gate. Called
    only by the gated update_task_comment() above once the plan is approved."""
    try:
        g = _guard_task(task_id, task_title or "", project_id)
        if g.status == "unavailable":
            return g.message
        if g.status == "mismatch":
            return (f"🛑 НЕ изменил комментарий — id это «{g.title}», а НЕ "
                    f"«{task_title}». Ничего не тронул.")
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
        warn = ("\n⚠️ id не среди открытых задач — название НЕ проверено."
                if g.status == "missing" else "")
        return f"Comment on '{task_title}' updated (проверено).{warn}"
    except Exception as e:
        logger.error(f"Error in update_task_comment: {e}")
        return f"Error updating comment: {str(e)}"


def _describe_delete_task_comment(p: Dict) -> str:
    return f'Удаляю комментарий на «{p.get("task_title")}»'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        task_title: Title of the task (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        project_id: ID of the project
        task_id: ID of the task
        comment_id: ID of the comment to delete
        manifest_id: from call #1's response — pass on call #2 to actually delete
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"task_title": task_title, "project_id": project_id,
              "task_id": task_id, "comment_id": comment_id}
    outcome = _gate_single("delete_task_comment", "delete_task_comment",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_delete_task_comment,
                           automation_key=automation_key)
    if not outcome.proceed:
        return outcome.message
    return await _delete_task_comment_impl(**outcome.extra)


async def _delete_task_comment_impl(task_title: str, project_id: str,
                                    task_id: str, comment_id: str) -> str:
    """Pure mutation logic for delete_task_comment — no consent gate. Called
    only by the gated delete_task_comment() above once the plan is approved."""
    try:
        g = _guard_task(task_id, task_title or "", project_id)
        if g.status == "unavailable":
            return g.message
        if g.status == "mismatch":
            return (f"🛑 НЕ удалил комментарий — id это «{g.title}», а НЕ "
                    f"«{task_title}». Ничего не тронул.")
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
        warn = ("\n⚠️ id не среди открытых задач — название НЕ проверено."
                if g.status == "missing" else "")
        return f"Comment on '{task_title}' deleted (проверено).{warn}"
    except Exception as e:
        logger.error(f"Error in delete_task_comment: {e}")
        return f"Error deleting comment: {str(e)}"


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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        project_name: Current name of the project (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        project_id: ID of the project
        name: New name (optional)
        color: New color hex like '#F18181' (optional)
        view_mode: 'list', 'kanban', or 'timeline' (optional)
        manifest_id: from call #1's response — pass on call #2 to actually update
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty
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
    params = {"project_name": project_name, "project_id": project_id,
              "name": name, "color": color, "view_mode": view_mode}
    outcome = _gate_single("update_project", "update_project",
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
    refuse = _guard_project(project_id, project_name, fresh=True,
                            require_known=True)
    if refuse:
        return refuse
    try:
        proj = await _run_blocking(lambda: ticktick.update_project(
            project_id, name=name, color=color, view_mode=view_mode))
        if 'error' in proj:
            return f"### ❌ Проект «{project_name}» НЕ обновлён\n\nTickTick отклонил: {proj['error']}"
    except Exception as e:
        logger.error(f"Error in update_project: {e}")
        return f"### ❌ Проект «{project_name}» НЕ обновлён\n\nОшибка: {str(e)}"

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
                f"⚠️ {_UNVERIFIED_MSG} ({e})")


def _describe_archive_project(p: Dict) -> str:
    verb = "Архивирую" if p.get("archived", True) else "Разархивирую"
    return f'{verb} проект «{p.get("project_name")}»'


@mcp.tool()
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

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Args:
        project_name: Name of the project (shown first in the summary you show the user) (there is no server-side confirmation dialog — printing this and getting the user's genuine "yes" is YOUR job, not the server's)
        project_id: ID of the project
        archived: True to archive, False to restore it to active
        manifest_id: from call #1's response — pass on call #2 to actually archive
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"project_name": project_name, "project_id": project_id,
              "archived": archived}
    outcome = _gate_single("archive_project", "archive_project",
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
        refuse = _guard_project(project_id, project_name, fresh=True,
                                require_known=True)
        if refuse:
            return refuse
    else:
        refuse = _guard_project(project_id, project_name, fresh=True)
        if refuse:
            return refuse
    live_name = _v2_project_names().get(project_id, project_name)
    verb = 'заархивирован' if archived else 'разархивирован'
    try:
        await _run_blocking(lambda: ticktick_v2.archive_project(project_id, closed=archived))
    except RuntimeError as e:
        return f"### ❌ Проект «{live_name}» НЕ {verb}\n\nTickTick отклонил: {e}"
    except Exception as e:
        logger.error(f"Error in archive_project: {e}")
        return f"Error archiving project: {str(e)}"

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
                f"⚠️ {_UNVERIFIED_MSG} ({e})")


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
      that field is present) and stop after 150 fetches (COMMENT_FETCH_CAP),
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
        logger.error(f"Error in search_all_tasks: {e}")
        return f"Error searching tasks: {str(e)}"


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
            return (f"Task {task_id} not found among open tasks "
                    "(it may be completed or in the trash).")

        pr = PRIORITY_MAP.get(t.get("priority", 0))
        status = {0: "Active", 2: "Completed", -1: "Won't do"}.get(t.get("status", 0), t.get("status"))
        creator = str(t.get("creator", ""))
        who = "you" if creator == owner else f"user {creator}"

        out = f"Task: {t.get('title')}\n"
        out += f"  id: {t.get('id')}  |  project: {names.get(t.get('projectId'), t.get('projectId'))}\n"
        out += f"  status: {status}  |  priority: {pr}\n"
        if t.get("parentId"):
            all_tasks = state.get("syncTaskBean", {}).get("update", []) or []
            parent = next((x for x in all_tasks if x.get("id") == t["parentId"]), None)
            pname = parent.get("title") if parent else t["parentId"]
            out += f"  parent: «{pname}»  (id:{t['parentId']})\n"
        if t.get("startDate"):
            sd = t["startDate"][:10] if t.get("isAllDay") else t["startDate"]
            out += f"  start: {sd}\n"
        if t.get("dueDate"):
            d = t["dueDate"][:10] if t.get("isAllDay") else t["dueDate"]
            out += f"  due: {d}{'  (all-day)' if t.get('isAllDay') else ''}\n"
        repeat = t.get("repeatFlag") or t.get("repeatRule")
        if repeat:
            out += f"  repeat: {repeat}\n"
        reminders = t.get("reminders") or []
        if reminders:
            out += f"  reminders: {', '.join(str(r) for r in reminders)}\n"
        if t.get("assignee"):
            out += f"  assignee: {t['assignee']}\n"
        if t.get("tags"):
            out += f"  tags: {', '.join('#'+x for x in t['tags'])}\n"
        if t.get("columnId"):
            out += f"  columnId: {t['columnId']}\n"
        content = t.get("content") or t.get("desc") or ""
        if content:
            out += f"  content: {content[:300]}\n"
        # Activity (no full edit-log endpoint exists; these are the task's stamps)
        out += "\nActivity:\n"
        out += f"  created: {t.get('createdTime', '?')} by {who}\n"
        out += f"  last modified: {t.get('modifiedTime', '?')}\n"
        if t.get("completedTime"):
            out += f"  completed: {t['completedTime']}\n"
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
        logger.error(f"Error in get_task_info: {e}")
        return f"Error fetching task info: {str(e)}"


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
        who = "you" if creator == owner else (f"user {creator}" if creator else "?")
        lines = [f"  created: {t.get('createdTime', '?')}  by {who}"]
        if t.get("modifiedTime"):
            lines.append(f"  last modified: {t['modifiedTime']}")
        if t.get("completedTime"):
            lines.append(f"  completed: {t['completedTime']}")
        return "\n".join(lines)
    except Exception:
        return None


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

        ACTION_LABELS = {
            "T_TITLE":   "renamed",
            "T_CONTENT": "edited description",
            "T_DUE":     "changed due date",
            "T_MOVE":    "moved to another list",
            "T_PARENT":  "changed parent/subtask",
            "T_CREATE":  "created",
            "T_COMPLETE":"completed",
            "T_DELETE":  "deleted",
            "T_PRIORITY":"changed priority",
            "T_TAG":     "changed tags",
        }

        out = f"Activity log ({len(events)} events):\n\n"
        for e in events:
            action = e.get("action", "?")
            when = (e.get("when") or "?")[:19].replace("T", " ")
            who = e.get("whoProfile", {})
            actor = "you" if who.get("isMyself") else who.get("displayName") or "someone"
            channel = e.get("deviceChannel", "")
            label = ACTION_LABELS.get(action, action)

            line = f"  {when}  {actor} {label}"
            if action == "T_TITLE" and e.get("title"):
                line += f' → "{e["title"]}"'
            elif action == "T_DUE":
                before = (e.get("dueDateBefore") or "")[:10] or "none"
                after = (e.get("dueDate") or "")[:10] or "none"
                line += f"  {before} → {after}"
                if e.get("isAllDay"):
                    line += " (all-day)"
            elif action == "T_MOVE":
                line += f"  {e.get('fromProjectId', '?')} → {e.get('toProjectId', '?')}"
            elif action == "T_CONTENT" and e.get("content"):
                snippet = str(e["content"])[:80].replace("\n", " ")
                line += f'  "{snippet}…"' if len(str(e["content"])) > 80 else f'  "{snippet}"'
            if channel:
                line += f"  [{channel}]"
            out += line + "\n"
        return out
    except Exception as e:
        logger.error(f"Error in get_task_activity: {e}")
        msg = f"Error fetching task activity: {str(e)}"
        if "404" in str(e):
            fallback = await _run_blocking(lambda: _task_activity_fallback(task_id))
            if fallback:
                msg += ("\n\nThis task's activity endpoint 404d — falling back "
                        "to what's on the task record itself:\n" + fallback)
        return msg


@mcp.tool(annotations=READONLY)
async def get_changes(since: str, until: str = None,
                      project_id: str = None) -> str:
    """
    Audit feed: everything that changed across the account in a date range —
    what was CREATED, COMPLETED, DELETED, and MODIFIED (requires v2 API).

    Use this to answer "what happened to my tasks yesterday / last week" —
    e.g. find tasks that disappeared (deleted) or got moved/edited. For the
    exact per-task history (who renamed it, which list it moved from→to, and
    WHO did it on shared lists) drill into a specific task with get_task_activity.

    Dates are matched at day granularity in UTC; a task completed late at night
    local time may land on the next UTC day.

    Args:
        since: Start date YYYY-MM-DD (inclusive)
        until: End date YYYY-MM-DD (inclusive; defaults to today)
        project_id: Optional — limit the feed to one list/project
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
            return names.get(pid, pid or "?")

        open_tasks = await _run_blocking(lambda: ticktick_v2.get_open_tasks())
        completed = await _run_blocking(lambda: ticktick_v2.get_completed_tasks(
            limit=100, from_str=since + " 00:00:00", to_str=until + " 23:59:59"))
        trash = await _run_blocking(lambda: ticktick_v2.get_trash(limit=300))

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
        header = f"Изменения с {since} по {until} ({len(events)}):\n\n"
        body = "\n".join(f"{icon} {line}" for _, icon, line in events)
        note = ("\n\nℹ️ Для точной истории конкретной задачи (кто/куда перенёс, "
                "что переименовал) используй get_task_activity.")
        return header + body + note
    except Exception as e:
        logger.error(f"Error in get_changes: {e}")
        return f"Error fetching changes: {str(e)}"


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
        logger.error(f"Error in get_project_members: {e}")
        return f"Error fetching project members: {str(e)}"


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
        logger.error(f"Error in get_tasks_by_assignee: {e}")
        return f"Error fetching tasks by assignee: {str(e)}"


@mcp.tool(annotations=READONLY)
async def list_project_columns(project_id: str) -> str:
    """
    List the kanban columns/sections of a project, with their IDs (uses the
    official API). Use a column id as column_id in create_task/update_task.

    Args:
        project_id: ID of the project
    """
    err = _ensure_official()
    if err:
        return err
    try:
        data = await _run_blocking(lambda: ticktick.get_project_with_data(project_id))
        if 'error' in data:
            return f"Error fetching project: {data['error']}"
        cols = data.get("columns", []) or []
        if not cols:
            return ("This project has no kanban columns (it may be a list-view "
                    "project). Switch its view to kanban to use sections.")
        cols = sorted(cols, key=lambda x: x.get("sortOrder", 0))
        return f"Columns of project {project_id} ({len(cols)}):\n" + "\n".join(
            f"- {col.get('name', '?')}  (id: {col.get('id')})" for col in cols)
    except Exception as e:
        logger.error(f"Error in list_project_columns: {e}")
        return f"Error fetching columns: {str(e)}"


def _describe_create_project_column(p: Dict) -> str:
    dest = p.get("project_name") or p.get("project_id")
    return f'Создаю раздел (колонку) «{p.get("name")}» в проекте «{dest}»'


@mcp.tool()
async def create_project_column(project_id: str, name: str,
                                project_name: str = "",
                                manifest_id: str = "", user_reply: str = "",
                                automation_key: str = "") -> str:
    """
    Create a kanban column/section inside a project (including the Inbox) and
    return its id (requires v2 API). Use the returned id as column_id in
    create_task/update_task to route tasks into this section. Gated 🟡
    (docs/DESIGN_approval_gate.md): two calls, same tool name — nothing is
    created on call #1.

    Call #1 (manifest_id omitted): builds a one-shot manifest and returns a
    preview — nothing is created yet. Call #2 (after the user actually
    replied): repeat the call with manifest_id=<id from call #1> and
    user_reply=<the user's literal last message> — the other arguments are
    ignored on this call (the manifest's own stored values are used). Do NOT
    make call #2 in the same turn as call #1.

    automation_key (call #2 only) is ONLY for headless automation clients
    (bots/pipelines): they pass their own connection secret to prove they are
    automation, which bypasses the interactive user_reply requirement.
    ⛔ INTERACTIVE ASSISTANTS: do NOT try to fill automation_key — you don't
    know it and guessing is a protocol violation.

    Sections only render in a project's kanban view; switch the project's view
    to kanban to see them.

    Args:
        project_id: ID of the project (or the Inbox id from get_projects)
        name: Name of the new column/section
        project_name: Name of the project (recommended — arms the identity
            guard so a stale/wrong project_id is refused instead of silently
            creating the column elsewhere)
        manifest_id: from call #1's response — pass on call #2 to actually create
        user_reply: the user's literal reply approving the plan — required on call #2
        automation_key: headless-automation only — bypasses user_reply on call #2 (see above); interactive assistants leave this empty

    Telegram approval layer (when it is enabled on this server): the plan
    built by call #1 is also sent to the owner as a Telegram message with
    ✅/🛑 buttons, and pressing ✅ makes the SERVER run the operation itself —
    this tool is NOT called a second time, and the result is written back
    into that same message. While that is in effect the TEXT path is CLOSED:
    call #2 is refused whatever `user_reply` says — before the press (wait for
    it) and after it too (the server is already running the operation). Do not
    retry it; just tell the user to tap the button.
    """
    err = _ensure_ready()
    if err:
        return err
    params = {"project_id": project_id, "name": name, "project_name": project_name}
    outcome = _gate_single("create_project_column", "create_project_column",
                           params if not manifest_id else None,
                           manifest_id, user_reply, _describe_create_project_column,
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
    refuse = _guard_project(project_id, project_name or "", fresh=True,
                            require_known=True)
    if refuse:
        return refuse
    live_pname = _v2_project_names().get(project_id, project_id)
    try:
        cid = await _run_blocking(lambda: ticktick_v2.create_column(project_id, name))
    except RuntimeError as e:
        return f"### ❌ Раздел «{name}» НЕ создан\n\nTickTick отклонил: {e}"
    except Exception as e:
        logger.error(f"Error in create_project_column: {e}")
        return f"Error creating column: {str(e)}"

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
                f"⚠️ {_UNVERIFIED_MSG} ({e})")


# ---------------------------------------------------------------------------
# TG auto-execute: registry + candidate finder + background poller
# (2026-08-05) — Python port of gmail-mcp's autoExecute.ts/http.ts
# runAutoExecutePoller. See tg_approval.py's block comment above
# try_auto_execute() for the architectural difference (in-memory _MANIFESTS
# here vs. one shared Postgres on the TS side) and why the candidate search
# below cannot be one SQL JOIN: the manifests simply aren't in Postgres. It is
# still ONE round-trip per pass, not one per manifest — the RAM walk collects
# the live ids, and tg_approval.get_tg_approvals() reads all their approval
# rows in a single `WHERE manifest_id = ANY(...)` query (2026-08-06).
# ---------------------------------------------------------------------------

class _AutoExecutorEntry:
    __slots__ = ("rehash", "execute")

    def __init__(self, rehash, execute):
        self.rehash = rehash    # Callable[[Dict], str]
        self.execute = execute  # async Callable[[str, Dict], Awaitable[str]]


# Registry keyed by the SAME `tool` string used at the manifest's
# _maybe_tg_notify_plan(tool, ...) call site (which is also what
# TG_APPROVAL_TOOLS allowlists against) — NOT by manifest "kind" directly,
# though in practice each kind maps to exactly one tool today (see
# _AUTO_EXECUTE_TOOL_FOR_KIND below). Populated once at import time, mirroring
# gmail-mcp's autoExecute.ts registerAutoExecutor() module-level calls.
_AUTO_EXECUTORS: Dict[str, _AutoExecutorEntry] = {}


def _register_auto_executor(tool: str, rehash, execute) -> None:
    _AUTO_EXECUTORS[tool] = _AutoExecutorEntry(rehash, execute)


def _rehash_delete_manifest(m: Dict) -> str:
    ids = [it["taskId"] for it in (m.get("items") or [])]
    return _manifest_object_hash("delete", ids)


async def _auto_execute_delete_tasks(manifest_id: str, m: Dict) -> str:
    return await _execute_task_deletion_impl(manifest_id, m)


def _rehash_declutter_manifest(m: Dict) -> str:
    return _manifest_object_hash("declutter", _dc_object_ids(m.get("actions") or {}))


async def _auto_execute_declutter(manifest_id: str, m: Dict) -> str:
    if m.get("persist") == "sheet":
        return await _execute_declutter_from_sheet(manifest_id)
    return await _execute_declutter_ram_impl(manifest_id, m)


def _rehash_create_manifest(m: Dict) -> str:
    """Пересчёт binding-хэша манифеста создания при нажатии кнопки. Зовёт
    РОВНО ту же `_create_object_hash`, что и plan_task_creation, — не
    «повторяет формулу», а буквально ту же функцию, поэтому побайтовое
    совпадение гарантировано конструкцией, а не аккуратностью."""
    return _create_object_hash(m.get("raw") or [])


async def _auto_execute_create_tasks(manifest_id: str, m: Dict) -> str:
    # Страховка от чужого манифеста с таким же kind: этот исполнитель умеет
    # распаковывать ТОЛЬКО форму, которую кладёт plan_task_creation
    # (summary + raw). Манифест на этот момент уже погашен вызывающим, так
    # что «отказ» здесь — это отказ ИСПОЛНЯТЬ, а не тихое исполнение не того.
    if m.get("_gate") != "create" or "raw" not in m:
        return ("🛑 Автоисполнение отменено: манифест не в формате плана "
                "создания задач (kind=create, но нет raw/_gate) — ничего не "
                "создано. Построй план заново через plan_task_creation.")
    return await _create_tasks_impl(m.get("summary") or "", m.get("raw") or [])


def _rehash_delete_project_manifest(m: Dict) -> str:
    """Та же формула, что в фазе плана delete_project (см. её `object_hash`)."""
    return _manifest_object_hash("delete_project", [m.get("project_id") or ""])


async def _auto_execute_delete_project(manifest_id: str, m: Dict) -> str:
    """Исполнение плана удаления ПРОЕКТА по нажатой кнопке (2026-08-06).

    Содержимое проекта перечитывается ЗАНОВО: журнал должен отражать то, что
    удаляется сейчас, а не снимок часовой давности. Не смогли прочитать —
    отказ, а не удаление вслепую (та же дисциплина, что на чат-пути)."""
    project_id = m.get("project_id") or ""
    name = m.get("project_name") or ""
    if not project_id:
        return ("🛑 Автоисполнение отменено: в манифесте нет id проекта — "
                "ничего не удалено. Построй план заново.")
    try:
        data = await _run_blocking(lambda: ticktick.get_project_with_data(project_id))
    except Exception as e:
        return (f"🛑 Автоисполнение отменено: не смог прочитать содержимое "
                f"проекта «{name}» ({e}) — не удаляю вслепую. Ничего не "
                "изменено, построй план заново.")
    if isinstance(data, dict) and data.get("error"):
        return (f"🛑 Автоисполнение отменено: не смог прочитать содержимое "
                f"проекта «{name}» ({data['error']}) — не удаляю вслепую. "
                "Ничего не изменено, построй план заново.")
    return await _delete_project_impl(project_id, name,
                                      (data or {}).get("tasks") or [])


def _rehash_rename_tag_manifest(m: Dict) -> str:
    """Та же формула, что в merge-ветке rename_tag (её `object_hash`)."""
    return _manifest_object_hash(
        "rename_tag_merge",
        [(m.get("old_name") or "").lower(), (m.get("new_name") or "").lower()])


async def _auto_execute_rename_tag(manifest_id: str, m: Dict) -> str:
    """Исполнение СЛИЯНИЯ тегов по нажатой кнопке (2026-08-06). Кнопка
    существует только у merge-ветки — обычное переименование гейта не имеет и
    манифеста не заводит, поэтому `merged=True` здесь всегда верно."""
    old_name, new_name = m.get("old_name") or "", m.get("new_name") or ""
    if not (old_name and new_name):
        return ("🛑 Автоисполнение отменено: в манифесте нет пары имён тегов — "
                "ничего не изменено. Построй план заново.")
    return await _rename_tag_impl(old_name, new_name, merged=True)


_register_auto_executor("delete_tasks", _rehash_delete_manifest, _auto_execute_delete_tasks)
_register_auto_executor("create_tasks", _rehash_create_manifest, _auto_execute_create_tasks)
# 2026-08-06 (button-only): без этих двух регистраций нажатие кнопки на плане
# удаления ПРОЕКТА и на плане СЛИЯНИЯ тегов не приводило ни к чему — поллеру
# нечего было позвать, а с закрытым текстовым путём операция стала бы
# неисполнимой вообще. Их манифесты строятся своим путём (естественный ключ +
# `_find_live_inline_manifest`), поэтому generic-исполнитель их не подхватывает
# (он умеет только `_gate` in batch/single) — нужна явная регистрация.
_register_auto_executor("delete_project", _rehash_delete_project_manifest,
                        _auto_execute_delete_project)
_register_auto_executor("rename_tag", _rehash_rename_tag_manifest,
                        _auto_execute_rename_tag)
# DISABLED 2026-08-04/05 together with the @mcp.tool() decorators above — the
# TG-button auto-execute poller (_tg_auto_execute_tick) dispatches through
# THIS registry directly, bypassing the MCP tool layer entirely. Commenting
# out only the decorators (first pass) left this path live: pressing the
# button on an already-computed declutter manifest would still have executed
# it for real. Both layers must stay disabled together.
# _register_auto_executor("execute_declutter", _rehash_declutter_manifest, _auto_execute_declutter)
# resume_declutter deliberately has NO separate registry entry: every
# sheet-persist declutter plan is tagged tool="execute_declutter" at the ONE
# place its manifest is created (plan_declutter, via _maybe_tg_notify_plan) —
# resume_declutter reuses that SAME manifest_id/tg_approvals row later, it
# never creates its own. So a RAM-manifest candidate for a declutter plan is
# always found and dispatched under "execute_declutter" above, regardless of
# which of the two tools a human would eventually call. The one case this
# poller genuinely cannot reach is the scenario resume_declutter itself
# exists for — the RAM pointer is GONE (process restarted between plan and
# button-press): there is nothing in _MANIFESTS left to scan, by definition.
# That gap is structural (a RAM-only candidate list can't see state that was
# never durable), not a missing registration, and is called out in full in
# the handoff report rather than worked around here.

# Maps a manifest's "kind" to the tool name it was created/tagged under, for
# the ONE spot below that needs to go from "found a _MANIFESTS entry" to
# "which _AUTO_EXECUTORS key" without re-deriving it from scratch. Extend
# this (and _AUTO_EXECUTORS) together when a future kind gets TG wiring.
# NOTE: since 2026-08-06 this is only the FALLBACK — _gate_batch/_gate_single
# manifests carry their own "tool" key, so they need no entry here at all. The
# three kinds below are the ones whose manifests are built OUTSIDE those two
# gates (plan_task_deletion / plan_task_creation / plan_declutter), so they
# still resolve through this map; "create" additionally carries tool=
# "create_tasks" on the manifest itself, making this entry belt-and-braces.
_AUTO_EXECUTE_TOOL_FOR_KIND = {"delete": "delete_tasks", "declutter": "execute_declutter",
                               "create": "create_tasks"}


# ───────────────── generic executor for the _gate_batch/_gate_single tools ──
# 2026-08-06: instead of 19 hand-written _register_auto_executor() calls (one
# per gated tool, each a copy of the same two lines and each a chance to get
# the argument order wrong), ONE pair of functions covers every manifest that
# _gate_batch/_gate_single created. That is possible because those two gates
# store everything needed to replay the call: `tool` (→ the `_<tool>_impl`
# function that the gated tool itself calls on the chat path — the SAME
# executor, not a second implementation) and `_gate` (→ the call shape).

async def _generic_gate_auto_execute(manifest_id: str, m: Dict) -> str:
    """Replays a _gate_batch/_gate_single plan through the tool's own
    `_<tool>_impl`, exactly as the gated tool would have after a chat «да»."""
    tool = m.get("tool") or m.get("_tg_tool") or ""
    impl = globals().get(f"_{tool}_impl")
    if not callable(impl):
        raise RuntimeError(f"нет исполнителя _{tool}_impl для манифеста {manifest_id}")
    if m.get("_gate") == "single":
        return await impl(**(m.get("params") or {}))
    return await impl(m.get("summary") or "", m.get("tasks") or [],
                      **(m.get("extra") or {}))


def _generic_gate_rehash(m: Dict) -> str:
    """MUST reproduce, byte for byte, the formula the hash was computed with
    at plan time (_gate_batch: _manifest_object_hash(kind, ids) over the same
    id-extraction expression; _gate_single: _manifest_params_hash(kind,
    params)) — a mismatch here silently disables the binding check, i.e. the
    button's protection against the plan drifting between show and press."""
    if m.get("_gate") == "single":
        return _manifest_params_hash(m.get("kind") or "", m.get("params") or {})
    ids = [str(t.get("taskId") or t.get("task_id") or "") for t in (m.get("tasks") or [])]
    return _manifest_object_hash(m.get("kind") or "", ids)


_GENERIC_GATE_ENTRY = _AutoExecutorEntry(_generic_gate_rehash,
                                         _generic_gate_auto_execute)


def _resolve_auto_executor(tool: str, m: Dict) -> Optional[_AutoExecutorEntry]:
    """Which executor runs this manifest: an explicitly registered one wins
    (delete_tasks today — its manifest shape predates the shared gates), the
    generic gate executor covers anything _gate_batch/_gate_single produced,
    and everything else returns None (candidate skipped, nothing happens).
    Deliberately does NOT resolve declutter: its registration stays commented
    out above, and a declutter manifest has no `_gate` key, so it falls
    through to None here too — both layers stay disabled together."""
    if not tool:
        return None
    entry = _AUTO_EXECUTORS.get(tool)
    if entry is not None:
        return entry
    if m.get("_gate") in ("batch", "single") and callable(
            globals().get(f"_{tool}_impl")):
        return _GENERIC_GATE_ENTRY
    return None


def _auto_execute_tool_of(m: Dict) -> str:
    """Tool name a live manifest belongs to: the TG notification's own tag
    first (set by _maybe_tg_notify_plan, the one place a button ever appears),
    then the gate's stored `tool`, then the legacy kind→tool map."""
    return (m.get("_tg_tool") or m.get("tool")
            or _AUTO_EXECUTE_TOOL_FOR_KIND.get(m.get("kind") or "") or "")


def _tg_auto_execute_pending() -> List[tuple]:
    """RAM-часть поиска кандидатов: живые (не consumed, не просроченные)
    манифесты, у которых есть авто-исполнитель и включён TG-гейт. НИ ОДНОГО
    обращения к базе и к сети — специально, потому что это единственный кусок,
    который трогает `_MANIFESTS`, и он обязан выполняться в event loop'е
    (иначе `_prune_manifests()` итерировал бы dict, который другая корутина
    в это же время пополняет → RuntimeError: dictionary changed size).
    Возвращает [(manifest_id, tool), …]."""
    _prune_manifests()
    out: List[tuple] = []
    for mid, m in list(_MANIFESTS.items()):
        if m.get("consumed"):
            continue
        tool = _auto_execute_tool_of(m)
        if _resolve_auto_executor(tool, m) is None:
            continue
        if not tg_approval.enabled_for(_TG_CFG, tool):
            continue
        out.append((mid, tool))
    return out


def _tg_auto_execute_approved(pending: List[tuple],
                              rows: Dict[str, dict]) -> List[Dict]:
    """Чистая (без БД и сети) вторая половина поиска: из списка живых
    манифестов и УЖЕ прочитанных пачкой строк tg_approvals оставляет те, что
    подтверждены кнопкой. Статус считает tg_approval.approval_status_of —
    та же формула, что у одиночного check_approval на чат-пути."""
    out: List[Dict] = []
    for mid, tool in pending:
        row = rows.get(mid)
        if not row or tg_approval.approval_status_of(row) != "approved":
            continue
        out.append({"manifest_id": mid, "tool": tool,
                    "chat_id": row.get("chat_id"),
                    "message_id": row.get("message_id"),
                    # Предыдущие куски длинного плана. Нужны, чтобы после
                    # исполнения убрать их из лички: наш reaper строки
                    # APPROVED не трогает (архив решения), а уборщик gmail-mcp
                    # знает только про message_id — без этого куски 1..N-1
                    # оставались бы в личном чате навсегда.
                    "extra_message_ids": list(row.get("extra_message_ids") or [])})
    return out


def _find_tg_auto_execute_candidates() -> List[Dict]:
    """Candidates for auto-execution: this server's own (in-memory) manifests
    that are AWAITING_CONSENT-equivalent (not consumed, not TTL-expired —
    _prune_manifests already dropped anything past that) that resolve to an
    executor (_resolve_auto_executor: explicit registry, else the generic
    _gate_batch/_gate_single replay) AND whose Telegram approval row is
    already APPROVED.

    ОДНО обращение к Postgres на весь проход, сколько бы живых планов ни было
    (2026-08-06). До этого на КАЖДЫЙ живой манифест шли check_approval() и,
    для одобренных, ещё get_tg_approval() — то есть 1–2 поездки на план. С 2
    гейтованными тулами планов было единицы; с 22 их десятки, база ходит через
    публичный прокси Railway, и проход переставал укладываться в 10-секундный
    интервал — подтверждённое кнопкой действие ждало исполнения минутами.

    Синхронная версия (её зовут тесты и любой не-async вызывающий); поллер
    использует те же две половины напрямую, чтобы поездку в базу увести в
    отдельный поток и не морозить event loop (см. _tg_auto_execute_tick)."""
    pending = _tg_auto_execute_pending()
    if not pending:
        return []
    try:
        rows = tg_approval.get_tg_approvals([mid for mid, _ in pending])
    except Exception as e:
        logger.warning(f"TG auto-execute: get_tg_approvals failed: {e}")
        return []
    return _tg_auto_execute_approved(pending, rows)


# Потолок на ОДНОГО кандидата: без него один зависший сетевой вызов к TickTick
# держит очередь остальных подтверждённых планов бесконечно (манифест уже
# погашен, повтора не будет — а человек так и сидит перед несработавшей
# кнопкой). Щедрый по умолчанию: цель — не оборвать нормальную работу, а
# гарантировать, что очередь ВСЕГДА двигается.
_TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S = float(
    os.environ.get("TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S", "120"))


# ---------------------------------------------------------------------------
# Честная пост-верификация автоисполнения (2026-08-06)
# ---------------------------------------------------------------------------
# ПОЧЕМУ это вообще появилось: до этой правки поллер брал ровно то, что вернул
# исполнитель (`entry.execute(...)`), и публиковал как ИТОГ. То есть словом
# «успешно» распоряжался тот же код, который мутацию и делал, — судья и
# подсудимый в одном лице. Теперь после исполнения обязательно делается
# НЕЗАВИСИМАЯ перепроверка живым чтением (`_build_operation_report`, который
# читает журнал мутаций и заново тянет состояние TickTick через
# `_open_by_id(fresh=True)`), и вердикт выносится ПО ЕЁ ФАКТАМ, а не по
# самоотчёту исполнителя.
#
# Ключевой принцип (Максим, пункт 3 ТЗ): «не удалось доказать» ≠ «получилось».
# Поэтому вердиктов ЧЕТЫРЕ, а не два: помимо ok/failed есть "partial" (часть
# подтвердилась, часть нет) и "unverified" (мутация, возможно, прошла, но мы
# этого НЕ ДОКАЗАЛИ — журнал пуст, живое чтение недоступно, формат итога не
# распознан, исключение). "unverified" НИКОГДА не выдаётся за "ok".
#
# ЧЕСТНОЕ ОГРАНИЧЕНИЕ: `_build_operation_report` умеет выносить вердикт не для
# всех типов операций. Для незнакомого `op` его построчный `_verify_item()`
# пишет «тип … не проверяется автоматически» со статусом "warn" — такие строки
# попадают в СРЕДНИЙ счётчик итоговой строки («⚠️ N не проверено»), а не в ✅.
# Здесь это трактуется как "unverified"/"partial" (подтверждать было нечем),
# но НИКОГДА как "ok" — см. `_verdict_from_totals`.

# Итоговая строка независимого отчёта: «**Итог: ✅ 3 подтверждено, ⚠️ 1 не
# проверено, ❌ 1 расхождений.**». Разбираем регуляркой, но НЕ доверяем ей
# слепо: если формат поменяется (или строки нет вовсе), `_parse_verify_totals`
# вернёт None, и вердикт станет "unverified" — то есть поломка парсера
# деградирует в «не доказано», а не в ложное «успешно».
#
# СРЕДНИЙ СЧЁТЧИК (⚠️ «не проверено») появился в `_build_operation_report`
# вместе с переводом `_verify_item` на явный (status, line) — до этого строки
# с ⚠️ печатались, но не попадали НИ В ОДИН счётчик. Регулярка обязана знать
# ровно тот формат, который печатает `_build_operation_report`: рассинхрон
# здесь не падает и не виден в тестах с фейковым отчётом — он просто делает
# вердикт вечным "unverified" (ровно это и случилось при слиянии 2026-08-06,
# найдено независимым аудитом). Контракт закреплён тестом, который прогоняет
# НАСТОЯЩИЙ `_build_operation_report` через этот парсер.
#
# ЯКОРЬ `^\*\*Итог:` (+ re.MULTILINE) — не косметика, а защита от подделки
# вердикта через ДАННЫЕ. `_build_operation_report` печатает НАЗВАНИЯ задач
# дословно («- ❌ **«…»** — ВСЁ ЕЩЁ существует»), а названия приходят извне
# (их сочиняет модель или тянет tg-ai-assistant из чужих сообщений в чатах).
# Без якоря задача с названием «Итог: ✅ 5 подтверждено, ❌ 0 расхождений»
# попадала в текст РАНЬШЕ настоящей итоговой строки, и `search` возвращал
# ЕЁ числа — провал (❌ 1) читался как "ok". Настоящий итог всегда начинает
# свою строку с `**Итог:`, строка-буллет — с «- ».

_VERIFY_TOTALS_RE = re.compile(
    r"^\*\*Итог:\s*✅\s*(\d+)\s*подтвержден\w*\s*,"
    r"\s*⚠️?\s*(\d+)\s*не\s+проверено\s*,"
    r"\s*❌\s*(\d+)\s*расхожден\w*",
    re.MULTILINE)

# Маркеры в тексте САМОГО исполнителя (у него уже есть свой read-back).
# Явный провал: 🛑 / «Ошибка» / «НЕ удалено». Частичный успех/сомнение:
# ⚠️ / ⏭ / «не подтверждён» (последнее — из _UNVERIFIED_MSG и родственных).
_EXEC_FAILURE_MARKERS = ("🛑", "Ошибка", "ошибка при", "НЕ удалено", "не удалено")
_EXEC_WARN_MARKERS = ("⚠️", "⚠", "⏭", "не подтверждён", "не подтверждена",
                      "НЕ ПОДТВЕРЖДЁН", "не подтвердилось")

# Признаки того, что независимый отчёт НЕ является настоящей перепроверкой
# (журнала нет / записей нет / живое состояние недоступно / внутренняя
# ошибка). Все они = "unverified": мутация могла пройти, но мы не смотрели.
#
# Фразы намеренно взяты ДЛИННЫМИ и дословными из `_build_operation_report`:
# короткое «невозможен»/«нет записей» могло бы случайно совпасть с НАЗВАНИЕМ
# задачи внутри отчёта и превратить настоящее подтверждение в «не доказано».
# (Ошибка в эту сторону безопасна, но врать в другую сторону тоже не надо.)
_REPORT_UNUSABLE_MARKERS = ("Журнал не найден", "В журнале нет записей по",
                            "Живое состояние TickTick недоступно",
                            "Error building operation report")

# Страховочные признаки того, что перепроверка ЧАСТИЧНО ничего не доказала.
# Основной механизм теперь структурный — средний счётчик ⚠️ «не проверено» в
# итоговой строке (`_verify_item` возвращает явный status="warn"), поэтому
# эмодзи ⚠️/⚠ ЗДЕСЬ БОЛЬШЕ НЕТ: с трёхсчётчиковым форматом итоговая строка
# содержит «⚠️ 0 не проверено» ВСЕГДА, и маркер-эмодзи срабатывал бы на каждом
# отчёте, вечно понижая честное "ok" до "partial". Остались только текстовые
# формулировки — на случай, если какая-то ветка отчёта напишет их, минуя
# счётчик. Ложное срабатывание (фраза внутри НАЗВАНИЯ задачи) даёт "partial"
# вместо "ok" — ошибка в безопасную сторону, как и в _REPORT_UNUSABLE_MARKERS.
_REPORT_DOUBT_MARKERS = ("НЕ ПОДТВЕРЖДЁН", "не проверяется автоматически")

# Исполнитель (`_execute_task_deletion_impl` и родня) УЖЕ вклеивает
# независимый отчёт в конец СВОЕГО текста — «чтобы модель не могла его
# пропустить». Ниже тот же отчёт печатается отдельным разделом целиком, и без
# вырезания самая длинная часть сообщения уезжала в группу ДВАЖДЫ: на живом
# прогоне 2026-08-06 отчёт по 200 задачам занимал 13 сообщений вместо 9, а
# Telegram уже на 13-м подряд отвечает 429 «retry after 37» — то есть дубль
# бил не по красоте, а по доставке. Второй экземпляр вдобавок тащил в
# группу-архив инструкцию «[агенту: перепечатай…]», которой там некому
# следовать.
#
# Обрезанный текст используется и для показа, и для поиска маркеров
# исполнителя (_EXEC_FAILURE_MARKERS/_EXEC_WARN_MARKERS): самоотчёт обязан
# оцениваться по СВОИМ словам, а не по вклеенному в него отчёту — иначе «⚠️»
# и «✅» из отчёта подменяют оценку исполнителя (см. _verified_auto_execute_
# report, шаг 2).
_EXEC_TRAILING_REPORT_RE = re.compile(
    r"\n*(?:###\s*)?🧾 (?:Независимый отчёт|Отчёт по|Журнал не найден|"
    r"В журнале нет записей)[\s\S]*$")


def _strip_trailing_independent_report(text: str) -> str:
    """Убирает хвостовой блок независимого отчёта из текста исполнителя. Если
    после вырезания не осталось ничего осмысленного (весь вывод и БЫЛ этим
    отчётом), возвращает исходный текст — лучше дубль, чем пустой раздел."""
    stripped = _EXEC_TRAILING_REPORT_RE.sub("", text or "").rstrip()
    return stripped if stripped.strip() else text


_VERDICT_EMOJI = {"ok": "✅", "partial": "⚠️", "failed": "🛑", "unverified": "❓"}
_VERDICT_WORD = {"ok": "подтверждено живым чтением",
                 "partial": "подтверждено частично",
                 "failed": "не выполнено",
                 "unverified": "НЕ подтверждено (проверить не удалось)"}


def _parse_verify_totals(text: str) -> Optional[Tuple[int, int, int]]:
    """(подтверждено, не проверено, расхождений) из итоговой строки
    независимого отчёта, либо None, если строку не нашли/не распознали.
    Вынесено отдельной чистой функцией специально ради тестируемости без сети
    и БД.

    Итоговая строка обязана быть РОВНО ОДНА: ноль — формат сменился или отчёт
    не тот; больше одной — текст неоднозначен (склеенные отчёты, подделка
    через данные), и какую из них считать «настоящей», мы не знаем. Оба
    случая = None, то есть "unverified" — а не догадка в пользу успеха."""
    if not text:
        return None
    found = _VERIFY_TOTALS_RE.findall(text)
    if len(found) != 1:
        return None
    try:
        return int(found[0][0]), int(found[0][1]), int(found[0][2])
    except (TypeError, ValueError):  # pragma: no cover — регулярка даёт цифры
        return None


def _verdict_from_totals(totals: Optional[Tuple[int, int, int]]) -> str:
    """Вердикт ТОЛЬКО по фактам независимой перепроверки.

    0/0/0 — это не успех: подтверждать было нечего (пустой набор), поэтому
    "unverified". >0 расхождений при 0 подтверждённых — операция провалилась
    целиком. Смешанный итог — "partial".

    Средний счётчик (⚠️ «не проверено») — это строки, по которым живое чтение
    исхода НЕ доказало: незнакомый движку тип операции, «восстановлена, но не
    в тот список», «не смогли перечитать проекты». Они не расхождения, но и не
    подтверждения, поэтому: есть хоть одно подтверждённое рядом → "partial";
    не подтверждено вообще ничего → "unverified" (это строже, чем "partial").

    "ok" выдаётся единственным сочетанием: расхождений 0, непроверенных 0 И
    подтверждённых больше нуля."""
    if totals is None:
        return "unverified"
    ok_n, warn_n, bad_n = totals
    if ok_n == 0 and warn_n == 0 and bad_n == 0:
        return "unverified"
    if bad_n > 0 and ok_n == 0:
        return "failed"
    if bad_n > 0:
        return "partial"
    if warn_n > 0:
        return "partial" if ok_n > 0 else "unverified"
    return "ok"


def _verified_auto_execute_report(manifest_id: str, tool: str,
                                  exec_output: str) -> Tuple[str, str]:
    """Возвращает (полный_markdown_отчёта, verdict).

    verdict ∈ {"ok", "partial", "failed", "unverified"} — см. блок-комментарий
    выше. Ничего не обрезает: длину теперь держит чанкинг в tg_approval
    (`send_message_chunked`), а не молчаливое обрезание отчёта по 4096."""
    exec_output = exec_output or ""

    # 1. Независимая перепроверка. Сама `_build_operation_report` ловит свои
    #    исключения и возвращает текст, но монкейпатч/будущая правка может и
    #    бросить — ловим, чтобы автоисполнение никогда не падало на этапе
    #    «рассказать, что получилось».
    independent: Optional[str] = None
    independent_err: Optional[str] = None
    try:
        independent = _build_operation_report(manifest_id)
    except Exception as e:
        independent_err = str(e)
        logger.error(f"TG auto-execute: независимая перепроверка "
                     f"{manifest_id} упала: {e}")

    report_usable = bool(independent) and not any(
        mark in independent for mark in _REPORT_UNUSABLE_MARKERS)
    totals = _parse_verify_totals(independent) if report_usable else None

    # 2. Явный провал по самоотчёту исполнителя перебивает всё: если он сам
    #    написал «🛑 / Ошибка / НЕ удалено» и при этом НИ ОДНОГО ✅ — это
    #    провал, независимо от того, что покажет журнал.
    #
    #    Маркеры ищутся в САМООТЧЁТЕ, то есть в тексте БЕЗ хвостового
    #    независимого отчёта, который часть исполнителей вклеивает в свой
    #    вывод (`_execute_task_deletion_impl` и родня). Иначе оценка
    #    исполнителя считалась бы по чужому тексту: итоговая строка отчёта
    #    содержит «⚠️ N не проверено» ВСЕГДА, поэтому любой вклеенный отчёт
    #    поднимал `exec_warn` и вечно понижал честный "ok" до "partial", а
    #    его же «✅» гасили `exec_failed` у реального провала.
    exec_self = _strip_trailing_independent_report(exec_output)
    exec_failed = (any(mark in exec_self for mark in _EXEC_FAILURE_MARKERS)
                   and "✅" not in exec_self)
    exec_warn = any(mark in exec_self for mark in _EXEC_WARN_MARKERS)
    # Сомнение В САМОЙ ПЕРЕПРОВЕРКЕ. Строки `_verify_item` вида «создана, но
    # раздел не применился» / «проект: проверка не удалась» / «тип … не
    # проверяется автоматически» теперь считаются СТРУКТУРНО — средним
    # счётчиком ⚠️ «не проверено» в итоговой строке, и его уже учёл
    # `_verdict_from_totals`. Этот флаг остался страховкой на текстовые
    # формулировки мимо счётчика (см. _REPORT_DOUBT_MARKERS).
    report_doubt = report_usable and any(
        mark in independent for mark in _REPORT_DOUBT_MARKERS)

    if exec_failed:
        verdict = "failed"
    else:
        verdict = _verdict_from_totals(totals)
        # Даунгрейд, но НИКОГДА не апгрейд: сомнение исполнителя (⚠️/⏭/«не
        # подтверждён») ИЛИ неучтённые строки самой перепроверки понижают
        # "ok" до "partial". "unverified" при этом остаётся "unverified" —
        # оно строже "partial" (там хотя бы часть доказана, здесь не доказано
        # ничего).
        if verdict == "ok" and (exec_warn or report_doubt):
            verdict = "partial"

    # 3. Полный markdown: оба раздела целиком + честное основание вердикта.
    if independent is None:
        independent_block = (f"⚠️ Перепроверку выполнить не удалось: "
                             f"{independent_err or 'неизвестная ошибка'}")
    else:
        independent_block = independent

    if totals is not None:
        basis = (f"Основание вердикта: независимая перепроверка живым чтением — "
                 f"✅ {totals[0]} подтверждено, ⚠️ {totals[1]} не проверено, "
                 f"❌ {totals[2]} расхождений.")
        if totals[1] > 0:
            basis += (" Непроверенные пункты — это НЕ подтверждение: живое "
                      "чтение по ним исхода не доказало.")
        if report_doubt:
            basis += (" В отчёте есть формулировки «не подтверждено» вне "
                      "счётчиков — по ним исход не доказан, поэтому вердикт "
                      "понижен.")
    elif not report_usable:
        basis = ("Основание вердикта: независимую перепроверку выполнить НЕ "
                 "удалось (журнал/живое состояние недоступны) — исход операции "
                 "НЕ ПОДТВЕРЖДЁН. Это не то же самое, что «успешно».")
    else:
        basis = ("Основание вердикта: формат итоговой строки независимого "
                 "отчёта не распознан — считаем исход НЕ ПОДТВЕРЖДЁННЫМ.")
    if exec_failed:
        basis = ("Основание вердикта: исполнитель сам отрапортовал провал "
                 "(в его отчёте есть маркер ошибки и ни одного ✅). " + basis)

    full_md = "\n".join([
        f"### {_VERDICT_EMOJI.get(verdict, '❓')} Автоисполнение «{tool}» — "
        f"{_VERDICT_WORD.get(verdict, verdict)}",
        f"_manifest: `{manifest_id}`_",
        "",
        "#### Что сделал исполнитель",
        exec_self.strip() or "_(исполнитель не вернул текста)_",
        "",
        "#### Независимая перепроверка (живое чтение)",
        independent_block.strip(),
        "",
        basis,
    ])
    return full_md, verdict


def _manifest_affected_count(m: Optional[Dict]) -> Optional[int]:
    """Сколько объектов затрагивал манифест — только для КОРОТКОЙ сводки в
    личку («затронуто 5 задач»). Что не знаем — просто None, и в сводке этой
    строки не будет; врать числом здесь нельзя.

    Форм манифеста несколько, и после 2026-08-06 (кнопка у 22 тулов) их надо
    знать все, иначе строка молча пропадала бы у всех новых исполнителей:
      * `items` — план удаления (plan_task_deletion / delete_tasks);
      * `tasks` — общий пакетный гейт `_gate_batch` (complete_tasks,
        move_tasks, set_task_tags, restore_tasks…);
      * `raw`   — план создания (plan_task_creation / create_tasks);
      * `_gate == "single"` — одиночный гейт (`_gate_single`): по определению
        РОВНО один объект (create_tag, delete_project, rename_tag…), поэтому
        1 — это факт из конструкции гейта, а не догадка."""
    if not isinstance(m, dict):
        return None
    for key in ("items", "tasks", "raw"):
        value = m.get(key)
        if isinstance(value, list):
            return len(value)
    if m.get("_gate") == "single":
        return 1
    return None


def _short_auto_execute_summary(tool: str, verdict: str,
                                affected: Optional[int],
                                group_delivered: bool,
                                fallback_to_dm: bool = False,
                                partial: Optional[Tuple[int, int]] = None) -> str:
    """2-4 строки в ЛИЧКУ владельца. Максим просил не захламлять личный чат
    1:1 простынями — подробности живут в группе «MCP Отчёты», сюда идёт
    только вердикт. Если в группу отчёт НЕ доставился, сводка обязана сказать
    это вслух: молчаливая потеря подробностей — тот же самый оптимистичный
    отчёт, только в другой обёртке.

    `partial=(доставлено, всего)` — отдельный, ТРЕТИЙ исход между «дошло» и
    «не дошло»: на затяжном флуд-лимите Telegram в группу ложатся, например,
    2 части из 6. Раньше такой случай не отличался от полного успеха (список
    id ведь непустой), и владельцу писали «Подробный отчёт — в группе», хотя
    там лежала треть. Теперь это называется своими словами."""
    lines = [f"{_VERDICT_EMOJI.get(verdict, '❓')} Автоисполнение «{tool}» — "
             f"{_VERDICT_WORD.get(verdict, verdict)}."]
    if affected is not None:
        lines.append(f"Затронуто объектов: {affected}.")
    if partial is not None:
        got, total = partial
        lines.append(f"⚠️ отчёт доставлен в группу частично ({got} из {total} "
                     f"частей) — остальное не дошло, подробности в логах "
                     f"сервера.")
    elif group_delivered:
        lines.append("Подробный отчёт — в группе «MCP Отчёты».")
    elif fallback_to_dm:
        lines.append("⚠️ В группу отчёт не ушёл (проверь TG_REPORTS_CHAT_ID и "
                     "что бот в группе) — полный текст прислан сюда отдельным "
                     "сообщением.")
    else:
        lines.append("⚠️ отчёт в группу не доставлен, подробности в логах "
                     "сервера.")
    return "\n".join(lines)


def _cleanup_plan_leftovers(candidate: Dict, tool: str) -> int:
    """Удаляет из ЛИЧКИ владельца предыдущие куски длинного плана, оставшиеся
    после того, как итог вписан в последнее сообщение. Возвращает, сколько
    удалить удалось (для лога и тестов).

    Три жёстких границы, каждая — из разбора реального инцидента:
      1. НИКОГДА не трогаем последнее сообщение (`message_id`) — в нём теперь
         живёт сводка; поэтому его id явно исключается, даже если он вдруг
         продублирован в extra_message_ids.
      2. НИКОГДА не трогаем группу-архив: чат берётся ТОЛЬКО из candidate
         (личка), `report_chat_id` сюда не попадает ни при каком условии.
      3. Best-effort целиком: любая неудача — только лог. Сообщение, стёртое
         человеком руками, или старше 48 часов (Bot API их удалять не даёт) —
         это не ошибка процесса и не повод менять вердикт."""
    chat_id = candidate.get("chat_id")
    extra = candidate.get("extra_message_ids") or []
    if not chat_id or not extra:
        return 0
    keep = candidate.get("message_id")
    removed = 0
    for mid in extra:
        if mid is None or (keep is not None and mid == keep):
            continue
        try:
            if tg_approval.delete_message(_TG_CFG, str(chat_id), mid):
                removed += 1
        except Exception as e:
            logger.warning(f"TG auto-execute: не смог удалить кусок плана "
                           f"{mid} в чате {chat_id} ({tool}/"
                           f"{candidate.get('manifest_id')}): {e}")
    if removed:
        logger.info(f"TG auto-execute: убрано лишних кусков плана в личке: "
                    f"{removed} ({tool}/{candidate.get('manifest_id')})")
    return removed


def _publish_auto_execute_outcome(candidate: Dict, tool: str, full_md: str,
                                  verdict: str,
                                  affected: Optional[int]) -> None:
    """Куда уходит итог (пункты 1-2 ТЗ): ПОЛНЫЙ отчёт — в группу «MCP Отчёты»
    (архив, чанкинг внутри `post_report_to_group`), КОРОТКАЯ сводка — в то же
    личное сообщение с кнопками (`summarize_in_owner_chat` редактирует текст
    и снимает кнопки).

    Обе публикации обёрнуты в try/except, хотя по контракту обе best-effort и
    не бросают: инвариант «упавший кандидат не рушит остальных» не должен
    зависеть от чужих гарантий. Функция СИНХРОННАЯ (requests + time.sleep на
    429) и обязана вызываться через `_run_blocking` — вызов напрямую из
    корутины блокирует event loop на всё время отправки отчёта."""
    # ЧЕСТНАЯ проверка полноты доставки (2026-08-06). Раньше здесь лежал
    # `delivered: List[int]`, и успех определялся как `bool(delivered)` —
    # то есть «хоть одно сообщение дошло» читалось как «отчёт опубликован».
    # На затяжном 429 в группу ложились 2 части из 6, а владелец получал в
    # личку «Подробный отчёт — в группе «MCP Отчёты»». Теперь полнота берётся
    # из `.ok`, а частичность называется вслух.
    delivery = tg_approval.ReportDelivery([], 0, 0, False)
    try:
        delivery = tg_approval.post_report_to_group(
            _TG_CFG, candidate["manifest_id"], full_md,
            tool=tool, verdict=verdict)
    except Exception as e:
        logger.error(f"TG auto-execute: публикация отчёта в группу упала "
                     f"({tool}/{candidate['manifest_id']}): {e}")

    # Фолбэк на ЛИЧКУ, если в группу не ушло НИЧЕГО. Самый вероятный прод-
    # сценарий: TG_REPORTS_CHAT_ID вписали руками с опечаткой или бота не
    # добавили в группу — Telegram отвечает «chat not found» / «bot is not a
    # member of the ... chat», send_message_chunked честно возвращает ok=False,
    # и без этой ветки ПОЛНЫЙ отчёт исчезал бы навсегда (в логах Railway он
    # тоже не появляется — туда пишется только факт неудачи). Владелец при
    # этом видел бы «⚠️ отчёт не доставлен» и не имел бы никакого способа
    # узнать, что именно сделала мутация. Дублирования не будет: если группа
    # и есть личка, отправка уже провалилась ровно в этот чат.
    fallback_ok = False
    reports_chat = str(getattr(_TG_CFG, "reports_chat_id", "") or "")
    owner_chat = str(candidate.get("chat_id") or "")
    if not delivery.message_ids and owner_chat and reports_chat != owner_chat:
        try:
            fb = tg_approval.send_message_chunked(_TG_CFG, owner_chat, full_md)
            fallback_ok = bool(fb.ok)
            if not fallback_ok:
                logger.error(f"TG auto-execute: отчёт не удалось доставить ни в "
                             f"группу, ни в личку ({tool}/"
                             f"{candidate['manifest_id']}): {fb.error}")
        except Exception as e:
            logger.error(f"TG auto-execute: фолбэк-отправка отчёта в личку "
                         f"упала ({tool}/{candidate['manifest_id']}): {e}")

    # Частичная доставка — отдельный исход, а не «успех» и не «ничего не
    # дошло»: часть отчёта в группе ЕСТЬ, поэтому дублировать его целиком в
    # личку (фолбэк выше) не надо, но и молчать об этом нельзя.
    partial = None
    if not delivery.ok and delivery.delivered > 0:
        partial = (delivery.delivered, delivery.total_chunks)
    short_md = _short_auto_execute_summary(tool, verdict, affected,
                                           bool(delivery.ok), fallback_ok,
                                           partial)
    summary_ok = False
    try:
        summary_ok = bool(tg_approval.summarize_in_owner_chat(
            _TG_CFG, candidate["chat_id"], candidate["message_id"], short_md))
    except Exception as e:
        logger.error(f"TG auto-execute: сводка в личку не отправилась "
                     f"({tool}/{candidate['manifest_id']}): {e}")

    # Уборка «сирот» плана в ЛИЧКЕ (2026-08-06). Длинный план уходит
    # несколькими сообщениями: последнее (с кнопками) лежит в message_id и
    # именно в него вписана сводка, а куски 1..N-1 — в extra_message_ids.
    # После исполнения по кнопке строка становится APPROVED, и её не трогает
    # НИКТО: наш reaper обходит APPROVED сознательно (архив состоявшегося
    # решения), а уборщик соседнего gmail-mcp удаляет только message_id.
    # Итог — куски плана оставались в личном чате навсегда, вопреки прямому
    # требованию «не захламляя личный чат 1:1».
    #
    # Только ПОСЛЕ успешно вписанной сводки: пока итога нет, удалять контекст
    # нельзя — владелец остался бы вообще без информации о том, что случилось.
    # Строго best-effort: неудача ничего не ломает и не меняет вердикт.
    # Сообщения в ГРУППЕ-АРХИВЕ здесь не трогаются ни при каких условиях —
    # используется только chat_id кандидата (личка) и только extra-куски.
    #
    # Внешний try — не паранойя: исключение, вылетевшее ОТСЮДА, поймал бы
    # `except` в `_tg_auto_execute_tick`, и тот опубликовал бы ВТОРОЙ отчёт с
    # вердиктом «🛑 ошибка исполнения» — про операцию, которая на самом деле
    # успешно выполнена и уже отчиталась. Врать в эту сторону нельзя тем более.
    if summary_ok:
        try:
            _cleanup_plan_leftovers(candidate, tool)
        except Exception as e:
            logger.warning(f"TG auto-execute: уборка кусков плана не удалась "
                           f"({tool}/{candidate['manifest_id']}): {e}")


async def _tg_auto_execute_tick() -> None:
    """One pass: find candidates, execute each via its registered executor,
    verify the outcome INDEPENDENTLY, then publish (full report → the «MCP
    Отчёты» group, short summary → the owner's original button message).
    Mirrors gmail-mcp's runAutoExecutePoller — errors from ONE candidate never
    abort the others.

    Три вещи, за которые здесь отвечает именно ЭТА функция (2026-08-06):
    1. Поиск кандидатов разделён: RAM-часть (_tg_auto_execute_pending) идёт в
       event loop'е, а ЕДИНСТВЕННАЯ поездка в Postgres — через _run_blocking,
       то есть в отдельном потоке. psycopg2 синхронный: вызов из корутины
       напрямую морозил ВЕСЬ сервер (не только поллер — и обычные MCP-запросы
       модели) на всё время запроса.
    2. Перепроверка и публикация отчёта в Telegram (requests + сон на 429) —
       тоже через _run_blocking, по той же причине.
    3. `try_auto_execute` наоборот ОБЯЗАН остаться в event loop'е: его
       атомарность («проверил consumed → выставил consumed» без await между)
       держится ровно на однопоточности loop'а. В поток его выносить нельзя.

    Пункт 5 ТЗ («несколько параллельных подтверждений») архитектурно уже
    поддержан, и это НЕ случайность, а свойство четырёх мест сразу:
      * `_MANIFESTS` — СЛОВАРЬ манифестов, а не единственный слот: сколько
        планов настроил — столько живых записей, они друг друга не вытесняют;
      * у КАЖДОЙ строки в `tg_approvals` свой `manifest_id` (PRIMARY KEY) и
        свой `expires_at` — TTL одного подтверждения не гасит соседние;
      * этот цикл обходит ВСЕХ кандидатов, а `try/except` стоит ВНУТРИ тела
        цикла: упавший кандидат съедает своё исключение и не прерывает
        обработку следующих;
      * `_consume_manifest_for_auto_execute` — атомарный compare-and-set без
        единого `await` между проверкой `consumed` и его выставлением, так что
        на однопоточном event loop два тика физически не могут исполнить один
        манифест дважды.
    Отдельного «один pending за раз» в коде нет — при ревизии 2026-08-06 такое
    место не найдено."""
    pending = _tg_auto_execute_pending()
    if not pending:
        return
    try:
        rows = await _run_blocking(tg_approval.get_tg_approvals,
                                   [mid for mid, _ in pending])
    except Exception as e:
        logger.warning(f"TG auto-execute: get_tg_approvals failed: {e}")
        return
    for c in _tg_auto_execute_approved(pending, rows):
        mid, tool = c["manifest_id"], c["tool"]
        entry = _resolve_auto_executor(tool, _MANIFESTS.get(mid) or {})
        if entry is None:
            continue
        try:
            consumed = tg_approval.try_auto_execute(
                manifest_id=mid, tool=tool,
                get_manifest=lambda i: _MANIFESTS.get(i),
                consume_manifest=_consume_manifest_for_auto_execute,
                rehash=entry.rehash,
            )
            if consumed is None:
                continue  # race/drift/already consumed — not an error
            # Потолок на одного кандидата (main): зависший сетевой вызов не
            # должен держать очередь остальных подтверждённых планов.
            #
            # Метка манифеста на время исполнения: всё, что исполнитель
            # запишет в журнал мутаций, получит `tg_manifest = mid`, и
            # независимая перепроверка ниже найдёт эти записи по manifest_id —
            # для ЛЮБОГО инструмента, а не только для delete_tasks (см.
            # _TG_AUTO_EXECUTE_MANIFEST). Снимается в finally: протёкшая метка
            # пометила бы чужие, не связанные с кнопкой мутации.
            token = _TG_AUTO_EXECUTE_MANIFEST.set(mid)
            try:
                report_text = await asyncio.wait_for(
                    entry.execute(mid, consumed),
                    _TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S)
            finally:
                _TG_AUTO_EXECUTE_MANIFEST.reset(token)
            # Слово «успешно» больше не принадлежит исполнителю: сначала
            # независимая перепроверка, только потом вердикт (пункт 3 ТЗ).
            #
            # Оба шага СИНХРОННЫЕ и оба ходят в сеть: перепроверка перечитывает
            # живое состояние TickTick (`_open_by_id(fresh=True)`), публикация
            # шлёт до нескольких сообщений через `requests` и на 429 честно
            # спит `retry_after` секунд. Вызванные напрямую из этой корутины,
            # они держали event loop десятки секунд — на streamable-http это
            # значит зависший /health и заткнувшиеся MCP-сессии. Уносим их в
            # поток тем же `_run_blocking`, которым пользуются все остальные
            # блокирующие вызовы этого файла.
            full_md, verdict = await _run_blocking(
                _verified_auto_execute_report, mid, tool, report_text)
            await _run_blocking(
                _publish_auto_execute_outcome, c, tool, full_md, verdict,
                _manifest_affected_count(consumed))
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                msg = (f"🛑 Автоисполнение «{tool}» не уложилось в "
                       f"{_TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S:.0f} c и было "
                       "прервано. План уже погашен — проверьте состояние в "
                       "TickTick: часть операции могла успеть примениться.")
            else:
                msg = f"🛑 Ошибка при автоисполнении «{tool}»: {e}"
            logger.error(f"TG auto-execute: ошибка при исполнении "
                        f"{tool}/{mid}: {e!r}")
            try:
                # Ошибка исполнения идёт по ТОМУ ЖЕ пути: полный текст (с
                # трассировкой смысла) — в группу как "failed", короткая
                # сводка — в личку. Так у владельца не остаётся операций,
                # про которые в архиве вообще ничего нет.
                err_md = "\n".join([
                    f"### 🛑 Автоисполнение «{tool}» — ошибка исполнения",
                    f"_manifest: `{mid}`_",
                    "",
                    # `msg` уже различает обычное исключение и срабатывание
                    # потолка на кандидата (asyncio.TimeoutError) — второй
                    # случай обязан звучать иначе: план ПОГАШЕН, а операция
                    # могла успеть примениться частично. Без этой строки
                    # таймаут в архиве был бы неотличим от обычной ошибки.
                    msg,
                    "",
                    f"Исключение: `{type(e).__name__}: {e}`",
                    "",
                    "Мутация могла быть выполнена ЧАСТИЧНО — исход не "
                    "подтверждён; проверь `operation_report` по этому "
                    "manifest_id.",
                ])
                await _run_blocking(_publish_auto_execute_outcome,
                                    c, tool, err_md, "failed", None)
            except Exception:
                pass


def _consume_manifest_for_auto_execute(manifest_id: str) -> Optional[Dict]:
    """The atomic one-shot step try_auto_execute() needs (see its docstring):
    called with NO `await` between the `consumed` check and the flip, so
    under asyncio's single-threaded event loop nothing else can interleave —
    this IS the atomic compare-and-set (the Python-side equivalent of the TS
    store's `UPDATE ... WHERE status = 'AWAITING_CONSENT' ... RETURNING`)."""
    m = _MANIFESTS.get(manifest_id)
    if m is None or m.get("consumed"):
        return None
    m["consumed"] = True
    # Оставляем «надгробие» ДО исполнения: если модель следом честно позовёт
    # execute_task_deletion по тому же id, она получит внятное «уже исполнено
    # по кнопке», а не безликое «не найден/истёк» (см. _manifest_gone_msg).
    _tombstone_manifest(manifest_id, "tg_auto_executed")
    return m


_TG_AUTO_EXECUTE_INTERVAL_S = float(os.environ.get("TG_AUTO_EXECUTE_INTERVAL_S", "10"))
# Как часто чистим просроченные подтверждения (пункт 4 ТЗ: сообщение, по
# которому решения так и не приняли, УДАЛЯЕТСЯ целиком). Отдельный интервал, а
# не «на каждом тике»: тик — 10 секунд, а уборка ходит в Postgres и в Telegram
# — гонять её в 6 раз чаще, чем нужно, смысла нет.
_TG_REAP_INTERVAL_S = float(os.environ.get("TG_REAP_INTERVAL_S", "60"))


async def _tg_auto_execute_poller_loop() -> None:
    """Background loop started from main() (see _run_server() below) only
    when TG_APPROVAL_ENABLED — every ~10s (env-tunable), forever, for the
    life of the process. A single tick's exception must never kill the loop
    (Maksim would then lose auto-execute silently until the next deploy).

    Два независимых TTL — честно про то, что будет, если их развести:
      * `_MANIFEST_TTL` (3600 c) — жизнь манифеста в RAM ЭТОГО процесса;
      * `TG_APPROVAL_TTL_S` (тоже 3600 c) — `expires_at` строки в Postgres,
        по которому `check_approval` начинает отвечать "none", а reaper
        сносит сообщение.
    Они НЕ связаны кодом, совпадение значений — соглашение, а не инвариант.
    Если RAM-TTL сделать КОРОЧЕ: манифест умрёт раньше строки — кнопка в
    Telegram «сработает» (строка станет APPROVED), но исполнять будет уже
    нечего (`_find_tg_auto_execute_candidates` не найдёт манифест в
    `_MANIFESTS`), и позже reaper уберёт сообщение — операция тихо не
    произойдёт. Если RAM-TTL сделать ДЛИННЕЕ: строка протухнет первой,
    `check_approval` вернёт "none", манифест доживёт в памяти бесполезным
    грузом до `_prune_manifests`. Тот же эффект, что и при рестарте процесса
    между планом и нажатием: RAM пуста, а строка в Postgres жива.

    Интервал отсчитывается ОТ НАЧАЛА прохода (2026-08-06). Раньше `sleep`
    стоял ПЕРЕД работой, то есть реальный период = интервал + длительность
    прохода: медленный проход (десятки живых планов × поездка в базу каждый)
    молча растягивал заявленные «каждые 10 секунд» в разы. Проходы при этом
    не накладываются и сейчас: они последовательны внутри одной корутины, а
    отрицательный остаток обрезается до нуля. Уборка (reaper) считается в тот
    же бюджет прохода — она стоит перед замером `elapsed`, поэтому её время
    не добавляется к интервалу сверху."""
    logger.info(f"TG auto-execute: poller started (every {_TG_AUTO_EXECUTE_INTERVAL_S:.0f}s)")
    last_reap = time.monotonic()
    while True:
        started = time.monotonic()
        try:
            await _tg_auto_execute_tick()
        except Exception as e:
            logger.error(f"TG auto-execute poller: unhandled error: {e}")
        # Уборка — отдельным таймером по монотонным часам (не по системным:
        # перевод времени/NTP-скачок не должен ни заморозить уборку на час,
        # ни устроить её на каждом тике). Падение уборки НИКОГДА не убивает
        # поллер — иначе просроченный мусор в Telegram стоил бы владельцу
        # всего автоисполнения до следующего деплоя.
        now = time.monotonic()
        if now - last_reap >= _TG_REAP_INTERVAL_S:
            last_reap = now
            # Флаг живёт в конфиге tg_approval (env TG_REAP_ENABLED); читаем
            # через getattr, чтобы старый конфиг без этого поля не ронял
            # поллер AttributeError'ом — по умолчанию уборка включена.
            if getattr(_TG_CFG, "reap_enabled", True):
                try:
                    n = tg_approval.reap_expired(_TG_CFG)
                    if n:
                        logger.info("TG reaper: прибрано просроченных "
                                    f"подтверждений: {n}")
                except Exception as e:
                    logger.error(f"TG reaper: уборка упала (поллер продолжает "
                                 f"работать): {e}")
        elapsed = time.monotonic() - started
        if elapsed > _TG_AUTO_EXECUTE_INTERVAL_S:
            logger.warning(
                f"TG auto-execute: проход занял {elapsed:.1f} c — дольше "
                f"интервала {_TG_AUTO_EXECUTE_INTERVAL_S:.0f} c")
        await asyncio.sleep(max(0.0, _TG_AUTO_EXECUTE_INTERVAL_S - elapsed))


def main():
    """Main entry point for the MCP server."""
    if _TG_CFG.enabled:
        db_url = os.environ.get("CONSENT_DATABASE_URL", "").strip()
        if not db_url:
            raise RuntimeError(
                "TG_APPROVAL_ENABLED=true, но CONSENT_DATABASE_URL не задан — "
                "нужен общий Postgres (тот же, что у gmail/sheets/calendar/docs/"
                "drive-mcp) для таблицы tg_approvals."
            )
        tg_approval.init_store(db_url)
        logger.info("TG approval: Postgres подключен, слой активен "
                    "(server=ticktick, webhook НЕ регистрируется — владелец gmail-mcp)")

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


if __name__ == "__main__":
    main()