import asyncio
import hmac
import os
import re
import logging
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Literal, Optional
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from dotenv import load_dotenv
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .ticktick_client import TickTickClient, _normalize_date, save_token_file
from .ticktick_v2_client import TickTickV2Client

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

def initialize_client():
    global ticktick, ticktick_v2
    try:
        # Credentials come from the durable volume file first (freshest after a
        # /setup or a token refresh on a previous container), then env vars.
        load_dotenv()

        from .ticktick_client import load_token_file
        if not os.getenv("TICKTICK_ACCESS_TOKEN") and not load_token_file().get("access_token"):
            logger.error("No TICKTICK_ACCESS_TOKEN set (env or volume). "
                         "Visit /setup/<MCP_SECRET> in a browser to authorize.")
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


# --- Self-service OAuth setup (interim flow) --------------------------------
# Lets a person authorize their own TickTick account straight from the
# deployed instance, without cloning the repo or running anything locally.
#
# TickTick only accepts a redirect_uri that is pre-registered for the OAuth
# app, and every instance gets a random Railway domain — so this instance
# cannot be the OAuth redirect target itself. Instead:
#   open /setup/<MCP_SECRET>
#     -> redirect to the shared oauth-proxy's /start (its redirect_uri IS
#        pre-registered), carrying this instance's own URL + secret in `state`
#     -> person logs into TickTick there
#     -> oauth-proxy exchanges the code for tokens (holds the client_secret,
#        which never touches this instance during setup) and redirects the
#        browser back to THIS instance's /auth/accept with the tokens
#     -> /auth/accept hot-swaps the in-memory client so the server works
#        immediately, and shows the tokens so the person can paste them into
#        Railway Variables (durability across restarts — the filesystem here
#        is ephemeral).
OAUTH_PROXY_URL = os.getenv(
    "TICKTICK_OAUTH_PROXY_URL", "https://ticktick-oauth-proxy-production.up.railway.app"
).rstrip("/")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    return JSONResponse({"status": "ok", "ticktick_connected": ticktick is not None})


@mcp.custom_route("/setup/{secret}", methods=["GET"])
async def setup_start(request: Request) -> Response:
    secret = request.path_params.get("secret", "")
    if not SECRET or not hmac.compare_digest(secret, SECRET):
        return HTMLResponse(_setup_error_page("Неверная ссылка. Проверь MCP_SECRET."), status_code=403)

    # Prefer Railway's injected public domain over the client-supplied Host
    # header — otherwise a spoofed Host could make the proxy relay tokens to an
    # attacker's return_to. Fall back to the request host only for local dev.
    public_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    netloc = public_domain or request.url.netloc
    return_to = f"https://{netloc}"
    params = {"return_to": return_to, "secret": secret}
    return RedirectResponse(f"{OAUTH_PROXY_URL}/start?" + urllib.parse.urlencode(params))


@mcp.custom_route("/auth/accept", methods=["POST"])
async def setup_accept(request: Request) -> Response:
    # Tokens arrive as a POST body (auto-submitted form from oauth-proxy),
    # not query params — query strings end up in access logs and history.
    form = await request.form()
    secret = form.get("secret", "")
    access_token = form.get("access_token", "")
    refresh_token = form.get("refresh_token", "")

    if not SECRET or not hmac.compare_digest(secret, SECRET):
        return HTMLResponse(_setup_error_page("Неверная ссылка — запусти /setup заново."), status_code=403)
    if not access_token:
        return HTMLResponse(_setup_error_page("Не пришёл токен от прокси."), status_code=400)

    # Persist to the durable volume file FIRST so the tokens survive a restart
    # even if this process dies right after; then hot-swap the in-memory client
    # so the server is usable immediately with no redeploy.
    persisted = save_token_file({"access_token": access_token, "refresh_token": refresh_token})
    os.environ["TICKTICK_ACCESS_TOKEN"] = access_token
    if refresh_token:
        os.environ["TICKTICK_REFRESH_TOKEN"] = refresh_token
    initialize_client()

    return HTMLResponse(_setup_success_page(access_token, refresh_token, persisted))


def _setup_success_page(access_token: str, refresh_token: str, persisted: bool = False) -> str:
    if persisted:
        subtitle = ("Сервер уже работает — проверяй в Claude прямо сейчас. "
                    "Токены сохранены надёжно и переживут перезапуск, ничего "
                    "больше делать не нужно.")
        tokens_html = ""
        step_html = ('<div class="step"><strong>Готово.</strong> Можно закрыть эту '
                     'страницу и вернуться в Claude. Если нужны расширенные функции '
                     '(теги, привычки, корзина) — добавь куку v2 из Chrome позже.</div>')
    else:
        subtitle = ("Сервер уже работает — можно проверять в Claude прямо сейчас. "
                    "Но том для постоянного хранения недоступен, поэтому сохрани "
                    "токены в Railway Variables вручную, иначе они пропадут при перезапуске.")
        tokens_html = f"""
  <div class="token-block">
    <label>TICKTICK_ACCESS_TOKEN</label>
    <div class="token-row">
      <div class="token-value" id="at">{access_token}</div>
      <button onclick="copy('at', this)">Копировать</button>
    </div>
  </div>
  <div class="token-block">
    <label>TICKTICK_REFRESH_TOKEN</label>
    <div class="token-row">
      <div class="token-value" id="rt">{refresh_token}</div>
      <button onclick="copy('rt', this)">Копировать</button>
    </div>
  </div>"""
        step_html = ('<div class="step"><strong>Дальше:</strong> Railway → твой сервис → '
                     'Variables → вставь оба значения в TICKTICK_ACCESS_TOKEN и '
                     'TICKTICK_REFRESH_TOKEN.</div>')
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TickTick подключён</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f5; min-height: 100vh; padding: 40px 16px; }}
  .card {{ background: white; border-radius: 12px; max-width: 640px;
           margin: 0 auto; padding: 40px; box-shadow: 0 2px 16px rgba(0,0,0,.08); }}
  h1 {{ font-size: 22px; color: #1a1a1a; margin-bottom: 8px; }}
  .subtitle {{ color: #666; font-size: 15px; margin-bottom: 32px; line-height: 1.5; }}
  .token-block {{ margin-bottom: 24px; }}
  label {{ display: block; font-size: 13px; font-weight: 600; color: #444;
           margin-bottom: 6px; }}
  .token-row {{ display: flex; gap: 8px; }}
  .token-value {{ flex: 1; font-family: monospace; font-size: 12px;
                  background: #f8f8f8; border: 1px solid #e0e0e0;
                  border-radius: 8px; padding: 10px 12px; word-break: break-all;
                  color: #333; max-height: 80px; overflow-y: auto; }}
  button {{ flex-shrink: 0; background: #0066ff; color: white; border: none;
            border-radius: 8px; padding: 0 16px; font-size: 14px; cursor: pointer;
            height: 40px; align-self: flex-start; }}
  button.copied {{ background: #22a55a; }}
  .step {{ background: #f0f7ff; border-radius: 8px; padding: 16px 20px;
           margin-top: 28px; font-size: 14px; color: #1a4a8a; line-height: 1.6; }}
</style>
</head>
<body>
<div class="card">
  <h1>✅ TickTick подключён</h1>
  <p class="subtitle">{subtitle}</p>
  {tokens_html}
  {step_html}
</div>
<script>
function copy(id, btn) {{
  navigator.clipboard.writeText(document.getElementById(id).textContent.trim());
  const orig = btn.textContent;
  btn.textContent = "Скопировано ✓";
  btn.classList.add("copied");
  setTimeout(() => {{ btn.textContent = orig; btn.classList.remove("copied"); }}, 2000);
}}
</script>
</body>
</html>"""


def _setup_error_page(detail: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Ошибка авторизации</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #f5f5f5;
         min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 16px; }}
  .card {{ background: white; border-radius: 12px; max-width: 520px; width: 100%;
           padding: 40px; box-shadow: 0 2px 16px rgba(0,0,0,.08); text-align: center; }}
  h1 {{ color: #c0392b; margin-bottom: 12px; }}
  p {{ color: #555; font-size: 14px; margin-bottom: 8px; }}
  code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
</style>
</head>
<body>
<div class="card">
  <h1>Что-то пошло не так</h1>
  <p>Детали: <code>{detail}</code></p>
</div>
</body>
</html>"""

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


def _flatten_task_tree(node: Dict, project_id: str, parent_id: str = None,
                       level: int = 0, max_level: int = 3):
    """Recursively flatten a nested task tree.
    Returns (tasks, relations) where:
      tasks     — list of v2 task objects WITHOUT parentId (TickTick ignores it in batch/task)
      relations — list of {"parentId","taskId","projectId"} for batch/taskParent call
    IDs are pre-generated so both calls can be built before any HTTP request.
    max_level=3 means task + 3 levels of nesting (4 levels total)."""
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
    tasks: List[Dict[str, Any]]
) -> str:
    """
    Create one or more tasks in TickTick with full nested subtask support
    (up to 4 levels: task → subtask → sub-subtask → sub-sub-subtask).

    summary (FIRST arg): one-line human sentence IN THE USER'S LANGUAGE shown
    at the TOP of the confirmation dialog, e.g.
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
      subtasks (list of strings OR list of full task objects — recursive, up to 3 levels deep)

    Dates: use "YYYY-MM-DD" for all-day; full ISO "YYYY-MM-DDThh:mm:ss+0000"
    only when the user specified an exact time. Do NOT invent a time.

    ── Examples ──

    Simple batch:
      [{"title": "A", "project_id": "x"},
       {"title": "B", "project_id": "x", "priority": 5, "due_date": "2026-07-10"}]

    Nested structure (strings):
      [{"title": "Epic", "project_id": "x",
        "subtasks": ["Step 1", "Step 2", "Step 3"]}]

    Nested structure with full params (up to 4 levels):
      [{"title": "Q3 Launch", "project_id": "x", "priority": 5,
        "subtasks": [
          {"title": "Design", "due_date": "2026-07-15", "priority": 3,
           "subtasks": [
             {"title": "Mockups", "due_date": "2026-07-10",
              "subtasks": [{"title": "Mobile screens"}]}
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
    """
    err = _ensure_official()
    if err:
        return err

    if not tasks:
        return "No tasks provided."

    created = []
    failed = []

    for i, t in enumerate(tasks):
        title = t.get("title")
        project_id = t.get("project_id") or t.get("projectId")
        if not title or not project_id:
            failed.append(f"#{i+1}: missing title or project_id")
            continue
        priority = t.get("priority", 0)
        if priority not in [0, 1, 3, 5]:
            failed.append(f"#{i+1} «{title}»: неверный приоритет")
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
                await _run_blocking(lambda: ticktick_v2.batch_create_tasks(tasks_flat))
                if relations:
                    await _run_blocking(lambda: ticktick_v2._request(
                        "POST", "/batch/taskParent", json=relations))
                await _run_blocking(lambda: ticktick_v2.invalidate_cache())
                root_id = tasks_flat[0]["id"]
                if t.get("column_id"):
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_column(root_id, t["column_id"]))
                    except Exception as e:
                        logger.warning(f"Column failed: {e}")
                total = len(tasks_flat)
                line = f"✓ «{title}» + {total - 1} подзадач (дерево, {total} всего)"
                if root_id:
                    line += f" (id:{root_id})"
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

            if ticktick_v2 and task_id:
                if t.get("tags"):
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_tags(task_id, t["tags"]))
                    except Exception as e:
                        logger.warning(f"Tagging failed: {e}")
                if t.get("assignee") is not None:
                    try:
                        await _run_blocking(lambda: ticktick_v2.batch_update_tasks(
                            [{"taskId": task_id, "assignee": t["assignee"]}]))
                    except Exception as e:
                        logger.warning(f"Assignee failed: {e}")
                if t.get("column_id"):
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_column(task_id, t["column_id"]))
                    except Exception as e:
                        logger.warning(f"Column failed: {e}")
                if t.get("parent_id"):
                    try:
                        await _run_blocking(lambda: ticktick_v2.batch_set_task_parent(
                            [task_id], t["parent_id"], project_id))
                    except Exception as e:
                        logger.warning(f"Parent link failed: {e}")

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
                    await _run_blocking(lambda: ticktick_v2.batch_create_tasks(all_sub_tasks))
                    if all_sub_rels:
                        await _run_blocking(lambda: ticktick_v2._request(
                            "POST", "/batch/taskParent", json=all_sub_rels))
                    await _run_blocking(lambda: ticktick_v2.invalidate_cache())
                    sub_count = len(all_sub_tasks)
                except Exception as e:
                    logger.warning(f"Batch subtasks failed: {e}")

            line = f"✓ «{title}»"
            if sub_count:
                line += f" + {sub_count} подзадач"
            if task_id:
                line += f" (id:{task_id})"
            created.append(line)

        except Exception as e:
            failed.append(f"#{i+1} «{title}»: {e}")

    parts = []
    if created:
        parts.append(f"Создано {len(created)}:\n" + "\n".join(created))
    if failed:
        parts.append(f"Ошибки ({len(failed)}):\n" + "\n".join(failed))
    return "\n\n".join(parts)

@mcp.tool()
async def update_tasks(
    summary: str,
    tasks: List[Dict[str, Any]]
) -> str:
    """
    Update one or more tasks in TickTick.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog, e.g. «Меняю задачу „Оплатить аренду":
    срок 2026-07-01, приоритет высокий» or «Меняю срок у 3 задач на 2026-07-05».
    Mention only what actually changes.

    Each item identifies a task and carries the fields to update. For a single
    task, use a one-element list. For multiple tasks, all items are processed in
    one call via v2 batch (limited fields). For a single task with advanced fields
    (repeat_flag, reminders, column_id), the official API is used.

    IMPORTANT: always include the task's current title in each item (as "title")
    so the user knows which task is being changed.

    Supported fields per item:
      taskId (required), projectId (required for single/advanced),
      title (current title, for the dialog), new_title, content,
      start_date ("YYYY-MM-DD" = all-day; full ISO only if time given),
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
        tasks: List of task change objects — one item for a single task
    """
    err = _ensure_official()
    if err:
        return err

    has_advanced = any(t.get("repeat_flag") or t.get("reminders") or t.get("column_id")
                       for t in tasks)

    if len(tasks) == 1 or has_advanced:
        results = []
        for t in tasks:
            tid = t.get("taskId") or t.get("task_id")
            pid = t.get("projectId") or t.get("project_id") or ""
            shown_title = t.get("title") or _lookup_task_title(tid)
            new_title = t.get("new_title")
            priority = t.get("priority")
            if priority is not None and priority not in [0, 1, 3, 5]:
                results.append(f"✗ «{shown_title}»: неверный приоритет (допустимо 0/1/3/5)")
                continue
            try:
                pid = _resolve_project_id(tid, pid)
                task = await _run_blocking(
                    ticktick.update_task,
                    task_id=tid,
                    project_id=pid,
                    title=new_title,
                    content=t.get("content"),
                    start_date=t.get("start_date"),
                    due_date=t.get("due_date"),
                    priority=priority,
                    repeat_flag=t.get("repeat_flag"),
                    reminders=t.get("reminders"),
                )
                if 'error' in task:
                    results.append(f"✗ «{shown_title}»: {task['error']}")
                    continue
                if t.get("tags") is not None and ticktick_v2:
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_tags(tid, t["tags"]))
                    except Exception as e:
                        logger.warning(f"Updated but tagging failed: {e}")
                if t.get("column_id") and ticktick_v2:
                    try:
                        await _run_blocking(lambda: ticktick_v2.set_task_column(tid, t["column_id"]))
                    except Exception as e:
                        logger.warning(f"Updated but column assignment failed: {e}")
                if t.get("assignee") is not None and ticktick_v2:
                    try:
                        await _run_blocking(lambda: ticktick_v2.batch_update_tasks([{"taskId": tid, "assignee": t["assignee"]}]))
                    except Exception as e:
                        logger.warning(f"Updated but assignee failed: {e}")
                results.append(f"✏️ «{shown_title}» обновлено")
            except Exception as e:
                results.append(f"✗ «{shown_title}»: {e}")
        return "\n".join(results)

    # Multiple tasks, no advanced fields — use v2 batch
    err = _ensure_ready()
    if err:
        return err
    try:
        changes = []
        labels = []
        for t in tasks:
            tid = t.get("taskId") or t.get("task_id")
            labels.append(t.get("title") or _lookup_task_title(tid))
            ch = {"taskId": tid}
            if t.get("new_title") is not None:
                ch["title"] = t["new_title"]
            if t.get("content") is not None:
                ch["content"] = t["content"]
            if t.get("priority") is not None:
                ch["priority"] = t["priority"]
            if t.get("tags") is not None:
                ch["tags"] = t["tags"]
            if t.get("assignee") is not None:
                ch["assignee"] = t["assignee"]
            for src, dst in (("due_date", "dueDate"), ("start_date", "startDate")):
                if t.get(src):
                    val, all_day = _normalize_date(t[src])
                    ch[dst] = val
                    if all_day:
                        ch["isAllDay"] = True
            changes.append(ch)
        await _run_blocking(lambda: ticktick_v2.batch_update_tasks(changes))
        labels_str = ", ".join(f"«{lbl}»" for lbl in labels)
        return f"✏️ Обновлено {len(changes)}: {labels_str}"
    except Exception as e:
        logger.error(f"Error in update_tasks: {e}")
        return f"Error updating tasks: {str(e)}"

@mcp.tool()
async def complete_tasks(summary: str, tasks: List[Dict[str, str]]) -> str:
    """
    Mark one or more tasks as complete in one call.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog, e.g. «Завершаю задачу „Купить молоко"
    в проекте „Покупки"» or «Завершаю 4 задачи».

    Put the human title inside each task object so the dialog shows what's
    being completed: [{"title": "Buy milk", "taskId": "abc", "projectId": "xyz"}].
    project_name is optional but nice to have for a single task.

    For a single task: [{"title": "...", "taskId": "...", "projectId": "...",
                         "projectName": "..."}]
    For multiple: [{"title": "...", "taskId": "..."}, ...]

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title","taskId","projectId"} objects — one item for single task
    """
    err = _ensure_official()
    if err:
        return err
    try:
        if ticktick_v2 and len(tasks) > 1:
            ids = [t.get("taskId") or t.get("task_id") for t in tasks]
            titles = [t.get("title") or _lookup_task_title(i) for t, i in zip(tasks, ids)]
            await _run_blocking(lambda: ticktick_v2.batch_complete_tasks(ids))
            titles_str = ", ".join(f"«{t}»" for t in titles)
            return f"✓ Завершено {len(ids)}: {titles_str}"
        else:
            results = []
            for t in tasks:
                tid = t.get("taskId") or t.get("task_id")
                pid = t.get("projectId") or t.get("project_id") or ""
                title = t.get("title") or _lookup_task_title(tid)
                pid = _resolve_project_id(tid, pid)
                pname = t.get("projectName") or _v2_project_names().get(pid, "")
                res = await _run_blocking(lambda: ticktick.complete_task(pid, tid))
                if 'error' in res:
                    results.append(f"✗ «{title}»: {res['error']}")
                else:
                    where = f" в «{pname}»" if pname else ""
                    results.append(f"✓ «{title}»{where}")
            return "\n".join(results)
    except Exception as e:
        logger.error(f"Error in complete_tasks: {e}")
        return f"Error completing tasks: {str(e)}"

@mcp.tool()
async def delete_tasks(summary: str, tasks: List[Dict[str, str]]) -> str:
    """
    ⚠️ Delete one or more tasks permanently in one call.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog. Destructive — START WITH ⚠️, e.g.
    «⚠️ Удаляю задачу „Купить молоко" из „Покупки"» or
    «⚠️ Удаляю 5 задач из „Inbox"».

    Put the human title and project name INSIDE each task object so the dialog
    shows what's being deleted:
    [{"title": "Buy milk", "projectName": "Groceries", "taskId": "abc",
      "projectId": "xyz"}]

    Args:
        summary: Human-readable confirmation line starting with ⚠️ (see above)
        tasks: List of {"title","projectName","taskId","projectId"} objects
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        items = [{"taskId": t.get("taskId") or t.get("task_id"),
                  "projectId": t.get("projectId") or t.get("project_id")} for t in tasks]
        titles = ([t.get("title") for t in tasks] if all(t.get("title") for t in tasks)
                  else [_lookup_task_title(i["taskId"]) for i in items])
        await _run_blocking(lambda: ticktick_v2.batch_delete_tasks(items))
        titles_str = ", ".join(f"«{t}»" for t in titles)
        return f"🗑 Удалено {len(items)}: {titles_str}"
    except Exception as e:
        logger.error(f"Error in delete_tasks: {e}")
        return f"Error deleting tasks: {str(e)}"

@mcp.tool()
async def delete_task_with_subtasks(
    summary: str,
    task_id: str,
    project_id: str,
    task_title: str = None,
    project_name: str = None,
) -> str:
    """
    ⚠️ Delete a parent task AND all its subtasks in one go.

    Finds every subtask whose parentId matches task_id, deletes them via
    batch delete, then deletes the parent itself.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog. Destructive — START WITH ⚠️ and say
    it takes the subtasks too, e.g. «⚠️ Удаляю задачу „Проект X" вместе с её
    подзадачами из проекта „Работа"».

    Args:
        summary: Human-readable confirmation line starting with ⚠️ (see above)
        task_id: ID of the parent task
        project_id: ID of the project
        task_title: Title of the parent task (optional, auto-looked-up)
        project_name: Name of the list the task is in (for the dialog)
    """
    err = _ensure_ready()
    if err:
        return err

    title = task_title or _lookup_task_title(task_id)
    try:
        project_id = _resolve_project_id(task_id, project_id)

        # Find subtasks from v2 cache (_ensure_ready guarantees ticktick_v2)
        subtasks = []
        try:
            all_open = await _run_blocking(lambda: ticktick_v2.get_open_tasks())
            subtasks = [t for t in all_open if t.get("parentId") == task_id]
        except Exception:
            pass

        # Delete subtasks first (batch)
        if subtasks:
            items = [{"taskId": t["id"], "projectId": t.get("projectId", project_id)} for t in subtasks]
            await _run_blocking(lambda: ticktick_v2.batch_delete_tasks(items))

        # Delete parent via official API
        result = await _run_blocking(lambda: ticktick.delete_task(project_id, task_id))
        if 'error' in result:
            return f"Error deleting parent task: {result['error']}"

        pname = project_name or _v2_project_names().get(project_id, "")
        where = f" from '{pname}'" if pname else ""
        if subtasks:
            sub_titles = ", ".join(f"«{t.get('title', t['id'][:8])}»" for t in subtasks)
            return f"🗑 Удалено «{title}»{where} + {len(subtasks)} подзадач: {sub_titles}"
        return f"🗑 Удалено «{title}»{where} (подзадач нет)"
    except Exception as e:
        logger.error(f"Error in delete_task_with_subtasks: {e}")
        return f"Error deleting task with subtasks: {str(e)}"


@mcp.tool()
async def create_project(
    name: str,
    color: str = "#F18181",
    view_mode: str = "list"
) -> str:
    """
    Create a new project in TickTick.
    
    Args:
        name: Project name
        color: Color code (hex format) (optional)
        view_mode: View mode - one of list, kanban, or timeline (optional)
    """
    err = _ensure_official()
    if err:
        return err
    
    # Validate view_mode
    if view_mode not in ["list", "kanban", "timeline"]:
        return "Invalid view_mode. Must be one of: list, kanban, timeline."
    
    try:
        project = await _run_blocking(
            ticktick.create_project,
            name=name,
            color=color,
            view_mode=view_mode
        )
        
        if 'error' in project:
            return f"Error creating project: {project['error']}"
        
        return "Project created successfully:\n\n" + format_project(project)
    except Exception as e:
        logger.error(f"Error in create_project: {e}")
        return f"Error creating project: {str(e)}"

@mcp.tool()
async def delete_project(project_name: str, project_id: str) -> str:
    """
    Delete a project permanently.

    Args:
        project_name: Name of the project (shown first in confirmation dialog)
        project_id: ID of the project
    """
    err = _ensure_official()
    if err:
        return err
    
    try:
        result = await _run_blocking(lambda: ticktick.delete_project(project_id))
        if 'error' in result:
            return f"Error deleting project: {result['error']}"
        
        return f"Project '{project_name}' deleted successfully."
    except Exception as e:
        logger.error(f"Error in delete_project: {e}")
        return f"Error deleting project: {str(e)}"
    

### Improved Task MCP Tools

# Helper Functions

# User's local timezone. Date comparisons for "today"/"overdue"/"due in N days"
# happen in this zone, not UTC, so an all-day task stored at local-midnight
# isn't off-by-one. Matches USER_TIMEZONE used by the client's date handling.
_USER_TZ = ZoneInfo(os.getenv("USER_TIMEZONE", "UTC"))


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
    """Return the task's due date as a date in the user's local timezone, or
    None if there's no/unparseable due date."""
    dt = _parse_ticktick_datetime(task.get('dueDate'))
    if dt is None:
        return None
    return dt.astimezone(_USER_TZ).date()


def _today_local():
    return datetime.now(_USER_TZ).date()


def _is_task_due_today(task: Dict[str, Any]) -> bool:
    """Check if a task is due today (in the user's local timezone)."""
    d = _task_due_local_date(task)
    return d is not None and d == _today_local()

def _is_task_overdue(task: Dict[str, Any]) -> bool:
    """Check if a task is overdue."""
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
                return "Задач не найдено."
            names = _v2_project_names()
            by_project: Dict[str, list] = {}
            for t in tasks:
                pid = t.get("projectId", "")
                by_project.setdefault(pid, []).append(t)
            out = f"Все открытые задачи ({len(tasks)}):\n\n"
            for pid, ptasks in by_project.items():
                pname = names.get(pid, pid or "Inbox")
                top = [t for t in ptasks if not t.get("parentId")]
                out += f"── {pname} ({len(top)} задач) ──\n"
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
    """Get all tasks from TickTick that are due within the next 7 days. Ignores closed projects."""
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
                return f"Ошибка получения проектов: {projects['error']}"
            all_open = []
            for p in projects:
                pid = p.get("id")
                data = await _run_blocking(lambda: ticktick.get_project_with_data(pid))
                all_open.extend(data.get("tasks", []))

        tasks = [t for t in all_open if t.get("repeatFlag")]
        if search_term.strip():
            tasks = [t for t in tasks if _task_matches_search(t, search_term.strip())]

        if not tasks:
            msg = f"Повторяющихся задач, подходящих под «{search_term}», не найдено." if search_term else "Повторяющихся задач не найдено."
            return msg

        label = f"Повторяющиеся задачи{f' по запросу «{search_term}»' if search_term else ''} ({len(tasks)}):"
        return label + "\n" + format_task_tree(tasks, 200)

    except Exception as e:
        logger.error(f"Error in get_recurring_tasks: {e}")
        return f"Ошибка при получении повторяющихся задач: {str(e)}"

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

@mcp.tool()
async def create_subtask(
    parent_task_title: str,
    subtask_title: str,
    parent_task_id: str,
    project_id: str,
    content: str = None,
    priority: int = 0
) -> str:
    """
    Create a subtask for a parent task within the same project.

    Args:
        parent_task_title: Title of the parent task (shown first in confirmation dialog)
        subtask_title: Title of the new subtask
        parent_task_id: ID of the parent task
        project_id: ID of the project (must be same for both parent and subtask)
        content: Optional content/description for the subtask
        priority: Priority level (0: None, 1: Low, 3: Medium, 5: High) (optional)
    """
    err = _ensure_official()
    if err:
        return err
    
    # Validate priority
    if priority not in [0, 1, 3, 5]:
        return "Invalid priority. Must be 0 (None), 1 (Low), 3 (Medium), or 5 (High)."
    
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
        
        return "Subtask created successfully:\n\n" + format_task(subtask)
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
async def get_completed_tasks(limit: int = 50) -> str:
    """
    Get recently completed tasks across all lists (requires v2 API).

    Args:
        limit: Maximum number of completed tasks to return (default 50)
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
        return f"Tags ({len(tags)}):\n" + "\n".join(lines)
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
async def move_tasks(summary: str, tasks: List[Dict[str, str]],
                     to_project_id: str, to_project_name: str = None) -> str:
    """
    Move one or more open tasks to a destination list in one call (requires v2 API).
    All tasks go to the same destination.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog, e.g. «Перемещаю задачу „Купить молоко"
    из „Inbox" в „Покупки"» or «Перемещаю 3 задачи в „Покупки"».

    Put the human title inside each task object so the dialog shows what moves:
    [{"title": "Buy milk", "taskId": "abc"}]

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title": "...", "taskId": "..."} objects — one item for single task
        to_project_id: Destination project/list ID for ALL tasks
        to_project_name: Destination list name (shown in the dialog)
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        ids = [t.get("taskId") or t.get("task_id") for t in tasks]
        titles = [t.get("title") or _lookup_task_title(i) for t, i in zip(tasks, ids)]
        to_name = to_project_name or _v2_project_names().get(to_project_id, to_project_id)
        await _run_blocking(lambda: ticktick_v2.batch_move_tasks(ids, to_project_id))
        titles_str = ", ".join(f"«{t}»" for t in titles)
        return f"↪ Перемещено {len(ids)} → «{to_name}»: {titles_str}"
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


@mcp.tool()
async def checkin_habit(habit_name: str, habit_id: str, date: str = None,
                        status: int = 2, value: float = None) -> str:
    """
    Record a habit check-in (requires v2 API).

    Args:
        habit_name: Name of the habit (shown first in confirmation dialog — get from get_habits)
        habit_id: ID of the habit
        date: Date to check in as YYYY-MM-DD (optional; defaults to today — pass a past date to backfill)
        status: 2 = done (default), 1 = failed, 0 = not done
        value: Numeric value for quantitative habits (optional; defaults to the goal when done)
    """
    err = _ensure_ready()
    if err:
        return err
    if status not in (0, 1, 2):
        return "Invalid status. Use 2 (done), 1 (failed), or 0 (not done)."
    try:
        await _run_blocking(lambda: ticktick_v2.checkin_habit(habit_id, date=date, status=status, value=value))
        when = date or "today"
        labels = {2: "done", 1: "failed", 0: "not done"}
        return f"Habit '{habit_name}' checked in for {when} as '{labels[status]}'."
    except Exception as e:
        logger.error(f"Error in checkin_habit: {e}")
        return f"Error checking in habit: {str(e)}"


@mcp.tool(annotations=READONLY)
async def get_habit_checkins(habit_name: str, habit_id: str, after_date: str) -> str:
    """
    Get a habit's check-in history (requires v2 API).

    Args:
        habit_name: Name of the habit (shown first in confirmation dialog — get from get_habits)
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
async def set_task_parent(summary: str, tasks: List[Dict[str, str]],
                          parent_task_id: str, project_id: str,
                          parent_task_title: str = None) -> str:
    """
    Nest one or more tasks under a parent in one call (requires v2 API).
    All tasks and the parent must be in the same project.

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog, e.g. «Делаю задачу „Шаг 1"
    подзадачей „Большой проект"» or «Делаю 3 задачи подзадачами „Большой проект"».

    Put the human title inside each task object so the dialog shows what's being
    nested: [{"title": "Step 1", "taskId": "abc"}].

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title": "...", "taskId": "..."} objects — one item for single task
        parent_task_id: ID of the parent task
        project_id: ID of the project all tasks live in
        parent_task_title: Title of the parent (shown in the dialog)
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        ids = [t.get("taskId") or t.get("task_id") for t in tasks]
        titles = [t.get("title") or _lookup_task_title(i) for t, i in zip(tasks, ids)]
        pname = parent_task_title or _lookup_task_title(parent_task_id)
        await _run_blocking(lambda: ticktick_v2.batch_set_task_parent(ids, parent_task_id, project_id))
        titles_str = ", ".join(f"«{t}»" for t in titles)
        return f"🔗 Вложено {len(ids)} под «{pname}»: {titles_str}"
    except Exception as e:
        logger.error(f"Error in set_task_parent: {e}")
        return f"Error nesting tasks: {str(e)}"


@mcp.tool()
async def unset_task_parent(task_title: str, parent_task_title: str, task_id: str, parent_task_id: str, project_id: str) -> str:
    """
    Detach a subtask from its parent, making it a top-level task (requires v2 API).

    Args:
        task_title: Title of the subtask being detached (shown first in confirmation dialog)
        parent_task_title: Title of its current parent task
        task_id: ID of the subtask to detach
        parent_task_id: ID of its current parent
        project_id: ID of the project both tasks live in
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.unset_task_parent(task_id, parent_task_id, project_id))
        return f"Task '{task_title}' detached from parent '{parent_task_title}'."
    except Exception as e:
        logger.error(f"Error in unset_task_parent: {e}")
        return f"Error detaching subtask: {str(e)}"


@mcp.tool()
async def set_task_tags(summary: str, tasks: List[Dict[str, Any]]) -> str:
    """
    Replace tags on one or more tasks in one call (requires v2 API).

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog, e.g. «Ставлю тег „работа" на задачу
    „Купить молоко"» or «Ставлю тег „работа" на 4 задачи».

    Each item carries the task's human title (for the dialog) and the full
    list of tags it should have (replaces existing):
    [{"title": "Buy milk", "taskId": "abc", "tags": ["errand", "today"]}]

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"title","taskId","tags"} objects — one item for a single task
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        changes = [{"taskId": t.get("taskId") or t.get("task_id"),
                    "tags": t.get("tags") or []} for t in tasks]
        labels = [t.get("title") or _lookup_task_title(c["taskId"])
                  for t, c in zip(tasks, changes)]
        await _run_blocking(lambda: ticktick_v2.batch_update_tasks(changes))
        labels_str = ", ".join(f"«{lbl}»" for lbl in labels)
        return f"🏷 Теги обновлены у {len(changes)}: {labels_str}"
    except Exception as e:
        logger.error(f"Error in set_task_tags: {e}")
        return f"Error setting tags: {str(e)}"


# ---------------------------------------------------------------------------
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
        return f"Project groups ({len(groups)}):\n" + "\n".join(
            f"- {g.get('name','?')}  (id: {g.get('id')})" for g in groups)
    except Exception as e:
        logger.error(f"Error in list_project_groups: {e}")
        return f"Error fetching project groups: {str(e)}"


@mcp.tool()
async def create_project_group(name: str) -> str:
    """Create a project group (folder) (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        gid = await _run_blocking(lambda: ticktick_v2.create_project_group(name))
        return f"Группа проектов «{name}» создана. (id: {gid})"
    except Exception as e:
        logger.error(f"Error in create_project_group: {e}")
        return f"Error creating project group: {str(e)}"


@mcp.tool()
async def delete_project_group(group_name: str, group_id: str) -> str:
    """
    Delete a project group/folder (projects inside are kept, just ungrouped) (requires v2 API).

    Args:
        group_name: Name of the group (shown first in confirmation dialog)
        group_id: ID of the group
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.delete_project_group(group_id))
        return f"Project group '{group_name}' deleted (projects ungrouped)."
    except Exception as e:
        logger.error(f"Error in delete_project_group: {e}")
        return f"Error deleting project group: {str(e)}"


@mcp.tool()
async def move_project_to_group(project_name: str, project_id: str, group_id: str) -> str:
    """
    Move a project into a group/folder (requires v2 API).

    Args:
        project_name: Name of the project (shown first in confirmation dialog)
        project_id: ID of the project to move
        group_id: ID of the destination group, or "NONE" to ungroup
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.move_project_to_group(project_id, group_id))
        dest = "ungrouped" if group_id == "NONE" else f"group {group_id}"
        return f"Project '{project_name}' moved to {dest}."
    except Exception as e:
        logger.error(f"Error in move_project_to_group: {e}")
        return f"Error moving project: {str(e)}"


# ---------------------------------------------------------------------------
# Task comments (v2)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READONLY)
async def get_task_comments(task_title: str, project_id: str, task_id: str) -> str:
    """
    Get comments on a task (requires v2 API).

    Args:
        task_title: Title of the task (shown first in confirmation dialog)
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
        out = f"Comments on '{task_title}' ({len(comments)}):\n"
        for c in comments:
            who = (c.get("userProfile") or {}).get("displayName") or c.get("userName", "?")
            # Include the comment id — delete_task_comment/update_task_comment need it.
            out += f"- (id:{c.get('id')}) [{who}] {c.get('title','')}\n"
        return out
    except Exception as e:
        logger.error(f"Error in get_task_comments: {e}")
        return f"Error fetching comments: {str(e)}"


@mcp.tool()
async def add_task_comment(task_title: str, text: str, project_id: str, task_id: str) -> str:
    """
    Add a comment to a task (requires v2 API).

    Args:
        task_title: Title of the task (shown first in confirmation dialog)
        text: Comment text
        project_id: ID of the project
        task_id: ID of the task
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.add_task_comment(project_id, task_id, text))
        return f"Comment added to '{task_title}'."
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
    List recently deleted (trashed) tasks (requires v2 API). Use restore_task
    to bring one back.

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
async def restore_tasks(summary: str, tasks: List[Dict[str, str]],
                        to_project_id: str = None) -> str:
    """
    Restore one or more tasks from the trash in one call (requires v2 API).

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog, e.g. «Восстанавливаю из корзины
    задачу „Купить молоко"» or «Восстанавливаю из корзины 3 задачи».

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of {"taskId": "...", "title": "..."} objects — one item for
            a single task, multiple for batch. Get IDs/titles from get_trash.
        to_project_id: Optional destination list for all tasks; defaults to each
            task's original list
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        ids = [t.get("taskId") or t.get("task_id") for t in tasks]
        titles = [t.get("title") or _lookup_task_title(t.get("taskId") or t.get("task_id") or "")
                  for t in tasks]
        await _run_blocking(lambda: ticktick_v2.batch_restore_tasks(ids, to_project_id))
        titles_str = ", ".join(f"«{t}»" for t in titles)
        return f"↩ Восстановлено из корзины {len(ids)}: {titles_str}"
    except Exception as e:
        logger.error(f"Error in restore_tasks: {e}")
        return f"Error restoring tasks: {str(e)}"


@mcp.tool()
async def attach_file_to_task(task_title: str, task_id: str, project_id: str,
                              url: str = None,
                              content_base64: str = None, filename: str = None) -> str:
    """
    Attach a file to a task (requires v2 API). Provide the file either by URL
    (the server downloads it) or as base64 content — e.g. a file fetched from
    Google Drive or generated by Claude. Max 20 MB.

    Args:
        task_title: Title of the task (shown first in confirmation dialog)
        task_id: ID of the task
        project_id: ID of the task's project (auto-corrected if stale)
        url: Public/direct URL to download the file from (optional)
        content_base64: Base64-encoded file content (optional, alternative to url)
        filename: File name to store it as (optional; inferred from url if omitted)
    """
    err = _ensure_ready()
    if err:
        return err
    if not url and not content_base64:
        return "Provide either a url or content_base64 for the file."
    title = task_title or _lookup_task_title(task_id)
    try:
        pid = _resolve_project_id(task_id, project_id)
        att = await _run_blocking(lambda: ticktick_v2.upload_attachment(
            pid, task_id, url=url, content_base64=content_base64, filename=filename))
        return (f"Attached '{att.get('fileName', filename)}' "
                f"({att.get('size', '?')} bytes) to '{title}'")
    except Exception as e:
        logger.error(f"Error in attach_file_to_task: {e}")
        return f"Error attaching file: {str(e)}"


# ---------------------------------------------------------------------------
# Tag write operations (v2)
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_tag(name: str, color: str = None) -> str:
    """Create a tag (requires v2 API). color is an optional hex like '#FF6161'."""
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.create_tag(name, color))
        return f"Tag '{name}' created."
    except Exception as e:
        logger.error(f"Error in create_tag: {e}")
        return f"Error creating tag: {str(e)}"


@mcp.tool()
async def rename_tag(old_name: str, new_name: str) -> str:
    """Rename a tag (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.rename_tag(old_name, new_name))
        return f"Tag '{old_name}' renamed to '{new_name}'."
    except Exception as e:
        logger.error(f"Error in rename_tag: {e}")
        return f"Error renaming tag: {str(e)}"


@mcp.tool()
async def delete_tag(name: str) -> str:
    """Delete a tag (requires v2 API). Tasks keep existing; they just lose the tag."""
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.delete_tag(name))
        return f"Tag '{name}' deleted."
    except Exception as e:
        logger.error(f"Error in delete_tag: {e}")
        return f"Error deleting tag: {str(e)}"




# ---------------------------------------------------------------------------
# Won't-do / duplicate (v2)
# ---------------------------------------------------------------------------

@mcp.tool()
async def abandon_task(summary: str, task_id: str, task_title: str = None) -> str:
    """
    Mark a task as 'Won't do' (requires v2 API).

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog, e.g. «Отмечаю «не буду делать»
    задачу „Купить молоко"».

    Args:
        summary: Human-readable confirmation line (see above)
        task_id: ID of the task
        task_title: Title of the task (optional but recommended)
    """
    err = _ensure_ready()
    if err:
        return err
    title = task_title or _lookup_task_title(task_id)
    try:
        await _run_blocking(lambda: ticktick_v2.abandon_task(task_id))
        return f"✗ Не буду делать: «{title}»"
    except Exception as e:
        logger.error(f"Error in abandon_task: {e}")
        return f"Error abandoning task: {str(e)}"


@mcp.tool()
async def duplicate_task(summary: str, task_id: str, task_title: str = None) -> str:
    """
    Duplicate a task within the same project (requires v2 API).

    summary (FIRST arg): one-line human sentence in the user's language shown
    at the TOP of the confirmation dialog, e.g. «Дублирую задачу „Купить молоко"».

    Args:
        summary: Human-readable confirmation line (see above)
        task_id: ID of the task
        task_title: Title of the task (optional but recommended for confirmation)
    """
    err = _ensure_ready()
    if err:
        return err
    title = task_title or _lookup_task_title(task_id)
    try:
        copy = await _run_blocking(lambda: ticktick_v2.duplicate_task(task_id))
        return f"Дублировано: «{title}» → копия «{copy.get('title') or title}»"
    except Exception as e:
        logger.error(f"Error in duplicate_task: {e}")
        return f"Error duplicating task: {str(e)}"


# ---------------------------------------------------------------------------
# Comment edit/delete (v2)
# ---------------------------------------------------------------------------

@mcp.tool()
async def update_task_comment(task_title: str, text: str, project_id: str,
                              task_id: str, comment_id: str) -> str:
    """
    Edit a task comment (requires v2 API).

    Args:
        task_title: Title of the task (shown first in confirmation dialog)
        text: New comment text
        project_id: ID of the project
        task_id: ID of the task
        comment_id: ID of the comment to edit
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.update_task_comment(project_id, task_id, comment_id, text))
        return f"Comment on '{task_title}' updated."
    except Exception as e:
        logger.error(f"Error in update_task_comment: {e}")
        return f"Error updating comment: {str(e)}"


@mcp.tool()
async def delete_task_comment(task_title: str, project_id: str, task_id: str, comment_id: str) -> str:
    """
    Delete a task comment (requires v2 API).

    Args:
        task_title: Title of the task (shown first in confirmation dialog)
        project_id: ID of the project
        task_id: ID of the task
        comment_id: ID of the comment to delete
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.delete_task_comment(project_id, task_id, comment_id))
        return f"Comment on '{task_title}' deleted."
    except Exception as e:
        logger.error(f"Error in delete_task_comment: {e}")
        return f"Error deleting comment: {str(e)}"


# ---------------------------------------------------------------------------
# Project update / archive
# ---------------------------------------------------------------------------

@mcp.tool()
async def update_project(project_name: str, project_id: str, name: str = None,
                         color: str = None, view_mode: str = None) -> str:
    """
    Update a project's name, color, or view mode (uses the official API).

    Args:
        project_name: Current name of the project (shown first in confirmation dialog)
        project_id: ID of the project
        name: New name (optional)
        color: New color hex like '#F18181' (optional)
        view_mode: 'list', 'kanban', or 'timeline' (optional)
    """
    err = _ensure_official()
    if err:
        return err
    try:
        proj = await _run_blocking(lambda: ticktick.update_project(
            project_id, name=name, color=color, view_mode=view_mode))
        if 'error' in proj:
            return f"Error updating project: {proj['error']}"
        return "Project updated:\n\n" + format_project(proj)
    except Exception as e:
        logger.error(f"Error in update_project: {e}")
        return f"Error updating project: {str(e)}"


@mcp.tool()
async def archive_project(project_name: str, project_id: str, archived: bool = True) -> str:
    """
    Archive (close) or unarchive a project (requires v2 API).

    Args:
        project_name: Name of the project (shown first in confirmation dialog)
        project_id: ID of the project
        archived: True to archive, False to restore it to active
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        await _run_blocking(lambda: ticktick_v2.archive_project(project_id, closed=archived))
        return f"Project '{project_name}' {'archived' if archived else 'unarchived'}."
    except Exception as e:
        logger.error(f"Error in archive_project: {e}")
        return f"Error archiving project: {str(e)}"


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
      that field is present) and stop after a fixed number of fetches, noting in
      the output how many were scanned and whether the cap was hit. Comment hits
      are reported in their own group. Turn this on only when you specifically
      need to find a task by something written in its comments.

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
        COMMENT_FETCH_CAP = 150
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
            out += f"\nВложения ({len(attachments)}):\n"
            for a in attachments:
                name = a.get("fileName") or a.get("name") or "(без имени)"
                size = a.get("fileSize")
                size_str = f"  {size // 1024} KB" if size else ""
                url = a.get("fileUrl") or a.get("url") or ""
                out += f"  📎 {name}{size_str}"
                if url:
                    out += f"\n     {url}"
                out += "\n"
        if not items and not kids and not attachments:
            out += "\n(нет чеклистов, подзадач и вложений)\n"
        return out
    except Exception as e:
        logger.error(f"Error in get_task_info: {e}")
        return f"Error fetching task info: {str(e)}"


@mcp.tool(annotations=READONLY)
async def get_task_activity(task_id: str, project_id: str) -> str:
    """
    Get the edit-history / activity log for a task (requires v2 API).
    Shows who changed what and when: title edits, due-date changes, moves,
    content updates, parent changes, etc.

    Args:
        task_id: ID of the task
        project_id: ID of the project the task belongs to
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        events = await _run_blocking(lambda: ticktick_v2.get_task_activity(project_id, task_id))
        if not events:
            return ("No activity found for this task. "
                    "The endpoint may not be available — try providing the exact URL "
                    "from the browser Network tab (F12) when viewing task activity.")

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
        return f"Error fetching task activity: {str(e)}"


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


@mcp.tool()
async def create_project_column(project_id: str, name: str) -> str:
    """
    Create a kanban column/section inside a project (including the Inbox) and
    return its id (requires v2 API). Use the returned id as column_id in
    create_task/update_task to route tasks into this section.

    Sections only render in a project's kanban view; switch the project's view
    to kanban to see them.

    Args:
        project_id: ID of the project (or the Inbox id from get_projects)
        name: Name of the new column/section
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        cid = await _run_blocking(lambda: ticktick_v2.create_column(project_id, name))
        return f"Column «{name}» created in project {project_id}. (id: {cid})"
    except Exception as e:
        logger.error(f"Error in create_project_column: {e}")
        return f"Error creating column: {str(e)}"


def main():
    """Main entry point for the MCP server."""
    if not initialize_client():
        # Don't stop the server: on streamable-http this leaves /setup and
        # /health reachable so a person can authorize via the browser and
        # the client hot-swaps in without a redeploy. Tools that need
        # `ticktick` already lazily retry initialize_client() on first call.
        logger.warning("TickTick client not initialized yet. "
                        "Visit /setup/<MCP_SECRET> in a browser to authorize, "
                        "or set TICKTICK_ACCESS_TOKEN and restart.")

    if TRANSPORT == "streamable-http":
        logger.info(f"Starting TickTick MCP server (streamable-http) on "
                    f"http://{HOST}:{PORT}{STREAMABLE_PATH}")
        mcp.run(transport="streamable-http")
    else:
        logger.info("Starting TickTick MCP server (stdio)")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()