"""PLAN_retrofit.md §16.3 (P0) — consolidated automation_key checklist.

Most of this is already covered piecemeal in test_package1_infra.py
(_resolve_automation_actor unit tests, no-leak-into-_gate_single) and
test_consent_gate.py (_require_consent bypass, create_tasks direct path).
This file exists to make the four required properties from §16.3 explicit
and traceable to the plan item in ONE place, and to add the one property
that wasn't independently covered yet: that `_require_consent`'s own
ConsentResult.reason string never contains the raw key, whether it matched
or not.
"""
import time

import ticktick_mcp.src.server as s


def _fresh_manifest(**overrides):
    now = time.monotonic()
    m = {"kind": "delete", "items": [{"taskId": "t1"}], "created": now,
         "plan_shown_at": now, "consumed": False}
    m.update(overrides)
    return m


def test_valid_automation_key_bypasses_the_gate(monkeypatch):
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY_TGBOT", "valid-secret")
    m = _fresh_manifest(consumed=True)  # would refuse on every other ground
    r = s._require_consent(action="delete", tier=2, manifest=m, user_reply="",
                           automation_key="valid-secret")
    assert r.ok is True


def test_invalid_automation_key_does_not_bypass_the_gate(monkeypatch):
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY_TGBOT", "valid-secret")
    m = _fresh_manifest()
    r = s._require_consent(action="delete", tier=2, manifest=m, user_reply="",
                           automation_key="totally-wrong")
    assert r.ok is False


def test_automation_key_is_never_MCP_SECRET(monkeypatch):
    """references/automation-secrets.md §8 / 17.5: automation_key is resolved
    against TICKTICK_AUTOMATION_KEY[_<name>] only — MCP_SECRET (the /mcp path
    secret, visible to every interactive caller by construction) must never
    also work, or handing the path secret to any consumer becomes a working
    gate bypass with no independent revocation."""
    monkeypatch.delenv("TICKTICK_AUTOMATION_KEY", raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("TICKTICK_AUTOMATION_KEY_"):
            monkeypatch.delenv(name, raising=False)
    assert s.SECRET  # sanity: MCP_SECRET path actually has a value in this env
    m = _fresh_manifest(consumed=True)
    r = s._require_consent(action="delete", tier=2, manifest=m, user_reply="",
                           automation_key=s.SECRET)
    assert r.ok is False


def test_automation_key_never_appears_in_require_consent_reason(monkeypatch):
    """Neither a matching NOR a non-matching key may leak into the reason
    string — that string is shown back to the (interactive) caller."""
    monkeypatch.setenv("TICKTICK_AUTOMATION_KEY_TGBOT", "super-secret-value")

    m = _fresh_manifest(consumed=True)
    ok_result = s._require_consent(action="delete", tier=2, manifest=m,
                                   user_reply="", automation_key="super-secret-value")
    assert ok_result.ok is True
    assert "super-secret-value" not in (ok_result.reason or "")

    m2 = _fresh_manifest()
    bad_result = s._require_consent(action="delete", tier=2, manifest=m2,
                                    user_reply="", automation_key="another-guess-xyz")
    assert "another-guess-xyz" not in (bad_result.reason or "")
    assert "super-secret-value" not in (bad_result.reason or "")


async def test_automation_key_never_appears_in_create_tasks_refusal(monkeypatch):
    """create_tasks is the one write tool whose ENTIRE gate is the
    automation_key check (module docstring, test_pkg16_reflexive_gate.py) —
    its refusal text must not echo back whatever key was guessed."""
    monkeypatch.delenv("TICKTICK_AUTOMATION_KEY", raising=False)
    result = await s.create_tasks("Test", [{"title": "X", "project_id": "p1"}],
                                  automation_key="guessed-secret-abc")
    assert "guessed-secret-abc" not in result
