"""Unit tests for declutter_sheet.py — the sole home of Google Sheets I/O for
the sheet-backed declutter manifest. `requests` is monkeypatched throughout;
nothing here touches the network. A real (locally generated) RSA keypair is
used to exercise the actual JWT signing path end to end."""
import json
import time

import pytest
import requests

import ticktick_mcp.src.declutter_sheet as ds


def _fake_rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode()


_TEST_PEM = _fake_rsa_pem()  # generated once — RSA keygen isn't free


@pytest.fixture(autouse=True)
def _clear_token_cache():
    ds._token_cache.clear()
    yield
    ds._token_cache.clear()


@pytest.fixture
def sa_json(monkeypatch):
    sa = {
        "type": "service_account",
        "client_email": "test-sa@example.iam.gserviceaccount.com",
        "private_key": _TEST_PEM,
        "token_uri": ds._TOKEN_URL,
    }
    monkeypatch.setenv("GSHEETS_SA_JSON", json.dumps(sa))
    monkeypatch.setenv("DECLUTTER_SHEET_ID", "test-sheet-id")
    return sa


class _Resp:
    def __init__(self, json_data=None, status=200):
        self._json = json_data or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _patch_google(monkeypatch, *, get=None, post=None, put=None):
    """Patch requests.get/post/put; POST to the Google token endpoint is
    always auto-answered with a fake access token so callers only need to
    supply behaviour for the Sheets API calls they actually care about."""
    def dispatch_post(url, **kw):
        if "oauth2.googleapis.com/token" in url:
            return _Resp({"access_token": "tok-abc", "expires_in": 3600})
        if post:
            return post(url, **kw)
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(requests, "post", dispatch_post)
    if get is not None:
        monkeypatch.setattr(requests, "get", get)
    if put is not None:
        monkeypatch.setattr(requests, "put", put)


# ---- config / degrade ------------------------------------------------------

def test_sheet_configured_false_without_env(monkeypatch):
    monkeypatch.delenv("DECLUTTER_SHEET_ID", raising=False)
    monkeypatch.delenv("GSHEETS_SA_JSON", raising=False)
    assert ds.sheet_configured() is False


def test_sheet_configured_true_with_both_env(sa_json):
    assert ds.sheet_configured() is True


def test_spreadsheet_id_missing_raises_clear_error(monkeypatch):
    monkeypatch.delenv("DECLUTTER_SHEET_ID", raising=False)
    with pytest.raises(ds.DeclutterSheetError, match="DECLUTTER_SHEET_ID"):
        ds.sheet_url()


def test_sa_json_missing_raises_clear_error(monkeypatch):
    monkeypatch.setenv("DECLUTTER_SHEET_ID", "sid")
    monkeypatch.delenv("GSHEETS_SA_JSON", raising=False)
    with pytest.raises(ds.DeclutterSheetError, match="GSHEETS_SA_JSON"):
        ds._access_token()


def test_sa_json_malformed_raises_clear_error(monkeypatch):
    monkeypatch.setenv("GSHEETS_SA_JSON", "{not valid json")
    with pytest.raises(ds.DeclutterSheetError):
        ds._load_service_account()


def test_sa_json_loaded_from_file_path(monkeypatch, tmp_path):
    sa = {"client_email": "x@y.com", "private_key": _TEST_PEM,
          "token_uri": ds._TOKEN_URL}
    p = tmp_path / "sa.json"
    p.write_text(json.dumps(sa))
    monkeypatch.setenv("GSHEETS_SA_JSON", str(p))
    loaded = ds._load_service_account()
    assert loaded["client_email"] == "x@y.com"


def test_network_failure_surfaces_as_declutter_sheet_error(sa_json, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(ds.DeclutterSheetError):
        ds.ensure_header()


# ---- access token -----------------------------------------------------------

def test_access_token_mints_once_and_caches(sa_json, monkeypatch):
    calls = {"n": 0}

    def fake_post(url, data=None, timeout=None):
        calls["n"] += 1
        return _Resp({"access_token": "tok-xyz", "expires_in": 3600})

    monkeypatch.setattr(requests, "post", fake_post)
    t1 = ds._access_token()
    t2 = ds._access_token()
    assert t1 == t2 == "tok-xyz"
    assert calls["n"] == 1, "second call must reuse the cached token"


def test_access_token_refreshes_after_cache_expiry(sa_json, monkeypatch):
    tokens = iter(["tok-1", "tok-2"])

    def fake_post(url, data=None, timeout=None):
        return _Resp({"access_token": next(tokens), "expires_in": 3600})

    monkeypatch.setattr(requests, "post", fake_post)
    t1 = ds._access_token()
    ds._token_cache["expires_at"] = time.time() - 1  # force expiry
    t2 = ds._access_token()
    assert (t1, t2) == ("tok-1", "tok-2")


def test_access_token_network_failure_raises_declutter_error(sa_json, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(ds.DeclutterSheetError):
        ds._access_token()


def test_access_token_bad_sa_key_field_raises_declutter_error(monkeypatch):
    monkeypatch.setenv("GSHEETS_SA_JSON", json.dumps({"client_email": "x@y.com"}))
    monkeypatch.setenv("DECLUTTER_SHEET_ID", "sid")
    with pytest.raises(ds.DeclutterSheetError, match="private_key"):
        ds._access_token()


# ---- ensure_header ----------------------------------------------------------

def test_ensure_header_writes_when_sheet_empty(sa_json, monkeypatch):
    put_calls = []

    def fake_get(url, headers=None, timeout=None):
        assert "A1:P1" in url
        return _Resp({"values": []})

    def fake_put(url, headers=None, timeout=None, params=None, json=None):
        put_calls.append((params, json))
        return _Resp({})

    _patch_google(monkeypatch, get=fake_get, put=fake_put)
    ds.ensure_header()
    assert len(put_calls) == 1
    params, body = put_calls[0]
    assert params["valueInputOption"] == "RAW"
    assert body["values"] == [ds.HEADER]


def test_ensure_header_skips_when_already_present(sa_json, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _Resp({"values": [ds.HEADER]})

    def fake_put(*a, **kw):
        raise AssertionError("must NOT rewrite an existing header")

    _patch_google(monkeypatch, get=fake_get, put=fake_put)
    ds.ensure_header()  # no raise = pass


# ---- append_rows -------------------------------------------------------------

def test_append_rows_empty_list_is_noop_without_network(sa_json, monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("must not touch the network for an empty append")
    _patch_google(monkeypatch, get=fail, post=fail, put=fail)
    assert ds.append_rows([]) == []


def test_append_rows_assigns_sequential_ids_after_existing_max(sa_json, monkeypatch):
    header_row = list(ds.HEADER)
    existing = ["3", "old-manifest"] + [""] * (len(ds.HEADER) - 2)
    appended = {}

    def fake_get(url, headers=None, timeout=None):
        return _Resp({"values": [header_row, existing]})

    def fake_post(url, headers=None, timeout=None, params=None, json=None):
        appended["url"] = url
        appended["params"] = params
        appended["values"] = json["values"]
        return _Resp({})

    _patch_google(monkeypatch, get=fake_get, post=fake_post)
    rows = [
        {"manifest_id": "m2", "task_id": "t1", "title": "A", "action": "delete",
         "decision": "approved", "status": "planned"},
        {"manifest_id": "m2", "task_id": "t2", "title": "B", "action": "rename",
         "decision": "approved", "status": "planned"},
    ]
    assigned = ds.append_rows(rows)
    assert assigned == [4, 5]
    assert appended["values"][0][0] == "4"
    assert appended["values"][1][0] == "5"
    assert appended["params"]["insertDataOption"] == "INSERT_ROWS"
    assert ":append" in appended["url"]


# ---- read_manifest_rows -------------------------------------------------------

def test_read_manifest_rows_filters_by_manifest_id_and_tags_sheet_row(sa_json, monkeypatch):
    header_row = list(ds.HEADER)

    def mkrow(row_id, mid, tid):
        r = [""] * len(ds.HEADER)
        r[0], r[1], r[3] = row_id, mid, tid
        return r

    row1, row2, row3 = mkrow("1", "mA", "t1"), mkrow("2", "mB", "t2"), mkrow("3", "mA", "t3")

    def fake_get(url, headers=None, timeout=None):
        return _Resp({"values": [header_row, row1, row2, row3]})

    _patch_google(monkeypatch, get=fake_get)
    rows = ds.read_manifest_rows("mA")
    assert [r["task_id"] for r in rows] == ["t1", "t3"]
    assert [r["_sheet_row"] for r in rows] == [2, 4]  # header is sheet row 1


def test_read_manifest_rows_empty_sheet_returns_empty(sa_json, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _Resp({"values": []})
    _patch_google(monkeypatch, get=fake_get)
    assert ds.read_manifest_rows("anything") == []


# ---- update_row / batch_update_rows -------------------------------------------

def test_batch_update_rows_addresses_by_stable_row_id_not_position(sa_json, monkeypatch):
    """Rows reordered in the sheet (row_id=5 sits ABOVE row_id=3) — updates
    must still land on the right physical row, found fresh by column A."""
    header_row = list(ds.HEADER)

    def mkrow(row_id):
        r = [""] * len(ds.HEADER)
        r[0] = row_id
        return r

    def fake_get(url, headers=None, timeout=None):
        return _Resp({"values": [header_row, mkrow("5"), mkrow("3")]})

    captured = {}

    def fake_post(url, headers=None, timeout=None, json=None):
        captured["url"] = url
        captured["data"] = json["data"]
        return _Resp({})

    _patch_google(monkeypatch, get=fake_get, post=fake_post)
    ds.batch_update_rows([{"row_id": 3, "status": "done",
                           "applied_ts": "2026-07-22T00:00:00-07:00"}])
    ranges = {d["range"]: d["values"][0][0] for d in captured["data"]}
    # row_id=3 is physically sheet row 3 (header=1, row_id5=2, row_id3=3).
    assert ranges.get("Declutter Log!M3") == "done"       # status column
    assert ranges.get("Declutter Log!N3") == "2026-07-22T00:00:00-07:00"  # applied_ts
    assert ":batchUpdate" in captured["url"]


def test_batch_update_rows_unknown_row_id_raises(sa_json, monkeypatch):
    header_row = list(ds.HEADER)

    def fake_get(url, headers=None, timeout=None):
        return _Resp({"values": [header_row]})

    _patch_google(monkeypatch, get=fake_get)
    with pytest.raises(ds.DeclutterSheetError, match="99"):
        ds.batch_update_rows([{"row_id": 99, "status": "done"}])


def test_batch_update_rows_ignores_unknown_fields(sa_json, monkeypatch):
    header_row = list(ds.HEADER)
    row = [""] * len(ds.HEADER)
    row[0] = "1"

    def fake_get(url, headers=None, timeout=None):
        return _Resp({"values": [header_row, row]})

    captured = {}

    def fake_post(url, headers=None, timeout=None, json=None):
        captured["data"] = json["data"]
        return _Resp({})

    _patch_google(monkeypatch, get=fake_get, post=fake_post)
    ds.batch_update_rows([{"row_id": 1, "status": "done", "not_a_real_column": "x"}])
    assert len(captured["data"]) == 1  # only the known field produced a write


def test_batch_update_rows_empty_list_is_noop_without_network(sa_json, monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("must not touch the network for an empty update")
    _patch_google(monkeypatch, get=fail, post=fail)
    ds.batch_update_rows([])  # no raise


def test_update_row_is_a_single_row_wrapper_of_batch_update_rows(sa_json, monkeypatch):
    header_row = list(ds.HEADER)
    row = [""] * len(ds.HEADER)
    row[0] = "7"

    def fake_get(url, headers=None, timeout=None):
        return _Resp({"values": [header_row, row]})

    captured = {}

    def fake_post(url, headers=None, timeout=None, json=None):
        captured["data"] = json["data"]
        return _Resp({})

    _patch_google(monkeypatch, get=fake_get, post=fake_post)
    ds.update_row(7, decision="rejected")
    # row_id=7 is the only data row, so it sits at sheet row 2 (row 1 = header).
    assert captured["data"][0]["range"] == "Declutter Log!L2"  # decision column
    assert captured["data"][0]["values"] == [["rejected"]]


# ---- sheet_url ----------------------------------------------------------------

def test_sheet_url_embeds_spreadsheet_id(monkeypatch):
    monkeypatch.setenv("DECLUTTER_SHEET_ID", "abc123")
    assert ds.sheet_url() == "https://docs.google.com/spreadsheets/d/abc123/edit"
