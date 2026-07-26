"""Tests for the sheet-backed declutter manifest glue in server.py: the pure
round-trip functions (_dc_actions_to_rows / _dc_rows_to_exec), the §5
idempotency/resume/lock-recovery guarantees, the §6 approval-race confirm
check, the §9.3 explicit-refusal degrade path, and persist="ram" backward
compatibility. `declutter_sheet` is faked with an in-memory store (its own
HTTP layer is covered separately in test_declutter_sheet.py); TickTick is
faked with a plain LIVE dict and mocked sub-tools, per the design doc's own
guidance ("мок под-инструментов")."""
import time

import pytest

import ticktick_mcp.src.server as s
from ticktick_mcp.src.declutter_sheet import DeclutterSheetError

NAMES = {"p1": "Работа"}


def _delete_actions(task_id="d1", keep_id="k1", title="Dup", keep_title="Keep",
                    reason="идентичные названия", snapshot=None):
    return {
        "delete": [{"taskId": task_id, "projectId": "p1", "title": title,
                    "keep_title": keep_title, "keep_id": keep_id,
                    "project": "Работа", "reason": reason,
                    "snapshot": snapshot if snapshot is not None else {"title": title}}],
        "rename": [], "group": [],
        "flag_obsolete": [], "flag_dupe": [], "flag_nonsmart": [],
    }


class FakeSheet:
    """In-memory stand-in for declutter_sheet's persistence functions — same
    signatures, no network. Faithfully simulates "every read is fresh from
    the store", so it correctly exercises idempotency/resume across repeated
    calls to _execute_declutter_from_sheet, exactly like the real sheet
    would (as opposed to handing back live-mutable references)."""

    def __init__(self):
        self._rows = {}
        self._next_id = 1

    def ensure_header(self):
        pass

    def append_rows(self, rows):
        ids = []
        for r in rows:
            rid = self._next_id
            self._next_id += 1
            full = dict(r)
            full["row_id"] = rid
            self._rows[rid] = full
            ids.append(rid)
        return ids

    def read_manifest_rows(self, manifest_id):
        return [dict(r) for r in self._rows.values()
                if r.get("manifest_id") == manifest_id]

    def batch_update_rows(self, updates):
        for upd in updates:
            rid = int(upd["row_id"])
            row = self._rows.get(rid)
            if row is None:
                raise DeclutterSheetError(f"row_id={rid} not found (fake sheet)")
            for k, v in upd.items():
                if k != "row_id":
                    row[k] = v

    def sheet_url(self):
        return "https://example.test/fake-declutter-sheet"

    def sheet_configured(self):
        return True


# =====================================================================
# Pure round-trip: _dc_actions_to_rows -> _dc_rows_to_exec
# =====================================================================

def test_delete_round_trips_through_rows_and_back():
    actions = _delete_actions(snapshot={"title": "Dup", "projectId": "p1"})
    rows = s._dc_actions_to_rows(actions, "mid1", "2026-07-22T10:00:00-07:00", NAMES)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "delete"
    assert r["decision"] == "approved"     # mutating actions default approved (§3)
    assert r["status"] == "planned"
    assert r["cluster_id"] == "k1"         # keep_id, for grouping in the sheet
    assert r["manifest_id"] == "mid1"

    r["row_id"] = 1  # simulate what append_rows() would assign
    out = s._dc_rows_to_exec([r])
    assert out["refused"] == []
    assert len(out["delete"]) == 1
    d = out["delete"][0]
    assert d["taskId"] == "d1"
    assert d["title"] == "Dup"
    assert d["snapshot"] == {"title": "Dup", "projectId": "p1"}
    assert "projectId" in d  # placeholder key must exist (execute_task_deletion indexes it)


def test_rename_round_trips_through_rows_and_back():
    actions = {"delete": [], "group": [],
               "rename": [{"taskId": "r1", "projectId": "p1", "title": "Банк",
                          "new_title": "Позвонить в банк по кредиту",
                          "project": "Работа", "reason": "конкретизировал"}],
               "flag_obsolete": [], "flag_dupe": [], "flag_nonsmart": []}
    rows = s._dc_actions_to_rows(actions, "mid2", "ts", NAMES)
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "rename" and r["decision"] == "approved"
    assert r["proposed_value"] == "Позвонить в банк по кредиту"
    r["row_id"] = 5

    out = s._dc_rows_to_exec([r])
    assert len(out["rename"]) == 1
    assert out["rename"][0] == {"taskId": "r1", "projectId": "",
                                "title": "Банк",
                                "new_title": "Позвонить в банк по кредиту"}


def test_group_round_trips_through_rows_and_back():
    actions = {"delete": [], "rename": [],
               "group": [{"parentId": "par1", "parent_title": "Ремонт",
                         "project_id": "p1", "project": "Работа",
                         "children": [{"taskId": "c1", "title": "Стены", "projectId": "p1"},
                                     {"taskId": "c2", "title": "Окна", "projectId": "p1"}]}],
               "flag_obsolete": [], "flag_dupe": [], "flag_nonsmart": []}
    rows = s._dc_actions_to_rows(actions, "mid3", "ts", NAMES)
    assert len(rows) == 2
    for i, r in enumerate(rows, start=1):
        assert r["action"] == "group" and r["decision"] == "approved"
        assert r["cluster_id"] == "par1"
        r["row_id"] = i

    out = s._dc_rows_to_exec(rows)
    assert len(out["group"]) == 1
    g = out["group"][0]
    assert g["parentId"] == "par1"
    assert {c["taskId"] for c in g["children"]} == {"c1", "c2"}


def test_flags_default_pending_and_are_never_mutating_actions():
    actions = {"delete": [], "rename": [], "group": [],
               "flag_obsolete": [{"taskId": "o1", "title": "Old", "project": "Работа",
                                 "due": "2026-01-01", "overdue_days": 200, "age_days": 90}],
               "flag_dupe": [{"titles": ["A", "A similar"], "ids": ["x1", "x2"],
                             "reason": "похожи, но не уверен"}],
               "flag_nonsmart": [{"taskId": "n1", "title": "Банк", "project": "Работа"}]}
    rows = s._dc_actions_to_rows(actions, "mid4", "ts", NAMES)
    # 1 obsolete + 2 dupe-cluster members + 1 nonsmart = 4 rows
    assert len(rows) == 4
    assert all(r["decision"] == "pending" for r in rows)
    assert {r["action"] for r in rows} == {"flag_obsolete", "flag_dupe", "flag_nonsmart"}
    assert all(r["action"] not in s._DC_MUTATING_ROW_ACTIONS for r in rows)
    # the two flag_dupe rows share a cluster_id so a human sees them together
    dupe_rows = [r for r in rows if r["action"] == "flag_dupe"]
    assert dupe_rows[0]["cluster_id"] == dupe_rows[1]["cluster_id"]


# =====================================================================
# §5.9 keep∉deletes invariant
# =====================================================================

def test_rows_to_exec_refuses_mutually_conflicting_delete_pair():
    """Two delete rows that each name the OTHER as 'keep' — a manual
    decision edit gone wrong. Both must be refused, neither deleted."""
    rows = [
        {"row_id": 1, "task_id": "a", "title": "A", "action": "delete",
         "cluster_id": "b", "decision": "approved", "status": "planned",
         "snapshot_json": "{}", "project": ""},
        {"row_id": 2, "task_id": "b", "title": "B", "action": "delete",
         "cluster_id": "a", "decision": "approved", "status": "planned",
         "snapshot_json": "{}", "project": ""},
    ]
    out = s._dc_rows_to_exec(rows)
    assert out["delete"] == []
    assert {r["row_id"] for r in out["refused"]} == {1, 2}
    assert all(r["reason"] for r in out["refused"])


def test_rows_to_exec_applies_normal_delete_when_keep_not_conflicted():
    rows = [{"row_id": 1, "task_id": "x", "title": "X", "action": "delete",
             "cluster_id": "k", "decision": "approved", "status": "planned",
             "snapshot_json": '{"title":"X"}', "project": "P"}]
    out = s._dc_rows_to_exec(rows)
    assert out["refused"] == []
    assert len(out["delete"]) == 1
    assert out["delete"][0]["snapshot"] == {"title": "X"}


# =====================================================================
# plan_declutter(persist="sheet") — explicit refusal on degrade (§9.3)
# =====================================================================

async def test_plan_declutter_sheet_unreachable_refuses_explicitly(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    tasks_by_id = {"a": {"id": "a", "title": "Позвонить в банк", "projectId": "p1",
                        "createdTime": "2026-01-01T00:00:00.000+0000",
                        "modifiedTime": "2026-07-20T00:00:00.000+0000"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: tasks_by_id)
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(NAMES))
    monkeypatch.setattr(s, "_dc_shim_available", lambda: False)

    def boom_ensure_header():
        raise DeclutterSheetError("DECLUTTER_SHEET_ID не задан")
    monkeypatch.setattr(s.declutter_sheet, "ensure_header", boom_ensure_header)

    before = set(s._MANIFESTS.keys())
    result = await s.plan_declutter(persist="sheet")
    assert "🛑 Отказ" in result
    assert "DECLUTTER_SHEET_ID" in result
    # No RAM manifest is created either — a refused plan leaves NOTHING behind.
    assert set(s._MANIFESTS.keys()) <= before


# =====================================================================
# persist="ram" — backward compatibility
# =====================================================================

async def test_plan_declutter_ram_default_never_touches_sheet(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    tasks_by_id = {f"t{i}": {"id": f"t{i}", "title": f"Задача {i}", "projectId": "p1",
                             "createdTime": "2026-01-01T00:00:00.000+0000",
                             "modifiedTime": "2026-07-20T00:00:00.000+0000"}
                  for i in range(3)}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: tasks_by_id)
    monkeypatch.setattr(s, "_v2_project_names", lambda: dict(NAMES))
    monkeypatch.setattr(s, "_dc_shim_available", lambda: False)

    def boom(*a, **kw):
        raise AssertionError("persist='ram' (default) must never touch declutter_sheet")
    monkeypatch.setattr(s.declutter_sheet, "ensure_header", boom)
    monkeypatch.setattr(s.declutter_sheet, "append_rows", boom)

    result = await s.plan_declutter()  # persist defaults to "ram"
    assert "📄" not in result  # no sheet note
    mids = [m for m, v in s._MANIFESTS.items() if v.get("kind") == "declutter"]
    assert mids
    assert "persist" not in s._MANIFESTS[mids[-1]]


# =====================================================================
# Full sheet-backed execute engine: idempotency, resume, lock recovery,
# approval race — using FakeSheet + mocked sub-tools ("мок под-инструментов").
# =====================================================================

def _plant_delete_manifest(fs, mid="mid-x", task_id="d1", keep_id="k1"):
    rows = s._dc_actions_to_rows(
        _delete_actions(task_id=task_id, keep_id=keep_id), mid, "ts", NAMES)
    return fs.append_rows(rows)


@pytest.fixture
def sheet_env(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fs = FakeSheet()
    monkeypatch.setattr(s.declutter_sheet, "read_manifest_rows", fs.read_manifest_rows)
    monkeypatch.setattr(s.declutter_sheet, "batch_update_rows", fs.batch_update_rows)
    monkeypatch.setattr(s.declutter_sheet, "sheet_url", fs.sheet_url)
    monkeypatch.setattr(s.declutter_sheet, "sheet_configured", fs.sheet_configured)
    return fs


async def test_execute_from_sheet_deletes_and_writes_done(monkeypatch, sheet_env):
    fs = sheet_env
    live = {"d1": {"id": "d1", "title": "Dup", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))

    calls = {"n": 0}

    async def fake_execute_task_deletion(manifest_id, confirm):
        calls["n"] += 1
        m = s._MANIFESTS.get(manifest_id)
        for it in m["items"]:
            live.pop(it["taskId"], None)
        return "🗑 deleted (fake)"
    monkeypatch.setattr(s, "execute_task_deletion", fake_execute_task_deletion)

    mid = "mid-del"
    ids = _plant_delete_manifest(fs, mid)

    await s._execute_declutter_from_sheet(mid, "DECLUTTER 1")
    assert calls["n"] == 1
    assert "d1" not in live
    row = fs.read_manifest_rows(mid)[0]
    assert row["status"] == "done"
    assert row["applied_ts"]
    assert row["row_id"] == ids[0]

    # ---- idempotency: a second run must NOT re-delete ---------------------
    result2 = await s._execute_declutter_from_sheet(mid, "DECLUTTER 0")
    assert calls["n"] == 1, "done rows must never be re-applied"
    assert "Нечего применять" in result2
    row2 = fs.read_manifest_rows(mid)[0]
    assert row2["status"] == "done"


async def test_stuck_applying_lock_resolved_by_live_delete_fact_not_retried(monkeypatch, sheet_env):
    """A row left `applying` by a crashed prior run, where the delete had
    ALREADY landed in TickTick — resume must recognise this from live state
    and mark it `done` WITHOUT calling execute_task_deletion again."""
    fs = sheet_env
    live = {}  # d1 already gone
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))

    calls = {"n": 0}

    async def fake_execute_task_deletion(manifest_id, confirm):
        calls["n"] += 1
        return "should not be called"
    monkeypatch.setattr(s, "execute_task_deletion", fake_execute_task_deletion)

    mid = "mid-stuck"
    ids = _plant_delete_manifest(fs, mid)
    fs.batch_update_rows([{"row_id": ids[0], "status": "applying"}])

    await s._execute_declutter_from_sheet(mid, "DECLUTTER 0")
    assert calls["n"] == 0
    row = fs.read_manifest_rows(mid)[0]
    assert row["status"] == "done"


async def test_stuck_applying_lock_not_yet_applied_is_retried(monkeypatch, sheet_env):
    """Same crashed-`applying` scenario, but live state shows the task is
    STILL open (the crash happened before the delete actually landed) — must
    be retried, not left stuck."""
    fs = sheet_env
    live = {"d1": {"id": "d1", "title": "Dup", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))

    calls = {"n": 0}

    async def fake_execute_task_deletion(manifest_id, confirm):
        calls["n"] += 1
        m = s._MANIFESTS.get(manifest_id)
        for it in m["items"]:
            live.pop(it["taskId"], None)
        return "🗑 deleted (fake, retried)"
    monkeypatch.setattr(s, "execute_task_deletion", fake_execute_task_deletion)

    mid = "mid-retry"
    ids = _plant_delete_manifest(fs, mid)
    fs.batch_update_rows([{"row_id": ids[0], "status": "applying"}])

    await s._execute_declutter_from_sheet(mid, "DECLUTTER 1")
    assert calls["n"] == 1
    assert "d1" not in live
    row = fs.read_manifest_rows(mid)[0]
    assert row["status"] == "done"


async def test_confirm_mismatch_after_decision_changed_since_plan(monkeypatch, sheet_env):
    """Human rejects the row in the sheet AFTER the plan printed N=1 —
    execute must refuse the stale confirm token, touching nothing."""
    fs = sheet_env
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: {})

    async def must_not_run(*a, **kw):
        raise AssertionError("must not apply anything on a confirm mismatch")
    monkeypatch.setattr(s, "execute_task_deletion", must_not_run)

    mid = "mid-race"
    ids = _plant_delete_manifest(fs, mid)
    fs.batch_update_rows([{"row_id": ids[0], "decision": "rejected"}])

    result = await s._execute_declutter_from_sheet(mid, "DECLUTTER 1")
    assert "🛑" in result and "не совпало" in result
    assert "DECLUTTER 0" in result
    row = fs.read_manifest_rows(mid)[0]
    assert row["status"] == "planned"  # untouched


async def test_keep_id_conflict_refused_end_to_end_others_still_apply(monkeypatch, sheet_env):
    """A 2-row batch: row A's keep_id conflicts (refused), row B is a normal
    unrelated delete that must still go through."""
    fs = sheet_env
    live = {"a": {"id": "a", "title": "A"}, "keep-a": {"id": "keep-a", "title": "KeepA"},
            "b": {"id": "b", "title": "B"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))

    async def fake_execute_task_deletion(manifest_id, confirm):
        m = s._MANIFESTS.get(manifest_id)
        for it in m["items"]:
            live.pop(it["taskId"], None)
        return "🗑 deleted (fake)"
    monkeypatch.setattr(s, "execute_task_deletion", fake_execute_task_deletion)

    mid = "mid-conflict"
    rows = []
    rows += s._dc_actions_to_rows(_delete_actions(task_id="a", keep_id="keep-a"),
                                  mid, "ts", NAMES)
    rows += s._dc_actions_to_rows(_delete_actions(task_id="keep-a", keep_id="other"),
                                  mid, "ts", NAMES)
    rows += s._dc_actions_to_rows(_delete_actions(task_id="b", keep_id="keep-b"),
                                  mid, "ts", NAMES)
    fs.append_rows(rows)

    # 3 approved+pending rows at plan time.
    await s._execute_declutter_from_sheet(mid, "DECLUTTER 3")
    assert "a" in live  # refused — its keep target was also slated for delete
    assert "b" not in live  # unrelated row still applied
    by_task = {r["task_id"]: r for r in fs.read_manifest_rows(mid)}
    assert by_task["a"]["status"] == "failed"
    assert by_task["b"]["status"] == "done"


async def test_resume_declutter_requires_sheet_configured(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s.declutter_sheet, "sheet_configured", lambda: False)
    result = await s.resume_declutter("some-mid", "DECLUTTER 0")
    assert "не настроен" in result


async def test_resume_declutter_unknown_manifest_is_friendly(monkeypatch, sheet_env):
    result = await s.resume_declutter("no-such-manifest", "DECLUTTER 0")
    assert "🛑" in result and "не найден" in result


async def test_set_declutter_decision_updates_and_reports_unknown(monkeypatch, sheet_env):
    fs = sheet_env
    mid = "mid-dec"
    ids = _plant_delete_manifest(fs, mid)

    ok = await s.set_declutter_decision(mid, [ids[0]], "rejected")
    assert "✅" in ok
    assert fs.read_manifest_rows(mid)[0]["decision"] == "rejected"

    unknown = await s.set_declutter_decision(mid, [999999], "approved")
    assert "🛑" in unknown

    bad_value = await s.set_declutter_decision(mid, [ids[0]], "maybe")
    assert "🛑" in bad_value


async def test_execute_declutter_dispatches_ram_and_sheet_correctly(monkeypatch, sheet_env):
    """execute_declutter (the public tool) must route a persist='sheet'
    manifest into the sheet engine and leave a persist='ram' (or legacy,
    key-absent) manifest on the untouched original branch."""
    fs = sheet_env
    live = {"d1": {"id": "d1", "title": "Dup", "projectId": "p1"}}
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))

    async def fake_execute_task_deletion(manifest_id, confirm):
        m = s._MANIFESTS.get(manifest_id)
        for it in m["items"]:
            live.pop(it["taskId"], None)
        return "🗑 deleted (fake)"
    monkeypatch.setattr(s, "execute_task_deletion", fake_execute_task_deletion)

    mid = "mid-dispatch"
    _plant_delete_manifest(fs, mid)
    s._MANIFESTS[mid] = {"kind": "declutter", "persist": "sheet",
                         "mutating_count": 1, "created": time.monotonic(),
                         "summary": "x", "consumed": False,
                         "actions": {"delete": [], "rename": [], "group": []}}

    await s.execute_declutter(mid, "DECLUTTER 1")
    assert "d1" not in live
    assert fs.read_manifest_rows(mid)[0]["status"] == "done"
