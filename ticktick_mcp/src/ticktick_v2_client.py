"""
Unofficial TickTick v2 API client.

The official Open API (ticktick_client.py) cannot read completed tasks, tags,
the Inbox, or move tasks between lists. This client talks to the *unofficial*
web API (api.ticktick.com/api/v2) that the TickTick web app itself uses, to
cover those gaps.

It authenticates with the user's email + password (session token), NOT OAuth.
This API is undocumented and may change or break without notice. It is enabled
only when TICKTICK_USERNAME and TICKTICK_PASSWORD are configured; otherwise the
server falls back to the official API only.
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


def _build_x_device() -> str:
    """Build the x-device header the web client sends; some endpoints 400 without it."""
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

    def __init__(self, username: str = None, password: str = None):
        self.username = username or os.getenv("TICKTICK_USERNAME")
        self.password = password or os.getenv("TICKTICK_PASSWORD")
        if not self.username or not self.password:
            raise ValueError(
                "TICKTICK_USERNAME and TICKTICK_PASSWORD must be set to use the v2 API."
            )
        self.x_device = _build_x_device()
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "x-device": self.x_device,
        })
        self.token: Optional[str] = None
        self.inbox_id: Optional[str] = None

    # ---- auth -------------------------------------------------------------
    def login(self) -> None:
        """Authenticate and store the session token cookie."""
        resp = self.session.post(
            f"{V2_BASE}/user/signon?wc=true&remember=true",
            json={"username": self.username, "password": self.password},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"TickTick v2 login failed ({resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        self.token = data.get("token")
        if not self.token:
            raise RuntimeError(f"TickTick v2 login returned no token: {data}")
        self.session.cookies.set("t", self.token)
        self.inbox_id = data.get("inboxId")
        logger.info("TickTick v2 API authenticated successfully")

    def _ensure_auth(self) -> None:
        if not self.token:
            self.login()

    # ---- low-level --------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> Any:
        self._ensure_auth()
        url = f"{V2_BASE}{path}"
        resp = self.session.request(method, url, **kwargs)
        # token may have expired -> re-login once
        if resp.status_code in (401, 403):
            logger.info("v2 session expired, re-authenticating...")
            self.token = None
            self._ensure_auth()
            resp = self.session.request(method, url, **kwargs)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

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
        result = []
        for task in self.get_open_tasks():
            tags = [t.lower() for t in (task.get("tags") or [])]
            if label in tags:
                result.append(task)
        return result

    def get_inbox_tasks(self) -> List[Dict]:
        state = self.get_state()
        inbox = self.inbox_id or state.get("inboxId")
        tasks = state.get("syncTaskBean", {}).get("update", []) or []
        return [t for t in tasks if t.get("projectId") == inbox]

    def get_completed_tasks(self, limit: int = 50) -> List[Dict]:
        """Best-effort fetch of recently completed tasks (unofficial endpoint)."""
        to = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params = {"from": "", "to": to, "limit": limit}
        data = self._request("GET", "/project/all/completed", params=params)
        return data if isinstance(data, list) else data.get("tasks", [])

    def move_task(self, task_id: str, to_project_id: str) -> Dict:
        """Move an open task to another project/list."""
        task = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Open task {task_id} not found in current sync state.")
        task["projectId"] = to_project_id
        return self._request("POST", "/batch/task", json={"update": [task]})
