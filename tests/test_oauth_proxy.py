"""oauth-proxy security: HMAC-signed state (no forgery), return_to allowlist
(no token exfil), and HTML escaping (no XSS). Import the module directly from
its file path with the required env set first."""
import importlib.util
import os
import pathlib

import pytest

PROXY = pathlib.Path(__file__).resolve().parents[1] / "oauth-proxy" / "api" / "index.py"


@pytest.fixture(scope="module")
def proxy():
    os.environ.setdefault("TICKTICK_CLIENT_ID", "cid")
    os.environ.setdefault("TICKTICK_CLIENT_SECRET", "sec")
    os.environ.setdefault("REDIRECT_URI", "https://proxy.up.railway.app/callback")
    os.environ["PROXY_STATE_SECRET"] = "fixed-test-secret"
    spec = importlib.util.spec_from_file_location("oauth_proxy_index", PROXY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_state_roundtrip(proxy):
    state = proxy._make_state("https://friend.up.railway.app", "friend-secret")
    got = proxy._read_state(state)
    assert got == ("https://friend.up.railway.app", "friend-secret")


def test_forged_state_rejected(proxy):
    state = proxy._make_state("https://friend.up.railway.app", "s")
    payload, _sig = state.rsplit(".", 1)
    tampered = payload + ".deadbeef"
    assert proxy._read_state(tampered) is None


def test_tampered_payload_rejected(proxy):
    import base64
    import json
    # attacker swaps return_to but keeps a random sig
    evil = base64.urlsafe_b64encode(
        json.dumps({"return_to": "https://evil.com", "secret": "s"}).encode()
    ).decode()
    assert proxy._read_state(evil + ".00") is None


def test_return_to_allowlist(proxy):
    assert proxy._return_to_is_allowed("https://x.up.railway.app") is True
    assert proxy._return_to_is_allowed("https://evil.com") is False
    assert proxy._return_to_is_allowed("http://x.up.railway.app") is False  # not https
    assert proxy._return_to_is_allowed("not-a-url") is False


def test_relay_page_escapes_tokens(proxy):
    html = proxy._relay_page(
        "https://friend.up.railway.app",
        "s",
        '"><script>alert(1)</script>',
        "ref",
    )
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_error_page_escapes(proxy):
    html = proxy._error_page('<img src=x onerror=alert(1)>')
    assert "<img src=x" not in html
