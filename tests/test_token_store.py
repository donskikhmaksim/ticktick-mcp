"""Durable volume token store: save/load round-trip, merge, graceful failure."""
import importlib

import ticktick_mcp.src.ticktick_client as tc


def _reload_with_path(monkeypatch, path):
    monkeypatch.setenv("TICKTICK_TOKEN_STORE", str(path))
    importlib.reload(tc)
    return tc


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    m = _reload_with_path(monkeypatch, tmp_path / "tok.json")
    assert m.save_token_file({"access_token": "AT", "refresh_token": "RT"}) is True
    got = m.load_token_file()
    assert got["access_token"] == "AT" and got["refresh_token"] == "RT"


def test_save_merges_not_clobbers(tmp_path, monkeypatch):
    m = _reload_with_path(monkeypatch, tmp_path / "tok.json")
    m.save_token_file({"access_token": "AT", "refresh_token": "RT"})
    # a later save with only access_token must keep the refresh_token
    m.save_token_file({"access_token": "AT2"})
    got = m.load_token_file()
    assert got["access_token"] == "AT2" and got["refresh_token"] == "RT"


def test_load_absent_returns_empty(tmp_path, monkeypatch):
    m = _reload_with_path(monkeypatch, tmp_path / "nope.json")
    assert m.load_token_file() == {}


def test_save_to_unwritable_path_is_graceful(monkeypatch):
    # a path under a file (not a dir) can't be created -> returns False, no raise
    m = _reload_with_path(monkeypatch, "/dev/null/cannot/tok.json")
    assert m.save_token_file({"access_token": "AT"}) is False


def test_disabled_when_empty_path(monkeypatch):
    m = _reload_with_path(monkeypatch, "")
    assert m.save_token_file({"access_token": "AT"}) is False
    assert m.load_token_file() == {}


def teardown_module(module):
    # restore default module state for other tests
    import os
    os.environ.pop("TICKTICK_TOKEN_STORE", None)
    importlib.reload(tc)
