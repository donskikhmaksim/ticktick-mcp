import base64
import json
import os
import urllib.parse

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()

CLIENT_ID = os.environ["TICKTICK_CLIENT_ID"]
CLIENT_SECRET = os.environ["TICKTICK_CLIENT_SECRET"]
REDIRECT_URI = os.environ["REDIRECT_URI"]  # fixed, pre-registered in TickTick dev console

# Every friend's own instance has a different, randomly assigned Railway
# domain — TickTick only accepts a redirect_uri that's pre-registered for the
# OAuth app, and registering one per instance isn't self-service. So this
# proxy owns the ONE registered redirect_uri, and after exchanging the code
# for tokens it forwards the browser to the friend's own instance
# (return_to + secret carried through `state`) — the instance's own
# /auth/accept route picks the tokens up and hot-swaps its in-memory client.
# TickTick client secret never leaves this proxy.


@app.get("/start")
def start(return_to: str, secret: str):
    state = base64.urlsafe_b64encode(
        json.dumps({"return_to": return_to, "secret": secret}).encode()
    ).decode()
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

    try:
        payload = json.loads(base64.urlsafe_b64decode(state.encode()))
        return_to = payload["return_to"].rstrip("/")
        friend_secret = payload["secret"]
    except Exception:
        return HTMLResponse(_error_page("invalid state"), status_code=400)

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
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Подключаем...</title></head>
<body>
<p>Авторизация прошла, переходим обратно на твой сервер...</p>
<form id="f" method="POST" action="{return_to}/auth/accept">
  <input type="hidden" name="secret" value="{secret}">
  <input type="hidden" name="access_token" value="{access_token}">
  <input type="hidden" name="refresh_token" value="{refresh_token}">
</form>
<script>document.getElementById('f').submit();</script>
</body>
</html>"""


def _error_page(detail: str) -> str:
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
  <p>Детали: <code>{detail}</code></p>
</div>
</body>
</html>"""
