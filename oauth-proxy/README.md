# TickTick OAuth Proxy

A tiny FastAPI service that owns the single TickTick-registered OAuth
`redirect_uri` and relays tokens to each friend's own MCP instance. Deployed to
Railway.

## Why it exists

TickTick only accepts a `redirect_uri` that is pre-registered in the developer
console, and registering one per friend's Railway subdomain isn't self-service.
So this proxy holds the one registered redirect URI, exchanges the auth code for
tokens, and forwards the browser back to the friend's instance
(`return_to` + `secret`, signed into the OAuth `state`). The TickTick client
secret never leaves the proxy.

## Security model

- `/start` accepts `return_to` + `secret`, validates `return_to` (https + host
  ending in an allowed suffix), then HMAC-signs both into `state`.
- `/callback` verifies the `state` signature before using `return_to` — a forged
  or tampered `return_to` is rejected, closing the open-redirect / token
  exfiltration hole.
- Tokens are delivered via an auto-submitting POST form (never in a URL), and
  every interpolated value is HTML-escaped.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `TICKTICK_CLIENT_ID` | ✅ | TickTick developer app client id |
| `TICKTICK_CLIENT_SECRET` | ✅ | TickTick developer app client secret |
| `REDIRECT_URI` | ✅ | The proxy's own `/callback` URL, registered in the TickTick console |
| `PROXY_STATE_SECRET` | ✅ (prod) | Secret used to HMAC-sign `state`. Generate with `openssl rand -hex 32`. If unset, an ephemeral one is minted per process (breaks across restarts/replicas). |
| `PROXY_ALLOWED_RETURN_SUFFIXES` | optional | Comma-separated host suffixes allowed for `return_to`. Default `.up.railway.app`. |

## Endpoints

- `GET /start?return_to=<url>&secret=<s>` — begins the OAuth flow, redirects to
  TickTick consent.
- `GET /callback?code=&state=` — TickTick redirects here; exchanges the code and
  relays tokens back to `return_to/auth/accept`.

## Deploy to Railway

1. New Project → Deploy from GitHub repo → set the root directory to
   `oauth-proxy/` (it has its own `requirements.txt` and `Procfile`).
2. Set the environment variables above.
3. Generate a public domain, then set `REDIRECT_URI` to
   `https://<proxy-domain>/callback` and register that exact URL in the TickTick
   developer console's OAuth Redirect URLs.
