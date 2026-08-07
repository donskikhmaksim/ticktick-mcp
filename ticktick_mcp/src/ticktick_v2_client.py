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
import base64
import logging
import mimetypes
import re
import time
import urllib.parse
import uuid
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

V2_BASE = "https://api.ticktick.com/api/v2"
# Attachment upload lives on the v1 path, not v2.
ATTACHMENT_BASE = "https://api.ticktick.com/api/v1"
ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB (premium cap)

# Never let an unofficial-API call hang the MCP request forever.
REQUEST_TIMEOUT = 20

# Completed-task endpoint hard-caps the page size.
COMPLETED_MAX_LIMIT = 100

# get_task_activity walks pages until one comes back empty/repeats, but stops
# after this many pages regardless — a per-task edit log (title/due/move/etc.
# events on ONE task), not an account-wide feed, so even a heavily-edited task
# realistically tops out at a few dozen events/pages. 20 is a generous multiple
# of that, kept as a safety net against a pathological task (e.g. years of daily
# recurrence edits) turning one tool call into an unbounded number of HTTP
# requests. Raise only if a real task is observed hitting this ceiling.
TASK_ACTIVITY_MAX_PAGES = 20

# TickTick spaces kanban columns with this default gap so new ones can be
# slotted between existing columns without renumbering (mirrors the web app).
COLUMN_SORT_STEP = 1099511627776


class TickTickAuthError(RuntimeError):
    """Raised when the v2 session token is missing, invalid, or expired."""


def id2error_failures(resp: Any, ids: List[str]) -> Dict[str, str]:
    """Per-item failures from a v2 batch response.

    TickTick's /batch/* endpoints return HTTP 200 with per-item rejections in
    an `id2error` map — a call can "succeed" while individual items failed.
    Returns {id: error} for the given ids (empty dict = no reported failures)."""
    if not isinstance(resp, dict):
        return {}
    errs = resp.get("id2error") or {}
    if not isinstance(errs, dict):
        return {}
    return {str(i): str(errs[i]) for i in ids if i in errs and errs[i]}


def new_attachment_id() -> str:
    """A fresh attachment id in TickTick's shape (24 hex chars). The CLIENT
    mints it — the upload URL contains it, so it has to exist before the file
    is sent (which is what makes a pre-signed upload link possible at all)."""
    return uuid.uuid4().hex[:24]


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
        # Short-lived cache of the 3 MB /batch/check/0 sync so a single
        # multi-tool turn doesn't refetch the full state on every call.
        self._state_cache: Optional[Dict] = None
        self._state_ts: float = 0.0
        self._state_ttl: float = 20.0

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
        try:
            body = resp.json()
        except ValueError:
            raise TickTickAuthError(
                "v2 login returned a non-JSON body (likely a captcha/HTML page). "
                "Use TICKTICK_V2_TOKEN (the `t` cookie) instead."
            )
        token = body.get("token")
        if not token:
            raise TickTickAuthError(f"v2 login returned no token: {body}")
        self.token = token
        self.session.cookies.set("t", token)
        logger.info("TickTick v2 authenticated via password (deprecated path)")

    # ---- low-level --------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> Any:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        url = f"{V2_BASE}{path}"
        # Any write invalidates the cached sync state so reads stay fresh.
        if method != "GET":
            self._state_cache = None
        # Retry on 429/5xx with exponential backoff (1s, 2s) — TickTick
        # rate-limits bursts; a short wait usually clears it. 502/504 are
        # transient proxy errors (Cloudflare) — retry those too, so a
        # post-mutation verify read doesn't fail on a blip.
        resp = self.session.request(method, url, **kwargs)
        for attempt in range(2):
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(2 ** attempt)
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
        # A 200 with a non-JSON body (e.g. a Cloudflare/HTML interstitial)
        # means the session isn't really authenticated — surface it as an auth
        # error rather than letting a raw JSONDecodeError escape.
        try:
            data = resp.json()
        except ValueError:
            raise TickTickAuthError(
                "TickTick v2 returned a non-JSON response (likely an HTML "
                "login/interstitial page). Re-extract the `t` cookie and "
                "update TICKTICK_V2_TOKEN."
            )
        # v2 signals auth/permission problems in the body even on HTTP 200.
        if isinstance(data, dict) and data.get("errorCode") in (
            "user_not_sign_on", "not_login", "access_forbidden"
        ):
            raise TickTickAuthError(
                f"TickTick v2 rejected the session ({data.get('errorCode')}). "
                "Re-extract the `t` cookie and update TICKTICK_V2_TOKEN."
            )
        return data

    def invalidate_cache(self) -> None:
        """Drop the cached sync state (call after an external write)."""
        self._state_cache = None

    def get_state(self, force: bool = False) -> Dict:
        """Full sync snapshot: projects, tags, open tasks, inboxId.
        Cached for a few seconds so back-to-back tool calls reuse one fetch."""
        if (not force and self._state_cache is not None
                and (time.monotonic() - self._state_ts) < self._state_ttl):
            return self._state_cache
        state = self._request("GET", "/batch/check/0")
        if isinstance(state, dict):
            self._state_cache = state
            self._state_ts = time.monotonic()
            if state.get("inboxId"):
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

    def get_completed_tasks(self, limit: int = 50, from_str: str = "",
                            to_str: str = None) -> List[Dict]:
        """Recently completed tasks across all lists (v2 endpoint, max 100).
        from_str/to_str are 'YYYY-MM-DD HH:MM:SS' bounds (empty = unbounded)."""
        limit = max(1, min(limit, COMPLETED_MAX_LIMIT))
        if to_str is None:
            to_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params = {"from": from_str, "to": to_str, "limit": limit}
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

    def batch_move_tasks(self, task_ids: List[str], to_project_id: str) -> Dict:
        """Move several open tasks to to_project_id in one batch/taskProject call.

        DERIVES each task's fromProjectId from get_open_tasks() — the v2
        open-task sync snapshot. If a task isn't in THAT snapshot (it can be
        missing for a task that is otherwise live and confirmed by other
        means — see server.py's identity-guard fallback and its docstring),
        this method silently DROPS it from the request body (`continue`
        below) — no error, no id2error entry, nothing sent to TickTick for
        it at all. A caller that already confirmed the task exists via a
        more reliable path (e.g. the official Open API) should use
        batch_move_tasks_raw() instead, passing the fromProjectId it already
        knows, rather than letting this method re-derive it from the same
        snapshot that may not have the task."""
        by_id = {t.get("id"): t for t in self.get_open_tasks()}
        body = []
        for tid in task_ids:
            t = by_id.get(tid)
            if not t:
                continue
            from_project = t.get("projectId")
            if from_project == to_project_id:
                continue
            body.append({"fromProjectId": from_project,
                         "toProjectId": to_project_id, "taskId": tid})
        if not body:
            return {"message": "No tasks to move (already in target or not found)."}
        return self._request("POST", "/batch/taskProject", json=body)

    def batch_move_tasks_raw(self, rows: List[Dict]) -> Dict:
        """Raw batch/taskProject move: rows of {"taskId", "fromProjectId",
        "toProjectId"} — lets the caller send each task's OWN already-
        confirmed live fromProjectId, instead of batch_move_tasks() above
        re-deriving it from get_open_tasks(). That re-derivation is exactly
        the bug behind a live incident (2026-08-07): identity-guard's
        official-API fallback correctly found and allowed a move for a task
        missing from the v2 open-task snapshot, batch_move_tasks() was then
        called with just its id, looked the id up in THAT SAME missing-it
        snapshot again, found nothing, and silently dropped the task from
        the request body — the guard's success never translated into an
        actual TickTick write, and no error was raised anywhere (id2error
        stayed empty, since TickTick was never even asked). Same "trust the
        caller's own already-verified projectId" pattern as
        set_task_parents() above."""
        body = [{"fromProjectId": r["fromProjectId"], "toProjectId": r["toProjectId"],
                 "taskId": r["taskId"]}
                for r in rows if r.get("fromProjectId") != r.get("toProjectId")]
        if not body:
            return {"message": "No tasks to move (already in target)."}
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

    # ---- habits: create / delete -----------------------------------------
    # Habits live ONLY in this unofficial v2 API (the official Open API has no
    # habit endpoints at all), and until 2026-08-06 this client could only read
    # them and check them in. Creating/deleting turned out to be the SAME batch
    # shape every other v2 collection uses (/batch/task, /batch/tag,
    # /batch/project, /batch/projectGroup): POST /habits/batch with
    # {"add": [...], "update": [...], "delete": [ids]}.
    #
    # Confirmed by a LIVE call against a throwaway habit (2026-08-06):
    #   add    → HTTP 200, {"id2etag":{"<minted id>":"se0q2pba"},"id2error":{}}
    #            and GET /habits went 11 → 12 with that exact id/name;
    #   delete → HTTP 200, {"id2etag":{},"id2error":{}}
    #            and GET /habits went 12 → 11 with the id gone.
    # As with attachments and check-ins, the CLIENT mints the habit id.

    # TickTick files every habit under a time-of-day section; the web app
    # always picks one, so we do too (a habit with no section would be an
    # object shape the real client never produces). Section names come back
    # from /habitSections as "_morning" / "_afternoon" / "_night".
    HABIT_SECTIONS = ("morning", "afternoon", "night")
    HABIT_SECTION_NONE = "-1"

    def get_habit_sections(self) -> List[Dict]:
        """Time-of-day sections habits are grouped into (morning/afternoon/night)."""
        data = self._request("GET", "/habitSections")
        return data if isinstance(data, list) else []

    def resolve_habit_section_id(self, section: str) -> str:
        """Id of the "_morning"/"_afternoon"/"_night" section, or "-1" when the
        account has no such section (best effort — never fails the create)."""
        want = f"_{(section or '').strip().lower().lstrip('_')}"
        try:
            for s in self.get_habit_sections():
                if str(s.get("name") or "").lower() == want:
                    return str(s.get("id"))
        except Exception as e:  # noqa: BLE001 - section is cosmetic, not identity
            logger.warning("Could not resolve habit section %r: %s", section, e)
        return self.HABIT_SECTION_NONE

    def create_habit(self, name: str, *, goal: float = 1.0, step: float = 0.0,
                     unit: str = "Count", habit_type: str = "Boolean",
                     repeat_rule: str = "RRULE:FREQ=DAILY;INTERVAL=1",
                     section: str = "morning", color: str = "#97E38B",
                     icon: str = "habit_daily_check_in",
                     encouragement: str = "") -> str:
        """Create a habit; returns the new habit's id (minted client-side).

        Raises RuntimeError when TickTick rejects the item in `id2error` —
        that map is how /batch/* reports a per-item failure while still
        answering HTTP 200 (see id2error_failures)."""
        hid = uuid.uuid4().hex[:24]
        now = self._now_iso()
        habit = {
            "id": hid,
            "name": name,
            "iconRes": icon,
            "color": color,
            "sortOrder": 0,
            "status": 0,                 # 0 = active (1 = archived)
            "encouragement": encouragement,
            "totalCheckIns": 0,
            "createdTime": now,
            "modifiedTime": now,
            "type": habit_type,          # "Boolean" | "Real"
            "goal": float(goal),
            "step": float(step),
            "unit": unit,
            "repeatRule": repeat_rule,
            "reminders": [],
            "recordEnable": False,
            "sectionId": self.resolve_habit_section_id(section),
            "targetDays": 0,
            "targetStartDate": int(datetime.now().strftime("%Y%m%d")),
            "completedCycles": 0,
            "exDates": [],
            "style": 0,
        }
        resp = self._request("POST", "/habits/batch",
                             json={"add": [habit], "update": [], "delete": []})
        err = id2error_failures(resp, [hid]).get(hid)
        if err:
            raise RuntimeError(f"TickTick rejected the habit: {err}")
        return hid

    def delete_habit(self, habit_id: str) -> Dict:
        """Delete a habit by id — TickTick drops its check-in history with it
        (irreversible; there is no habit trash). Caller is responsible for the
        identity check: this method deletes exactly the id it is given."""
        return self._request("POST", "/habits/batch",
                             json={"add": [], "update": [], "delete": [habit_id]})

    # ---- subtasks (parent/child) -----------------------------------------
    def set_task_parent(self, task_id: str, parent_id: str, project_id: str) -> Dict:
        body = [{"parentId": parent_id, "taskId": task_id, "projectId": project_id}]
        return self._request("POST", "/batch/taskParent", json=body)

    def unset_task_parent(self, task_id: str, parent_id: str, project_id: str) -> Dict:
        body = [{"oldParentId": parent_id, "taskId": task_id, "projectId": project_id}]
        return self._request("POST", "/batch/taskParent", json=body)

    def batch_set_task_parent(self, task_ids: List[str], parent_id: str,
                              project_id: str) -> Dict:
        """Nest several tasks under one parent in a single batch/taskParent call."""
        body = [{"parentId": parent_id, "taskId": tid, "projectId": project_id}
                for tid in task_ids]
        return self._request("POST", "/batch/taskParent", json=body)

    def set_task_parents(self, rows: List[Dict]) -> Dict:
        """Raw batch/taskParent: rows of {"parentId","taskId","projectId"} —
        lets the caller send each child's OWN live projectId."""
        return self._request("POST", "/batch/taskParent", json=rows)

    # ---- batch -----------------------------------------------------------
    # NOTE on merge-based updates: the v2 /batch/task endpoint takes FULL task
    # objects, so every mutation here re-posts the whole task with one field
    # changed. To keep the clobber window (a concurrent edit from the phone
    # being overwritten by a stale copy) as small as possible, the base object
    # is always fetched force-fresh immediately before the write. Minimal
    # patches are NOT attempted — the API's behaviour for partial objects is
    # undocumented and a silently-ignored partial body is worse than a small
    # race window (cf. update_task_comment: "a partial body is silently
    # ignored").
    def batch_complete_tasks(self, task_ids: List[str]) -> Dict:
        """Mark several open tasks complete in one call."""
        self.get_state(force=True)  # fresh base: shrink the clobber window
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

    def batch_update_tasks(self, changes: List[Dict]) -> Dict:
        """Apply field changes to several open tasks in one call. Each change is
        {"taskId": ..., <field>: <value>, ...}; the current task object is
        fetched force-fresh from the sync state and the given fields are merged
        onto it (see the merge-base note above batch_complete_tasks)."""
        self.get_state(force=True)  # fresh base: shrink the clobber window
        by_id = {t.get("id"): t for t in self.get_open_tasks()}
        updates = []
        for ch in changes:
            tid = ch.get("taskId") or ch.get("id")
            base = by_id.get(tid)
            if not base:
                continue
            merged = dict(base)
            for k, v in ch.items():
                if k in ("taskId",):
                    continue
                merged[k] = v
            updates.append(merged)
        if not updates:
            return {"message": "No matching open tasks found."}
        return self._request("POST", "/batch/task",
                             json={"add": [], "update": updates, "delete": []})

    # ---- project groups / folders ----------------------------------------
    def list_project_groups(self) -> List[Dict]:
        return self.get_state().get("projectGroups", []) or []

    def list_projects(self) -> List[Dict]:
        return self.get_state().get("projectProfiles", []) or []

    def create_project_group(self, name: str) -> str:
        gid = uuid.uuid4().hex[:24]
        resp = self._request("POST", "/batch/projectGroup",
                             json={"add": [{"id": gid, "name": name, "listType": "group"}],
                                   "update": [], "delete": []})
        err = id2error_failures(resp, [gid]).get(gid)
        if err:
            raise RuntimeError(f"TickTick rejected the project group: {err}")
        return gid

    def delete_project_group(self, group_id: str) -> Dict:
        return self._request("POST", "/batch/projectGroup",
                             json={"add": [], "update": [], "delete": [group_id]})

    def move_project_to_group(self, project_id: str, group_id: str) -> Dict:
        """group_id='NONE' ungroups the project. Sends the FULL live project
        object (force-fresh) with only groupId changed, so no other field is
        reverted to a stale value as a side effect of the move.

        РАЗГРУППИРОВКА (2026-08-06). До этой правки сентинел 'NONE' уходил в
        TickTick БУКВАЛЬНОЙ строкой: `upd["groupId"] = "NONE"`. «Без папки» в
        модели данных TickTick — это `groupId: null`, а не группа с именем
        NONE, поэтому запрос «вынь проект из папки» не выполнялся никогда:
        сервер получал ссылку на несуществующую группу. Контракт из этого же
        докстринга («'NONE' ungroups») выполнял только тестовый фейк
        (tests/test_tier0_gate_conversion.py сам переводил 'NONE' в None) — то
        есть трансляции не было ровно в одном месте, в бою.

        Пустая строка и None трактуются так же, как 'NONE': снаружи все три
        означают «никакой группы», и молча превратить их в id группы ""
        (ссылка в никуда) было бы тем же самым тихим отказом."""
        self.get_state(force=True)
        proj = next((p for p in self.list_projects() if p.get("id") == project_id), None)
        if not proj:
            raise ValueError(f"Project {project_id} not found.")
        upd = dict(proj)
        upd["groupId"] = None if group_id in ("NONE", "", None) else group_id
        resp = self._request("POST", "/batch/project",
                             json={"add": [], "delete": [], "update": [upd]})
        err = id2error_failures(resp, [project_id]).get(project_id)
        if err:
            raise RuntimeError(f"TickTick rejected the move: {err}")
        return resp

    # ---- task comments ---------------------------------------------------
    def get_task_comments(self, project_id: str, task_id: str) -> List[Dict]:
        data = self._request("GET", f"/project/{project_id}/task/{task_id}/comments")
        return data if isinstance(data, list) else []

    def get_task_activity(self, project_id: str = None, task_id: str = None,
                          *, skip: int = None, last_id: str = None,
                          max_pages: int = TASK_ACTIVITY_MAX_PAGES) -> List[Dict]:
        """Fetch the edit-history / activity log for a task.

        Endpoint confirmed via a live DevTools capture of TickTick's own
        "Task Activities" panel (2026-07, HTTP 200):
            GET /api/v1/task/activity/{taskId}
        Note: v1 (not v2), singular "activity" (not "activities"), and no
        projectId in the path at all. `project_id` is kept as the first
        positional arg for backward compatibility with existing callers but
        is unused in the URL.

        Pages via the same path plus query params `skip` (count of records
        already fetched) and `lastId` (id of the last activity record
        already fetched). By default this walks every page (bounded by
        max_pages) and returns the concatenated list; pass skip/last_id
        yourself to fetch a single page instead.
        """
        if not task_id:
            raise ValueError("task_id is required.")
        url = f"{ATTACHMENT_BASE}/task/activity/{task_id}"

        def _fetch_page(skip_val: Optional[int], last_id_val: Optional[str]) -> List[Dict]:
            params: Dict[str, Any] = {}
            if skip_val:
                params["skip"] = skip_val
            if last_id_val:
                params["lastId"] = last_id_val
            resp = self.session.get(url, params=params or None, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (401, 403):
                raise TickTickAuthError(
                    "TickTick v2 session token is invalid or expired. Re-extract "
                    "the `t` cookie and update TICKTICK_V2_TOKEN.")
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.text:
                return []
            try:
                data = resp.json()
            except ValueError:
                raise TickTickAuthError(
                    "TickTick v2 returned a non-JSON response (likely an HTML "
                    "login/interstitial page). Re-extract the `t` cookie and "
                    "update TICKTICK_V2_TOKEN.")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # some payload shapes wrap the list in {"items": [...]} etc.
                for key in ("items", "activities", "data"):
                    if isinstance(data.get(key), list):
                        return data[key]
            return []

        # Single explicit page requested — caller drives its own pagination.
        if skip is not None or last_id is not None:
            return _fetch_page(skip, last_id)

        # Otherwise walk every page until one comes back empty or repeats.
        events: List[Dict] = []
        cur_skip = 0
        cur_last_id: Optional[str] = None
        for _ in range(max(1, max_pages)):
            page = _fetch_page(cur_skip, cur_last_id)
            if not page:
                break
            events.extend(page)
            cur_skip += len(page)
            new_last_id = page[-1].get("id") if isinstance(page[-1], dict) else None
            if not new_last_id or new_last_id == cur_last_id:
                break
            cur_last_id = new_last_id
        return events

    def add_task_comment(self, project_id: str, task_id: str, text: str) -> Dict:
        body = {"id": uuid.uuid4().hex[:24], "title": text,
                "taskId": task_id, "projectId": project_id}
        return self._request("POST", f"/project/{project_id}/task/{task_id}/comment",
                             json=body)

    # ---- project members / shares -----------------------------------------
    def get_project_members(self, project_id: str) -> List[Dict]:
        """List users a project is shared with (owner + collaborators).
        Each entry carries userId/username/displayName and acceptance status."""
        data = self._request("GET", f"/project/{project_id}/shares")
        return data if isinstance(data, list) else []

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
        return self.batch_restore_tasks([task_id], to_project_id)

    def batch_restore_tasks(self, task_ids: List[str], to_project_id: str = None) -> Dict:
        """Restore several tasks from trash in one call. Each task's original
        list is looked up from the trash unless to_project_id overrides it."""
        trashed = self.get_trash(limit=500)
        by_id = {x.get("id"): x for x in trashed}
        body = []
        missing = []
        for tid in task_ids:
            t = by_id.get(tid)
            if not t:
                missing.append(tid)
                continue
            from_pid = t.get("projectId")
            body.append({"fromProjectId": from_pid, "taskId": tid,
                         "toProjectId": to_project_id or from_pid})
        if missing:
            raise ValueError(f"Task(s) not found in trash: {', '.join(missing)}")
        return self._request("POST", "/trash/restore", json=body)

    # ---- attachments -----------------------------------------------------
    def get_task_attachments(self, task_id: str) -> List[Dict]:
        """Raw attachment metadata dicts for an OPEN task, straight from the
        /batch/check/0 sync feed (whatever keys TickTick sends — not all
        accounts/attachments carry the same fields, so callers should not
        assume e.g. 'fileSize' or 'fileUrl' are always present)."""
        task = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Open task {task_id} not found.")
        return task.get("attachments") or []

    def get_content_attachment_refs(self, task_id: str) -> List[Dict]:
        """Fallback/cross-check source: TickTick embeds inline attachment
        refs directly in a task's content/desc as markdown-ish tokens:
            ![file](<24-hex attachmentId>/<url-encoded fileName>)
        Parsed straight from the task text — useful when the structured
        `attachments` array is missing an id field for this account/attachment
        (observed in practice: the sync feed's attachments entries can carry
        fileName without an id), or as a second opinion to cross-check it."""
        task = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Open task {task_id} not found.")
        text = (task.get("content") or "") + "\n" + (task.get("desc") or "")
        refs = []
        for m in re.finditer(r"!\[file\]\(([0-9a-fA-F]{24})/([^)]+)\)", text):
            att_id, enc_name = m.group(1), m.group(2)
            try:
                name = urllib.parse.unquote(enc_name)
            except Exception:
                name = enc_name
            refs.append({"id": att_id, "fileName": name})
        return refs

    def download_attachment(self, project_id: str, task_id: str,
                            attachment_id: str, filename: str = None
                            ) -> "tuple[str, bytes, str]":
        """Download a file attachment's bytes. Endpoint mirrors the (known
        working) upload path minus the '/upload' segment — confirmed by
        probing: this exact 3-segment v1 shape returns 401 (route exists,
        needs auth) while the v2 equivalent and other shapes 404:
            GET /api/v1/attachment/{projectId}/{taskId}/{attachmentId}
        Returns (filename, content_bytes, mime_type)."""
        url = f"{ATTACHMENT_BASE}/attachment/{project_id}/{task_id}/{attachment_id}"
        resp = self.session.get(url, timeout=60)
        if resp.status_code in (401, 403):
            raise TickTickAuthError(
                "TickTick v2 session token is invalid or expired. Re-extract "
                "the `t` cookie and update TICKTICK_V2_TOKEN.")
        if resp.status_code == 404:
            raise ValueError(
                f"Attachment {attachment_id} not found on task {task_id} "
                "(wrong id, or the file was removed).")
        resp.raise_for_status()
        data = resp.content
        if len(data) > ATTACHMENT_MAX_BYTES:
            raise ValueError(
                f"File is {len(data) // (1024*1024)} MB; refusing to load "
                f"more than {ATTACHMENT_MAX_BYTES // (1024*1024)} MB into memory.")

        resolved_name = filename
        if not resolved_name:
            cd = resp.headers.get("Content-Disposition", "")
            m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
            if m:
                try:
                    resolved_name = urllib.parse.unquote(m.group(1))
                except Exception:
                    resolved_name = m.group(1)
        resolved_name = resolved_name or f"attachment_{attachment_id}"

        mime = resp.headers.get("Content-Type")
        if not mime or mime == "application/octet-stream":
            mime = mimetypes.guess_type(resolved_name)[0] or mime or "application/octet-stream"
        return resolved_name, data, mime

    def open_attachment_stream(self, project_id: str, task_id: str,
                               attachment_id: str) -> "requests.Response":
        """Same endpoint as download_attachment, but STREAMING: returns the raw
        (already status-checked) requests.Response with stream=True, so the
        caller can relay the bytes onward without ever holding the whole file in
        memory. Caller owns the response and MUST close() it.
        No size cap here on purpose — nothing is buffered."""
        url = f"{ATTACHMENT_BASE}/attachment/{project_id}/{task_id}/{attachment_id}"
        resp = self.session.get(url, timeout=60, stream=True)
        if resp.status_code in (401, 403):
            resp.close()
            raise TickTickAuthError(
                "TickTick v2 session token is invalid or expired. Re-extract "
                "the `t` cookie and update TICKTICK_V2_TOKEN.")
        if resp.status_code == 404:
            resp.close()
            raise ValueError(
                f"Attachment {attachment_id} not found on task {task_id} "
                "(wrong id, or the file was removed).")
        try:
            resp.raise_for_status()
        except Exception:
            resp.close()
            raise
        return resp

    def upload_attachment_bytes(self, project_id: str, task_id: str,
                                attachment_id: str, data: bytes,
                                filename: str = None) -> Dict:
        """The raw upload request, with the attachmentId supplied by the caller
        (24-hex; TickTick lets the client mint it). Used both by
        upload_attachment below and by the relay endpoint, so the two can never
        drift apart. Note what this deliberately does NOT do: it does not touch
        the task's content/desc, i.e. no `![file](id/name)` marker is written —
        TickTick's own upload apparently attaches the file without it, and this
        mirrors that exactly."""
        filename = filename or f"attachment_{attachment_id}"
        if len(data) > ATTACHMENT_MAX_BYTES:
            raise ValueError(
                f"File is {len(data) // (1024*1024)} MB; TickTick caps attachments at 20 MB.")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        upload_url = (f"{ATTACHMENT_BASE}/attachment/upload/"
                      f"{project_id}/{task_id}/{attachment_id}")
        self._state_cache = None  # task now has an attachment
        # Drop the JSON content-type so requests sets the multipart boundary;
        # cookie + x-device come from the session.
        resp = self.session.post(upload_url,
                                 files={"file": (filename, data, mime)},
                                 headers={"Content-Type": None}, timeout=60)
        if resp.status_code in (401, 403):
            raise TickTickAuthError(
                "TickTick v2 session token is invalid or expired. Re-extract "
                "the `t` cookie and update TICKTICK_V2_TOKEN.")
        resp.raise_for_status()
        if not resp.text:
            return {}
        try:
            return resp.json()
        except ValueError:
            # Upload succeeded (2xx) but body wasn't JSON; don't crash the tool.
            logger.warning("Attachment upload returned a non-JSON body.")
            return {}

    def upload_attachment(self, project_id: str, task_id: str, *,
                          url: str = None, content_base64: str = None,
                          filename: str = None) -> Dict:
        """Upload a file attachment to a task. Source is either a URL (the
        server downloads it) or base64 content. Endpoint:
        POST /api/v1/attachment/upload/{projectId}/{taskId}/{attachmentId},
        multipart with a single `file` field."""
        if url:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            data = r.content
            if not filename:
                filename = url.split("?")[0].rstrip("/").split("/")[-1] or "attachment"
        elif content_base64:
            data = base64.b64decode(content_base64)
            filename = filename or "attachment"
        else:
            raise ValueError("Provide either url or content_base64.")

        return self.upload_attachment_bytes(
            project_id, task_id, new_attachment_id(), data, filename=filename)

    # ---- smart-list (filter) execution -----------------------------------
    def run_filter(self, filter_id_or_name: str) -> List[Dict]:
        """Matching open tasks only. Prefer run_filter_detailed() where the
        caller can tell the user that part of the rule was not applied."""
        return self.run_filter_detailed(filter_id_or_name)[0]

    def run_filter_detailed(self, filter_id_or_name: str) -> tuple:
        """Fetch all open tasks and return those matching a saved filter's rule,
        which TickTick evaluates client-side (no server endpoint exists).

        Returns (tasks, unsupported_conditions). The second element names the
        rule conditions this client could not evaluate; those conditions
        narrowed nothing, so the caller MUST say so rather than present the
        result as fully filtered."""
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
        owner_id = self._owner_user_id()  # resolves the `assignee: me` token
        tasks = state.get("syncTaskBean", {}).get("update", []) or []
        matched = [t for t in tasks
                   if _rule_matches(t, rule, inbox, proj_group, owner_id)]
        return matched, rule_unsupported_conditions(rule)

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
        self.get_state(force=True)  # fresh base: shrink the clobber window
        task = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Open task {task_id} not found.")
        task = dict(task)
        task["tags"] = [t.lstrip("#").lower() for t in tags]
        return self._request("POST", "/batch/task",
                             json={"add": [], "update": [task], "delete": []})

    def _owner_user_id(self) -> Optional[int]:
        """Numeric owner id, parsed from the sync inboxId ('inbox<userId>').
        The v2 /column add payload wants it; TickTick derives its own from the
        session, so a missing value is tolerated."""
        inbox = self.inbox_id or self.get_state().get("inboxId") or ""
        if inbox.startswith("inbox"):
            try:
                return int(inbox[len("inbox"):])
            except ValueError:
                return None
        return None

    def get_project_columns(self, project_id: str) -> List[Dict]:
        """List a project's kanban columns/sections (v2), sorted by position."""
        data = self._request("GET", f"/column/project/{project_id}")
        cols = data if isinstance(data, list) else []
        return sorted(cols, key=lambda c: c.get("sortOrder", 0) or 0)

    def create_column(self, project_id: str, name: str) -> str:
        """Create a kanban column/section in a project and return its (client-
        generated) id. The new column is appended after any existing ones.
        Uses the v2 `/column` batch endpoint, which reports per-item failures
        in `id2error`."""
        existing = self.get_project_columns(project_id)
        if existing:
            max_sort = max((c.get("sortOrder", 0) or 0) for c in existing)
            sort_order = max_sort + COLUMN_SORT_STEP
        else:
            sort_order = 0
        cid = uuid.uuid4().hex[:24]
        column = {
            "id": cid,
            "userId": self._owner_user_id(),
            "createdTime": self._now_iso(),
            "name": name,
            "projectId": project_id,
            "sortOrder": sort_order,
        }
        resp = self._request("POST", "/column",
                             json={"add": [column], "update": [], "delete": []})
        if isinstance(resp, dict):
            err = (resp.get("id2error") or {}).get(cid)
            if err:
                raise RuntimeError(f"TickTick rejected the column: {err}")
        return cid

    def set_task_column(self, task_id: str, column_id: str) -> Dict:
        """Move a task to a kanban column/section (v2 `columnId`)."""
        self.get_state(force=True)  # fresh base: shrink the clobber window
        task = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Open task {task_id} not found.")
        task = dict(task)
        task["columnId"] = column_id
        return self._request("POST", "/batch/task",
                             json={"add": [], "update": [task], "delete": []})

    # ---- won't-do / duplicate -------------------------------------------
    def abandon_task(self, task_id: str) -> Dict:
        """Mark a task 'Won't do' (v2 status -1)."""
        self.get_state(force=True)  # fresh base: shrink the clobber window
        task = next((t for t in self.get_open_tasks() if t.get("id") == task_id), None)
        if not task:
            raise ValueError(f"Open task {task_id} not found.")
        task = dict(task)
        task["status"] = -1
        return self._request("POST", "/batch/task",
                             json={"add": [], "update": [task], "delete": []})

    def duplicate_task(self, task_id: str) -> Dict:
        """Create a copy of an open task. NOT carried over (v2 quirks): the
        checklist items, the kanban column, and the parent link — callers must
        say so honestly. Raises RuntimeError if TickTick rejects the create."""
        self.get_state(force=True)  # fresh source: copy the CURRENT task
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
        resp = self.batch_create_tasks([copy])
        err = id2error_failures(resp, [copy["id"]]).get(copy["id"])
        if err:
            raise RuntimeError(f"TickTick rejected the duplicate: {err}")
        return copy

    # ---- comments edit/delete -------------------------------------------
    def update_task_comment(self, project_id: str, task_id: str,
                            comment_id: str, text: str) -> Dict:
        """Edit a comment. The API needs the FULL comment object PUT to
        /comment/{id} (id in the path) — a partial body is silently ignored."""
        comments = self.get_task_comments(project_id, task_id)
        cm = next((c for c in comments if c.get("id") == comment_id), None)
        if not cm:
            raise ValueError(f"Comment {comment_id} not found.")
        cm = dict(cm)
        cm["title"] = text
        return self._request(
            "PUT", f"/project/{project_id}/task/{task_id}/comment/{comment_id}",
            json=cm)

    def delete_task_comment(self, project_id: str, task_id: str,
                            comment_id: str) -> Dict:
        return self._request(
            "DELETE", f"/project/{project_id}/task/{task_id}/comment/{comment_id}")

    # ---- project archive -------------------------------------------------
    def archive_project(self, project_id: str, closed: bool = True) -> Dict:
        """Archive/unarchive a project. The base object is fetched force-fresh
        immediately before the write so a concurrent in-app rename/recolor
        isn't reverted; a per-item rejection raises instead of passing silently."""
        self.get_state(force=True)
        proj = next((p for p in self.list_projects() if p.get("id") == project_id), None)
        if not proj:
            raise ValueError(f"Project {project_id} not found.")
        upd = dict(proj)
        upd["closed"] = closed
        resp = self._request("POST", "/batch/project",
                             json={"add": [], "delete": [], "update": [upd]})
        err = id2error_failures(resp, [project_id]).get(project_id)
        if err:
            raise RuntimeError(f"TickTick rejected the archive change: {err}")
        return resp


# ---- filter rule evaluation (client-side, mirrors the TickTick web app) ----

# Conditions this client can actually evaluate. Anything outside this set is
# NOT quietly treated as "matches everything" — see rule_unsupported_conditions
# and the warning run_filter's caller prints.
SUPPORTED_FILTER_CONDITIONS = frozenset({
    "list", "listOrGroup", "tag", "priority", "dueDate", "assignee",
})


def _node_children(node: Dict):
    """(items, combine) for one rule node — the single place that knows how a
    node exposes its children, shared by the matcher and by the static
    unsupported-condition scan so the two cannot drift apart."""
    items = node.get("or")
    if items is None:
        return (node.get("and") or []), all
    return items, any


def rule_unsupported_conditions(rule: Dict) -> List[str]:
    """Condition names present in a saved filter's rule that this client
    cannot evaluate (in rule order, de-duplicated).

    Read STATICALLY off the rule, not collected while matching: `all`/`any`
    short-circuit, and an empty task list would evaluate no leaf at all, so a
    match-time collection would report an unsupported condition only
    sometimes. The caller uses this to tell the user their result is NOT
    filtered by that condition instead of passing a full pool off as a
    filtered one."""
    found: List[str] = []

    def walk(node: Dict) -> None:
        items, _ = _node_children(node)
        if not items:
            return
        if isinstance(items[0], dict):
            for it in items:
                walk(it)
            return
        name = node.get("conditionName")
        if name not in SUPPORTED_FILTER_CONDITIONS and name not in found:
            found.append(name)

    for group in (rule.get("and") or []):
        walk(group)
    return found


def _rule_matches(task: Dict, rule: Dict, inbox: str, proj_group: Dict,
                  owner_id=None) -> bool:
    groups = rule.get("and") or []
    if not groups:
        return True  # empty rule = everything
    return all(_node_matches(task, g, inbox, proj_group, owner_id) for g in groups)


def _node_matches(task: Dict, node: Dict, inbox: str, proj_group: Dict,
                  owner_id=None) -> bool:
    items, combine = _node_children(node)
    if not items:
        return True
    # Nested condition objects → recurse.
    if items and isinstance(items[0], dict):
        return combine(_node_matches(task, it, inbox, proj_group, owner_id) for it in items)
    return _leaf_matches(task, node.get("conditionName"), items, inbox, proj_group,
                         owner_id)


def _assignee_matches(task, values, owner_id) -> bool:
    """TickTick's `assignee` condition. Values are the web app's own tokens:
    'noassignee' (nobody assigned), 'me' (the account owner), or a member's
    numeric userId. Ids are compared as strings — the sync payload is not
    consistent about int vs str."""
    assignee = task.get("assignee")
    unassigned = assignee in (None, "", 0)
    for v in values:
        if v == "noassignee":
            if unassigned:
                return True
        elif v == "me":
            if not unassigned and owner_id is not None and str(assignee) == str(owner_id):
                return True
        elif not unassigned and str(assignee) == str(v):
            return True
    return False


def _leaf_matches(task, name, values, inbox, proj_group, owner_id=None) -> bool:
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
    if name == "assignee":
        return _assignee_matches(task, values, owner_id)
    # Unknown condition: don't exclude — but this is NOT silent. The condition
    # name is reported separately by rule_unsupported_conditions() and surfaced
    # in the tool output, because "matches everything" used to make an
    # unfiltered pool (all 1477 tasks, live 2026-08-07) look like a result.
    return True


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
