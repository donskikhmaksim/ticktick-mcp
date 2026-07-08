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
- `get_habits` / `checkin_habit` (backdatable) / `get_habit_checkins` — habits
- `list_filters` / `run_filter` — list and **execute** saved smart lists
- `set_task_parent` / `unset_task_parent` — subtasks
- `batch_complete_tasks` / `batch_delete_tasks` — bulk operations
- `list_project_groups` / `create_project_group` / `delete_project_group` / `move_project_to_group` — folders
- `get_task_comments` / `add_task_comment` — comments
- `get_statistics` — achievement score & completion counts
- `get_trash` — view deleted tasks (restore is app-only)
- `build_recurrence_rule` / `build_reminder` — helpers for repeat & reminder strings

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
| `MCP_SECRET` | strongly recommended | secret appended to URL path: `/mcp/<secret>`; also gates the self-service `/setup/<secret>` route |
| `TICKTICK_OAUTH_PROXY_URL` | optional | URL of the shared OAuth proxy for the `/setup` flow (defaults to the hosted proxy) |
| `USER_TIMEZONE` | optional | IANA timezone for due-date handling (e.g. `Europe/Moscow`); defaults to UTC |
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

Railway health-checks `/health` (configured in `railway.toml`).

## Self-service OAuth (`/setup` + the oauth-proxy)

Instead of running the local `auth` flow, an instance can obtain its own
TickTick tokens through the browser with no CLI:

1. `scripts/setup.sh` provisions the instance and hands the owner a
   `https://<your-app>.up.railway.app/setup/<MCP_SECRET>` link.
2. `/setup/<MCP_SECRET>` redirects to the shared **oauth-proxy** (see
   [`oauth-proxy/README.md`](oauth-proxy/README.md)), which owns the single
   TickTick-registered `redirect_uri`.
3. After TickTick consent, the proxy relays the tokens back (signed `state`,
   POST body) to this instance's `/auth/accept`, which hot-swaps the in-memory
   client — no redeploy.

## Isolation & privacy

**Every self-hosted instance is a fully separate tenant.** You deploy your own
instance, generate your own random `MCP_SECRET` (which is the URL path only you
know), and authorize *your* TickTick account. Tasks go only to your account, and
your `https://<your-app>/mcp/<MCP_SECRET>` URL is the one you plug into
tg-ai-assistant (or any MCP client). No one shares an instance, a secret, or a
token with anyone else.

The one shared component in the default setup is the OAuth **proxy**. Depending
on how much you want to depend on the maintainer's infrastructure, choose one of
three auth paths, in order of privacy:

1. **Fully isolated (recommended).** Register *your own* TickTick developer app
   at [developer.ticktick.com](https://developer.ticktick.com), put
   `TICKTICK_CLIENT_ID` / `TICKTICK_CLIENT_SECRET` in your `.env`, and authorize
   with the **local flow**:

   ```bash
   uv run -m ticktick_mcp.cli auth
   ```

   This opens a browser, exchanges the code with *your* client credentials
   locally, and writes `TICKTICK_ACCESS_TOKEN` to your `.env`. It **never
   touches the shared proxy** (or any maintainer infrastructure). Paste the
   resulting token into your Railway `TICKTICK_ACCESS_TOKEN` variable. This is
   the only path with zero dependency on the maintainer.

2. **Self-hosted proxy.** If you want the browser-based `/setup` flow but still
   full isolation, host `oauth-proxy/` yourself (see
   [`oauth-proxy/README.md`](oauth-proxy/README.md)). Register your own TickTick
   app with its redirect URI pointing at *your* proxy's `REDIRECT_URI`
   (`https://<your-proxy>/callback`), then point your instance at it:

   ```
   TICKTICK_OAUTH_PROXY_URL=https://<your-proxy-domain>
   ```

   Now the code exchange runs on infrastructure you control.

3. **Default shared proxy (convenient, acceptable).** If you leave
   `TICKTICK_OAUTH_PROXY_URL` unset, `/setup` uses the maintainer-operated proxy
   at `https://ticktick-oauth-proxy-production.up.railway.app`, which uses the
   maintainer's TickTick dev-app `client_id`/`secret`. That proxy **momentarily
   relays** your access token back to your instance and does **not** persist
   tokens — but you are still depending on, and observable by, the maintainer's
   infrastructure during the exchange. For full isolation prefer option 1 or 2.

Set `TICKTICK_OAUTH_PROXY_URL` if you host your own proxy (option 2), or leave it
unset to use the default shared proxy (option 3). Option 1 ignores the proxy
entirely.

### Standing up your own instance — the sequence

1. **Deploy your own instance** (Railway → Deploy from GitHub, per the section
   above) — or run `scripts/setup.sh`, which does this for you.
2. **Generate your own `MCP_SECRET`** (`openssl rand -hex 24`) and set it in
   Railway. This becomes your private URL path.
3. **Authorize YOUR TickTick** — either the local `auth` flow (option 1) or the
   `/setup/<MCP_SECRET>` browser flow (option 2/3). Log in to *your own*
   TickTick account when the consent screen appears.
4. **Use YOUR resulting URL** — `https://<your-app>/mcp/<MCP_SECRET>` — as the
   connector URL in tg-ai-assistant (or the Claude app). That URL, with your
   secret and your token, is what keeps your data yours.

## Connect from your phone

In the Claude app: **Settings → Connectors → Add custom connector**, paste the
full URL including the secret path. The server speaks Streamable HTTP, which
the Claude apps support for remote MCP connectors.

## Security

The public endpoint is protected only by the unguessable `MCP_SECRET` in the
URL path — anyone with the full URL can control your TickTick account. Keep it
private, use a long random secret, and rotate it by changing the variable.
