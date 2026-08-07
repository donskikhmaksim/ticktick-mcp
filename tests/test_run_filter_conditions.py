"""run_filter must never silently pass everything through.

Live acceptance (2026-08-07): the owner's saved filter «For me»
(rule `{"conditionName":"assignee","or":["noassignee","me"]}`) returned 1477
tasks — the ENTIRE open-task pool — because `_leaf_matches()` knew only
list / listOrGroup / tag / priority / dueDate and answered `return True` for
anything else. The output carried no hint of that, so a long list reads as a
filtered result when it is in fact no filtering at all.

Two behaviours are locked in here:
  1. `assignee` is genuinely evaluated (noassignee / me / a member's userId);
  2. any condition this client still cannot evaluate is REPORTED — the tool
     output says which condition was skipped and that the result is therefore
     not filtered by it.
"""
import json

import pytest

import ticktick_mcp.src.server as s
from ticktick_mcp.src.ticktick_v2_client import TickTickV2Client

OWNER_ID = 123456


def _rule(condition_name, values):
    return json.dumps({
        "type": 0,
        "and": [{"conditionType": 1, "or": list(values), "conditionName": condition_name}],
        "version": 1,
    })


def _tasks():
    return [
        {"id": "t1", "title": "Ничей", "projectId": "p1"},
        {"id": "t2", "title": "Мой", "projectId": "p1", "assignee": OWNER_ID},
        {"id": "t3", "title": "Чужой", "projectId": "p1", "assignee": 999999},
    ]


def _client(rule_json, name="For me", tasks=None):
    """A real TickTickV2Client with its sync state stubbed — the filter logic
    under test is the client's own, no network."""
    state = {
        "inboxId": f"inbox{OWNER_ID}",
        "filters": [{"id": "f1", "name": name, "rule": rule_json}],
        "projectProfiles": [{"id": "p1", "name": "Работа", "groupId": None}],
        "syncTaskBean": {"update": tasks if tasks is not None else _tasks()},
    }
    c = TickTickV2Client(token="test-token")
    c.get_state = lambda force=False: state  # noqa: ARG005
    return c


# ---- 1. assignee is really evaluated --------------------------------------


def test_assignee_me_and_noassignee_excludes_other_peoples_tasks():
    c = _client(_rule("assignee", ["noassignee", "me"]))

    titles = [t["title"] for t in c.run_filter("For me")]

    assert titles == ["Ничей", "Мой"], (
        "фильтр assignee пропустил чужую задачу — условие не вычисляется"
    )


def test_assignee_me_only():
    c = _client(_rule("assignee", ["me"]))

    assert [t["title"] for t in c.run_filter("For me")] == ["Мой"]


def test_assignee_noassignee_only():
    c = _client(_rule("assignee", ["noassignee"]))

    assert [t["title"] for t in c.run_filter("For me")] == ["Ничей"]


def test_assignee_explicit_user_id():
    c = _client(_rule("assignee", ["999999"]))

    assert [t["title"] for t in c.run_filter("For me")] == ["Чужой"]


# ---- 2. an unknown condition is reported, not silently ignored ------------


def test_unknown_condition_is_reported_by_the_client():
    c = _client(_rule("someFutureCondition", ["whatever"]), name="Странный")

    tasks, unsupported = c.run_filter_detailed("Странный")

    assert unsupported == ["someFutureCondition"]
    # the tasks themselves still come back (a partial filter beats no result),
    # but the caller now knows the result is not filtered by that condition
    assert len(tasks) == 3


def test_known_conditions_are_not_reported_as_unsupported():
    for name, values in (
        ("list", ["all"]),
        ("listOrGroup", ["all"]),
        ("tag", ["дом"]),
        ("priority", [5]),
        ("dueDate", ["today"]),
        ("assignee", ["me"]),
    ):
        c = _client(_rule(name, values), name="X")
        _, unsupported = c.run_filter_detailed("X")
        assert unsupported == [], f"условие {name} поддерживается, но помечено как неизвестное"


@pytest.fixture
def _ready(monkeypatch):
    monkeypatch.setattr(s, "ticktick", object())


async def test_tool_output_warns_about_the_unsupported_condition(monkeypatch, _ready):
    c = _client(_rule("someFutureCondition", ["whatever"]), name="Странный")
    monkeypatch.setattr(s, "ticktick_v2", c)

    out = await s.run_filter("Странный")

    assert "⚠" in out, "нет предупреждения — результат выглядит как отфильтрованный"
    assert "someFutureCondition" in out, "не названо условие, по которому не фильтровали"
    assert "не применял" in out.lower(), "не сказано, что фильтрация НЕ применялась"


async def test_tool_output_has_no_warning_for_a_supported_filter(monkeypatch, _ready):
    c = _client(_rule("assignee", ["noassignee", "me"]))
    monkeypatch.setattr(s, "ticktick_v2", c)

    out = await s.run_filter("For me")

    assert "⚠" not in out
    assert "Мой" in out and "Ничей" in out
    assert "Чужой" not in out
