"""Shared fixtures. Env is set BEFORE importing the server module so its
module-level config (SECRET, _USER_TZ) reads deterministic values."""
import os

os.environ.setdefault("MCP_SECRET", "test-secret")
os.environ.setdefault("USER_TIMEZONE", "UTC")
os.environ.setdefault("TICKTICK_CLIENT_ID", "cid")
os.environ.setdefault("TICKTICK_CLIENT_SECRET", "csecret")
os.environ.setdefault("TICKTICK_ACCESS_TOKEN", "atoken")
# The consent gate (docs/DESIGN_approval_gate.md §4.3.4) requires a minimum
# wall-clock gap between plan_* and execute_*(user_reply=...) to catch a
# model firing both in the same turn. Unit tests build a manifest and consume
# it within microseconds on purpose — default that gap to 0 here so ordinary
# tests aren't fighting a timer; a dedicated consent test overrides this back
# to a positive value with monkeypatch to exercise the gap itself.
os.environ.setdefault("MIN_CONSENT_GAP", "0")
