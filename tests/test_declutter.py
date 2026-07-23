"""Pure-logic tests for the retroactive declutter engine: clustering, the
keep-both safety bias, obsolete FLAG-only handling, umbrella grouping, SMART
rewrites, and the confirm-token / manifest shaping. The shim and the live task
list are mocked — no network, no TickTick."""
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

def test_exact_duplicate_becomes_delete_action():
    tasks = [_mk("a", "Позвонить в банк", due="2026-08-01"),
             _mk("b", "Позвонить в банк")]
    out = s._dc_analyze(tasks, NAMES, judge_fn=None, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=False)
    assert len(out["delete"]) == 1
    d = out["delete"][0]
    # richer copy (with a due date) is kept; the bare one is deleted
    assert d["taskId"] == "b"
    assert d["keep_id"] == "a"
    assert "snapshot" in d and d["snapshot"]["title"] == "Позвонить в банк"


def test_fuzzy_cluster_keeps_both_when_no_judge():
    tasks = [_mk("a", "Оплатить аренду офиса"),
             _mk("b", "Оплатить аренду офиса срочно сегодня")]
    out = s._dc_analyze(tasks, NAMES, judge_fn=None, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=True)
    assert out["delete"] == []
    assert len(out["flag_dupe"]) == 1


def test_fuzzy_cluster_merges_only_when_judge_is_sure():
    tasks = [_mk("a", "Оплатить аренду офиса"),
             _mk("b", "Оплатить аренду офиса срочно сегодня")]

    def judge_unsure(clusters):
        return [{"i": 0, "is_duplicate": False, "reason": "разные"}]

    def judge_sure(clusters):
        return [{"i": 0, "is_duplicate": True, "keep": 0, "reason": "одно и то же"}]

    unsure = s._dc_analyze(tasks, NAMES, judge_fn=judge_unsure, smart_fn=None,
                           today=TODAY, now=NOW, fuzzy=True)
    assert unsure["delete"] == [] and len(unsure["flag_dupe"]) == 1

    sure = s._dc_analyze(tasks, NAMES, judge_fn=judge_sure, smart_fn=None,
                         today=TODAY, now=NOW, fuzzy=True)
    assert len(sure["delete"]) == 1 and sure["delete"][0]["taskId"] == "b"


def test_judge_exception_defaults_to_keep_both():
    tasks = [_mk("a", "Договор с подрядчиком"),
             _mk("b", "Договор с подрядчиком новый вариант")]

    def boom(clusters):
        raise RuntimeError("shim down mid-call")

    out = s._dc_analyze(tasks, NAMES, judge_fn=boom, smart_fn=None,
                        today=TODAY, now=NOW, fuzzy=True)
    assert out["delete"] == [] and len(out["flag_dupe"]) == 1


# ---- obsolete: FLAG ONLY -------------------------------------------------

def test_obsolete_is_flagged_never_deleted():
    stale = _mk("a", "Старая задача", due="2026-01-01",
                modified="2026-01-05T00:00:00.000+0000")
    out = s._dc_analyze([stale], NAMES, today=TODAY, now=NOW, fuzzy=False)
    assert out["delete"] == [] and out["rename"] == []
    assert len(out["flag_obsolete"]) == 1
    assert out["flag_obsolete"][0]["taskId"] == "a"


def test_recently_touched_overdue_is_not_obsolete():
    fresh = _mk("a", "Недавно тронутая", due="2026-06-01",
                modified="2026-07-21T00:00:00.000+0000")
    out = s._dc_analyze([fresh], NAMES, today=TODAY, now=NOW, fuzzy=False)
    assert out["flag_obsolete"] == []


def test_task_without_due_is_not_obsolete():
    out = s._dc_analyze([_mk("a", "Без срока")], NAMES, today=TODAY, now=NOW,
                        fuzzy=False)
    assert out["flag_obsolete"] == []


# ---- umbrella grouping ---------------------------------------------------

def test_umbrella_group_detected():
    tasks = [_mk("h", "Ремонт квартиры"),
             _mk("c1", "Ремонт квартиры покраска стен"),
             _mk("c2", "Ремонт квартиры замена окон")]
    out = s._dc_analyze(tasks, NAMES, today=TODAY, now=NOW, fuzzy=False)
    assert len(out["group"]) == 1
    g = out["group"][0]
    assert g["parentId"] == "h"
    assert {c["taskId"] for c in g["children"]} == {"c1", "c2"}


def test_no_group_across_projects():
    tasks = [_mk("h", "Проект альфа", project="p1"),
             _mk("c1", "Проект альфа этап один", project="p2"),
             _mk("c2", "Проект альфа этап два", project="p2")]
    out = s._dc_analyze(tasks, NAMES, today=TODAY, now=NOW, fuzzy=False)
    # header in p1 has no same-project children; p2 has no header prefix task
    assert out["group"] == []


def test_already_parented_task_not_regrouped():
    tasks = [_mk("h", "Ремонт квартиры"),
             _mk("c1", "Ремонт квартиры покраска", parent="something"),
             _mk("c2", "Ремонт квартиры окна", parent="something")]
    out = s._dc_analyze(tasks, NAMES, today=TODAY, now=NOW, fuzzy=False)
    assert out["group"] == []


# ---- SMART rewrites ------------------------------------------------------

def test_smart_rewrite_becomes_rename_action():
    tasks = [_mk("a", "Банк")]

    def smart(titles):
        return [{"i": 0, "new_title": "Позвонить в банк по кредиту",
                 "reason": "конкретизировал"}]

    out = s._dc_analyze(tasks, NAMES, smart_fn=smart, today=TODAY, now=NOW,
                        fuzzy=False)
    assert len(out["rename"]) == 1
    assert out["rename"][0]["new_title"] == "Позвонить в банк по кредиту"


def test_nonsmart_without_shim_is_flagged_not_renamed():
    out = s._dc_analyze([_mk("a", "Банк")], NAMES, smart_fn=None, today=TODAY,
                        now=NOW, fuzzy=False)
    assert out["rename"] == []
    assert len(out["flag_nonsmart"]) == 1


def test_smart_empty_rewrite_keeps_title():
    def smart(titles):
        return [{"i": 0, "new_title": "", "reason": "уже норм"}]

    out = s._dc_analyze([_mk("a", "Банк")], NAMES, smart_fn=smart, today=TODAY,
                        now=NOW, fuzzy=False)
    assert out["rename"] == [] and len(out["flag_nonsmart"]) == 1


# ---- an id claimed by one action is not reused by another ----------------

def test_deleted_duplicate_not_also_grouped_or_renamed():
    tasks = [_mk("a", "X"), _mk("b", "X")]  # exact dupes, both short titles

    def smart(titles):
        return [{"i": i, "new_title": f"Сделать {t}", "reason": "r"}
                for i, t in enumerate(titles)]

    out = s._dc_analyze(tasks, NAMES, smart_fn=smart, today=TODAY, now=NOW,
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
