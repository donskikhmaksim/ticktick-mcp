import os
import re
import time
import json
import base64
import threading
import requests
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from typing import Dict, List

from zoneinfo import ZoneInfo

# Set up logging
logger = logging.getLogger(__name__)

# Hard cap on every TickTick HTTP call so a stalled connection can never hang
# the whole MCP request indefinitely.
REQUEST_TIMEOUT = 20

# Durable token store on a Railway Volume (survives restarts/redeploys without
# needing a Railway API token or manual paste into Variables). Defaults to a
# file under /data; set TICKTICK_TOKEN_STORE to override or "" to disable.
TOKEN_STORE_PATH = os.getenv("TICKTICK_TOKEN_STORE", "/data/ticktick_tokens.json").strip()


def load_token_file() -> Dict[str, str]:
    """Read persisted tokens from the volume file, or {} if absent/unreadable."""
    if not TOKEN_STORE_PATH:
        return {}
    try:
        with open(TOKEN_STORE_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_token_file(tokens: Dict[str, str]) -> bool:
    """Persist tokens to the volume file. Best-effort: returns False (never
    raises) if the directory isn't writable (e.g. no volume mounted)."""
    if not TOKEN_STORE_PATH:
        return False
    try:
        path = Path(TOKEN_STORE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = load_token_file()
        merged = {**existing}
        for k in ("access_token", "refresh_token"):
            if tokens.get(k):
                merged[k] = tokens[k]
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(merged, f)
        tmp.replace(path)  # atomic
        return True
    except OSError as e:
        logger.warning(f"Volume token persistence unavailable ({e}); "
                       "tokens will not survive a restart unless set in Railway Variables.")
        return False

_DATE_ONLY = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# User's local timezone for all-day date storage. Set USER_TIMEZONE env var
# (e.g. "America/Los_Angeles") so midnight means local midnight, not UTC midnight.
_USER_TZ = ZoneInfo(os.getenv("USER_TIMEZONE", "UTC"))


def _normalize_date(value):
    """If a date is given without a time (YYYY-MM-DD), it's an all-day date:
    return (local-midnight-padded value, True). Otherwise return (value, False).
    Midnight is in the user's local timezone (USER_TIMEZONE env var) so TickTick
    stores the correct calendar date rather than off-by-one due to UTC conversion."""
    if value and _DATE_ONLY.match(value.strip()):
        naive = datetime.fromisoformat(value.strip() + "T00:00:00")
        local_midnight = naive.replace(tzinfo=_USER_TZ)
        offset = local_midnight.utcoffset()
        total_minutes = int(offset.total_seconds() / 60)
        sign = '+' if total_minutes >= 0 else '-'
        h, m = divmod(abs(total_minutes), 60)
        tz_str = f"{sign}{h:02d}{m:02d}"
        return value.strip() + f"T00:00:00{tz_str}", True
    return value, False

class TickTickClient:
    """
    Client for the TickTick API using OAuth2 authentication.
    """

    # Optional callback invoked after any successful write (POST/DELETE), so
    # the v2 client can drop its cached sync state and stay consistent.
    write_hook = None

    def __init__(self):
        load_dotenv()
        # Reuse one TCP/TLS connection across calls (keep-alive) instead of a
        # fresh handshake per request — noticeably faster for batches.
        self.session = requests.Session()
        self.client_id = os.getenv("TICKTICK_CLIENT_ID")
        self.client_secret = os.getenv("TICKTICK_CLIENT_SECRET")
        # Prefer tokens persisted to the volume (freshest after a refresh or a
        # /setup that happened on a previous container), falling back to env.
        persisted = load_token_file()
        self.access_token = persisted.get("access_token") or os.getenv("TICKTICK_ACCESS_TOKEN")
        self.refresh_token = persisted.get("refresh_token") or os.getenv("TICKTICK_REFRESH_TOKEN")

        # Serialize token refreshes: without this, two concurrent 401s both
        # refresh, and the second one uses an already-rotated (consumed)
        # refresh token, permanently breaking auth.
        self._refresh_lock = threading.Lock()

        if not self.access_token:
            raise ValueError("TICKTICK_ACCESS_TOKEN environment variable is not set. "
                            "Please run 'uv run -m ticktick_mcp.authenticate' to set up your credentials.")
            
        self.base_url = os.getenv("TICKTICK_BASE_URL") or "https://api.ticktick.com/open/v1"
        self.token_url = os.getenv("TICKTICK_TOKEN_URL") or "https://ticktick.com/oauth/token"
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept-Encoding": None,
            "User-Agent": 'curl/8.7.1'
        }
    
    def _refresh_access_token(self, prev_access_token: str = None) -> bool:
        """
        Refresh the access token using the refresh token.

        Thread-safe: only one refresh runs at a time. If another thread already
        refreshed the token while this one waited for the lock (detected via
        prev_access_token no longer matching the current one), this call skips
        the network round-trip and returns success — so a rotated refresh token
        is never consumed twice.

        Args:
            prev_access_token: the access token the caller saw 401 on; used to
                detect that another thread already refreshed under the lock.

        Returns:
            True if a valid (possibly already-refreshed) token is in place.
        """
        if not self.refresh_token:
            logger.warning("No refresh token available. Cannot refresh access token.")
            return False

        if not self.client_id or not self.client_secret:
            logger.warning("Client ID or Client Secret missing. Cannot refresh access token.")
            return False

        with self._refresh_lock:
            # Another thread may have refreshed while we waited for the lock.
            # If so, our old (prev) token differs from the current one — reuse it.
            if prev_access_token is not None and self.access_token != prev_access_token:
                logger.info("Token already refreshed by another thread; reusing it.")
                return True

            # Prepare the token request
            token_data = {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token
            }

            # Prepare Basic Auth credentials
            auth_str = f"{self.client_id}:{self.client_secret}"
            auth_bytes = auth_str.encode('ascii')
            auth_b64 = base64.b64encode(auth_bytes).decode('ascii')

            headers = {
                "Authorization": f"Basic {auth_b64}",
                "Content-Type": "application/x-www-form-urlencoded"
            }

            try:
                # Send the token request
                response = requests.post(self.token_url, data=token_data, headers=headers, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()

                # Parse the response — guard against a 200 with a non-JSON body.
                try:
                    tokens = response.json()
                except ValueError:
                    logger.error("Token refresh returned a non-JSON body; cannot refresh.")
                    return False

                # Update the tokens
                self.access_token = tokens.get('access_token')
                if 'refresh_token' in tokens:
                    self.refresh_token = tokens.get('refresh_token')

                # Update the headers
                self.headers["Authorization"] = f"Bearer {self.access_token}"

                # Persist the rotated tokens so they survive a container restart.
                self._persist_tokens(tokens)

                logger.info("Access token refreshed successfully.")
                return True

            except requests.exceptions.RequestException as e:
                logger.error(f"Error refreshing access token: {e}")
                return False

    def _persist_tokens(self, tokens: Dict[str, str]) -> None:
        """
        Persist refreshed tokens so they survive a process/container restart.

        Isolated, pluggable, and defensive: any failure here is logged but never
        raised, so an in-memory refresh still succeeds even if persistence fails.

        On Railway the working-directory .env is EPHEMERAL — a refresh-token
        rotation followed by a restart would break auth permanently. So when
        Railway API credentials are present, upsert the variables via Railway's
        GraphQL API. Otherwise (local dev) fall back to writing ./.env.
        """
        # 1) Durable volume file (default on Railway with a mounted /data volume).
        try:
            if save_token_file(tokens):
                return
        except Exception as e:
            logger.warning(f"Volume token persistence failed (continuing): {e}")

        # 2) Railway API (only if an API token is configured).
        try:
            if self._persist_tokens_to_railway(tokens):
                return
        except Exception as e:
            logger.warning(f"Railway token persistence failed (continuing): {e}")

        # 3) Local .env (dev fallback).
        try:
            self._save_tokens_to_env(tokens)
        except Exception as e:
            logger.warning(f"Local .env token persistence failed (continuing): {e}")

    def _persist_tokens_to_railway(self, tokens: Dict[str, str]) -> bool:
        """
        Upsert TICKTICK_ACCESS_TOKEN/TICKTICK_REFRESH_TOKEN into the Railway
        service via the GraphQL variableUpsert mutation, so they persist across
        restarts. Returns True if Railway persistence was attempted and all
        upserts succeeded; False if Railway env is not configured (fall back to
        .env). Raises on a hard failure so the caller can log a warning.
        """
        api_token = os.getenv("RAILWAY_API_TOKEN") or os.getenv("RAILWAY_TOKEN")
        service_id = os.getenv("RAILWAY_SERVICE_ID")
        project_id = os.getenv("RAILWAY_PROJECT_ID")
        environment_id = os.getenv("RAILWAY_ENVIRONMENT_ID")
        if not (api_token and service_id and project_id):
            return False  # not on Railway with API access → caller uses .env

        to_set = {"TICKTICK_ACCESS_TOKEN": tokens.get("access_token", "")}
        if tokens.get("refresh_token"):
            to_set["TICKTICK_REFRESH_TOKEN"] = tokens["refresh_token"]

        mutation = (
            "mutation variableUpsert($input: VariableUpsertInput!) {"
            " variableUpsert(input: $input) }"
        )
        endpoint = os.getenv("RAILWAY_API_URL", "https://backboard.railway.app/graphql/v2")
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        for name, value in to_set.items():
            variables = {
                "input": {
                    "projectId": project_id,
                    "serviceId": service_id,
                    "name": name,
                    "value": value,
                }
            }
            if environment_id:
                variables["input"]["environmentId"] = environment_id
            resp = requests.post(endpoint, json={"query": mutation, "variables": variables},
                                 headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError:
                raise RuntimeError("Railway API returned a non-JSON response.")
            if body.get("errors"):
                raise RuntimeError(f"Railway variableUpsert error: {body['errors']}")

        logger.info("Refreshed tokens persisted to Railway variables.")
        return True

    def _save_tokens_to_env(self, tokens: Dict[str, str]) -> None:
        """
        Save the tokens to the .env file.
        
        Args:
            tokens: A dictionary containing the access_token and optionally refresh_token
        """
        # Load existing .env file content
        env_path = Path('.env')
        env_content = {}
        
        if env_path.exists():
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        env_content[key] = value
        
        # Update with new tokens
        env_content["TICKTICK_ACCESS_TOKEN"] = tokens.get('access_token', '')
        if 'refresh_token' in tokens:
            env_content["TICKTICK_REFRESH_TOKEN"] = tokens.get('refresh_token', '')
        
        # Make sure client credentials are saved as well
        if self.client_id and "TICKTICK_CLIENT_ID" not in env_content:
            env_content["TICKTICK_CLIENT_ID"] = self.client_id
        if self.client_secret and "TICKTICK_CLIENT_SECRET" not in env_content:
            env_content["TICKTICK_CLIENT_SECRET"] = self.client_secret
        
        # Write back to .env file
        with open(env_path, 'w') as f:
            for key, value in env_content.items():
                f.write(f"{key}={value}\n")
        
        logger.debug("Tokens saved to .env file")
    
    def _make_request(self, method: str, endpoint: str, data=None) -> Dict:
        """
        Makes a request to the TickTick API.
        
        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (without base URL)
            data: Request data (for POST, PUT)
        
        Returns:
            API response as a dictionary
        """
        url = f"{self.base_url}{endpoint}"
        
        try:
            def _issue():
                if method == "GET":
                    return self.session.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
                elif method == "POST":
                    return self.session.post(url, headers=self.headers, json=data, timeout=REQUEST_TIMEOUT)
                elif method == "DELETE":
                    return self.session.delete(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")

            # Make the request
            response = _issue()

            # Retry on 429/5xx with exponential backoff (1s, 2s) — rate limits
            # on bursts usually clear after a short wait.
            for _attempt in range(2):
                if response.status_code not in (429, 500, 503):
                    break
                time.sleep(2 ** _attempt)
                response = _issue()

            # Check if the request was unauthorized (401)
            if response.status_code == 401:
                logger.info("Access token expired. Attempting to refresh...")

                # Try to refresh the access token (pass the token we saw 401 on
                # so a concurrent refresh under the lock isn't repeated).
                if self._refresh_access_token(prev_access_token=self.access_token):
                    # Retry the request with the new token
                    if method == "GET":
                        response = self.session.get(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
                    elif method == "POST":
                        response = self.session.post(url, headers=self.headers, json=data, timeout=REQUEST_TIMEOUT)
                    elif method == "DELETE":
                        response = self.session.delete(url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            
            # Raise an exception for 4xx/5xx status codes
            response.raise_for_status()

            # A write succeeded — let the v2 client drop its cached sync state
            # so subsequent v2 reads reflect this change immediately.
            if method != "GET" and TickTickClient.write_hook:
                try:
                    TickTickClient.write_hook()
                except Exception:
                    pass

            # Return empty dict for 204 No Content
            if response.status_code == 204 or response.text == "":
                return {}

            # Guard against a 200 with a non-JSON body (e.g. a Cloudflare/HTML
            # interstitial): return the normal error convention instead of
            # letting JSONDecodeError escape.
            try:
                return response.json()
            except ValueError:
                logger.error("API returned a non-JSON response body.")
                return {"error": "Non-JSON response from TickTick API."}
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            return {"error": str(e)}
    
    # Project methods
    def get_projects(self) -> List[Dict]:
        """Gets all projects for the user."""
        return self._make_request("GET", "/project")
    
    def get_project(self, project_id: str) -> Dict:
        """Gets a specific project by ID."""
        return self._make_request("GET", f"/project/{project_id}")
    
    def get_project_with_data(self, project_id: str) -> Dict:
        """Gets project with tasks and columns."""
        return self._make_request("GET", f"/project/{project_id}/data")
    
    def create_project(self, name: str, color: str = "#F18181", view_mode: str = "list", kind: str = "TASK") -> Dict:
        """Creates a new project."""
        data = {
            "name": name,
            "color": color,
            "viewMode": view_mode,
            "kind": kind
        }
        return self._make_request("POST", "/project", data)
    
    def update_project(self, project_id: str, name: str = None, color: str = None, 
                       view_mode: str = None, kind: str = None) -> Dict:
        """Updates an existing project."""
        data = {}
        if name:
            data["name"] = name
        if color:
            data["color"] = color
        if view_mode:
            data["viewMode"] = view_mode
        if kind:
            data["kind"] = kind
            
        return self._make_request("POST", f"/project/{project_id}", data)
    
    def delete_project(self, project_id: str) -> Dict:
        """Deletes a project."""
        return self._make_request("DELETE", f"/project/{project_id}")
    
    # Task methods
    def get_task(self, project_id: str, task_id: str) -> Dict:
        """Gets a specific task by project ID and task ID."""
        return self._make_request("GET", f"/project/{project_id}/task/{task_id}")
    
    def create_task(self, title: str, project_id: str, content: str = None, 
                   start_date: str = None, due_date: str = None, 
                   priority: int = 0, is_all_day: bool = False,
                   repeat_flag: str = None, reminders: List[str] = None) -> Dict:
        """Creates a new task."""
        data = {
            "title": title,
            "projectId": project_id
        }
        
        if content:
            data["content"] = content
        date_only = False
        if start_date:
            start_date, d = _normalize_date(start_date)
            date_only = date_only or d
            data["startDate"] = start_date
        if due_date:
            due_date, d = _normalize_date(due_date)
            date_only = date_only or d
            data["dueDate"] = due_date
        if priority is not None:
            data["priority"] = priority
        # A date with no time = an all-day task; don't invent a clock time.
        if date_only:
            is_all_day = True
        if is_all_day is not None:
            data["isAllDay"] = is_all_day
        if repeat_flag:
            data["repeatFlag"] = repeat_flag
        if reminders:
            data["reminders"] = reminders

        return self._make_request("POST", "/task", data)
    
    def update_task(self, task_id: str, project_id: str, title: str = None,
                   content: str = None, priority: int = None,
                   start_date: str = None, due_date: str = None,
                   repeat_flag: str = None, reminders: List[str] = None) -> Dict:
        """Updates an existing task."""
        data = {
            "id": task_id,
            "projectId": project_id
        }
        
        if title:
            data["title"] = title
        if content:
            data["content"] = content
        if priority is not None:
            data["priority"] = priority
        date_only = False
        if start_date:
            start_date, d = _normalize_date(start_date)
            date_only = date_only or d
            data["startDate"] = start_date
        if due_date:
            due_date, d = _normalize_date(due_date)
            date_only = date_only or d
            data["dueDate"] = due_date
        if date_only:
            data["isAllDay"] = True
        if repeat_flag:
            data["repeatFlag"] = repeat_flag
        if reminders:
            data["reminders"] = reminders

        return self._make_request("POST", f"/task/{task_id}", data)
    
    def complete_task(self, project_id: str, task_id: str) -> Dict:
        """Marks a task as complete."""
        return self._make_request("POST", f"/project/{project_id}/task/{task_id}/complete")
    
    def delete_task(self, project_id: str, task_id: str) -> Dict:
        """Deletes a task."""
        return self._make_request("DELETE", f"/project/{project_id}/task/{task_id}")
    
    def create_subtask(self, subtask_title: str, parent_task_id: str, project_id: str, 
                      content: str = None, priority: int = 0) -> Dict:
        """
        Creates a subtask for a parent task within the same project.
        
        Args:
            subtask_title: Title of the subtask
            parent_task_id: ID of the parent task
            project_id: ID of the project (must be same for both parent and subtask)
            content: Optional content/description for the subtask
            priority: Priority level (0-3, where 3 is highest)
        
        Returns:
            API response as a dictionary containing the created subtask
        """
        data = {
            "title": subtask_title,
            "projectId": project_id,
            "parentId": parent_task_id
        }
        
        if content:
            data["content"] = content
        if priority is not None:
            data["priority"] = priority
            
        return self._make_request("POST", "/task", data)