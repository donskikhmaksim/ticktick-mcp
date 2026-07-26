"""Sheet-backed durable manifest for declutter — ALL Google I/O lives here.

Implements docs/DESIGN_sheet_backed_declutter.md §4.4 / §8.2: a thin Sheets
REST v4 client (service-account JWT auth, plain `requests` — NOT the full
google-api-python-client) hardcoded to ONE spreadsheet (id from env
`DECLUTTER_SHEET_ID`), following the same safety model as G-MCP's
`sheets-mcp/src/tools/triage.ts`: a fixed spreadsheet id means this module
physically cannot write to any other document.

Every public function either succeeds or raises `DeclutterSheetError` with a
human-readable (Russian) message — missing creds, unreachable network, a
malformed sheet, whatever. Callers (server.py) must catch it and refuse
cleanly; per Maksim's decision (design doc §9.3) there is NO silent fallback
to RAM here — durability is the whole point of persist="sheet".
"""
import json
import os
import time
from typing import Dict, List

import jwt
import requests

SHEET_NAME = "Declutter Log"

# Column order = sheet columns A..P (§3 of the design doc). row_id is
# assigned by append_rows(); everything else is written by plan/execute.
HEADER = [
    "row_id", "manifest_id", "run_ts", "task_id", "title", "project",
    "column", "cluster_id", "action", "proposed_value", "reason",
    "decision", "status", "applied_ts", "error", "snapshot_json",
]
_LAST_COL = chr(ord("A") + len(HEADER) - 1)  # "P"
_RANGE_ALL = f"{SHEET_NAME}!A:{_LAST_COL}"
_RANGE_HEADER = f"{SHEET_NAME}!A1:{_LAST_COL}1"

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_TOKEN_REFRESH_BUFFER = 300  # seconds; refresh a bit before real expiry
_HTTP_TIMEOUT = 20

# Module-level cache: {"token": str, "expires_at": epoch_seconds}. A fresh
# token is minted once and reused for ~55 min (Google tokens last 60 min).
_token_cache: Dict[str, object] = {}


class DeclutterSheetError(Exception):
    """Any sheet-I/O failure. Always carries a human-readable message —
    callers surface `str(e)` directly to the user, never a raw traceback."""
    pass


def sheet_configured() -> bool:
    """Cheap, no-I/O check: are both env vars set? Used by server.py to skip
    even ATTEMPTING sheet access (and thus any network call) when the
    feature simply isn't provisioned — keeps the ram-only path silent."""
    return bool((os.environ.get("DECLUTTER_SHEET_ID") or "").strip()
                and (os.environ.get("GSHEETS_SA_JSON") or "").strip())


def _spreadsheet_id() -> str:
    sid = (os.environ.get("DECLUTTER_SHEET_ID") or "").strip()
    if not sid:
        raise DeclutterSheetError(
            "DECLUTTER_SHEET_ID не задан — не могу сохранить план durable в "
            "Google Sheet. Задай env DECLUTTER_SHEET_ID (id таблицы) и "
            "убедись, что таблица расшарена на email service-account.")
    return sid


def _load_service_account() -> dict:
    raw = (os.environ.get("GSHEETS_SA_JSON") or "").strip()
    if not raw:
        raise DeclutterSheetError(
            "GSHEETS_SA_JSON не задан — нет ключа service-account для записи "
            "в Google Sheet. Задай env GSHEETS_SA_JSON (JSON ключа целиком, "
            "либо путь к файлу с ключом).")
    try:
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        with open(raw, "r", encoding="utf-8") as f:
            return json.load(f)
    except DeclutterSheetError:
        raise
    except Exception as e:
        raise DeclutterSheetError(f"GSHEETS_SA_JSON нечитаем: {e}")


def _access_token() -> str:
    """Mint (or reuse a cached) OAuth access token via the SA JWT-bearer
    grant — no google-api-python-client, just PyJWT[crypto] + requests."""
    now = time.time()
    cached = _token_cache.get("token")
    if cached and _token_cache.get("expires_at", 0) > now:
        return cached  # type: ignore[return-value]
    sa = _load_service_account()
    token_uri = sa.get("token_uri") or _TOKEN_URL
    try:
        claims = {
            "iss": sa["client_email"],
            "scope": _SCOPE,
            "aud": token_uri,
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        signed = jwt.encode(claims, sa["private_key"], algorithm="RS256")
    except KeyError as e:
        raise DeclutterSheetError(
            f"GSHEETS_SA_JSON без обязательного поля {e} — некорректный "
            "ключ service-account.")
    except Exception as e:
        raise DeclutterSheetError(f"Не удалось подписать JWT для service-account: {e}")
    try:
        r = requests.post(token_uri, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed,
        }, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        token = data["access_token"]
        ttl = int(data.get("expires_in", 3600))
    except DeclutterSheetError:
        raise
    except Exception as e:
        raise DeclutterSheetError(
            f"Не удалось получить access token у Google (сеть/креды): {e}")
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(ttl - _TOKEN_REFRESH_BUFFER, 60)
    return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json"}


def _api_base() -> str:
    return f"https://sheets.googleapis.com/v4/spreadsheets/{_spreadsheet_id()}"


def _get_values(range_: str) -> List[List[str]]:
    try:
        r = requests.get(f"{_api_base()}/values/{range_}",
                         headers=_headers(), timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json().get("values", [])
    except DeclutterSheetError:
        raise
    except Exception as e:
        raise DeclutterSheetError(f"Не удалось прочитать Google Sheet: {e}")


def _read_all_rows() -> List[List[str]]:
    """Full sheet content INCLUDING the header row (index 0), fresh every
    call — nothing here is cached, so callers always see the latest human
    edits to `decision`."""
    return _get_values(_RANGE_ALL)


def _row_to_dict(row: List[str]) -> dict:
    return {h: (row[i] if i < len(row) else "") for i, h in enumerate(HEADER)}


def ensure_header() -> None:
    """Write the header row iff the sheet/tab is empty (as triage.ts:21)."""
    values = _get_values(_RANGE_HEADER)
    if values:
        return
    try:
        r = requests.put(
            f"{_api_base()}/values/{_RANGE_HEADER}",
            headers=_headers(), timeout=_HTTP_TIMEOUT,
            params={"valueInputOption": "RAW"},
            json={"values": [HEADER]})
        r.raise_for_status()
    except DeclutterSheetError:
        raise
    except Exception as e:
        raise DeclutterSheetError(f"Не удалось записать заголовок листа: {e}")


def append_rows(rows: List[dict]) -> List[int]:
    """Append rows (dicts keyed by HEADER field names — `row_id` may be
    omitted, it is assigned here as lastId+1, lastId+2, ... exactly like
    triage.ts's `triage_log_add`). Returns the assigned row_ids in order.
    A no-op (returns []) for an empty list — callers may call this even when
    a plan produced zero rows without an extra guard."""
    if not rows:
        return []
    ensure_header()
    all_rows = _read_all_rows()
    data_rows = all_rows[1:] if len(all_rows) > 1 else []
    last_id = 0
    for r in data_rows:
        try:
            last_id = max(last_id, int((r[0] if r else "0") or 0))
        except (ValueError, IndexError):
            continue
    assigned: List[int] = []
    values: List[List[str]] = []
    for i, row in enumerate(rows):
        rid = last_id + i + 1
        assigned.append(rid)
        full = dict(row)
        full["row_id"] = rid
        values.append([
            "" if full.get(h) is None else str(full.get(h)) for h in HEADER
        ])
    try:
        r = requests.post(
            f"{_api_base()}/values/{_RANGE_ALL}:append",
            headers=_headers(), timeout=_HTTP_TIMEOUT,
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"values": values})
        r.raise_for_status()
    except DeclutterSheetError:
        raise
    except Exception as e:
        raise DeclutterSheetError(f"Не удалось записать строки в Google Sheet: {e}")
    return assigned


def read_manifest_rows(manifest_id: str) -> List[dict]:
    """Fresh read (never cached) of every row belonging to `manifest_id`.
    Each dict is keyed by HEADER field names plus an internal `_sheet_row`
    (1-based sheet row number, header = row 1) used only within this module
    for update addressing."""
    all_rows = _read_all_rows()
    out = []
    for i, row in enumerate(all_rows[1:] if all_rows else [], start=2):
        d = _row_to_dict(row)
        if d.get("manifest_id") == manifest_id:
            d["_sheet_row"] = i
            out.append(d)
    return out


def _col_letter(field: str) -> str:
    return chr(ord("A") + HEADER.index(field))


def update_row(row_id, **fields) -> None:
    """Update one row's fields, addressed by the stable `row_id` (column A —
    survives reordering), not by sheet row number."""
    batch_update_rows([{"row_id": row_id, **fields}])


def batch_update_rows(updates: List[dict]) -> None:
    """Apply several per-row field updates in one Sheets API call. Each
    update dict: {"row_id": <int-ish>, <HEADER field>: <value>, ...}.
    Re-reads the sheet fresh to map row_id -> current sheet row (exactly
    triage.ts's `triage_log_update`: `findIndex` by column A), so this is
    correct even if rows were manually reordered. Unknown fields are
    ignored; a `row_id` that isn't found raises (nothing partially applied
    for that row)."""
    if not updates:
        return
    all_rows = _read_all_rows()
    row_of_id: Dict[str, int] = {}
    for i, row in enumerate(all_rows[1:] if all_rows else [], start=2):
        rid = (row[0] if row else "").strip()
        if rid:
            row_of_id[rid] = i
    data = []
    for upd in updates:
        rid = str(upd.get("row_id"))
        sheet_row = row_of_id.get(rid)
        if sheet_row is None:
            raise DeclutterSheetError(
                f"Строка row_id={rid} не найдена в листе — не могу обновить "
                "(манифест мог быть создан на другой таблице, либо строка "
                "удалена вручную).")
        for field, value in upd.items():
            if field == "row_id" or field not in HEADER:
                continue
            col = _col_letter(field)
            data.append({
                "range": f"{SHEET_NAME}!{col}{sheet_row}",
                "values": [["" if value is None else str(value)]],
            })
    if not data:
        return
    try:
        r = requests.post(
            f"{_api_base()}/values:batchUpdate",
            headers=_headers(), timeout=_HTTP_TIMEOUT,
            json={"valueInputOption": "RAW", "data": data})
        r.raise_for_status()
    except DeclutterSheetError:
        raise
    except Exception as e:
        raise DeclutterSheetError(f"Не удалось обновить строки в Google Sheet: {e}")


def sheet_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{_spreadsheet_id()}/edit"
