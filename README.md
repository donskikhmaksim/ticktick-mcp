# TickTick MCP Server (remote / Railway)

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for
TickTick. It runs as a **remote Streamable-HTTP server** (e.g. on Railway) so
you can manage TickTick from the Claude mobile app — or any MCP client — from
your phone.

Based on [`jacepark12/ticktick-mcp`](https://github.com/jacepark12/ticktick-mcp),
extended with HTTP transport, a hardened public endpoint, and an optional
unofficial-v2 layer for the things the official API can't do.

## Capabilities

### Official Open API (always on)
Projects (list / get / create / delete), tasks (get / create / update /
complete / delete / subtasks), and client-side views: by priority, due
today / tomorrow / in N days / this week, overdue, search (open tasks),
batch create, GTD "engaged"/"next".

### Unofficial v2 API (optional — set `TICKTICK_V2_TOKEN`)
Fills the gaps the official API lacks:
- `get_completed_tasks` — recently completed tasks
- `list_tags` / `get_tasks_by_tag` — tag support
- `get_inbox_tasks` — read the Inbox
- `move_task` — move a task between lists

> ⚠️ The v2 API is undocumented and may break without notice. Auth is the **`t`
> cookie** from a logged-in ticktick.com browser session — NOT username/password
> (TickTick gates signon behind a captcha). Get it from DevTools → Application →
> Cookies → `ticktick.com` → copy the value of `t`, and set it as
> `TICKTICK_V2_TOKEN`. Leave unset to disable. The token is long-lived but
> eventually expires; when it does the v2 tools return a "re-extract the cookie"
> message and the official API keeps working.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `TICKTICK_ACCESS_TOKEN` | ✅ | Open API OAuth token (from local `auth` flow) |
| `TICKTICK_CLIENT_ID` / `TICKTICK_CLIENT_SECRET` | for auth flow | TickTick developer app creds |
| `TICKTICK_V2_TOKEN` | optional | the `t` cookie — enables the v2 API |
| `MCP_TRANSPORT` | for remote | `streamable-http` (default `stdio`) |
| `MCP_SECRET` | strongly recommended | secret appended to URL path: `/mcp/<secret>` |
| `MCP_HOST` / `PORT` | auto on Railway | bind address / port |

## Local setup

```bash
uv venv --python 3.12
uv pip install -r requirements.txt

# One-time: get an Open API access token (opens a browser)
cp .env.template .env          # fill CLIENT_ID / CLIENT_SECRET
uv run -m ticktick_mcp.cli auth

# Run locally over stdio (for desktop Claude / testing)
uv run -m ticktick_mcp.cli run
```

To test the HTTP transport locally:

```bash
MCP_TRANSPORT=streamable-http MCP_SECRET=dev123 MCP_PORT=8000 \
  uv run -m ticktick_mcp.cli run
# → http://localhost:8000/mcp/dev123
```

## Deploy to Railway

1. Push this repo to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo** → pick this repo.
   It builds from the `Dockerfile` automatically.
3. Set environment variables (Railway → Variables):
   - `MCP_TRANSPORT=streamable-http`
   - `TICKTICK_ACCESS_TOKEN=<token from the local auth flow>`
   - `MCP_SECRET=<openssl rand -hex 24>`
   - *(optional)* `TICKTICK_V2_TOKEN` (the `t` cookie — enables v2 features)
   - `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET`
   - `PORT` is injected by Railway — do not set it.
4. Generate a public domain (Railway → Settings → Networking → Generate Domain).
5. Your MCP URL is: `https://<your-app>.up.railway.app/mcp/<MCP_SECRET>`

## Connect from your phone

In the Claude app: **Settings → Connectors → Add custom connector**, paste the
full URL including the secret path. The server speaks Streamable HTTP, which
the Claude apps support for remote MCP connectors.

## Security

The public endpoint is protected only by the unguessable `MCP_SECRET` in the
URL path — anyone with the full URL can control your TickTick account. Keep it
private, use a long random secret, and rotate it by changing the variable.
