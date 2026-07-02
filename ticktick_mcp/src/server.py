import asyncio
import json
import os
import logging
from datetime import datetime, timezone, date, timedelta
from typing import Dict, List, Any, Optional

from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

from .ticktick_client import TickTickClient, _normalize_date
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

# Create TickTick clients
ticktick = None       # official Open API (OAuth)
ticktick_v2 = None    # unofficial v2 API (email/password), optional

def initialize_client():
    global ticktick, ticktick_v2
    try:
        # Credentials come from environment variables (.env locally, Railway
        # dashboard in production). No file write-back needed.
        load_dotenv()

        if os.getenv("TICKTICK_ACCESS_TOKEN") is None:
            logger.error("No TICKTICK_ACCESS_TOKEN set. Run 'uv run -m ticktick_mcp.cli auth' "
                         "locally, or set it in the Railway environment.")
            return False

        # Initialize the official Open API client
        ticktick = TickTickClient()
        logger.info("TickTick Open API client initialized")

        # Test API connectivity
        projects = ticktick.get_projects()
        if 'error' in projects:
            logger.error(f"Failed to access TickTick API: {projects['error']}")
            logger.error("Your access token may have expired. Re-run 'uv run -m ticktick_mcp.cli auth'.")
            return False
        logger.info(f"Connected to TickTick Open API with {len(projects)} projects")

        # Optionally initialize the unofficial v2 client (tags, completed,
        # inbox, move). Preferred auth is the browser `t` cookie via
        # TICKTICK_V2_TOKEN; username/password is a deprecated fallback.
        # Failure here is non-fatal — the Open API still works.
        candidate = TickTickV2Client()
        if candidate.enabled:
            try:
                candidate.authenticate()
                ticktick_v2 = candidate
                logger.info("TickTick v2 API enabled (tags/completed/inbox/move)")
            except Exception as e:
                ticktick_v2 = None
                logger.warning(f"v2 API unavailable, continuing with Open API only: {e}")
        else:
            logger.info("v2 API disabled (set TICKTICK_V2_TOKEN to enable)")

        # Official-API writes must drop the v2 sync cache so v2 reads stay
        # consistent (e.g. create a task via the official API, then move it).
        TickTickClient.write_hook = lambda: (
            ticktick_v2.invalidate_cache() if ticktick_v2 else None)

        return True
    except Exception as e:
        logger.error(f"Failed to initialize TickTick client: {e}")
        return False

# Format a task object from TickTick for better display
def format_task(task: Dict) -> str:
    """Format a task into a human-readable string."""
    formatted = f"ID: {task.get('id', 'No ID')}\n"
    formatted += f"Title: {task.get('title', 'No title')}\n"
    
    # Add project ID
    formatted += f"Project ID: {task.get('projectId', 'None')}\n"
    
    # Add dates if available
    if task.get('startDate'):
        formatted += f"Start Date: {task.get('startDate')}\n"
    if task.get('dueDate'):
        formatted += f"Due Date: {task.get('dueDate')}\n"
    
    # Add priority if available
    priority_map = {0: "None", 1: "Low", 3: "Medium", 5: "High"}
    priority = task.get('priority', 0)
    formatted += f"Priority: {priority_map.get(priority, str(priority))}\n"
    
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
    
    return formatted

# Format a project object from TickTick for better display
def format_project(project: Dict) -> str:
    """Format a project into a human-readable string."""
    formatted = f"Name: {project.get('name', 'No name')}\n"
    formatted += f"ID: {project.get('id', 'No ID')}\n"
    
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
    """Map projectId -> name (incl. Inbox) from the cached v2 state."""
    if not ticktick_v2:
        return {}
    try:
        st = ticktick_v2.get_state()
        names = {p["id"]: p.get("name") for p in (st.get("projectProfiles") or [])}
        if st.get("inboxId"):
            names[st["inboxId"]] = "Inbox"
        return names
    except Exception:
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
    """Render tasks as a hierarchy: subtasks indented under their parent.
    If a subtask's parent is not in this list, it appears at the top level."""
    names = _v2_project_names()
    task_ids = {t.get("id") for t in tasks if t.get("id")}
    top = [t for t in tasks if not t.get("parentId") or t.get("parentId") not in task_ids]
    children: Dict[str, List] = {}
    for t in tasks:
        pid = t.get("parentId")
        if pid and pid in task_ids:
            children.setdefault(pid, []).append(t)
    lines = []
    count = 0
    for t in top:
        if count >= limit:
            break
        lines.append(format_task_line(t, names.get(t.get("projectId"))))
        count += 1
        for kid in children.get(t.get("id") or "", []):
            if count >= limit:
                break
            lines.append("  ↳ " + format_task_line(kid))
            count += 1
    out = "\n".join(lines)
    if len(tasks) > limit:
        out += f"\n... and {len(tasks) - limit} more."
    return out


# MCP Tools

@mcp.tool()
async def get_projects() -> str:
    """Get all projects from TickTick."""
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        projects = ticktick.get_projects()
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

@mcp.tool()
async def get_project(project_id: str) -> str:
    """
    Get details about a specific project.
    
    Args:
        project_id: ID of the project
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        project = ticktick.get_project(project_id)
        if 'error' in project:
            return f"Error fetching project: {project['error']}"
        
        return format_project(project)
    except Exception as e:
        logger.error(f"Error in get_project: {e}")
        return f"Error retrieving project: {str(e)}"

@mcp.tool()
async def get_project_tasks(project_id: str) -> str:
    """
    Get all tasks in a specific project.
    
    Args:
        project_id: ID of the project
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        project_data = ticktick.get_project_with_data(project_id)
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

@mcp.tool()
async def get_task(project_id: str, task_id: str) -> str:
    """
    Get details about a specific task.
    
    Args:
        project_id: ID of the project
        task_id: ID of the task
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        task = ticktick.get_task(project_id, task_id)
        if 'error' in task:
            return f"Error fetching task: {task['error']}"
        
        return format_task(task)
    except Exception as e:
        logger.error(f"Error in get_task: {e}")
        return f"Error retrieving task: {str(e)}"

@mcp.tool()
async def create_tasks(
    summary: str,
    tasks: List[Dict[str, Any]]
) -> str:
    """
    Create one or more tasks in TickTick, optionally with subtasks.

    summary (FIRST arg): one-line human sentence IN THE USER'S LANGUAGE shown
    at the TOP of the confirmation dialog, e.g.
    «Создаю задачу „Позвонить маме" в „Личное", срок 2026-07-01, приоритет высокий»
    or «Создаю 3 задачи в „Работа"». Include date and priority when set.

    For a single task, pass a one-element list. For multiple tasks, pass all items
    at once — do NOT call this tool in a loop.

    Supported fields per item:
      title (required), project_id (required),
      content, start_date, due_date,
      priority (0=None/1=Low/3=Medium/5=High, default 0),
      repeat_flag (RRULE; use build_recurrence_rule),
      reminders (list of triggers; use build_reminder),
      is_all_day, tags (list of names; requires v2 API),
      column_id (kanban section; use list_project_columns; requires v2 API),
      subtasks (list of subtask titles; requires v2 API)

    Dates: use "YYYY-MM-DD" for all-day; full ISO "YYYY-MM-DDThh:mm:ss+0000"
    only when the user specified an exact time. Do NOT invent a time.

    Example (single): [{"title": "Buy milk", "project_id": "abc",
                         "due_date": "2026-07-05", "priority": 1}]
    Example (batch):  [{"title": "A", "project_id": "x"},
                       {"title": "B", "project_id": "x", "priority": 5}]

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of task definition objects — one item for a single task
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."

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
        try:
            task = ticktick.create_task(
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
            if t.get("tags") and ticktick_v2 and task_id:
                try:
                    ticktick_v2.set_task_tags(task_id, t["tags"])
                except Exception as e:
                    logger.warning(f"Created but tagging failed: {e}")
            if t.get("column_id") and ticktick_v2 and task_id:
                try:
                    ticktick_v2.set_task_column(task_id, t["column_id"])
                except Exception as e:
                    logger.warning(f"Created but column failed: {e}")
            sub_ok = []
            sub_fail = []
            if t.get("subtasks") and task_id:
                for st_title in t["subtasks"]:
                    try:
                        st = ticktick.create_subtask(st_title, task_id, project_id)
                        (sub_ok if 'error' not in st else sub_fail).append(st_title)
                    except Exception:
                        sub_fail.append(st_title)
            line = f"✓ «{title}»"
            if sub_ok:
                line += f" + {len(sub_ok)} подзадач"
            if sub_fail:
                line += f" (подзадачи не созданы: {', '.join(sub_fail)})"
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
      tags (replaces existing), column_id (single task only)

    Example (single): [{"title": "Pay rent", "taskId": "abc",
                         "projectId": "xyz", "due_date": "2026-07-01",
                         "priority": 5}]
    Example (batch):  [{"title": "A", "taskId": "1", "priority": 3},
                       {"title": "B", "taskId": "2", "due_date": "2026-07-05"}]

    Args:
        summary: Human-readable confirmation line (see above)
        tasks: List of task change objects — one item for a single task
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."

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
                task = ticktick.update_task(
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
                        ticktick_v2.set_task_tags(tid, t["tags"])
                    except Exception as e:
                        logger.warning(f"Updated but tagging failed: {e}")
                if t.get("column_id") and ticktick_v2:
                    try:
                        ticktick_v2.set_task_column(tid, t["column_id"])
                    except Exception as e:
                        logger.warning(f"Updated but column assignment failed: {e}")
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
            for src, dst in (("due_date", "dueDate"), ("start_date", "startDate")):
                if t.get(src):
                    val, all_day = _normalize_date(t[src])
                    ch[dst] = val
                    if all_day:
                        ch["isAllDay"] = True
            changes.append(ch)
        ticktick_v2.batch_update_tasks(changes)
        labels_str = ", ".join(f"«{l}»" for l in labels)
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
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    try:
        if ticktick_v2 and len(tasks) > 1:
            ids = [t.get("taskId") or t.get("task_id") for t in tasks]
            titles = [t.get("title") or _lookup_task_title(i) for t, i in zip(tasks, ids)]
            ticktick_v2.batch_complete_tasks(ids)
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
                res = ticktick.complete_task(pid, tid)
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
        ticktick_v2.batch_delete_tasks(items)
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

        # Find subtasks from v2 cache
        subtasks = []
        if ticktick_v2:
            try:
                all_open = ticktick_v2.get_open_tasks()
                subtasks = [t for t in all_open if t.get("parentId") == task_id]
            except Exception:
                pass

        # Delete subtasks first (batch)
        if subtasks:
            items = [{"taskId": t["id"], "projectId": t.get("projectId", project_id)} for t in subtasks]
            ticktick_v2.batch_delete_tasks(items)

        # Delete parent via official API
        result = ticktick.delete_task(project_id, task_id)
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
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    # Validate view_mode
    if view_mode not in ["list", "kanban", "timeline"]:
        return "Invalid view_mode. Must be one of: list, kanban, timeline."
    
    try:
        project = ticktick.create_project(
            name=name,
            color=color,
            view_mode=view_mode
        )
        
        if 'error' in project:
            return f"Error creating project: {project['error']}"
        
        return f"Project created successfully:\n\n" + format_project(project)
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
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        result = ticktick.delete_project(project_id)
        if 'error' in result:
            return f"Error deleting project: {result['error']}"
        
        return f"Project '{project_name}' deleted successfully."
    except Exception as e:
        logger.error(f"Error in delete_project: {e}")
        return f"Error deleting project: {str(e)}"
    

### Improved Task MCP Tools

# Helper Functions

PRIORITY_MAP = {0: "None", 1: "Low", 3: "Medium", 5: "High"}

def _is_task_due_today(task: Dict[str, Any]) -> bool:
    """Check if a task is due today."""
    due_date = task.get('dueDate')
    if not due_date:
        return False
    
    try:
        task_due_date = datetime.strptime(due_date, "%Y-%m-%dT%H:%M:%S.%f%z").date()
        today_date = datetime.now(timezone.utc).date()
        return task_due_date == today_date
    except (ValueError, TypeError):
        return False

def _is_task_overdue(task: Dict[str, Any]) -> bool:
    """Check if a task is overdue."""
    due_date = task.get('dueDate')
    if not due_date:
        return False
    
    try:
        task_due = datetime.strptime(due_date, "%Y-%m-%dT%H:%M:%S.%f%z")
        return task_due < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False

def _is_task_due_in_days(task: Dict[str, Any], days: int) -> bool:
    """Check if a task is due in exactly X days."""
    due_date = task.get('dueDate')
    if not due_date:
        return False
    
    try:
        task_due_date = datetime.strptime(due_date, "%Y-%m-%dT%H:%M:%S.%f%z").date()
        target_date = (datetime.now(timezone.utc) + timedelta(days=days)).date()
        return task_due_date == target_date
    except (ValueError, TypeError):
        return False

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

def _validate_task_data(task_data: Dict[str, Any], task_index: int) -> Optional[str]:
    """
    Validate a single task's data for batch creation.
    
    Returns:
        None if valid, error message string if invalid
    """
    # Check required fields
    if 'title' not in task_data or not task_data['title']:
        return f"Task {task_index + 1}: 'title' is required and cannot be empty"
    
    if 'project_id' not in task_data or not task_data['project_id']:
        return f"Task {task_index + 1}: 'project_id' is required and cannot be empty"
    
    # Validate priority if provided
    priority = task_data.get('priority')
    if priority is not None and priority not in [0, 1, 3, 5]:
        return f"Task {task_index + 1}: Invalid priority {priority}. Must be 0 (None), 1 (Low), 3 (Medium), or 5 (High)"
    
    # Validate dates if provided
    for date_field in ['start_date', 'due_date']:
        date_str = task_data.get(date_field)
        if date_str:
            try:
                # Try to parse the date to validate it
                # Handle both with and without timezone info
                if date_str.endswith('Z'):
                    datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                elif '+' in date_str or date_str.endswith(('00', '30')):
                    datetime.fromisoformat(date_str)
                else:
                    # Assume local timezone if no timezone specified
                    datetime.fromisoformat(date_str)
            except ValueError:
                return f"Task {task_index + 1}: Invalid {date_field} format '{date_str}'. Use ISO format: YYYY-MM-DDTHH:mm:ss or with timezone"
    
    return None

def _get_project_tasks_by_filter(projects: List[Dict], filter_func, filter_name: str) -> str:
    """
    Helper function to filter tasks across all projects.
    
    Args:
        projects: List of project dictionaries
        filter_func: Function that takes a task and returns True if it matches the filter
        filter_name: Name of the filter for output formatting
    
    Returns:
        Formatted string of filtered tasks
    """
    # Prefer the v2 open-task pool: it includes the Inbox (which the official
    # API leaves out of the project list) and is a single call instead of one
    # request per project. Falls back to official iteration when v2 is off.
    if ticktick_v2:
        try:
            state = ticktick_v2.get_state()
            inbox = state.get("inboxId")
            names = {p["id"]: p.get("name")
                     for p in (state.get("projectProfiles") or [])}
            names[inbox] = "Inbox"
            tasks = state.get("syncTaskBean", {}).get("update", []) or []
            matched = [t for t in tasks if filter_func(t)]
            if not matched:
                return f"No tasks found that are '{filter_name}'."
            out = f"Tasks that are '{filter_name}' ({len(matched)}):\n"
            return out + format_task_tree(matched)
        except Exception as e:
            logger.warning(f"v2 task pool failed, falling back to official API: {e}")

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

@mcp.tool()
async def get_all_tasks() -> str:
    """Get all tasks from TickTick. Ignores closed projects."""
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        def all_tasks_filter(task: Dict[str, Any]) -> bool:
            return True  # Include all tasks
        
        return _get_project_tasks_by_filter(projects, all_tasks_filter, "included")
        
    except Exception as e:
        logger.error(f"Error in get_all_tasks: {e}")
        return f"Error retrieving projects: {str(e)}"

@mcp.tool()
async def get_tasks_by_priority(priority_id: int) -> str:
    """
    Get all tasks from TickTick by priority. Ignores closed projects.

    Args:
        priority_id: Priority of tasks to retrieve {0: "None", 1: "Low", 3: "Medium", 5: "High"}
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    if priority_id not in PRIORITY_MAP:
        return f"Invalid priority_id. Valid values: {list(PRIORITY_MAP.keys())}"
    
    try:
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        def priority_filter(task: Dict[str, Any]) -> bool:
            return task.get('priority', 0) == priority_id
        
        priority_name = f"{PRIORITY_MAP[priority_id]} ({priority_id})"
        return _get_project_tasks_by_filter(projects, priority_filter, f"priority '{priority_name}'")
        
    except Exception as e:
        logger.error(f"Error in get_tasks_by_priority: {e}")
        return f"Error retrieving projects: {str(e)}"

@mcp.tool()
async def get_tasks_due_today() -> str:
    """Get all tasks from TickTick that are due today. Ignores closed projects."""
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        def today_filter(task: Dict[str, Any]) -> bool:
            return _is_task_due_today(task)
        
        return _get_project_tasks_by_filter(projects, today_filter, "due today")
        
    except Exception as e:
        logger.error(f"Error in get_tasks_due_today: {e}")
        return f"Error retrieving projects: {str(e)}"

@mcp.tool()
async def get_overdue_tasks() -> str:
    """Get all overdue tasks from TickTick. Ignores closed projects."""
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        def overdue_filter(task: Dict[str, Any]) -> bool:
            return _is_task_overdue(task)
        
        return _get_project_tasks_by_filter(projects, overdue_filter, "overdue")
        
    except Exception as e:
        logger.error(f"Error in get_overdue_tasks: {e}")
        return f"Error retrieving projects: {str(e)}"

@mcp.tool()
async def get_tasks_due_tomorrow() -> str:
    """Get all tasks from TickTick that are due today. Ignores closed projects."""
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        def today_filter(task: Dict[str, Any]) -> bool:
            return _is_task_due_in_days(task, 1)
        
        return _get_project_tasks_by_filter(projects, today_filter, "due today")
        
    except Exception as e:
        logger.error(f"Error in get_tasks_due_today: {e}")
        return f"Error retrieving projects: {str(e)}"
    
@mcp.tool()
async def get_tasks_due_in_days(days: int) -> str:
    """
    Get all tasks from TickTick that are due in exactly X days. Ignores closed projects.
    
    Args:
        days: Number of days from today (0 = today, 1 = tomorrow, etc.)
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    if days < 0:
        return "Days must be a non-negative integer."
    
    try:
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        def days_filter(task: Dict[str, Any]) -> bool:
            return _is_task_due_in_days(task, days)
        
        day_description = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
        return _get_project_tasks_by_filter(projects, days_filter, f"due {day_description}")
        
    except Exception as e:
        logger.error(f"Error in get_tasks_due_in_days: {e}")
        return f"Error retrieving projects: {str(e)}"

@mcp.tool()
async def get_tasks_due_this_week() -> str:
    """Get all tasks from TickTick that are due within the next 7 days. Ignores closed projects."""
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        def week_filter(task: Dict[str, Any]) -> bool:
            due_date = task.get('dueDate')
            if not due_date:
                return False
            
            try:
                task_due_date = datetime.strptime(due_date, "%Y-%m-%dT%H:%M:%S.%f%z").date()
                today = datetime.now(timezone.utc).date()
                week_from_today = today + timedelta(days=7)
                return today <= task_due_date <= week_from_today
            except (ValueError, TypeError):
                return False
        
        return _get_project_tasks_by_filter(projects, week_filter, "due this week")
        
    except Exception as e:
        logger.error(f"Error in get_tasks_due_this_week: {e}")
        return f"Error retrieving projects: {str(e)}"

@mcp.tool()
async def search_tasks(search_term: str) -> str:
    """
    Search for tasks in TickTick by title, content, or subtask titles. Ignores closed projects.
    
    Args:
        search_term: Text to search for (case-insensitive)
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    if not search_term.strip():
        return "Search term cannot be empty."

    try:
        # Prefer the v2 open-task pool: it includes the Inbox (which the
        # official API omits from the project list) and is one fast call.
        if ticktick_v2:
            tasks = [t for t in ticktick_v2.get_open_tasks()
                     if _task_matches_search(t, search_term)]
            if not tasks:
                return f"No tasks found matching '{search_term}'."
            return (f"Tasks matching '{search_term}' ({len(tasks)}):\n"
                    + format_task_tree(tasks, 100))

        # Fallback (no v2): iterate official projects — note this misses the Inbox.
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"

        def search_filter(task: Dict[str, Any]) -> bool:
            return _task_matches_search(task, search_term)

        return _get_project_tasks_by_filter(projects, search_filter, f"matching '{search_term}'")

    except Exception as e:
        logger.error(f"Error in search_tasks: {e}")
        return f"Error retrieving projects: {str(e)}"

@mcp.tool()
async def get_recurring_tasks(search_term: str = "") -> str:
    """
    Get all tasks that have a recurrence rule (repeatFlag set), i.e. repeating tasks.
    Optionally filter by title/content search term.

    Do NOT call this in a loop — it already scans all open tasks at once.

    Args:
        search_term: Optional text to further filter by title/content (case-insensitive).
                     Leave empty to return all recurring tasks.
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."

    try:
        if ticktick_v2:
            all_open = ticktick_v2.get_open_tasks()
        else:
            projects = ticktick.get_projects()
            if 'error' in projects:
                return f"Ошибка получения проектов: {projects['error']}"
            all_open = []
            for p in projects:
                pid = p.get("id")
                data = ticktick.get_project_with_data(pid)
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

@mcp.tool()
async def get_engaged_tasks() -> str:
    """
    Get all tasks from TickTick that are "Engaged".
    This includes tasks marked as high priority (5), due today or overdue.
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        def engaged_filter(task: Dict[str, Any]) -> bool:
            is_high_priority = task.get('priority', 0) == 5
            is_overdue = _is_task_overdue(task)
            is_today = _is_task_due_today(task)
            return is_high_priority or is_overdue or is_today
        
        return _get_project_tasks_by_filter(projects, engaged_filter, "engaged")
        
    except Exception as e:
        logger.error(f"Error in get_engaged_tasks: {e}")
        return f"Error retrieving projects: {str(e)}"

@mcp.tool()
async def get_next_tasks() -> str:
    """
    Get all tasks from TickTick that are "Next".
    This includes tasks marked as medium priority (3) or due tomorrow.
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    try:
        projects = ticktick.get_projects()
        if 'error' in projects:
            return f"Error fetching projects: {projects['error']}"
        
        def next_filter(task: Dict[str, Any]) -> bool:
            is_medium_priority = task.get('priority', 0) == 3
            is_due_tomorrow = _is_task_due_in_days(task, 1)
            return is_medium_priority or is_due_tomorrow
        
        return _get_project_tasks_by_filter(projects, next_filter, "next")
        
    except Exception as e:
        logger.error(f"Error in get_next_tasks: {e}")
        return f"Error retrieving projects: {str(e)}"

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
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    
    # Validate priority
    if priority not in [0, 1, 3, 5]:
        return "Invalid priority. Must be 0 (None), 1 (Low), 3 (Medium), or 5 (High)."
    
    try:
        subtask = ticktick.create_subtask(
            subtask_title=subtask_title,
            parent_task_id=parent_task_id,
            project_id=project_id,
            content=content,
            priority=priority
        )
        
        if 'error' in subtask:
            return f"Error creating subtask: {subtask['error']}"
        
        return f"Subtask created successfully:\n\n" + format_task(subtask)
    except Exception as e:
        logger.error(f"Error in create_subtask: {e}")
        return f"Error creating subtask: {str(e)}"

# ---------------------------------------------------------------------------
# v2 API tools (unofficial). Available only when TICKTICK_USERNAME/PASSWORD
# are configured. They cover what the official Open API cannot do.
# ---------------------------------------------------------------------------

_V2_DISABLED_MSG = (
    "The unofficial v2 API is not enabled (or its session token expired). "
    "Set TICKTICK_V2_TOKEN to the `t` cookie from a logged-in ticktick.com "
    "browser session to use tags, completed tasks, the Inbox, and moving "
    "tasks between lists."
)


@mcp.tool()
async def get_completed_tasks(limit: int = 50) -> str:
    """
    Get recently completed tasks across all lists (requires v2 API).

    Args:
        limit: Maximum number of completed tasks to return (default 50)
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    if not ticktick_v2:
        return _V2_DISABLED_MSG
    try:
        tasks = ticktick_v2.get_completed_tasks(limit=limit)
        if not tasks:
            return "No completed tasks found."
        out = f"Completed tasks ({len(tasks)}):\n\n"
        return out + format_task_list(tasks)
    except Exception as e:
        logger.error(f"Error in get_completed_tasks: {e}")
        return f"Error fetching completed tasks: {str(e)}"


@mcp.tool()
async def list_tags() -> str:
    """List all tags in the account (requires v2 API)."""
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    if not ticktick_v2:
        return _V2_DISABLED_MSG
    try:
        tags = ticktick_v2.get_tags()
        if not tags:
            return "No tags found."
        lines = [f"- {t.get('label', t.get('name', '?'))}" for t in tags]
        return f"Tags ({len(tags)}):\n" + "\n".join(lines)
    except Exception as e:
        logger.error(f"Error in list_tags: {e}")
        return f"Error fetching tags: {str(e)}"


@mcp.tool()
async def get_tasks_by_tag(tag: str) -> str:
    """
    Get open tasks that carry a given tag (requires v2 API).

    Args:
        tag: Tag label, with or without the leading '#'
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    if not ticktick_v2:
        return _V2_DISABLED_MSG
    try:
        tasks = ticktick_v2.get_tasks_by_tag(tag)
        if not tasks:
            return f"No open tasks found with tag '{tag}'."
        out = f"Tasks tagged '{tag}' ({len(tasks)}):\n\n"
        return out + format_task_tree(tasks)
    except Exception as e:
        logger.error(f"Error in get_tasks_by_tag: {e}")
        return f"Error fetching tasks by tag: {str(e)}"


@mcp.tool()
async def get_inbox_tasks() -> str:
    """Get open tasks in the Inbox (requires v2 API)."""
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    if not ticktick_v2:
        return _V2_DISABLED_MSG
    try:
        tasks = ticktick_v2.get_inbox_tasks()
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
        ticktick_v2.batch_move_tasks(ids, to_project_id)
        titles_str = ", ".join(f"«{t}»" for t in titles)
        return f"↪ Перемещено {len(ids)} → «{to_name}»: {titles_str}"
    except Exception as e:
        logger.error(f"Error in move_tasks: {e}")
        return f"Error moving tasks: {str(e)}"




# ---------------------------------------------------------------------------
# Habits (v2)
# ---------------------------------------------------------------------------

def _ensure_ready() -> Optional[str]:
    """Return an error string if the client/v2 isn't ready, else None."""
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    if not ticktick_v2:
        return _V2_DISABLED_MSG
    return None


@mcp.tool()
async def get_habits() -> str:
    """List all habits with their goal and current streak (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        habits = ticktick_v2.get_habits()
        if not habits:
            return "No habits found."
        active = [h for h in habits if h.get("status") == 0 or h.get("status") == 1]
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
        ticktick_v2.checkin_habit(habit_id, date=date, status=status, value=value)
        when = date or "today"
        labels = {2: "done", 1: "failed", 0: "not done"}
        return f"Habit '{habit_name}' checked in for {when} as '{labels[status]}'."
    except Exception as e:
        logger.error(f"Error in checkin_habit: {e}")
        return f"Error checking in habit: {str(e)}"


@mcp.tool()
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
        result = ticktick_v2.get_habit_checkins([habit_id], stamp)
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

@mcp.tool()
async def list_filters() -> str:
    """List saved smart-list filters with their query rules (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        filters = ticktick_v2.get_filters()
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
        ticktick_v2.batch_set_task_parent(ids, parent_task_id, project_id)
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
        ticktick_v2.unset_task_parent(task_id, parent_task_id, project_id)
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
        ticktick_v2.batch_update_tasks(changes)
        labels_str = ", ".join(f"«{l}»" for l in labels)
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

@mcp.tool()
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


@mcp.tool()
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

@mcp.tool()
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
        tasks = ticktick_v2.run_filter(filter)
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

@mcp.tool()
async def list_project_groups() -> str:
    """List project groups (folders) (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        groups = ticktick_v2.list_project_groups()
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
        gid = ticktick_v2.create_project_group(name)
        return f"Project group '{name}' created (id: {gid})."
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
        ticktick_v2.delete_project_group(group_id)
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
        ticktick_v2.move_project_to_group(project_id, group_id)
        dest = "ungrouped" if group_id == "NONE" else f"group {group_id}"
        return f"Project '{project_name}' moved to {dest}."
    except Exception as e:
        logger.error(f"Error in move_project_to_group: {e}")
        return f"Error moving project: {str(e)}"


# ---------------------------------------------------------------------------
# Task comments (v2)
# ---------------------------------------------------------------------------

@mcp.tool()
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
        comments = ticktick_v2.get_task_comments(project_id, task_id)
        if not comments:
            return f"No comments on task '{task_title}'."
        out = f"Comments on '{task_title}' ({len(comments)}):\n"
        for c in comments:
            who = (c.get("userProfile") or {}).get("displayName") or c.get("userName", "?")
            out += f"- [{who}] {c.get('title','')}\n"
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
        ticktick_v2.add_task_comment(project_id, task_id, text)
        return f"Comment added to '{task_title}'."
    except Exception as e:
        logger.error(f"Error in add_task_comment: {e}")
        return f"Error adding comment: {str(e)}"


# ---------------------------------------------------------------------------
# Statistics & trash (v2)
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_statistics() -> str:
    """Get productivity statistics: achievement score/level and completion counts (requires v2 API)."""
    err = _ensure_ready()
    if err:
        return err
    try:
        s = ticktick_v2.get_statistics()
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


@mcp.tool()
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
        tasks = ticktick_v2.get_trash(limit)
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
        ticktick_v2.batch_restore_tasks(ids, to_project_id)
        titles_str = ", ".join(f"«{t}»" for t in titles)
        return f"↩ Восстановлено из корзины {len(ids)}: {titles_str}"
    except Exception as e:
        logger.error(f"Error in restore_tasks: {e}")
        return f"Error restoring tasks: {str(e)}"


@mcp.tool()
async def attach_file_to_task(task_id: str, project_id: str, task_title: str = None,
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
        att = ticktick_v2.upload_attachment(
            pid, task_id, url=url, content_base64=content_base64, filename=filename)
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
        ticktick_v2.create_tag(name, color)
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
        ticktick_v2.rename_tag(old_name, new_name)
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
        ticktick_v2.delete_tag(name)
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
        ticktick_v2.abandon_task(task_id)
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
        copy = ticktick_v2.duplicate_task(task_id)
        return f"Дублировано: «{title}» → новый id: {copy.get('id')}"
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
        ticktick_v2.update_task_comment(project_id, task_id, comment_id, text)
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
        ticktick_v2.delete_task_comment(project_id, task_id, comment_id)
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
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    try:
        proj = ticktick.update_project(project_id, name=name, color=color,
                                       view_mode=view_mode)
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
        ticktick_v2.archive_project(project_id, closed=archived)
        return f"Project '{project_name}' {'archived' if archived else 'unarchived'}."
    except Exception as e:
        logger.error(f"Error in archive_project: {e}")
        return f"Error archiving project: {str(e)}"


# ---------------------------------------------------------------------------
# Search across open + completed (v2)
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_all_tasks(query: str, include_completed: bool = True) -> str:
    """
    Search tasks by title/content across open and (optionally) completed tasks (requires v2 API).

    Args:
        query: Text to search for (case-insensitive substring)
        include_completed: Also search recently completed tasks (default True)
    """
    err = _ensure_ready()
    if err:
        return err
    try:
        q = query.lower()
        pool = list(ticktick_v2.get_open_tasks())
        if include_completed:
            pool += ticktick_v2.get_completed_tasks(limit=100)
        matches = [t for t in pool
                   if q in (t.get("title", "") or "").lower()
                   or q in (t.get("content", "") or "").lower()]
        if not matches:
            return f"No tasks matched '{query}'."
        return (f"Matches for '{query}' ({len(matches)}):\n"
                + format_task_tree(matches, 100))
    except Exception as e:
        logger.error(f"Error in search_all_tasks: {e}")
        return f"Error searching tasks: {str(e)}"


@mcp.tool()
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
        state = ticktick_v2.get_state()
        owner = (state.get("inboxId") or "").replace("inbox", "")
        names = _v2_project_names()
        tasks = state.get("syncTaskBean", {}).get("update", []) or []
        t = next((x for x in tasks if x.get("id") == task_id), None)
        if not t:
            return (f"Task {task_id} not found among open tasks "
                    "(it may be completed or in the trash).")

        pr = {0: "None", 1: "Low", 3: "Medium", 5: "High"}.get(t.get("priority", 0))
        status = {0: "Active", 2: "Completed", -1: "Won't do"}.get(t.get("status", 0), t.get("status"))
        creator = str(t.get("creator", ""))
        who = "you" if creator == owner else f"user {creator}"

        out = f"Task: {t.get('title')}\n"
        out += f"  id: {t.get('id')}  |  project: {names.get(t.get('projectId'), t.get('projectId'))}\n"
        out += f"  status: {status}  |  priority: {pr}\n"
        if t.get("dueDate"):
            d = t["dueDate"][:10] if t.get("isAllDay") else t["dueDate"]
            out += f"  due: {d}{'  (all-day)' if t.get('isAllDay') else ''}\n"
        if t.get("tags"):
            out += f"  tags: {', '.join('#'+x for x in t['tags'])}\n"
        if t.get("columnId"):
            out += f"  columnId: {t['columnId']}\n"
        if t.get("content"):
            out += f"  content: {t['content'][:300]}\n"
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
        if not items and not kids:
            out += "\n(no checklist items or subtasks)\n"
        return out
    except Exception as e:
        logger.error(f"Error in get_task_info: {e}")
        return f"Error fetching task info: {str(e)}"


@mcp.tool()
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
        events = ticktick_v2.get_task_activity(project_id, task_id)
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


@mcp.tool()
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
        pname = lambda pid: names.get(pid, pid or "?")

        open_tasks = ticktick_v2.get_open_tasks()
        completed = ticktick_v2.get_completed_tasks(
            limit=100, from_str=since + " 00:00:00", to_str=until + " 23:59:59")
        trash = ticktick_v2.get_trash(limit=300)

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


@mcp.tool()
async def list_project_columns(project_id: str) -> str:
    """
    List the kanban columns/sections of a project, with their IDs (uses the
    official API). Use a column id as column_id in create_task/update_task.

    Args:
        project_id: ID of the project
    """
    if not ticktick:
        if not initialize_client():
            return "Failed to initialize TickTick client. Please check your API credentials."
    try:
        data = ticktick.get_project_with_data(project_id)
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


def main():
    """Main entry point for the MCP server."""
    if not initialize_client():
        logger.error("Failed to initialize TickTick client. Please check your API credentials.")
        return

    if TRANSPORT == "streamable-http":
        logger.info(f"Starting TickTick MCP server (streamable-http) on "
                    f"http://{HOST}:{PORT}{STREAMABLE_PATH}")
        mcp.run(transport="streamable-http")
    else:
        logger.info("Starting TickTick MCP server (stdio)")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()