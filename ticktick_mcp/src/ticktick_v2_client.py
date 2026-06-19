"""
Unofficial TickTick v2 API client.

The official Open API (ticktick_client.py) cannot read completed tasks, tags,
the Inbox, or move tasks between lists. This client talks to the *unofficial*
web API (api.ticktick.com/api/v2) that the TickTick web app itself uses, to
cover those gaps.

Authentication: a **browser session token** — the value of the `t` cookie from
a logged-in ticktick.com session — supplied via TICKTICK_V2_TOKEN. We do NOT
log in with username/password: TickTick now gates /user/signon behind a captcha
and locks accounts after repeated automated attempts (see ticktick-py issues
#52/#56). A pre-obtained `t` cookie sidesteps all of that and works from a
datacenter IP. The token is long-lived but does eventually expire — when it
does, every call raises TickTickAuthError asking for a fresh cookie.

A username/password fallback remains for local/residential use only.
"""

import os
import json
import logging
import uuid
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

V2_BASE = "https://api.ticktick.com/api/v2"

# Never let an unofficial-API call hang the MCP request forever.
REQUEST_TIMEOUT = 20

# Completed-task endpoint hard-caps the page size.
COMPLETED_MAX_LIMIT = 100


class TickTickAuthError(RuntimeError):
    """Raised when the v2 session token is missing, invalid, or expired."""


def _build_x_device() -> str:
    """The x-device header the web client sends; v2 returns 500 without it."""
    return json.dumps({
        "platform": "web",
        "os": "macOS 10.15.7",
        "device": "Chrome 120.0.0.0",
        "name": "",
        "version": 6070,
        "id": uuid.uuid4().hex[:24],
        "channel": "website",
        "campaign": "",
        "websocket": "",
    })


class TickTickV2Client:
    """Session-based client for the unofficial TickTick v2 API."""

    def __init__(self, token: str = None, username: str = None, password: str = None):
        self.token = token or os.getenv("TICKTICK_V2_TOKEN")
        self.username = username or os.getenv("TICKTICK_USERNAME")
        self.password = password or os.getenv("TICKTICK_PASSWORD")

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "x-device": _build_x_device(),
        })
        self.inbox_id: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.token or (self.username and self.password))

    # ---- auth -------------------------------------------------------------
    def authenticate(self) -> None:
        """Attach the session token. Prefer the browser `t` cookie; fall back
        to username/password signon only if no token is configured."""
        if self.token:
            self.session.cookies.set("t", self.token)
            # Validate eagerly so startup fails loudly with a clear message.
            self._request("GET", "/batch/check/0")
            logger.info("TickTick v2 authenticated via session token")
            return
        if self.username and self.password:
            self._login_with_password()
            return
        raise TickTickAuthError(
            "No TICKTICK_V2_TOKEN (preferred) or TICKTICK_USERNAME/PASSWORD set."
        )

    # Backwards-compatible alias used by server.initialize_client().
    def login(self) -> None:
        self.authenticate()

    def _login_with_password(self) -> None:
        """DEPRECATED password signon — captcha-gated, residential IP only."""
        resp = self.session.post(
            f"{V2_BASE}/user/signon?wc=true&remember=true",
            json={"username": self.username, "password": self.password},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise TickTickAuthError(
                f"v2 password login failed ({resp.status_code}): {resp.text[:200]}. "
                "TickTick now gates this behind a captcha — use TICKTICK_V2_TOKEN "
                "(the `t` cookie from a logged-in browser) instead."
            )
        token = resp.json().get("token")
        if not token:
            raise TickTickAuthError(f"v2 login returned no token: {resp.json()}")
        self.token = token
        self.session.cookies.set("t", token)
        logger.info("TickTick v2 authenticated via password (deprecated path)")

    # ---- low-level --------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> Any:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        url = f"{V2_BASE}{path}"
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code in (401, 403):
            raise TickTickAuthError(
                "TickTick v2 session token is invalid or expired. Re-extract the "
                "`t` cookie from a logged-in ticktick.com browser session and "
                "update TICKTICK_V2_TOKEN."
            )
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.text:
            return {}
        data = resp.json()
        # v2 signals auth/permission problems in the body even on HTTP 200.
        if isinstance(data, dict) and data.get("errorCode") in (
            "user_not_sign_on", "not_login", "access_forbidden"
        ):
            raise TickTickAuthError(
                f"TickTick v2 rejected the session ({data.get('errorCode')}). "
                "Re-extract the `t` cookie and update TICKTICK_V2_TOKEN."
            )
        return data

    def get_state(self) -> Dict:
        """Full sync snapshot: projects, tags, open tasks, inboxId."""
        state = self._request("GET", "/batch/check/0")
        if isinstance(state, dict) and state.get("inboxId"):
            self.inbox_id = state["inboxId"]
        return state

    # ---- features the Open API lacks -------------------------------------
    def get_tags(self) -> List[Dict]:
        return self.get_state().get("tags", []) or []

    def get_open_tasks(self) -> List[Dict]:
        state = self.get_state()
        return state.get("syncTaskBean", {}).get("update", []) or []

    def get_tasks_by_tag(self, tag_label: str) -> List[Dict]:
        label = tag_label.lstrip("#").lower()
        return [
            t for t in self.get_open_tasks()
            if label in [x.lower() for x in (t.get("tags") or [])]
        ]

    def get_inbox_tasks(self) -> List[Dict]:
        state = self.get_state()
        inbox = self.inbox_id or state.get("inboxId")
        tasks = state.get("syncTaskBean", {}).get("update", []) or []
        return [t for t in tasks if t.get("projectId") == inbox]

    def get_completed_tasks(self, limit: int = 50) -> List[Dict]:
        """Recently completed tasks across all lists (v2 endpoint, max 100)."""
        limit = max(1, min(limit, COMPLETED_MAX_LIMIT))
        params = {"from": "", "to": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "limit": limit}
        data = self._request("GET", "/project/all/completed", params=params)
        return data if isinstance(data, list) else data.get("tasks", [])

    def move_task(self, task_id: str, to_project_id: str) -> Dict:
        """Move an open task to another project/list via batch/taskProject."""
        task = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Open task {task_id} not found in current sync state.")
        from_project = task.get("projectId")
        if from_project == to_project_id:
            return {"message": "Task already in that project."}
        body = [{
            "fromProjectId": from_project,
            "toProjectId": to_project_id,
            "taskId": task_id,
        }]
        return self._request("POST", "/batch/taskProject", json=body)

    # ---- smart lists / filters -------------------------------------------
    def get_filters(self) -> List[Dict]:
        return self.get_state().get("filters", []) or []

    # ---- habits ----------------------------------------------------------
    @staticmethod
    def _now_iso() -> str:
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    def get_habits(self) -> List[Dict]:
        data = self._request("GET", "/habits")
        return data if isinstance(data, list) else []

    def get_habit_checkins(self, habit_ids: List[str], after_stamp: int) -> Dict:
        """after_stamp is an int date like 20260101. Returns {habitId: [entries]}."""
        data = self._request("POST", "/habitCheckins/query",
                              json={"habitIds": habit_ids, "afterStamp": after_stamp})
        return data.get("checkins", {}) if isinstance(data, dict) else {}

    def checkin_habit(self, habit_id: str, date: str = None,
                      status: int = 2, value: float = None, goal: float = 1.0) -> Dict:
        """Record a habit check-in. date='YYYY-MM-DD' (default today) enables
        backdating. status 2=done, 1=failed, 0=not-done."""
        if date:
            stamp = int(date.replace("-", ""))
        else:
            stamp = int(datetime.now().strftime("%Y%m%d"))
        if value is None:
            value = goal if status == 2 else 0.0
        entry = {
            "id": uuid.uuid4().hex[:24],
            "habitId": habit_id,
            "checkinStamp": stamp,
            "checkinTime": self._now_iso(),
            "opTime": self._now_iso(),
            "value": float(value),
            "goal": float(goal),
            "status": int(status),
        }
        return self._request("POST", "/habitCheckins/batch",
                             json={"add": [entry], "update": [], "delete": []})

    # ---- subtasks (parent/child) -----------------------------------------
    def set_task_parent(self, task_id: str, parent_id: str, project_id: str) -> Dict:
        body = [{"parentId": parent_id, "taskId": task_id, "projectId": project_id}]
        return self._request("POST", "/batch/taskParent", json=body)

    def unset_task_parent(self, task_id: str, parent_id: str, project_id: str) -> Dict:
        body = [{"oldParentId": parent_id, "taskId": task_id, "projectId": project_id}]
        return self._request("POST", "/batch/taskParent", json=body)

    # ---- batch -----------------------------------------------------------
    def batch_complete_tasks(self, task_ids: List[str]) -> Dict:
        """Mark several open tasks complete in one call."""
        by_id = {t.get("id"): t for t in self.get_open_tasks()}
        updates = []
        for tid in task_ids:
            t = by_id.get(tid)
            if t:
                t = dict(t)
                t["status"] = 2
                updates.append(t)
        if not updates:
            return {"message": "No matching open tasks found."}
        return self._request("POST", "/batch/task",
                             json={"add": [], "update": updates, "delete": []})

    def batch_delete_tasks(self, items: List[Dict]) -> Dict:
        """items: list of {"taskId": ..., "projectId": ...}."""
        return self._request("POST", "/batch/task",
                             json={"add": [], "update": [], "delete": items})

    # raw create/update helpers for batch task creation via v2
    def batch_create_tasks(self, tasks: List[Dict]) -> Dict:
        return self._request("POST", "/batch/task",
                             json={"add": tasks, "update": [], "delete": []})

    # ---- project groups / folders ----------------------------------------
    def list_project_groups(self) -> List[Dict]:
        return self.get_state().get("projectGroups", []) or []

    def list_projects(self) -> List[Dict]:
        return self.get_state().get("projectProfiles", []) or []

    def create_project_group(self, name: str) -> str:
        gid = uuid.uuid4().hex[:24]
        self._request("POST", "/batch/projectGroup",
                      json={"add": [{"id": gid, "name": name, "listType": "group"}],
                            "update": [], "delete": []})
        return gid

    def delete_project_group(self, group_id: str) -> Dict:
        return self._request("POST", "/batch/projectGroup",
                             json={"add": [], "update": [], "delete": [group_id]})

    def move_project_to_group(self, project_id: str, group_id: str) -> Dict:
        """group_id='NONE' ungroups the project."""
        proj = next((p for p in self.list_projects() if p.get("id") == project_id), None)
        if not proj:
            raise ValueError(f"Project {project_id} not found.")
        return self._request("POST", "/batch/project",
                             json={"add": [], "delete": [], "update": [
                                 {"id": project_id, "name": proj.get("name"),
                                  "groupId": group_id}]})

    # ---- task comments ---------------------------------------------------
    def get_task_comments(self, project_id: str, task_id: str) -> List[Dict]:
        data = self._request("GET", f"/project/{project_id}/task/{task_id}/comments")
        return data if isinstance(data, list) else []

    def add_task_comment(self, project_id: str, task_id: str, text: str) -> Dict:
        body = {"id": uuid.uuid4().hex[:24], "title": text,
                "taskId": task_id, "projectId": project_id}
        return self._request("POST", f"/project/{project_id}/task/{task_id}/comment",
                             json=body)

    # ---- statistics ------------------------------------------------------
    def get_statistics(self) -> Dict:
        data = self._request("GET", "/statistics/general")
        return data if isinstance(data, dict) else {}

    # ---- trash -----------------------------------------------------------
    def get_trash(self, limit: int = 50) -> List[Dict]:
        data = self._request("GET", "/project/all/trash/pagination",
                             params={"start": 0, "limit": max(1, min(limit, 500))})
        return data.get("tasks", []) if isinstance(data, dict) else []

    def restore_task(self, task_id: str, to_project_id: str = None) -> Dict:
        """Restore a task from trash to its original list (or to_project_id)."""
        trashed = self.get_trash(limit=500)
        t = next((x for x in trashed if x.get("id") == task_id), None)
        if not t:
            raise ValueError(f"Task {task_id} not found in trash.")
        from_pid = t.get("projectId")
        body = [{"fromProjectId": from_pid, "taskId": task_id,
                 "toProjectId": to_project_id or from_pid}]
        return self._request("POST", "/trash/restore", json=body)

    # ---- smart-list (filter) execution -----------------------------------
    def run_filter(self, filter_id_or_name: str) -> List[Dict]:
        """Fetch all open tasks and return those matching a saved filter's rule,
        which TickTick evaluates client-side (no server endpoint exists)."""
        filters = self.get_filters()
        flt = next((f for f in filters
                    if f.get("id") == filter_id_or_name
                    or f.get("name") == filter_id_or_name), None)
        if not flt:
            raise ValueError(f"Filter '{filter_id_or_name}' not found.")
        try:
            rule = json.loads(flt.get("rule") or "{}")
        except (ValueError, TypeError):
            rule = {}
        state = self.get_state()
        inbox = state.get("inboxId")
        # map projectId -> groupId for listOrGroup conditions
        proj_group = {p["id"]: p.get("groupId") for p in
                      (state.get("projectProfiles", []) or [])}
        tasks = state.get("syncTaskBean", {}).get("update", []) or []
        return [t for t in tasks if _rule_matches(t, rule, inbox, proj_group)]

    # ---- tag write ops ---------------------------------------------------
    def create_tag(self, name: str, color: str = None) -> Dict:
        label = name
        return self._request("POST", "/batch/tag", json={
            "add": [{"name": name.lower(), "label": label, "color": color,
                     "sortOrder": 0, "parent": None}],
            "update": [], "delete": []})

    def rename_tag(self, old_name: str, new_name: str) -> Dict:
        return self._request("PUT", "/tag/rename",
                             json={"name": old_name.lower(), "newName": new_name})

    def delete_tag(self, name: str) -> Dict:
        return self._request("DELETE", "/tag", params={"name": name.lower()})

    def set_task_tags(self, task_id: str, tags: List[str]) -> Dict:
        task = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Open task {task_id} not found.")
        task = dict(task)
        task["tags"] = [t.lstrip("#").lower() for t in tags]
        return self._request("POST", "/batch/task",
                             json={"add": [], "update": [task], "delete": []})

    # ---- won't-do / duplicate -------------------------------------------
    def abandon_task(self, task_id: str) -> Dict:
        """Mark a task 'Won't do' (v2 status -1)."""
        task = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Open task {task_id} not found.")
        task = dict(task)
        task["status"] = -1
        return self._request("POST", "/batch/task",
                             json={"add": [], "update": [task], "delete": []})

    def duplicate_task(self, task_id: str) -> Dict:
        src = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not src:
            raise ValueError(f"Open task {task_id} not found.")
        copy = {k: src[k] for k in ("projectId", "content", "desc", "priority",
                                    "tags", "isAllDay", "startDate", "dueDate",
                                    "timeZone", "repeatFlag", "reminders")
                if k in src}
        copy["id"] = uuid.uuid4().hex[:24]
        copy["title"] = (src.get("title", "") + " (copy)")
        copy["status"] = 0
        self.batch_create_tasks([copy])
        return copy

    # ---- comments delete -------------------------------------------------
    def delete_task_comment(self, project_id: str, task_id: str,
                            comment_id: str) -> Dict:
        return self._request(
            "DELETE", f"/project/{project_id}/task/{task_id}/comment/{comment_id}")

    # ---- project archive -------------------------------------------------
    def archive_project(self, project_id: str, closed: bool = True) -> Dict:
        proj = next((p for p in self.list_projects() if p.get("id") == project_id), None)
        if not proj:
            raise ValueError(f"Project {project_id} not found.")
        upd = dict(proj)
        upd["closed"] = closed
        return self._request("POST", "/batch/project",
                             json={"add": [], "delete": [], "update": [upd]})


# ---- filter rule evaluation (client-side, mirrors the TickTick web app) ----

def _rule_matches(task: Dict, rule: Dict, inbox: str, proj_group: Dict) -> bool:
    groups = rule.get("and") or []
    if not groups:
        return True  # empty rule = everything
    return all(_node_matches(task, g, inbox, proj_group) for g in groups)


def _node_matches(task: Dict, node: Dict, inbox: str, proj_group: Dict) -> bool:
    items = node.get("or")
    combine = any
    if items is None:
        items = node.get("and") or []
        combine = all
    if not items:
        return True
    # Nested condition objects → recurse.
    if items and isinstance(items[0], dict):
        return combine(_node_matches(task, it, inbox, proj_group) for it in items)
    return _leaf_matches(task, node.get("conditionName"), items, inbox, proj_group)


def _leaf_matches(task, name, values, inbox, proj_group) -> bool:
    if name in ("list", "listOrGroup"):
        if "all" in values:
            return True
        resolved = {inbox if v == "inbox" else v for v in values}
        pid = task.get("projectId")
        return pid in resolved or proj_group.get(pid) in resolved
    if name == "tag":
        tset = {x.lower() for x in (task.get("tags") or [])}
        pos = {v.lower() for v in values if not str(v).startswith("!")}
        neg = {v[1:].lower() for v in values if str(v).startswith("!")}
        if neg and (tset & neg):
            return False
        return (not pos) or bool(tset & pos)
    if name == "priority":
        return task.get("priority", 0) in set(values)
    if name == "dueDate":
        return any(_due_token_matches(task, v) for v in values)
    return True  # unknown condition → don't exclude


def _task_due_date(task):
    raw = task.get("dueDate")
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _due_token_matches(task, token) -> bool:
    from datetime import date, timedelta
    d = _task_due_date(task)
    today = date.today()
    if token == "nodate":
        return d is None
    if token == "recurring":
        return bool(task.get("repeatFlag") or task.get("repeatRule"))
    if d is None:
        return False
    if token == "today":
        return d == today
    if token == "tomorrow":
        return d == today + timedelta(days=1)
    if token == "overdue":
        return d < today and task.get("status", 0) == 0
    if token == "thisweek":
        start = today - timedelta(days=today.weekday())
        return start <= d <= start + timedelta(days=6)
    if token == "nextweek":
        start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        return start <= d <= start + timedelta(days=6)
    if token == "thismonth":
        return d.year == today.year and d.month == today.month
    if "~" in str(token):  # explicit range "YYYY-MM-DD~YYYY-MM-DD"
        try:
            a, b = token.split("~")
            return (datetime.strptime(a[:10], "%Y-%m-%d").date() <= d
                    <= datetime.strptime(b[:10], "%Y-%m-%d").date())
        except (ValueError, TypeError):
            return False
    return False
