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

### Optional LLM judge (set `CLAUDE_CLI_URL`)
Want smarter fuzzy-duplicate detection and SMART-rewrite suggestions in
`plan_declutter`, plus project-destination suggestions in
`plan_task_creation`? Deploy your own
[`claude-p-shim`](https://github.com/donskikhmaksim/claude-p-shim) — a tiny
Railway service (~5 min to set up) that runs on your own Claude Pro/Max
subscription, no Anthropic API key needed — and set `CLAUDE_CLI_URL` /
`CLAUDE_CLI_TOKEN` / `CLAUDE_CLI_MODEL` as Railway variables on this service.
Without it, everything still works — declutter just falls back to
exact-title matching, and destination suggestions are skipped.

## Two TickTick APIs, and what breaks if the unofficial one goes down

This server talks to TickTick over two very different APIs. It's worth
understanding the split before you depend on it for anything important.

- **v1 — official Open API.** Documented, stable, OAuth-authenticated —
  what `TICKTICK_ACCESS_TOKEN` unlocks. It's also very limited: the Open API
  only exposes single-item operations — get/create/update/delete/complete
  *one* task or project at a time, plus create-subtask. It has **no listing,
  no filtering, no batch operations, no `get_changes`, no task history, no
  habits, no trash/restore, and no project groups.**
- **v2 — unofficial, reverse-engineered.** What `TICKTICK_V2_TOKEN` (the
  browser session cookie) unlocks. It's the sync API TickTick's own web and
  mobile clients use internally — undocumented, changed at TickTick's
  convenience, with zero support contract or notice period. It also powers
  almost everything people actually use this server for day to day: listing
  every open task in one call (including the Inbox), filtering by due date /
  priority / tag, searching, batch create/update/complete/delete, the
  `plan_declutter`/`execute_declutter` dedup pass, `get_changes` (the audit
  feed), habits, project groups, trash/restore, and comments.

**The gate isn't always the engine.** A number of read tools —
`get_all_tasks`, `search_tasks`, `get_tasks_due_today` and its siblings,
`get_recurring_tasks`, `get_engaged_tasks`, `get_next_tasks` — check that the
v1 client is configured before running (so there's a fallback path), but when
`TICKTICK_V2_TOKEN` is set they actually read from the v2 sync state in one
fast call and only fall back to slow per-project v1 iteration when v2 is
unavailable. Same story for the write tools: `create_tasks`, `update_tasks`,
and `complete_tasks` create/update/complete a lone task via v1, but tags,
assignees, kanban columns, nested subtask trees, and any true batch (more
than one task, no advanced fields) all go through v2 — with v1 as a slower,
narrower fallback when v2 is off.

| Capability | API | Notes |
|---|---|---|
| Get / create / update / delete / complete **one** task or project, by id | v1 | The only thing guaranteed to survive a v2 outage |
| Create a subtask under an existing task | v1 | `create_subtask` |
| List/browse projects and a single project's tasks | v1 | `get_projects`, `get_project`, `get_project_tasks`, `get_task` |
| List/filter across **all** open tasks (priority, due today/tomorrow/in N days/this week, overdue, search, recurring, GTD engaged/next) | v2 preferred, v1 fallback | v1 fallback works but is slower (one request per project) and omits the Inbox |
| Batch create / update / complete (>1 task, no advanced fields) | v2, v1 fallback | Without v2 these degrade to one-at-a-time v1 calls — no tags/assignee/columns |
| Tags, assignees, kanban columns/sections, nested subtask trees, `move_task`, `set_task_parent`/`unset_task_parent` | v2 only | No official-API equivalent exists |
| Completed-tasks list, Inbox read, project groups (folders) | v2 only | — |
| Habits (`get_habits` / `checkin_habit` / `get_habit_checkins`) | v2 only | — |
| Trash / restore | v2 only | — |
| Task comments, `get_task_activity` (edit history), `get_changes` (the audit-log feed) | v2 only | — |
| `plan_declutter` / `execute_declutter` (dedup, SMART rewrite) | v2 only | Reads the whole task pool via v2 sync state; nothing to fall back to |
| Project members, `get_statistics` | v2 only | — |

**If v2 breaks** — TickTick changes their web client and the sync API
underneath it, with no notice and no changelog, since it was never a
contract to begin with — everything in the "v2 only" rows above stops
working, and the "v2 preferred" rows fall back to their slower, narrower v1
path. What keeps working unconditionally is single-task/single-project CRUD
by id (get/create/update/delete/complete) plus creating a subtask. In short:
TickTick becomes usable only one task at a time, by id — everything that
depends on *seeing* the whole task pool at once (which is most of what makes
this server useful to an LLM) is gone until v2 comes back or TickTick ships
an equivalent official endpoint. The server fails soft, not hard: v2 tools
return a clear "not enabled / session expired" message instead of crashing,
so a v2 outage degrades functionality rather than taking down the whole
server.

**Why there's no separate v1-only fallback server.** This was considered and
rejected. A v1-only server could only offer bare single-task/single-project
CRUD — no listing, no filtering, no batch, no `get_changes`-based audit
logging, no habits, no dedup — which isn't enough surface area to run any of
this project's actual daily-use workflows (declutter, batch task management,
audit logging). Building one wouldn't buy real resilience; it would just be
this same server with `TICKTICK_V2_TOKEN` unset, which you already get for
free today by leaving that variable out. If v2 ever breaks for good, the fix
is a client update here (or TickTick shipping a broader official API) — not
maintaining a second codebase that covers a fraction of the functionality.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `TICKTICK_ACCESS_TOKEN` | ✅ | Open API OAuth token (from local `auth` flow) |
| `TICKTICK_CLIENT_ID` / `TICKTICK_CLIENT_SECRET` | for auth flow | TickTick developer app creds |
| `TICKTICK_V2_TOKEN` | optional | the `t` cookie — enables the v2 API |
| `CLAUDE_CLI_URL` / `CLAUDE_CLI_TOKEN` / `CLAUDE_CLI_MODEL` | optional | LLM judge for declutter dedup/SMART-rewrite + destination suggestions — see above |
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
