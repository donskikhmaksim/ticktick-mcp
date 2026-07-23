"""Pure-logic tests for the retroactive declutter engine: clustering, the
keep-both safety bias, obsolete FLAG-only handling, umbrella grouping, SMART
rewrites, and the confirm-token / manifest shaping. The shim and the live task
list are mocked — no network, no TickTick."""
import time
from datetime import datetime, timezone

import ticktick_mcp.src.server as s


def _mk(tid, title, project="p1", parent=None, due=None, priority=0,
        created="2026-01-01T00:00:00.000+0000",
        modified="2026-07-20T00:00:00.000+0000", content=""):
    t = {"id": tid, "title": title, "projectId": project, "priority": priority,
         "createdTime": created, "modifiedTime": modified, "content": content}
    if parent:
        t["parentId"] = parent
    if due:
        t["dueDate"] = due
    return t


NAMES = {"p1": "Работа", "p2": "Дом", "inbox": "Inbox"}
NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
TODAY = NOW.date()


# ---- clustering ----------------------------------------------------------

def test_exact_duplicates_cluster_without_shim():
    tasks = [_mk("a", "Позвонить в банк"), _mk("b", "позвонить в банк "),
             _mk("c", "Купить молоко")]
    clusters = s._dc_cluster_duplicates(tasks, fuzzy=False)
    assert len(clusters) == 1
    assert clusters[0]["exact"] is True
    assert {t["id"] for t in clusters[0]["tasks"]} == {"a", "b"}


def test_fuzzy_clusters_only_when_enabled():
    tasks = [_mk("a", "Оплатить аренду офиса"),
             _mk("b", "Оплатить аренду офиса срочно сегодня")]
    assert s._dc_cluster_duplicates(tasks, fuzzy=False) == []
    fuzzy = s._dc_cluster_duplicates(tasks, fuzzy=True)
    assert len(fuzzy) == 1 and fuzzy[0]["exact"] is False


def test_pick_primary_prefers_richer_task():
    poor = _mk("a", "Task")
    rich = _mk("b", "Task", due="2026-08-01", priority=5, content="details here")
    assert s._dc_pick_primary([poor, rich]) == 1


# ---- analyze: duplicates + keep-both bias --------------------------------

async def test_exact_duplicate_becomes_delete_action():
    tasks = [_mk("a", "Позвонить в банк", due="2026-08-01"),
             _mk("b", "Позвонить в банк")]
    out = await s._dc_analyze(tasks, NAMES, judge_fn=None, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=False)
    assert len(out["delete"]) == 1
    d = out["delete"][0]
    # richer copy (with a due date) is kept; the bare one is deleted
    assert d["taskId"] == "b"
    assert d["keep_id"] == "a"
    assert "snapshot" in d and d["snapshot"]["title"] == "Позвонить в банк"


async def test_fuzzy_cluster_keeps_both_when_no_judge():
    tasks = [_mk("a", "Оплатить аренду офиса"),
             _mk("b", "Оплатить аренду офиса срочно сегодня")]
    out = await s._dc_analyze(tasks, NAMES, judge_fn=None, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=True)
    assert out["delete"] == []
    assert len(out["flag_dupe"]) == 1


async def test_fuzzy_cluster_merges_only_when_judge_is_sure():
    tasks = [_mk("a", "Оплатить аренду офиса"),
             _mk("b", "Оплатить аренду офиса срочно сегодня")]

    def judge_unsure(clusters):
        return [{"i": 0, "is_duplicate": False, "reason": "разные"}]

    def judge_sure(clusters):
        return [{"i": 0, "is_duplicate": True, "keep": 0, "reason": "одно и то же"}]

    unsure = await s._dc_analyze(tasks, NAMES, judge_fn=judge_unsure, smart_fn=None,
                           today=TODAY, now=NOW, fuzzy=True)
    assert unsure["delete"] == [] and len(unsure["flag_dupe"]) == 1

    sure = await s._dc_analyze(tasks, NAMES, judge_fn=judge_sure, smart_fn=None,
                         today=TODAY, now=NOW, fuzzy=True)
    assert len(sure["delete"]) == 1 and sure["delete"][0]["taskId"] == "b"


async def test_judge_exception_defaults_to_keep_both():
    tasks = [_mk("a", "Договор с подрядчиком"),
             _mk("b", "Договор с подрядчиком новый вариант")]

    def boom(clusters):
        raise RuntimeError("shim down mid-call")

    out = await s._dc_analyze(tasks, NAMES, judge_fn=boom, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=True)
    assert out["delete"] == [] and len(out["flag_dupe"]) == 1


# ---- Finding #1: never delete a task with live children ------------------

async def test_duplicate_with_live_children_is_never_deleted():
    """Reviewer's 'Ремонт машины' scenario: an exact duplicate pair where one
    member has live subtasks. Metadata scoring alone would pick the OTHER
    (richer) member to keep and delete the one with children — but that would
    orphan the children (parentId pointing at a deleted task). The task with
    children must always be forced onto the KEEP side."""
    parent_with_kids = _mk("a", "Ремонт машины")
    richer_bare_dup = _mk("b", "Ремонт машины", due="2026-08-01", priority=5,
                         content="подробности здесь")
    kid = _mk("k1", "Заменить масло", parent="a")
    tasks = [parent_with_kids, richer_bare_dup, kid]

    # Sanity check: without the children guard, metadata scoring alone would
    # pick "b" (richer) to keep and delete "a" — exactly the orphaning bug.
    assert s._dc_pick_primary([parent_with_kids, richer_bare_dup]) == 1

    out = await s._dc_analyze(tasks, NAMES, judge_fn=None, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=False)
    deleted_ids = {d["taskId"] for d in out["delete"]}
    assert "a" not in deleted_ids, "task with live children must never be deleted"
    assert "b" in deleted_ids
    assert out["delete"][0]["keep_id"] == "a"


async def test_duplicate_pair_both_with_children_is_flagged():
    """If BOTH members of a duplicate pair have live children, there is no
    safe single keeper — route to flag_dupe instead of guessing."""
    a = _mk("a", "Проект X")
    b = _mk("b", "Проект X")
    kid_a = _mk("ka", "Шаг 1", parent="a")
    kid_b = _mk("kb", "Шаг 2", parent="b")
    out = await s._dc_analyze([a, b, kid_a, kid_b], NAMES, judge_fn=None, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=False)
    assert out["delete"] == []
    assert len(out["flag_dupe"]) == 1
    assert set(out["flag_dupe"][0]["ids"]) == {"a", "b"}


async def test_fuzzy_duplicate_with_children_keeps_child_owner_even_if_judge_picks_other():
    """Same guard, but on the fuzzy (judge-confirmed) path: even when the judge
    explicitly names the OTHER member as 'keep', the child-owning member must
    still survive."""
    parent_with_kids = _mk("a", "Оплатить аренду офиса")
    bare_dup = _mk("b", "Оплатить аренду офиса срочно сегодня")
    kid = _mk("k1", "Подписать договор", parent="a")

    def judge_keep_b(clusters):
        return [{"i": 0, "is_duplicate": True, "keep": 1, "reason": "судья выбрал b"}]

    out = await s._dc_analyze([parent_with_kids, bare_dup, kid], NAMES,
                        judge_fn=judge_keep_b, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=True)
    deleted_ids = {d["taskId"] for d in out["delete"]}
    assert "a" not in deleted_ids
    assert "b" in deleted_ids
    assert out["delete"][0]["keep_id"] == "a"


# ---- Finding #2: fuzzy hub-clusters (3+ members) never auto-delete --------

async def test_hub_cluster_of_three_with_dissimilar_members_is_flagged_not_deleted():
    """Reviewer's hub-cluster scenario: the fuzzy pass is anchor/'star'
    clustering — each member is only checked against the ANCHOR, never against
    each other. Here B and C are each similar enough to anchor A to join its
    cluster, but B and C are NOT similar to each other — a 3-member 'hub'
    cluster mixing what may be two unrelated tasks. Even if the judge returns
    one is_duplicate=True verdict for the whole cluster, auto-delete must be
    capped at 2 members: any 3+-member fuzzy cluster is routed to flag_dupe
    instead."""
    a = _mk("a", "Счет банк")
    b = _mk("b", "Счет банк реквизиты")
    c = _mk("c", "Счет банк договор")
    tasks = [a, b, c]

    # Confirm the premise: it really is one 3-member hub cluster, and B/C are
    # not pairwise-similar to each other even though both cleared the anchor.
    clusters = s._dc_cluster_duplicates(tasks, fuzzy=True)
    assert len(clusters) == 1 and len(clusters[0]["tasks"]) == 3
    jac_bc = s._dc_jaccard(s._dc_tokens(b["title"]), s._dc_tokens(c["title"]))
    assert jac_bc < s._DC_FUZZY_THRESHOLD

    def judge_all_dupe(clusters):
        return [{"i": 0, "is_duplicate": True, "keep": 0, "reason": "судья считает дублями"}]

    out = await s._dc_analyze(tasks, NAMES, judge_fn=judge_all_dupe, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=True)
    assert out["delete"] == []
    assert len(out["flag_dupe"]) == 1
    assert set(out["flag_dupe"][0]["ids"]) == {"a", "b", "c"}


async def test_fuzzy_pair_of_two_still_auto_deletes_when_judge_confirms():
    """The 3+ cap must not regress the ordinary 2-member fuzzy-duplicate path
    (already covered by test_fuzzy_cluster_merges_only_when_judge_is_sure, but
    re-asserted here right next to the cap test for contrast)."""
    tasks = [_mk("a", "Оплатить аренду офиса"),
             _mk("b", "Оплатить аренду офиса срочно сегодня")]

    def judge_sure(clusters):
        return [{"i": 0, "is_duplicate": True, "keep": 0, "reason": "одно и то же"}]

    out = await s._dc_analyze(tasks, NAMES, judge_fn=judge_sure, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=True)
    assert len(out["delete"]) == 1 and out["delete"][0]["taskId"] == "b"


# ---- obsolete: FLAG ONLY -------------------------------------------------

async def test_obsolete_is_flagged_never_deleted():
    stale = _mk("a", "Старая задача", due="2026-01-01",
                modified="2026-01-05T00:00:00.000+0000")
    out = await s._dc_analyze([stale], NAMES, today=TODAY, now=NOW, fuzzy=False)
    assert out["delete"] == [] and out["rename"] == []
    assert len(out["flag_obsolete"]) == 1
    assert out["flag_obsolete"][0]["taskId"] == "a"


async def test_recently_touched_overdue_is_not_obsolete():
    fresh = _mk("a", "Недавно тронутая", due="2026-06-01",
                modified="2026-07-21T00:00:00.000+0000")
    out = await s._dc_analyze([fresh], NAMES, today=TODAY, now=NOW, fuzzy=False)
    assert out["flag_obsolete"] == []


async def test_task_without_due_is_not_obsolete():
    out = await s._dc_analyze([_mk("a", "Без срока")], NAMES, today=TODAY, now=NOW,
                        fuzzy=False)
    assert out["flag_obsolete"] == []


# ---- umbrella grouping ---------------------------------------------------

async def test_umbrella_group_detected():
    tasks = [_mk("h", "Ремонт квартиры"),
             _mk("c1", "Ремонт квартиры покраска стен"),
             _mk("c2", "Ремонт квартиры замена окон")]
    out = await s._dc_analyze(tasks, NAMES, today=TODAY, now=NOW, fuzzy=False)
    assert len(out["group"]) == 1
    g = out["group"][0]
    assert g["parentId"] == "h"
    assert {c["taskId"] for c in g["children"]} == {"c1", "c2"}


async def test_no_group_across_projects():
    tasks = [_mk("h", "Проект альфа", project="p1"),
             _mk("c1", "Проект альфа этап один", project="p2"),
             _mk("c2", "Проект альфа этап два", project="p2")]
    out = await s._dc_analyze(tasks, NAMES, today=TODAY, now=NOW, fuzzy=False)
    # header in p1 has no same-project children; p2 has no header prefix task
    assert out["group"] == []


async def test_already_parented_task_not_regrouped():
    tasks = [_mk("h", "Ремонт квартиры"),
             _mk("c1", "Ремонт квартиры покраска", parent="something"),
             _mk("c2", "Ремонт квартиры окна", parent="something")]
    out = await s._dc_analyze(tasks, NAMES, today=TODAY, now=NOW, fuzzy=False)
    assert out["group"] == []


# ---- SMART rewrites ------------------------------------------------------

async def test_smart_rewrite_becomes_rename_action():
    tasks = [_mk("a", "Банк")]

    def smart(titles):
        return [{"i": 0, "new_title": "Позвонить в банк по кредиту",
                 "reason": "конкретизировал"}]

    out = await s._dc_analyze(tasks, NAMES, smart_fn=smart, today=TODAY, now=NOW,
                        fuzzy=False)
    assert len(out["rename"]) == 1
    assert out["rename"][0]["new_title"] == "Позвонить в банк по кредиту"


async def test_nonsmart_without_shim_is_flagged_not_renamed():
    out = await s._dc_analyze([_mk("a", "Банк")], NAMES, smart_fn=None, today=TODAY,
                        now=NOW, fuzzy=False)
    assert out["rename"] == []
    assert len(out["flag_nonsmart"]) == 1


async def test_smart_empty_rewrite_keeps_title():
    def smart(titles):
        return [{"i": 0, "new_title": "", "reason": "уже норм"}]

    out = await s._dc_analyze([_mk("a", "Банк")], NAMES, smart_fn=smart, today=TODAY,
                        now=NOW, fuzzy=False)
    assert out["rename"] == [] and len(out["flag_nonsmart"]) == 1


# ---- an id claimed by one action is not reused by another ----------------

async def test_deleted_duplicate_not_also_grouped_or_renamed():
    tasks = [_mk("a", "X"), _mk("b", "X")]  # exact dupes, both short titles

    def smart(titles):
        return [{"i": i, "new_title": f"Сделать {t}", "reason": "r"}
                for i, t in enumerate(titles)]

    out = await s._dc_analyze(tasks, NAMES, smart_fn=smart, today=TODAY, now=NOW,
                        fuzzy=False)
    claimed = {d["taskId"] for d in out["delete"]}
    claimed |= {d["keep_id"] for d in out["delete"]}
    renamed = {r["taskId"] for r in out["rename"]}
    assert not (claimed & renamed)  # no id in both buckets


# ---- manifest shaping / confirm token ------------------------------------

def test_mutating_count_sums_all_action_types():
    actions = {
        "delete": [{"taskId": "1"}, {"taskId": "2"}],
        "rename": [{"taskId": "3"}],
        "group": [{"children": [{"taskId": "4"}, {"taskId": "5"}]}],
        "flag_obsolete": [{"taskId": "x"}], "flag_dupe": [], "flag_nonsmart": [],
    }
    assert s._dc_mutating_count(actions) == 5  # 2 + 1 + 2, flags excluded


def test_scope_filter_inbox_and_substring():
    tasks = [_mk("a", "t1", project="inbox"), _mk("b", "t2", project="p1"),
             _mk("c", "t3", project="p2")]
    assert {t["id"] for t in s._dc_scope_filter(tasks, NAMES, "inbox")} == {"a"}
    assert {t["id"] for t in s._dc_scope_filter(tasks, NAMES, "раб")} == {"b"}
    assert len(s._dc_scope_filter(tasks, NAMES, "")) == 3


# ---- Finding #7: shim-call-failure tracking (vs. simply unconfigured) ----

def test_shim_json_records_failure_on_non_ok_response(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_URL", "http://shim.example")
    monkeypatch.setenv("CLAUDE_CLI_TOKEN", "tok")
    import requests

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": False}

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Resp())
    tracker: list = []
    res = s._dc_shim_json("sys", "prompt", fail_tracker=tracker)
    assert res is None
    assert tracker == [True]


def test_shim_json_records_failure_on_exception(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_URL", "http://shim.example")
    monkeypatch.setenv("CLAUDE_CLI_TOKEN", "tok")
    import requests

    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(requests, "post", boom)
    tracker: list = []
    res = s._dc_shim_json("sys", "prompt", fail_tracker=tracker)
    assert res is None
    assert tracker == [True]


def test_shim_json_no_failure_recorded_on_success(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_URL", "http://shim.example")
    monkeypatch.setenv("CLAUDE_CLI_TOKEN", "tok")
    import requests

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "result": "[1, 2, 3]"}

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Resp())
    tracker: list = []
    res = s._dc_shim_json("sys", "prompt", fail_tracker=tracker)
    assert res == [1, 2, 3]
    assert tracker == []


def test_shim_json_unconfigured_is_not_counted_as_a_failure(monkeypatch):
    """Not having CLAUDE_CLI_URL set at all is a different situation from a
    configured shim whose call failed — plan_declutter already has a separate
    warning for 'not configured', so an unset shim must NOT populate the
    fail_tracker."""
    monkeypatch.delenv("CLAUDE_CLI_URL", raising=False)
    tracker: list = []
    res = s._dc_shim_json("sys", "prompt", fail_tracker=tracker)
    assert res is None
    assert tracker == []


# ---- Finding #8: execute_declutter never raises with the manifest already
# marked consumed — an internal glue-code exception must come back as a
# graceful error string. -------------------------------------------------

async def test_execute_declutter_returns_graceful_error_on_internal_exception(monkeypatch):
    monkeypatch.setattr(s, "ticktick_v2", object())  # _ensure_ready() passes
    mid = "test-declutter-mid"
    s._MANIFESTS[mid] = {
        "kind": "declutter",
        "actions": {
            "delete": [],
            "rename": [{"taskId": "x", "projectId": "p1", "title": "t",
                        "new_title": "t2"}],
            "group": [],
        },
        "mutating_count": 1,
        "created": time.monotonic(),
        "summary": "test",
        "consumed": False,
    }

    async def boom(*args, **kwargs):
        raise RuntimeError("glue-code exploded")

    monkeypatch.setattr(s, "update_tasks", boom)

    result = await s.execute_declutter(mid, confirm="DECLUTTER 1")
    assert "Error executing declutter manifest" in result
    assert "glue-code exploded" in result
    # The manifest was already marked consumed before the crash — that's fine,
    # as long as the caller gets a readable error instead of an unhandled
    # exception with no response.
    assert s._MANIFESTS[mid]["consumed"] is True


# ---- Timeout fix: input-size cap + concurrent shim dispatch ---------------

async def test_plan_declutter_refuses_over_cap_before_clustering(monkeypatch):
    """A scope resolving to more tasks than _DC_MAX_TASKS must be refused
    BEFORE any O(n^2) clustering/grouping or shim call — that's the whole
    point of the cap (those passes, plus the two shim prompts, are what blew
    past the MCP client's 60s timeout on a real full-pile task list)."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    n = s._DC_MAX_TASKS + 37
    tasks_by_id = {f"t{i}": _mk(f"t{i}", f"Задача {i}") for i in range(n)}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: tasks_by_id)
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(NAMES))

    def boom(*a, **kw):
        raise AssertionError("must not run above the input-size cap")

    # Neither the O(n^2) passes nor a shim call may happen once refused.
    monkeypatch.setattr(s, "_dc_cluster_duplicates", boom)
    monkeypatch.setattr(s, "_dc_group_candidates", boom)
    monkeypatch.setattr(s, "_dc_shim_available", lambda: True)
    monkeypatch.setattr(s, "_dc_judge_fn", boom)
    monkeypatch.setattr(s, "_dc_smart_fn", boom)

    before_keys = set(s._MANIFESTS.keys())
    result = await s.plan_declutter()
    # Refusal is read-only: no NEW manifest created (existing ones may still
    # get pruned as a normal side effect of _prune_manifests(), unrelated to
    # this refusal path).
    assert set(s._MANIFESTS.keys()) <= before_keys

    assert "🛑" in result
    assert str(n) in result  # actual count, so the caller knows how far over
    assert str(s._DC_MAX_TASKS) in result
    assert "scope" in result.lower()


async def test_plan_declutter_within_cap_still_analyzes(monkeypatch):
    """Sanity counterpart: at/under the cap, analysis proceeds as normal."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    n = s._DC_MAX_TASKS  # exactly at the cap must NOT be refused
    tasks_by_id = {f"t{i}": _mk(f"t{i}", f"Задача {i}") for i in range(n)}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: tasks_by_id)
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(NAMES))
    monkeypatch.setattr(s, "_dc_shim_available", lambda: False)

    result = await s.plan_declutter()
    assert "🛑 Отказ" not in result
    assert "разбора помойки" in result
    assert f"проверено задач: {n}" in result


async def test_analyze_dispatches_judge_and_smart_concurrently():
    """judge_fn and smart_fn are independent (different candidate sets,
    different shim prompts) and must run CONCURRENTLY (asyncio.gather over
    asyncio.to_thread), not back-to-back — sequential dispatch was half of
    why plan_declutter could exceed the 60s MCP client timeout. Fake both
    calls as blocking sleeps and assert wall time tracks max(), not sum()."""
    SLEEP = 0.2

    def slow_judge(clusters):
        time.sleep(SLEEP)
        return [{"i": 0, "is_duplicate": True, "keep": 0, "reason": "судья подтвердил"}]

    def slow_smart(titles):
        time.sleep(SLEEP)
        return [{"i": i, "new_title": f"Сделать {t}", "reason": "r"}
                for i, t in enumerate(titles)]

    tasks = [_mk("a", "Оплатить аренду офиса"),
             _mk("b", "Оплатить аренду офиса срочно сегодня"),
             _mk("c", "Банк")]

    start = time.monotonic()
    out = await s._dc_analyze(tasks, NAMES, judge_fn=slow_judge, smart_fn=slow_smart,
                              today=TODAY, now=NOW, fuzzy=True)
    elapsed = time.monotonic() - start

    # Sequential would cost >= 2*SLEEP; concurrent should track ~1*SLEEP.
    assert elapsed < SLEEP * 1.7, (
        f"judge_fn/smart_fn look like they ran sequentially (elapsed={elapsed:.3f}s, "
        f"expected ~{SLEEP:.3f}s if truly concurrent)")

    # Both calls' results actually made it into the output.
    assert len(out["delete"]) == 1  # fuzzy pair merged per slow_judge's verdict
    assert len(out["rename"]) == 1  # "Банк" -> SMART rewrite per slow_smart
    assert out["rename"][0]["taskId"] == "c"
