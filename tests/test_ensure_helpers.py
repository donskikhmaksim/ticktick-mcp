"""Guard against the _ensure_ready infinite-recursion regression and verify
the readiness helpers behave. These are called at the top of every tool."""
import ticktick_mcp.src.server as s


def test_ensure_ready_no_recursion_when_v2_absent(monkeypatch):
    # v2 disabled -> must return the disabled message, NOT recurse forever.
    monkeypatch.setattr(s, "ticktick_v2", None)
    monkeypatch.setattr(s, "initialize_client", lambda: False)
    result = s._ensure_ready()
    assert result == s._V2_DISABLED_MSG


def test_ensure_ready_ok_when_v2_present(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", object())
    assert s._ensure_ready() is None


def test_ensure_official_ok_when_client_present(monkeypatch):
    monkeypatch.setattr(s, "ticktick", object())
    assert s._ensure_official() is None


def test_ensure_official_fails_when_init_fails(monkeypatch):
    monkeypatch.setattr(s, "ticktick", None)
    monkeypatch.setattr(s, "initialize_client", lambda: False)
    assert s._ensure_official() == s._INIT_FAIL_MSG
