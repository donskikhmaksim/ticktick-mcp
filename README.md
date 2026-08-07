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
- `get_habits` / `create_habit` / `delete_habit` / `checkin_habit` (backdatable)
  / `get_habit_checkins` — habits
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

### Optional sheet-backed declutter manifest (set `DECLUTTER_SHEET_ID`)
By default `plan_declutter` keeps its manifest only in the server's RAM — it
survives a redeploy/restart for exactly nothing; the plan is gone the moment
the process recycles. `plan_declutter(persist="sheet")` instead writes every
proposed edit as a row in a `Declutter Log` Google Sheet, which becomes the
durable source of truth: it survives a restart, you can edit the `decision`
column (`approved`/`rejected`) directly in the sheet, and `resume_declutter`
picks up exactly where a crashed/partial run left off — see
[`docs/DESIGN_sheet_backed_declutter.md`](docs/DESIGN_sheet_backed_declutter.md)
for the full design.

To enable it:
1. Create a Google Cloud service account, generate a JSON key for it, and
   enable the Sheets API for that project.
2. Create (or pick) a spreadsheet and share it with the service account's
   `client_email` as **Editor**.
3. Set `DECLUTTER_SHEET_ID` (the spreadsheet id from its URL) and
   `GSHEETS_SA_JSON` (the key JSON — either the raw JSON string, or a path to
   the key file) as env vars.

Leave both unset to skip this entirely — `plan_declutter`'s default
`persist="ram"` behaves exactly as before, byte-for-byte. If `persist="sheet"`
is requested but the sheet is unreachable (missing creds, no network),
`plan_declutter` REFUSES explicitly rather than silently falling back to RAM
— durability is the entire point of that mode.

### Optional Telegram approval layer (set `TG_APPROVAL_ENABLED`)

By default, every mutating tool is gated by a plan → chat-confirmation →
execute flow: the model shows you a plan, you type something affirmative
back in the same conversation, and only then does it execute. Set
`TG_APPROVAL_ENABLED=true` to add a **second, out-of-band factor** (a
confirmation channel independent from the chat itself) on top of that: the
plan also has to be confirmed with a button tap in Telegram, so a chat reply
that was fabricated or misread by the model can no longer execute anything
on its own.

How it works, end to end:
1. When the model plans a mutating action, the plan is sent to you in a
   private Telegram DM with **✅ Confirm** / **🛑 Reject** buttons. A long
   plan is split across several Telegram messages automatically — the
   buttons live on the last one.
2. Tapping a button doesn't hit this server directly by default. The
   **webhook** (the HTTP endpoint Telegram calls when you tap something)
   lives in the neighboring **gmail-mcp** service, which writes your decision
   into the shared `tg_approvals` table. ticktick-mcp itself never registers
   a webhook for this — unless you opt into **own_bot** (see below), which
   flips this one step to a webhook this server owns itself.
3. A background **poller** (a job that periodically checks for new state
   instead of being pushed to) in ticktick-mcp, running every
   `TG_AUTO_EXECUTE_INTERVAL_S` seconds, sees the approval and executes the
   action itself — the model does not need to call the tool a second time.
4. After executing, the server independently re-reads the result to verify
   it (a real re-check by reading live state, not an "assume it worked"
   report), then posts the **full** report to the "MCP Отчёты" ("MCP
   Reports") group chat as an archive, while only a short summary is sent
   back to your DM.
5. If neither button is ever tapped, a **reaper** (background cleanup job)
   running every `TG_REAP_INTERVAL_S` seconds (toggle: `TG_REAP_ENABLED`)
   deletes the plan message and everything tied to it once
   `TG_APPROVAL_TTL_S` seconds have passed — the whole message is removed,
   not just its buttons.

`TG_APPROVAL_ENABLED=false` (the default) keeps this server byte-for-byte
identical to before this layer existed — no network calls, no Postgres
access, and chat confirmation alone remains the only gate.

> ⚠️ `tg_approvals` is a table **shared with gmail-mcp** (and
> sheets/calendar/docs/drive-mcp, all backed by the same Postgres). Its
> schema is treated as frozen: don't change its columns or status semantics
> unilaterally from this repo — every other server reading/writing that
> table depends on the current shape.

#### Own bot instead of the shared one (`TG_BOT_TOKEN_OVERRIDE`)

Set `TG_BOT_TOKEN_OVERRIDE` to give this server its own Telegram bot instead
of sharing `@maksim_mcp_approval_bot` with the other five MCP servers. This is
purely additive and fully backward compatible — leave it unset and everything
above behaves byte-for-byte the same (shared bot, gmail-mcp owns the webhook,
this server's poller executes). When it IS set:

- `TG_BOT_TOKEN_OVERRIDE`'s value is used for every Telegram API call this
  server makes (plans, reports, the webhook) instead of `TG_BOT_TOKEN`.
- This server registers its **own** `/tg/webhook` with Telegram at startup
  (`setWebhook`) — no conflict with the shared bot's webhook, since the two
  bot tokens are different and Telegram routes updates per-token.
- `TG_APPROVAL_WEBHOOK_SECRET` becomes required — the webhook checks it
  (constant-time) against Telegram's `X-Telegram-Bot-Api-Secret-Token` header
  on every request, so the URL alone isn't enough to forge a button press.
- Requires `MCP_TRANSPORT=streamable-http` (there's no HTTP server to receive
  a webhook on `stdio`) and a resolvable `PUBLIC_BASE_URL` (or Railway's
  `RAILWAY_PUBLIC_DOMAIN`) to register the webhook against. If you set
  `TG_BOT_TOKEN_OVERRIDE` while running `stdio` locally, the server logs a
  loud warning at startup instead of silently doing nothing — own_bot buttons
  won't work until you switch transport, but the server still starts and the
  plain chat-confirmation path is unaffected.
- The webhook only records your decision (PENDING → APPROVED/REJECTED) — the
  same background poller described above still does the actual execution,
  reading that same table. This means own_bot introduces no new way for an
  action to run twice: the poller's existing atomic claim
  (`manifest_store.claim`, one `UPDATE … WHERE consumed_at IS NULL …
  RETURNING`) is still the single place a plan gets consumed, regardless of
  which webhook (shared or own) flipped the row to APPROVED.

Rollback is just unsetting `TG_BOT_TOKEN_OVERRIDE` — no redeploy of routing
required; the `/tg/webhook` route stays mounted either way and simply 404s
when own_bot is off (same as if the route didn't exist).

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
| List/browse projects and a single project's tasks | v1 (+ v2 for the Inbox) | `get_projects`, `get_project`, `get_project_tasks`, `get_task`. The official API has no Inbox at all, so the built-in Inbox is added to the listing from the v2 state and its id is served from v2 as well — without v2 the Inbox is simply absent, never faked |
| List/filter across **all** open tasks (priority, due today/tomorrow/in N days/this week, overdue, search, recurring, GTD engaged/next) | v2 preferred, v1 fallback | v1 fallback works but is slower (one request per project) and omits the Inbox |
| Batch create / update / complete (>1 task, no advanced fields) | v2, v1 fallback | Without v2 these degrade to one-at-a-time v1 calls — no tags/assignee/columns |
| Tags, assignees, kanban columns/sections, nested subtask trees, `move_task`, `set_task_parent`/`unset_task_parent` | v2 only | No official-API equivalent exists |
| Completed-tasks list, Inbox read, project groups (folders) | v2 only | — |
| Habits (`get_habits` / `create_habit` / `delete_habit` / `checkin_habit` / `get_habit_checkins`) | v2 only | Create/delete go through `POST /habits/batch`, the same batch shape as tasks/tags/projects |
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
| `DECLUTTER_SHEET_ID` / `GSHEETS_SA_JSON` | optional | sheet-backed declutter manifest (`plan_declutter(persist="sheet")`) — see below |
| `TG_APPROVAL_ENABLED` | optional | enables the Telegram approval layer (2nd factor on top of chat confirmation) — see above; `false` (default) = no network calls, behaves exactly as before |
| `TG_BOT_TOKEN` | if `TG_APPROVAL_ENABLED=true` and `TG_BOT_TOKEN_OVERRIDE` unset | Telegram bot token for the shared approval bot |
| `TG_BOT_TOKEN_OVERRIDE` | optional | own bot token instead of the shared one — see "Own bot instead of the shared one" above; also the single switch that turns on `own_bot` mode |
| `TG_APPROVAL_WEBHOOK_SECRET` | if `TG_BOT_TOKEN_OVERRIDE` is set | random string checked against Telegram's `X-Telegram-Bot-Api-Secret-Token` on `/tg/webhook` — generate with `openssl rand -hex 24` |
| `TG_OWNER_CHAT_ID` | if `TG_APPROVAL_ENABLED=true` | your personal Telegram chat id — where the plan with buttons is sent |
| `TG_REPORTS_CHAT_ID` | optional (default: `TG_OWNER_CHAT_ID`) | group chat id for the auto-execution report archive (e.g. the real "MCP Отчёты" group: `-1004357150083`); unset sends reports to your DM instead |
| `TG_APPROVAL_TOOLS` | optional | comma-separated tool names to gate with TG approval; empty (default) = all gated tools |
| `TG_APPROVAL_TTL_S` | optional (default `3600`) | seconds an approval request stays alive; on expiry the plan message is deleted entirely, not just its buttons |
| `TG_AUTO_EXECUTE_INTERVAL_S` | optional (default `10`) | poll interval, in seconds, for the background poller that executes button-approved actions |
| `TG_REAP_INTERVAL_S` | optional (default `60`) | poll interval, in seconds, for the reaper that cleans up expired approval requests |
| `TG_REAP_ENABLED` | optional (default `true`) | kill switch for the reaper — `false` disables TTL-based message deletion |
| `CONSENT_DATABASE_URL` | if `TG_APPROVAL_ENABLED=true` | DSN (connection string) of the shared Postgres — the same one gmail/sheets/calendar/docs/drive-mcp use — holding the `tg_approvals` table |
| `MCP_TRANSPORT` | for remote | `streamable-http` (default `stdio`) |
| `MCP_SECRET` | strongly recommended | secret appended to URL path: `/mcp/<secret>` — lightweight auth for the public endpoint; also the root of the attachment-link signing key |
| `PUBLIC_BASE_URL` | optional | public base URL of this server (e.g. `https://<app>.up.railway.app`), used to build attachment transfer links; falls back to Railway's `RAILWAY_PUBLIC_DOMAIN` |
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

Two more public routes exist for attachment transfer: `GET /dl/<token>` streams
a file out of TickTick and `PUT /ul/<token>` relays one in. Both are useless
without a valid short-lived token — it is signed with a key derived from
`MCP_SECRET` (`HMAC(MCP_SECRET, "attachment-link")`, never the secret itself),
carries the project/task/attachment ids plus an expiry, and is minted by the
`get_attachment_download_url` / `create_attachment_upload_url` tools. Nothing is
stored server-side, and the TickTick session cookie never leaves the server.
On Railway `RAILWAY_PUBLIC_DOMAIN` is enough; behind a custom domain or proxy
set `PUBLIC_BASE_URL`.

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

## Migrating from upstream (pre-fork deployments)

If you deployed this server before the fork-based auto-update mechanism existed
(i.e. your Railway service points directly to `donskikhmaksim/ticktick-mcp` or
you cloned it manually), you can migrate to the new fork-and-sync model in one
step:

```bash
gh_user=$(gh api user --jq .login) && \
gh repo fork donskikhmaksim/ticktick-mcp --clone=false && \
gh api -X PUT "repos/$gh_user/ticktick-mcp/actions/permissions" -F enabled=true && \
railway service source connect --repo "$gh_user/ticktick-mcp" --branch main --service ticktick-mcp
```

What it does:
1. Forks the upstream repo into your GitHub account (idempotent — if already
   forked, it's a no-op).
2. Enables GitHub Actions on your fork (required for the `sync-upstream`
   workflow).
3. Repoints your Railway service to deploy from your fork instead of upstream.

After this, the bundled [`sync-upstream`](.github/workflows/sync-upstream.yml)
workflow will run every ~5 minutes to pull in new features and fixes, and each
sync automatically triggers a redeploy. Your data, tokens, and configuration are
never touched.

Alternatively, do it manually: fork on GitHub.com, enable Actions in the fork's
Actions tab, and in Railway → **Settings → Source** connect to your fork's
`main` with Deploy on push enabled.

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
