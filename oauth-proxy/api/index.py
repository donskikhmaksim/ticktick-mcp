import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import urllib.parse
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()

logger = logging.getLogger("oauth-proxy")

CLIENT_ID = os.environ["TICKTICK_CLIENT_ID"]
CLIENT_SECRET = os.environ["TICKTICK_CLIENT_SECRET"]
REDIRECT_URI = os.environ["REDIRECT_URI"]  # fixed, pre-registered in TickTick dev console

# Secret used to HMAC-sign the OAuth `state` so the return_to + secret pair
# carried through TickTick's authorize round-trip can't be forged or tampered
# with. Read from env; if unset, mint an ephemeral one (fine for a single
# process, but set PROXY_STATE_SECRET in production so it survives restarts and
# multiple replicas).
STATE_SECRET = os.environ.get("PROXY_STATE_SECRET") or secrets.token_hex(32)

# Only allow forwarding tokens back to hosts we trust. Each friend's instance
# lives on a Railway subdomain, so by default we accept https hosts ending in
# `.up.railway.app`. Override / extend via a comma-separated env allowlist.
ALLOWED_RETURN_SUFFIXES = tuple(
    s.strip()
    for s in os.environ.get("PROXY_ALLOWED_RETURN_SUFFIXES", ".up.railway.app").split(",")
    if s.strip()
)

# Every friend's own instance has a different, randomly assigned Railway
# domain — TickTick only accepts a redirect_uri that's pre-registered for the
# OAuth app, and registering one per instance isn't self-service. So this
# proxy owns the ONE registered redirect_uri, and after exchanging the code
# for tokens it forwards the browser to the friend's own instance
# (return_to + secret carried through `state`) — the instance's own
# /auth/accept route picks the tokens up and hot-swaps its in-memory client.
# TickTick client secret never leaves this proxy.


def _return_to_is_allowed(return_to: str) -> bool:
    """https + host must end in one of the allowed suffixes."""
    try:
        parsed = urlparse(return_to)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == suf.lstrip(".") or host.endswith(suf) for suf in ALLOWED_RETURN_SUFFIXES)


def _sign(payload_b64: str) -> str:
    return hmac.new(STATE_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def _make_state(return_to: str, secret: str) -> str:
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps({"return_to": return_to, "secret": secret}).encode()
    ).decode()
    sig = _sign(payload_b64)
    return f"{payload_b64}.{sig}"


def _read_state(state: str):
    """Verify signature and return (return_to, secret) or None on any failure."""
    try:
        payload_b64, sig = state.rsplit(".", 1)
    except ValueError:
        return None
    expected = _sign(payload_b64)
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
        return payload["return_to"], payload["secret"]
    except Exception:
        return None


@app.get("/start")
def start(return_to: str, secret: str):
    return_to = return_to.rstrip("/")
    if not _return_to_is_allowed(return_to):
        logger.warning("rejected /start: return_to not allowed (host=%r)", urlparse(return_to).hostname)
        return HTMLResponse(_error_page("return_to не разрешён"), status_code=400)

    state = _make_state(return_to, secret)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "tasks:read tasks:write",
        "state": state,
    }
    return RedirectResponse("https://ticktick.com/oauth/authorize?" + urllib.parse.urlencode(params))


@app.get("/callback")
def callback(code: str = None, state: str = None, error: str = None):
    if error or not code or not state:
        return HTMLResponse(_error_page(error or "no code/state returned"), status_code=400)

    parsed = _read_state(state)
    if parsed is None:
        logger.warning("rejected /callback: state signature verification failed")
        return HTMLResponse(_error_page("invalid state"), status_code=400)
    return_to, friend_secret = parsed
    return_to = return_to.rstrip("/")

    # Re-validate after verifying the signature — belt and suspenders in case
    # the allowlist tightened between /start and /callback.
    if not _return_to_is_allowed(return_to):
        logger.warning("rejected /callback: return_to not allowed (host=%r)", urlparse(return_to).hostname)
        return HTMLResponse(_error_page("return_to не разрешён"), status_code=400)

    auth_b64 = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    try:
        resp = httpx.post(
            "https://ticktick.com/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "scope": "tasks:read tasks:write",
            },
            headers={
                "Authorization": f"Basic {auth_b64}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "curl/8.7.1",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return HTMLResponse(_error_page(str(e)), status_code=500)

    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    if not access_token:
        return HTMLResponse(_error_page(str(data)), status_code=500)

    # Deliver tokens via an auto-submitting POST form instead of a redirect
    # with tokens in the query string — GET params end up in server access
    # logs and browser history, POST bodies don't.
    return HTMLResponse(_relay_page(return_to, friend_secret, access_token, refresh_token))


def _relay_page(return_to: str, secret: str, access_token: str, refresh_token: str) -> str:
    action = html.escape(f"{return_to}/auth/accept", quote=True)
    secret_v = html.escape(secret, quote=True)
    access_v = html.escape(access_token, quote=True)
    refresh_v = html.escape(refresh_token, quote=True)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Подключаем...</title></head>
<body>
<p>Авторизация прошла, переходим обратно на твой сервер...</p>
<form id="f" method="POST" action="{action}">
  <input type="hidden" name="secret" value="{secret_v}">
  <input type="hidden" name="access_token" value="{access_v}">
  <input type="hidden" name="refresh_token" value="{refresh_v}">
</form>
<script>document.getElementById('f').submit();</script>
</body>
</html>"""


def _error_page(detail: str) -> str:
    detail_v = html.escape(detail, quote=True)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Ошибка — TickTick MCP</title>
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
  <p>Попробуй начать заново со ссылки, которую тебе дали (или запусти скрипт установки ещё раз).</p>
  <p>Детали: <code>{detail_v}</code></p>
</div>
</body>
</html>"""
