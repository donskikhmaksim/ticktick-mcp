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
