"""Shared fixtures. Env is set BEFORE importing the server module so its
module-level config (SECRET, _USER_TZ) reads deterministic values."""
import os

os.environ.setdefault("MCP_SECRET", "test-secret")
os.environ.setdefault("USER_TIMEZONE", "UTC")
os.environ.setdefault("TICKTICK_CLIENT_ID", "cid")
os.environ.setdefault("TICKTICK_CLIENT_SECRET", "csecret")
os.environ.setdefault("TICKTICK_ACCESS_TOKEN", "atoken")
