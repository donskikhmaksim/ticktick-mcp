"""Nested-subtask depth guard (night QA campaign, 2026-08-06).

Bug: create_subtask/set_task_parent/create_tasks docstrings promised a
nesting limit ("up to 4 levels") but nothing on the server actually checked
it — a 5th level (and deeper) went through silently on both the PLAN call
and the EXECUTE call.

Fact-check against TickTick's own official help center ("Multilevel Tasks"
article, FAQ "Does multi-level task support unlimited splitting?"): «At
present, we only allow up to 5 levels of nested tasks. If the limit is
exceeded, you cannot continue to add.» — TickTick's REAL cap is 5 total
levels (root task = level 1), not 4. That cap lives in TickTick's own apps'
UI, NOT in the v2 API this server calls directly, so a tree built through
this server could silently exceed what TickTick's own apps can display or
let you edit further.

Chosen fix: option (a) — real server-side validation, computed from LIVE
parentId chains (never from what a caller claims), refusing fail-closed
both when a plan is built and when it executes. MAX_TASK_NEST_LEVELS = 5
(server.py) is the single source of truth for the number itself.

These tests are pure-logic: the pure `_..._impl` functions are exercised
directly (bypassing the plan/approve consent gate, which is covered by its
own tests elsewhere), with `_open_by_id` and the v2/official clients
replaced by small in-memory fakes — mirroring the pattern in
tests/test_slice1_real_gates.py.
"""
import ticktick_mcp.src.server as s


# ===========================================================================
# Pure helpers — no I/O, no live state
# ===========================================================================

def test_task_level_of_root_is_1():
    by_id = {"root": {"id": "root"}}
    assert s._task_level("root", by_id) == 1


def test_task_level_walks_parent_chain():
    by_id = {
        "l1": {"id": "l1"},
        "l2": {"id": "l2", "parentId": "l1"},
        "l3": {"id": "l3", "parentId": "l2"},
        "l4": {"id": "l4", "parentId": "l3"},
        "l5": {"id": "l5", "parentId": "l4"},
    }
    assert s._task_level("l1", by_id) == 1
    assert s._task_level("l3", by_id) == 3
    assert s._task_level("l5", by_id) == 5


def test_task_level_cycle_does_not_hang():
    by_id = {"a": {"id": "a", "parentId": "b"}, "b": {"id": "b", "parentId": "a"}}
    # must return, not loop forever — the exact number doesn't matter as
    # much as termination, but it should stop at the cycle boundary.
    assert s._task_level("a", by_id) == 2


def test_subtree_height_of_leaf_is_1():
    by_id = {"x": {"id": "x"}}
    assert s._subtree_height("x", s._children_index(by_id)) == 1


def test_subtree_height_counts_deepest_branch():
    by_id = {
        "p": {"id": "p"},
        "c1": {"id": "c1", "parentId": "p"},
        "c2": {"id": "c2", "parentId": "p"},
        "g1": {"id": "g1", "parentId": "c1"},
    }
    # p -> c1 -> g1 is the deepest branch: height 3 (p, c1, g1)
    assert s._subtree_height("p", s._children_index(by_id)) == 3


def test_subtree_height_cycle_does_not_hang():
    children_of = {"a": ["b"], "b": ["a"]}  # deliberately corrupt/circular
    assert s._subtree_height("a", children_of) >= 1  # returns, doesn't hang


def test_requested_tree_depth_flat_task_is_1():
    assert s._requested_tree_depth({"title": "Solo"}) == 1


def test_requested_tree_depth_string_subtasks_add_one_level():
    node = {"title": "Root", "subtasks": ["A", "B", "C"]}
    assert s._requested_tree_depth(node) == 2


def test_requested_tree_depth_nested_dicts():
    node = {"title": "Root", "subtasks": [
        {"title": "Sub", "subtasks": [
            {"title": "SubSub", "subtasks": [
                {"title": "SubSubSub"}
            ]}
        ]}
    ]}
    # Root(1) -> Sub(2) -> SubSub(3) -> SubSubSub(4)
    assert s._requested_tree_depth(node) == 4


def test_requested_tree_depth_picks_deepest_branch():
    node = {"title": "Root", "subtasks": [
        "Shallow sibling",
        {"title": "Deep", "subtasks": [{"title": "Deeper", "subtasks": [{"title": "Deepest"}]}]},
    ]}
    assert s._requested_tree_depth(node) == 4  # Root -> Deep -> Deeper -> Deepest


# ===========================================================================
# create_subtask — depth guard against a LIVE parent chain
# ===========================================================================

class _FakeOfficialSubtask:
    def __init__(self, live):
        self.live = live
        self.calls = []

    def create_subtask(self, subtask_title, parent_task_id, project_id,
                       content=None, priority=0):
        self.calls.append(subtask_title)
        tid = f"new-{len(self.calls)}"
        self.live[tid] = {"id": tid, "title": subtask_title,
                          "projectId": project_id, "parentId": parent_task_id}
        return {"id": tid, "title": subtask_title, "parentId": parent_task_id}


def _chain(n, project_id="p1"):
    """{"l1": level1 (no parent), "l2": under l1, ...}. Not open, but each
    consecutive id parents into the previous one — an n-level-deep chain."""
    by_id = {}
    prev = None
    for i in range(1, n + 1):
        tid = f"l{i}"
        t = {"id": tid, "title": f"Level {i}", "projectId": project_id}
        if prev:
            t["parentId"] = prev
        by_id[tid] = t
        prev = tid
    return by_id


async def test_create_subtask_refused_when_parent_already_at_max_depth(monkeypatch):
    live = _chain(5)  # l5 sits at level 5 — TickTick's real cap
    official = _FakeOfficialSubtask(live)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "ticktick", official)

    result = await s._create_subtask_impl(
        parent_task_title="Level 5", subtask_title="Too deep",
        parent_task_id="l5", project_id="p1")

    assert "🛑" in result
    assert "5" in result  # mentions the levels involved
    assert official.calls == []  # nothing was actually created


async def test_create_subtask_allowed_one_below_max_depth(monkeypatch):
    live = _chain(4)  # l4 sits at level 4 -> a new subtask would be level 5 (OK)
    official = _FakeOfficialSubtask(live)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "ticktick", official)

    result = await s._create_subtask_impl(
        parent_task_title="Level 4", subtask_title="Just fits",
        parent_task_id="l4", project_id="p1")

    assert "🛑" not in result
    assert official.calls == ["Just fits"]


# ===========================================================================
# set_task_parent — batch depth guard, including a task that already has its
# OWN descendants (moving it drags them along)
# ===========================================================================

class _FakeV2Parent:
    def __init__(self, live):
        self.live = live
        self.calls = []

    def set_task_parents(self, rows):
        self.calls.append(rows)
        for r in rows:
            if r["taskId"] in self.live:
                self.live[r["taskId"]]["parentId"] = r["parentId"]
        return {}


def _wire_parent(monkeypatch, live):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    fake = _FakeV2Parent(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


async def test_set_task_parent_refuses_when_result_exceeds_cap(monkeypatch):
    live = _chain(4)  # l4 = level 4
    live["leaf"] = {"id": "leaf", "title": "Leaf", "projectId": "p1"}
    fake = _wire_parent(monkeypatch, live)

    # leaf under l4 -> would land at level 5: fine
    # leaf under... let's instead push a task under l4 that ALREADY has a
    # child of its own, so nesting it drags the child to level 6.
    live["leaf"]["parentId"] = None
    live["leafchild"] = {"id": "leafchild", "title": "Leaf child",
                         "projectId": "p1", "parentId": "leaf"}

    result = await s._set_task_parent_impl(
        "тест", [{"taskId": "leaf", "title": "Leaf"}],
        parent_task_id="l4", project_id="p1", parent_task_title="Level 4")

    assert "🛑" in result
    assert fake.calls == []  # set_task_parents was never even called
    assert live["leaf"].get("parentId") is None  # unchanged


async def test_set_task_parent_allows_leaf_at_exactly_max_depth(monkeypatch):
    live = _chain(4)
    live["leaf"] = {"id": "leaf", "title": "Leaf", "projectId": "p1"}
    fake = _wire_parent(monkeypatch, live)

    result = await s._set_task_parent_impl(
        "тест", [{"taskId": "leaf", "title": "Leaf"}],
        parent_task_id="l4", project_id="p1", parent_task_title="Level 4")

    assert "🛑" not in result
    assert live["leaf"]["parentId"] == "l4"
    assert fake.calls  # the v2 call actually happened


async def test_set_task_parent_batch_mixed_ok_and_too_deep(monkeypatch):
    live = _chain(4)
    live["shallow"] = {"id": "shallow", "title": "Shallow", "projectId": "p1"}
    live["deep"] = {"id": "deep", "title": "Deep", "projectId": "p1"}
    live["deepchild"] = {"id": "deepchild", "title": "Deep child",
                         "projectId": "p1", "parentId": "deep"}
    _wire_parent(monkeypatch, live)

    result = await s._set_task_parent_impl(
        "тест", [{"taskId": "shallow", "title": "Shallow"},
                {"taskId": "deep", "title": "Deep"}],
        parent_task_id="l4", project_id="p1", parent_task_title="Level 4")

    assert live["shallow"]["parentId"] == "l4"  # the ok one went through
    assert live["deep"].get("parentId") is None  # the too-deep one refused
    assert "🛑" in result
    assert "Deep" in result


# ===========================================================================
# create_tasks — depth guard on the REQUESTED tree, plus the live depth of
# an EXISTING parent_id target when the tree attaches under one
# ===========================================================================

class _FakeV2Create:
    def __init__(self, live):
        self.live = live
        self.batch_calls = []

    def invalidate_cache(self):
        pass

    def batch_create_tasks(self, tasks_flat):
        self.batch_calls.append(tasks_flat)
        for obj in tasks_flat:
            self.live[obj["id"]] = {"id": obj["id"], "title": obj.get("title"),
                                    "projectId": obj.get("projectId")}
        return {}

    def _request(self, method, path, json=None):
        for rel in (json or []):
            t = self.live.setdefault(rel["taskId"], {"id": rel["taskId"]})
            t["parentId"] = rel["parentId"]
        return {}


def _wire_create(monkeypatch, live):
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "ticktick", object())  # just needs to be truthy
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Работа"})
    fake = _FakeV2Create(live)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


def _nest(depth, title="T"):
    """A dict subtree exactly `depth` levels deep (depth=1 -> a leaf)."""
    node = {"title": f"{title}{depth}"}
    for d in range(depth - 1, 0, -1):
        node = {"title": f"{title}{d}", "subtasks": [node]}
    return node


async def test_create_tasks_refuses_a_root_tree_deeper_than_cap(monkeypatch):
    live = {}
    fake = _wire_create(monkeypatch, live)
    tree = _nest(6, title="X")  # 6 levels requested — TickTick's real cap is 5
    tree["project_id"] = "p1"

    result = await s._create_tasks_impl("тест", [tree])

    assert "🛑" in result
    assert fake.batch_calls == []  # nothing created at all — fail-closed


async def test_create_tasks_allows_a_root_tree_exactly_at_cap(monkeypatch):
    live = {}
    fake = _wire_create(monkeypatch, live)
    tree = _nest(5, title="X")  # exactly 5 levels — TickTick's real cap
    tree["project_id"] = "p1"

    result = await s._create_tasks_impl("тест", [tree])

    assert "🛑" not in result
    assert fake.batch_calls  # the tree was actually built
    created_titles = {t["title"] for t in fake.batch_calls[0]}
    assert created_titles == {"X1", "X2", "X3", "X4", "X5"}


async def test_create_tasks_refuses_attach_under_an_already_deep_parent(monkeypatch):
    live = _chain(4)  # l4 = level 4
    fake = _wire_create(monkeypatch, live)
    # attaching even a single flat task under l4 (level 4) would be fine
    # (lands at level 5) — but a 2-level tree would reach level 6.
    tree = _nest(2, title="Y")
    tree["project_id"] = "p1"
    tree["parent_id"] = "l4"

    result = await s._create_tasks_impl("тест", [tree])

    assert "🛑" in result
    assert fake.batch_calls == []


async def test_create_tasks_allows_attach_under_parent_leaving_exact_headroom(monkeypatch):
    live = _chain(3)  # l3 = level 3, exactly 2 levels of headroom left
    fake = _wire_create(monkeypatch, live)
    tree = _nest(2, title="Y")  # Y1 -> Y2, 2 levels
    tree["project_id"] = "p1"
    tree["parent_id"] = "l3"

    result = await s._create_tasks_impl("тест", [tree])

    assert "🛑" not in result
    assert fake.batch_calls
