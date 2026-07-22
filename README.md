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
| `MCP_SECRET` | strongly recommended | secret appended to URL path: `/mcp/<secret>` — lightweight auth for the public endpoint |
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

## One-command self-deploy (`scripts/setup.sh`)

`scripts/setup.sh` provisions your own instance end to end and is safe to
re-run (project, service, and fork are reused, never duplicated):

1. Forks `donskikhmaksim/ticktick-mcp` into **your** GitHub account (via `gh`,
   with a browser-fork fallback) and enables Actions on the fork.
2. Creates a Railway project + service and points it at **your fork** as the
   deploy source (native GitHub deploy) — so every push to your fork's `main`
   redeploys automatically. The bundled `.github/workflows/sync-upstream.yml`
   fast-forwards your fork from upstream every 5 minutes, so bug fixes ship to
   you without any manual step.
3. Attaches a `/data` volume, sets your env vars, and authorizes **your**
   TickTick with the **local `auth` flow** (browser OAuth on your own machine —
   your `client_secret` never leaves it), storing the resulting token as a
   Railway variable.

```bash
bash <(curl -fsSL https://github.com/donskikhmaksim/ticktick-mcp/raw/main/scripts/setup.sh) \
  --client-id "<your-client-id>" \
  --client-secret "<your-client-secret>" \
  --timezone "Europe/Moscow"
```

Register your own TickTick developer app at
[developer.ticktick.com](https://developer.ticktick.com) to get the
`client-id` / `client-secret`. See [`ONBOARDING.md`](ONBOARDING.md) for the
step-by-step walkthrough.

## Isolation & privacy

**Every self-hosted instance is a fully separate, single-tenant deployment.**
You fork the repo into your own GitHub, deploy your own Railway instance,
generate your own random `MCP_SECRET` (the URL path only you know), and
authorize *your* TickTick account with the local `auth` flow. Tasks go only to
your account, your `https://<your-app>/mcp/<MCP_SECRET>` URL is the one you plug
into tg-ai-assistant (or any MCP client), and your `client_secret` and access
token never touch anyone else's infrastructure. No one shares an instance, a
secret, or a token with anyone else.

### The auth path — local `auth` flow (the only one)

Register *your own* TickTick developer app at
[developer.ticktick.com](https://developer.ticktick.com), put
`TICKTICK_CLIENT_ID` / `TICKTICK_CLIENT_SECRET` in your `.env`, and authorize
locally:

```bash
uv run -m ticktick_mcp.cli auth
```

This opens a browser, exchanges the code with *your* client credentials on your
own machine, and writes `TICKTICK_ACCESS_TOKEN` (and `TICKTICK_REFRESH_TOKEN`)
to your `.env`. Paste the access token into your Railway `TICKTICK_ACCESS_TOKEN`
variable (or let `scripts/setup.sh` do all of this for you). There is no
server-side browser OAuth and no shared proxy — nothing depends on the
maintainer's infrastructure at runtime.

### Standing up your own instance — the sequence

1. **Fork + deploy** — run `scripts/setup.sh` (recommended), or fork the repo
   yourself and point Railway at your fork (Deploy from GitHub).
2. **Generate your own `MCP_SECRET`** (`openssl rand -hex 24`) and set it in
   Railway. This becomes your private URL path.
3. **Authorize YOUR TickTick** with the local `auth` flow. Log in to *your own*
   TickTick account when the consent screen appears; set the resulting
   `TICKTICK_ACCESS_TOKEN` in Railway.
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
