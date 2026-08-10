"""Автоуборка («разбор помойки») — ВЫНЕСЕНА ЗА ПРЕДЕЛЫ ПАКЕТА (2026-08-09).

Четыре команды автоуборки (plan_declutter, execute_declutter,
resume_declutter, set_declutter_decision) отключены с 4 августа
комментированием декораторов. Отключение комментарием — дыра: вернуть их в
строй можно было за минуту, и рядом в коде прямо написано, как. Пункт 1.2.4
захода 1 закрывает дыру ФИЗИЧЕСКИ: код уехал в каталог `attic/`, где нет
`__init__.py`, под именем, которое не является модулем пакета
`ticktick_mcp.src`. Раскомментировать больше нечего — чтобы вернуть
автоуборку, её надо осознанно перенести обратно.

Файл НИКЕМ в пакете не импортируется. Его грузят только тесты автоуборки,
через `tests/attic_loader.py` (importlib по пути), — иначе 73 живых теста
пришлось бы выбросить вместе с кодом.

Ниже — ДОСЛОВНЫЙ перенос: строки 7709–9200 файла `ticktick_mcp/src/server.py`
на ревизии 0b3c04c62c93, ни одного изменённого символа. Совпадение
доказывается контрольной суммой (`tools/verify_move.sh 1`).
"""
import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ticktick_mcp.src import declutter_sheet
from ticktick_mcp.src.server import (  # noqa: F401  — имена, которые кусок
    # берёт снаружи (43 имени / 223 обращения по замеру связанности)
    _MANIFESTS, _STATE_UNAVAILABLE_MSG, _UNVERIFIED_MSG, _USER_TZ,
    _build_operation_report, _ensure_ready, _execute_task_deletion_impl,
    _local_date_str, _manifest_object_hash, _mark_manifest_consumed,
    _maybe_tg_notify_plan, _names_agree, _norm_name, _open_by_id,
    _parse_ticktick_datetime, _plan_id_line, _prune_manifests,
    _redact_for_user, _rehydrate_manifest, _require_consent, _run_blocking,
    _set_task_parent_impl, _snapshot_of, _task_due_local_date, _today_local,
    _tool_error, _update_tasks_impl, _v2_project_names, logger, ticktick_v2)


# === MOVED-BLOCK BEGIN (server.py @ 0b3c04c62c93171106a294f7090f366b05ea43e7, lines 7709–9200) ===
# ---------------------------------------------------------------------------
# Retroactive declutter ("разбор помойки") — plan → confirm → execute
# ---------------------------------------------------------------------------
# The ingest pipeline (tg-ai) dedupes NEW tasks at capture time. This works
# RETROACTIVELY over the EXISTING pile of open tasks: it clusters near-duplicate
# titles, flags long-overdue/stale candidates, spots umbrella groups, and
# proposes SMART re-titles. It CREATES and CHANGES nothing — plan_declutter
# builds a manifest, execute_declutter (after an explicit confirm token) routes
# every mutation through the already-audited deletion / update / parent tools,
# so each one is identity-guarded, journalled and post-verified. The same
# safety philosophy as the ingest judge governs the analysis: uncertain merges
# default to KEEP-BOTH, and obsolete tasks are only FLAGGED — never auto-deleted
# or auto-completed.

# Similarity threshold for fuzzy (token-Jaccard) duplicate candidates — only
# reached when the CLAUDE_CLI judge is available to confirm each candidate.
_DC_FUZZY_THRESHOLD = 0.6
# Obsolete = overdue by at least this many days AND untouched at least this long.
_DC_OBSOLETE_OVERDUE_DAYS = 30
_DC_OBSOLETE_STALE_DAYS = 60

# Hard cap on how many open tasks a single plan_declutter scope may resolve
# to. _dc_cluster_duplicates and _dc_group_candidates are both O(n^2) over
# this set, and (when the shim is available) the analysis also bundles ALL
# fuzzy-duplicate clusters into one judge prompt and ALL vague-title
# candidates into one SMART-rewrite prompt — bigger input means bigger
# prompts on top of the quadratic clustering cost. The MCP client this tool
# is called through has a 60s timeout; a real user's full open-task list has
# been observed to blow past it. 200 is picked as a middle ground: at that
# size the O(n^2) passes stay well under a second in pure Python, and the two
# (now-parallel, individually timeout-capped — see _DC_SHIM_TIMEOUT) shim
# prompts stay small enough to answer quickly, leaving real headroom under
# the 60s ceiling. A single project or a day/week's worth of inbox triage
# should resolve well under this; the whole-pile case (what the timeout was
# actually hit on) is exactly what should be narrowed via `scope` instead.
# Bumped 200 -> 300 (2026-08-05 cap-sizing pass): a genuinely active user's
# single project/column can plausibly hold 200-300+ untriaged tasks before
# their first declutter — 200 was clipping that common case into an
# unnecessary refusal+retry; 300 stays well inside the sub-second O(n^2) and
# ~25s shim budget (see rationale above) while leaving real headroom under
# _DC_MAX_TASKS_HARD_CAP=500 for the explicit-override path.
_DC_MAX_TASKS = 300
# 17.6 (security-checklist.md §4): plan_declutter's `max_tasks` argument can
# raise the per-call cap above _DC_MAX_TASKS, but a cap that the caller can
# override without any ceiling isn't a cap — the model can (and, asked to
# "just declutter everything", will) pass an arbitrarily large value and walk
# straight into the O(n^2) clustering pass over the whole account. This is a
# hard, server-side maximum that `max_tasks` can NEVER exceed, independent of
# what the argument says.
_DC_MAX_TASKS_HARD_CAP = 500
# Per-call timeout for the two declutter shim calls (judge_fn/smart_fn only —
# NOT _dc_shim_json's default, which other/future callers may still rely on
# at 90s). The two calls now run concurrently, so wall-clock cost is
# ~max(judge, smart) rather than their sum, but each individual call still
# needs its own sane ceiling well under the 60s client timeout.
_DC_SHIM_TIMEOUT = 25


def _dc_tokens(title: str) -> set:
    """Normalised word-token set of a title (for Jaccard similarity)."""
    return set(re.findall(r"\w+", _norm_name(title), flags=re.UNICODE))


def _dc_jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dc_shim_available() -> bool:
    return bool(os.environ.get("CLAUDE_CLI_URL") and os.environ.get("CLAUDE_CLI_TOKEN"))


def _dc_shim_json(system: str, prompt: str, timeout: int = 90,
                  fail_tracker: Optional[list] = None):
    """Call the CLAUDE_CLI shim and return the parsed JSON array/object, or None
    on ANY failure (unset, unreachable, malformed). Same shim the bot and
    _suggest_destinations use.

    fail_tracker: optional mutable list — if the shim WAS configured (url set)
    but the call itself failed (network error, non-ok response, unparsable
    reply), True is appended so callers can distinguish "not configured" from
    "configured but degraded during this run"."""
    url = os.environ.get("CLAUDE_CLI_URL")
    token = os.environ.get("CLAUDE_CLI_TOKEN")
    if not url:
        return None
    import requests as _rq
    try:
        r = _rq.post(url, json={
            "system": system,
            "prompt": prompt,
            "model": os.environ.get("CLAUDE_CLI_MODEL", "sonnet"),
        }, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            if fail_tracker is not None:
                fail_tracker.append(True)
            return None
        text = data.get("result") or ""
        a = min([i for i in (text.find("["), text.find("{")) if i != -1] or [-1])
        b = max(text.rfind("]"), text.rfind("}"))
        if a == -1 or b == -1:
            if fail_tracker is not None:
                fail_tracker.append(True)
            return None
        return json.loads(text[a:b + 1])
    except Exception as e:
        logger.warning(f"declutter shim call failed: {e}")
        if fail_tracker is not None:
            fail_tracker.append(True)
        return None


def _dc_cluster_duplicates(tasks: List[Dict], fuzzy: bool) -> List[Dict]:
    """Cluster open tasks by title. Returns list of {"tasks": [...], "exact":
    bool}. EXACT clusters share an identical normalised title (safe to act on
    even without the LLM). When fuzzy=True, remaining tasks are additionally
    grouped by token-Jaccard ≥ threshold into non-exact clusters (candidates
    that REQUIRE the judge to confirm). Only clusters of size ≥ 2 are returned."""
    # Exact normalised-title buckets first.
    exact: Dict[str, List[Dict]] = {}
    for t in tasks:
        key = _norm_name(t.get("title") or "")
        if key:
            exact.setdefault(key, []).append(t)
    clusters: List[Dict] = []
    claimed = set()
    for key, group in exact.items():
        if len(group) >= 2:
            clusters.append({"tasks": group, "exact": True})
            for t in group:
                claimed.add(t.get("id"))
    if not fuzzy:
        return clusters
    # Fuzzy pass over the not-yet-claimed tasks: greedy single-link by Jaccard.
    rest = [t for t in tasks if t.get("id") not in claimed and (t.get("title") or "").strip()]
    toks = {t.get("id"): _dc_tokens(t.get("title") or "") for t in rest}
    used = set()
    for i, a in enumerate(rest):
        if a.get("id") in used:
            continue
        group = [a]
        used.add(a.get("id"))
        for b in rest[i + 1:]:
            if b.get("id") in used:
                continue
            if _dc_jaccard(toks[a.get("id")], toks[b.get("id")]) >= _DC_FUZZY_THRESHOLD:
                group.append(b)
                used.add(b.get("id"))
        if len(group) >= 2:
            clusters.append({"tasks": group, "exact": False})
    return clusters


def _dc_metadata_score(task: Dict) -> tuple:
    """Richness score for choosing which duplicate to KEEP — the survivor should
    carry the most information. Higher tuple sorts first."""
    return (
        1 if task.get("dueDate") else 0,
        task.get("priority") or 0,
        len(task.get("content") or task.get("desc") or ""),
        len(task.get("tags") or []),
        # Older task (earlier createdTime) breaks ties — keep the original.
        -(_dc_created_sort_key(task)),
    )


def _dc_created_sort_key(task: Dict) -> float:
    dt = _parse_ticktick_datetime(task.get("createdTime"))
    return dt.timestamp() if dt else 0.0


def _dc_pick_primary(cluster_tasks: List[Dict]) -> int:
    """Index of the task to KEEP within a duplicate cluster (richest metadata)."""
    best_i, best_score = 0, None
    for i, t in enumerate(cluster_tasks):
        score = _dc_metadata_score(t)
        if best_score is None or score > best_score:
            best_i, best_score = i, score
    return best_i


def _dc_task_age_days(task: Dict, now: datetime) -> Optional[int]:
    """Days since the task was last modified (or created). None if unknown."""
    dt = _parse_ticktick_datetime(task.get("modifiedTime") or task.get("createdTime"))
    if dt is None:
        return None
    return (now - dt).days


def _dc_is_obsolete(task: Dict, today, now: datetime) -> Optional[Dict]:
    """A long-overdue AND untouched task → {overdue_days, age_days}. None when
    it is not a stale-obsolete candidate. FLAG-only: never acted on."""
    due = _task_due_local_date(task)
    if due is None:
        return None
    overdue_days = (today - due).days
    if overdue_days < _DC_OBSOLETE_OVERDUE_DAYS:
        return None
    age = _dc_task_age_days(task, now)
    if age is not None and age < _DC_OBSOLETE_STALE_DAYS:
        return None
    return {"overdue_days": overdue_days, "age_days": age}


def _dc_is_nonsmart_candidate(title: str) -> bool:
    """Cheap prefilter for non-SMART titles: a very short / vague title (≤ 2
    meaningful words). The LLM does the actual judging + reformulation; without
    it these are surfaced as flags only."""
    toks = _dc_tokens(title)
    return 1 <= len(toks) <= 2


def _dc_word_prefix(short_title: str, long_title: str) -> bool:
    """True if short_title's tokens are a PROPER leading sub-sequence of
    long_title's tokens — i.e. short is a natural umbrella/header for long."""
    st = re.findall(r"\w+", _norm_name(short_title), flags=re.UNICODE)
    lt = re.findall(r"\w+", _norm_name(long_title), flags=re.UNICODE)
    return bool(st) and len(st) < len(lt) and lt[:len(st)] == st


def _dc_group_candidates(tasks: List[Dict], skip_ids: set) -> List[Dict]:
    """Umbrella groups: a task whose title is a word-prefix of ≥ 2 other open
    tasks IN THE SAME PROJECT becomes the parent, those become children. Only
    genuine headers qualify (no synthetic parents) — safe, reversible nesting.
    Tasks already parented, or in skip_ids (e.g. slated for deletion), are
    excluded."""
    pool = [t for t in tasks
            if t.get("id") not in skip_ids and not t.get("parentId")
            and (t.get("title") or "").strip()]
    by_project: Dict[str, List[Dict]] = {}
    for t in pool:
        by_project.setdefault(t.get("projectId") or "", []).append(t)
    groups: List[Dict] = []
    used = set()
    for pid, plist in by_project.items():
        # Longer titles first so a shorter header is tested against them.
        for parent in sorted(plist, key=lambda t: len(_dc_tokens(t.get("title") or ""))):
            if parent.get("id") in used:
                continue
            children = [c for c in plist
                        if c.get("id") not in used and c.get("id") != parent.get("id")
                        and _dc_word_prefix(parent.get("title") or "", c.get("title") or "")]
            if len(children) >= 2:
                used.add(parent.get("id"))
                for c in children:
                    used.add(c.get("id"))
                groups.append({"parent": parent, "children": children})
    return groups


async def _dc_analyze(tasks: List[Dict], names: Dict, judge_fn=None, smart_fn=None,
                      today=None, now: Optional[datetime] = None,
                      fuzzy: bool = True) -> Dict:
    """Analysis core (the only I/O is the two injected shim calls, dispatched
    concurrently): from a list of live open tasks build the proposed
    declutter actions + flags. judge_fn/smart_fn are injected so the logic is
    unit-testable with mocks (plain sync callables are fine — they are run
    via asyncio.to_thread); the real tool wires them to the shim.

    judge_fn(clusters) -> aligned list of {"is_duplicate": bool, "keep": int,
        "reason": str} (or [] / None to abstain → KEEP-BOTH).
    smart_fn(titles) -> aligned list of {"new_title": str, "reason": str}
        (empty new_title → leave as-is).

    judge_fn and smart_fn look at independent slices of the task pile
    (fuzzy-duplicate clusters vs. vague-title candidates) and are run
    CONCURRENTLY via asyncio.gather/to_thread rather than sequentially, since
    each is one (potentially slow) shim HTTP call and running them back to
    back risked summing past the MCP client's own timeout. The smart_fn
    candidate pool is gathered up front (before duplicate-resolution below
    decides what gets consumed) precisely so its dispatch doesn't have to
    wait on the judge's verdict; any candidate that duplicate/group
    resolution later claims is simply dropped from the final rename/flag
    output — see the id-keyed lookup in step 4.

    Returns dict with mutating action lists (delete/rename/group) and flag lists
    (flag_obsolete/flag_dupe/flag_nonsmart)."""
    today = today or _today_local()
    now = now or datetime.now(timezone.utc)
    out = {"delete": [], "rename": [], "group": [],
           "flag_obsolete": [], "flag_dupe": [], "flag_nonsmart": []}
    consumed = set()  # ids already claimed by a delete/rename/group action

    # Ids that are the parentId of at least one task in THIS SAME open-task
    # pass — i.e. tasks with live children. A task with live children must
    # NEVER be the "redundant" (delete) side of a duplicate pair: deleting it
    # would orphan its children (parentId pointing at a deleted task).
    # plan_task_deletion already guards this with BFS subtree expansion;
    # declutter's own duplicate-scoring must not bypass that protection.
    children_of = {t.get("parentId") for t in tasks if t.get("parentId")}

    # ---- Prepare BOTH shim inputs up front, then dispatch concurrently ----
    clusters = _dc_cluster_duplicates(tasks, fuzzy=fuzzy)
    fuzzy_clusters = [c for c in clusters if not c["exact"]]
    # Full non-SMART candidate pool, NOT yet excluding tasks that duplicate/
    # group resolution below will consume (that isn't known until after the
    # judge verdict is in) — see the docstring above for why.
    full_nonsmart_cand = [t for t in tasks
                          if _dc_is_nonsmart_candidate(t.get("title") or "")]

    async def _run_judge() -> Dict[int, Dict]:
        if not (fuzzy_clusters and judge_fn):
            return {}
        try:
            res = await asyncio.to_thread(
                judge_fn, [[t.get("title") or "" for t in c["tasks"]]
                          for c in fuzzy_clusters]) or []
            return {i: v for i, v in enumerate(res) if isinstance(v, dict)}
        except Exception as e:
            logger.warning(f"declutter judge failed: {e}")
            return {}

    async def _run_smart() -> Dict[Any, Dict]:
        if not (full_nonsmart_cand and smart_fn):
            return {}
        try:
            res = await asyncio.to_thread(
                smart_fn, [t.get("title") or "" for t in full_nonsmart_cand]) or []
            by_id = {}
            for i, v in enumerate(res):
                if isinstance(v, dict) and i < len(full_nonsmart_cand):
                    by_id[full_nonsmart_cand[i].get("id")] = v
            return by_id
        except Exception as e:
            logger.warning(f"declutter smart rewrite failed: {e}")
            return {}

    verdicts, smart_by_id = await asyncio.gather(_run_judge(), _run_smart())

    # ---- 1. Duplicate clusters -------------------------------------------
    fuzzy_idx = 0
    for c in clusters:
        ctasks = c["tasks"]
        if c["exact"]:
            confident, keep_i, reason = True, _dc_pick_primary(ctasks), "идентичные названия"
        else:
            v = verdicts.get(fuzzy_idx)
            fuzzy_idx += 1
            # Bias to KEEP-BOTH: only merge when the judge is explicitly sure.
            if v and v.get("is_duplicate") is True:
                keep_i = v.get("keep")
                keep_i = keep_i if isinstance(keep_i, int) and 0 <= keep_i < len(ctasks) \
                    else _dc_pick_primary(ctasks)
                confident, reason = True, (v.get("reason") or "судья подтвердил дубликат")
            else:
                confident, keep_i, reason = False, None, (
                    (v or {}).get("reason") or "похожи, но слияние не подтверждено")

        # Finding #2: the fuzzy pass is anchor/"star" clustering — every member
        # is only checked against the cluster's anchor, never against each
        # other, so a 3+-member cluster can silently mix a genuine duplicate
        # pair with a task that only shares anchor-similarity. The judge
        # returns a single is_duplicate/keep verdict for the WHOLE cluster, so
        # an all-or-nothing delete on 3+ members risks wiping a distinct task.
        # Cap auto-delete at exactly 2 members; any bigger FUZZY cluster is
        # routed to flag_dupe as a suggestion instead, regardless of verdict.
        if confident and not c["exact"] and len(ctasks) >= 3:
            confident, keep_i = False, None
            reason = ("группа из 3+ похожих задач — попарное сходство между "
                      "всеми не гарантировано, слияние не автоматическое, "
                      "проверь сам")

        # Finding #1: a task with live children must never be the deleted
        # side. If exactly one cluster member has children, force it onto the
        # KEEP side regardless of metadata scoring / judge choice. If 2+
        # members have children, there is no safe single keeper — flag it.
        if confident:
            child_ids = [t.get("id") for t in ctasks if t.get("id") in children_of]
            if len(child_ids) >= 2:
                confident, keep_i = False, None
                reason = ("у нескольких похожих задач есть живые подзадачи — "
                          "не могу безопасно выбрать, кого оставить, реши сам")
            elif len(child_ids) == 1:
                forced_i = next(j for j, t in enumerate(ctasks)
                                if t.get("id") == child_ids[0])
                if forced_i != keep_i:
                    keep_i = forced_i
                    reason = reason + " (оставил — у задачи есть подзадачи)"

        if confident:
            keeper = ctasks[keep_i]
            consumed.add(keeper.get("id"))
            redundant = [t for j, t in enumerate(ctasks) if j != keep_i]
            for r in redundant:
                consumed.add(r.get("id"))
                out["delete"].append({
                    "taskId": r.get("id"), "projectId": r.get("projectId"),
                    "title": r.get("title") or "", "snapshot": _snapshot_of(r),
                    "keep_title": keeper.get("title") or "",
                    "keep_id": keeper.get("id"),
                    "project": names.get(r.get("projectId"), ""),
                    "reason": reason,
                })
        else:
            out["flag_dupe"].append({
                "titles": [t.get("title") or "" for t in ctasks],
                "ids": [t.get("id") for t in ctasks],
                "reason": reason,
            })

    # ---- 2. Obsolete (FLAG ONLY — never mutated) -------------------------
    for t in tasks:
        info = _dc_is_obsolete(t, today, now)
        if info:
            out["flag_obsolete"].append({
                "taskId": t.get("id"), "title": t.get("title") or "",
                "project": names.get(t.get("projectId"), ""),
                # Печатает это plan_declutter: «срок X (просрочено N дней)».
                # Счётчик N уже считался в зоне владельца
                # (_dc_is_obsolete → _task_due_local_date), а срок рядом
                # брался сырым срезом — строка противоречила сама себе на
                # день, и по ней человек решает, добивать задачу или нет.
                "due": _local_date_str(t, "dueDate") if t.get("dueDate") else "",
                "overdue_days": info["overdue_days"], "age_days": info["age_days"],
            })

    # ---- 3. Umbrella groups ---------------------------------------------
    for g in _dc_group_candidates(tasks, skip_ids=consumed):
        parent = g["parent"]
        kids = [c for c in g["children"] if c.get("id") not in consumed]
        if len(kids) < 2:
            continue
        consumed.add(parent.get("id"))
        for c in kids:
            consumed.add(c.get("id"))
        out["group"].append({
            "parentId": parent.get("id"), "parent_title": parent.get("title") or "",
            "project_id": parent.get("projectId"),
            "project": names.get(parent.get("projectId"), ""),
            "children": [{"taskId": c.get("id"), "title": c.get("title") or "",
                          "projectId": c.get("projectId")} for c in kids],
        })

    # ---- 4. Non-SMART titles --------------------------------------------
    # smart_fn was already called (concurrently with judge_fn) above, against
    # full_nonsmart_cand — the pre-consumption superset of this list. Re-apply
    # the same "not consumed" filter now that consumed is final, and look each
    # survivor's rewrite up by task id in smart_by_id (built from that earlier
    # call) instead of re-invoking the shim.
    cand = [t for t in tasks if t.get("id") not in consumed
            and _dc_is_nonsmart_candidate(t.get("title") or "")]
    for t in cand:
        v = smart_by_id.get(t.get("id"))
        new_title = (v or {}).get("new_title") if v else None
        if new_title and _norm_name(new_title) and _norm_name(new_title) != _norm_name(t.get("title") or ""):
            consumed.add(t.get("id"))
            out["rename"].append({
                "taskId": t.get("id"), "projectId": t.get("projectId"),
                "title": t.get("title") or "", "new_title": new_title.strip(),
                "project": names.get(t.get("projectId"), ""),
                "reason": (v or {}).get("reason") or "",
            })
        else:
            out["flag_nonsmart"].append({
                "taskId": t.get("id"), "title": t.get("title") or "",
                "project": names.get(t.get("projectId"), ""),
            })
    return out


def _dc_judge_fn(clusters: List[List[str]],
                 fail_tracker: Optional[list] = None) -> List[Dict]:
    """Wire the fuzzy-cluster judge to the shim. Bias: KEEP-BOTH unless sure."""
    if not clusters:
        return []
    blocks = []
    for i, titles in enumerate(clusters):
        lst = "\n".join(f"   {j}. «{t}»" for j, t in enumerate(titles))
        blocks.append(f"Кластер {i}:\n{lst}")
    prompt = (
        "Ниже кластеры ПОХОЖИХ задач владельца. Для КАЖДОГО реши: это один и тот "
        "же пункт (дубликаты, можно слить), или разные дела? Правило безопасности: "
        "если сомневаешься — is_duplicate=false (лучше оставить обе, чем потерять "
        "задачу). Когда дубликаты — укажи keep = индекс той версии, которую оставить "
        "(с датой/приоритетом/деталями).\n\n"
        + "\n\n".join(blocks) +
        '\n\nОтвет СТРОГО JSON-массивом по одному объекту на кластер:\n'
        '[{"i":0,"is_duplicate":true,"keep":1,"reason":"<кратко>"}]'
    )
    res = _dc_shim_json("Ты вычищаешь дубликаты в списке задач. Отвечай только JSON.",
                        prompt, timeout=_DC_SHIM_TIMEOUT, fail_tracker=fail_tracker)
    if not isinstance(res, list):
        return []
    aligned = [{} for _ in clusters]
    for it in res:
        if isinstance(it, dict):
            idx = it.get("i")
            if isinstance(idx, int) and 0 <= idx < len(clusters):
                aligned[idx] = it
    return aligned


def _dc_smart_fn(titles: List[str], fail_tracker: Optional[list] = None) -> List[Dict]:
    """Wire the SMART-rewrite proposer to the shim."""
    if not titles:
        return []
    numbered = "\n".join(f"{i}. «{t}»" for i, t in enumerate(titles))
    prompt = (
        "Ниже короткие/расплывчатые названия задач. Для каждого предложи более "
        "SMART-формулировку: конкретное действие + объект (что именно сделать и с "
        "чем), тем же языком. Время/срок ДОБАВЛЯТЬ НЕ обязательно. Если название "
        "уже нормальное — верни пустую строку в new_title.\n\n"
        f"{numbered}\n\n"
        'Ответ СТРОГО JSON-массивом:\n'
        '[{"i":0,"new_title":"<или пусто>","reason":"<кратко>"}]'
    )
    res = _dc_shim_json("Ты переформулируешь задачи в SMART-вид. Отвечай только JSON.",
                        prompt, timeout=_DC_SHIM_TIMEOUT, fail_tracker=fail_tracker)
    if not isinstance(res, list):
        return []
    aligned = [{} for _ in titles]
    for it in res:
        if isinstance(it, dict):
            idx = it.get("i")
            if isinstance(idx, int) and 0 <= idx < len(titles):
                aligned[idx] = it
    return aligned


def _dc_scope_filter(tasks: List[Dict], names: Dict, scope: str,
                     col_names: Optional[Dict] = None) -> List[Dict]:
    """Optional narrowing. Forms of `scope`:
      'inbox'          → Inbox only
      'Project'        → case-insensitive substring match on the project name
      'Project/Column' → project-name substring AND kanban-column-name substring
                         (col_names: {columnId: column_name} for the matched
                         project(s), built by the caller via
                         _dc_column_names_for_scope). Narrowing a big project to
                         one column is the intended way to stay under
                         _DC_MAX_TASKS.
    An empty column part after '/' keeps every column (same as project-only)."""
    s = (scope or "").strip()
    if not s:
        return tasks
    # Column-qualified scope: "<project>/<column>". Split on the FIRST '/'.
    if "/" in s:
        proj_part, col_part = s.split("/", 1)
        proj = proj_part.strip().lower()
        col = col_part.strip().lower()
        cn = col_names or {}
        out = []
        for t in tasks:
            pname = (names.get(t.get("projectId"), "") or "").lower()
            if proj and proj not in pname:
                continue
            if col:
                cname = (cn.get(t.get("columnId"), "") or "").lower()
                if col not in cname:
                    continue
            out.append(t)
        return out
    s = s.lower()
    if s == "inbox":
        return [t for t in tasks if names.get(t.get("projectId"), "").lower() == "inbox"]
    return [t for t in tasks
            if s in (names.get(t.get("projectId"), "") or "").lower()]


async def _dc_column_names_for_scope(scope: str, names: Dict) -> Dict:
    """For a 'Project/Column' scope, build {columnId: column_name} across the
    projects whose name matches the project part, so _dc_scope_filter can match
    the requested column by name. Returns {} for non-column scopes, when the v2
    client is unavailable, or on any per-project failure (the column part then
    simply matches nothing, and plan_declutter reports 'nothing in this area')."""
    s = (scope or "").strip()
    if "/" not in s or not ticktick_v2:
        return {}
    proj_part = s.split("/", 1)[0].strip().lower()
    if not proj_part:
        return {}
    pids = [pid for pid, nm in names.items() if proj_part in (nm or "").lower()]
    out: Dict = {}
    for pid in pids:
        try:
            cols = await _run_blocking(lambda p=pid: ticktick_v2.get_project_columns(p))
            for c in cols or []:
                cid = c.get("id")
                if cid:
                    out[cid] = c.get("name") or c.get("title") or ""
        except Exception as e:
            logger.warning(f"declutter column scope: columns for {pid} failed: {e}")
    return out


def _dc_mutating_count(actions: Dict) -> int:
    return (len(actions["delete"]) + len(actions["rename"])
            + sum(len(g["children"]) for g in actions["group"]))


def _dc_object_ids(actions: Dict) -> List[str]:
    """Every task id a declutter manifest would actually TOUCH — the binding
    set for _manifest_object_hash (docs/DESIGN_approval_gate.md §4.3.2). The
    delete manifests had this from day one; without it a declutter manifest
    whose stored actions changed between plan and consent would be applied
    silently. Flags are excluded: they mutate nothing."""
    ids = [it["taskId"] for it in actions.get("delete", [])]
    ids += [it["taskId"] for it in actions.get("rename", [])]
    for g in actions.get("group", []):
        ids += [c["taskId"] for c in g.get("children", [])]
    return ids


# ---------------------------------------------------------------------------
# Sheet-backed declutter manifest (persist="sheet") — see
# docs/DESIGN_sheet_backed_declutter.md. All Google I/O lives in
# declutter_sheet.py; everything here is pure/deterministic glue so it stays
# unit-testable without network (§8.3/§8.4 of the design doc).
# ---------------------------------------------------------------------------

# Sheet row actions that actually mutate TickTick — flags never do (§3/§4.1).
_DC_MUTATING_ROW_ACTIONS = ("delete", "rename", "group")


def _dc_now_la_iso() -> str:
    """Timestamp for the sheet's run_ts/applied_ts columns — America/Los_Angeles
    (via the app-wide _USER_TZ, same convention already used for due dates and
    the plan_declutter header), never UTC."""
    return datetime.now(_USER_TZ).isoformat()


def _dc_actions_to_rows(actions: Dict, manifest_id: str, run_ts: str,
                        names: Optional[Dict] = None) -> List[Dict]:
    """Pure: flatten a declutter `actions` dict (the output of _dc_analyze)
    into the flat per-row shape of the `Declutter Log` sheet (§3 of the
    design doc). No I/O — `row_id` is assigned later by
    declutter_sheet.append_rows(). Mutating actions (delete/rename/
    group-child) default decision="approved" (today's apply-everything-
    proposed semantics, §3 "Дефолт decision"); flag_* rows default
    decision="pending" and are NEVER auto-applied (§5.9/§9.4 — MVP: flags
    stay informational)."""
    names = names or {}

    def row(task_id, title, project, action, proposed_value, reason,
            cluster_id="", decision="approved", snapshot=None):
        return {
            "manifest_id": manifest_id, "run_ts": run_ts,
            "task_id": task_id or "", "title": title or "",
            "project": project or "", "column": "",
            "cluster_id": cluster_id or "", "action": action,
            "proposed_value": proposed_value or "", "reason": reason or "",
            "decision": decision, "status": "planned",
            "applied_ts": "", "error": "",
            "snapshot_json": json.dumps(snapshot or {}, ensure_ascii=False, default=str),
        }

    rows: List[Dict] = []

    for it in actions.get("delete", []):
        keep_id = it.get("keep_id") or ""
        rows.append(row(
            it.get("taskId"), it.get("title"), it.get("project"),
            "delete", f"keep→{keep_id}", it.get("reason"),
            cluster_id=keep_id, decision="approved",
            snapshot=it.get("snapshot")))

    for it in actions.get("rename", []):
        rows.append(row(
            it.get("taskId"), it.get("title"), it.get("project"),
            "rename", it.get("new_title"), it.get("reason"),
            decision="approved"))

    for g in actions.get("group", []):
        pid = g.get("parentId") or ""
        for c in g.get("children", []):
            rows.append(row(
                c.get("taskId"), c.get("title"),
                names.get(c.get("projectId"), g.get("project")),
                "group", f"parent→{pid}",
                f"под «{g.get('parent_title', '')}»",
                cluster_id=pid, decision="approved"))

    for it in actions.get("flag_obsolete", []):
        age = (f"{it.get('age_days')}д без правок"
               if it.get("age_days") is not None else "возраст неизв.")
        rows.append(row(
            it.get("taskId"), it.get("title"), it.get("project"),
            "flag_obsolete", "",
            f"просрочено {it.get('overdue_days')}д, {age}",
            decision="pending"))

    for i, it in enumerate(actions.get("flag_dupe", [])):
        cid = f"dupeflag-{i}"
        ids = it.get("ids") or []
        titles = it.get("titles") or []
        for j, tid in enumerate(ids):
            rows.append(row(
                tid, titles[j] if j < len(titles) else "", "",
                "flag_dupe", "", it.get("reason"),
                cluster_id=cid, decision="pending"))

    for it in actions.get("flag_nonsmart", []):
        rows.append(row(
            it.get("taskId"), it.get("title"), it.get("project"),
            "flag_nonsmart", "", "", decision="pending"))

    return rows


def _dc_rows_to_exec(rows: List[Dict]) -> Dict:
    """Pure: reconstruct the input for execute_task_deletion / update_tasks /
    set_task_parent from an ALREADY-FILTERED list of sheet rows (the caller
    picks `apply_rows` = decision=="approved" AND status in
    ("planned","failed") AND action in _DC_MUTATING_ROW_ACTIONS). No I/O.

    Enforces the keep∉deletes invariant (§5.9): a delete row's `cluster_id`
    IS the id of the task being kept. If that id is itself among the
    task_ids approved for deletion in this same batch, the row is refused
    (moved to `refused`, not `delete`) — a human approved a self-contradicting
    edit (probably by hand-editing `decision`), and applying it would delete
    the very task the OTHER row promised to keep.

    Returns {"delete": [...], "rename": [...], "group": [...],
             "refused": [{"row_id", "task_id", "cluster_id", "reason"}]} —
    `delete`/`rename` items carry a "projectId": "" placeholder (the sheet
    does not store project ids, only display names — every downstream
    sub-tool re-resolves the REAL projectId from live state via the identity
    guard, so an empty placeholder here is never actually trusted)."""
    delete_rows = [r for r in rows if r.get("action") == "delete"]
    rename_rows = [r for r in rows if r.get("action") == "rename"]
    group_rows = [r for r in rows if r.get("action") == "group"]

    delete_task_ids = {r.get("task_id") for r in delete_rows}
    refused: List[Dict] = []
    delete: List[Dict] = []
    for r in delete_rows:
        keep_id = r.get("cluster_id") or ""
        if keep_id and keep_id in delete_task_ids:
            refused.append({
                "row_id": r.get("row_id"), "task_id": r.get("task_id"),
                "cluster_id": keep_id,
                "reason": (f"задача-«оставить» ({str(keep_id)[:8]}…) сама "
                           "числится на удаление в этом же прогоне — "
                           "кластер отклонён, поправь decision вручную"),
            })
            continue
        try:
            snapshot = json.loads(r.get("snapshot_json") or "{}")
        except (ValueError, TypeError):
            snapshot = {}
        delete.append({
            "taskId": r.get("task_id"), "projectId": "",
            "title": r.get("title") or "", "project": r.get("project") or "",
            "snapshot": snapshot,
        })

    rename = [{"taskId": r.get("task_id"), "projectId": "",
               "title": r.get("title") or "",
               "new_title": r.get("proposed_value") or ""}
              for r in rename_rows]

    groups_by_parent: Dict[str, Dict] = {}
    for r in group_rows:
        pid = r.get("cluster_id") or ""
        g = groups_by_parent.setdefault(pid, {
            "parentId": pid, "parent_title": "", "project_id": "",
            "children": [],
        })
        g["children"].append({"taskId": r.get("task_id"),
                              "title": r.get("title") or ""})
    group = [g for g in groups_by_parent.values() if g["children"]]

    return {"delete": delete, "rename": rename, "group": group,
            "refused": refused}


async def _execute_declutter_from_sheet(manifest_id: str) -> str:
    """Sheet-backed execute/resume engine — shared by execute_declutter (when
    the RAM pointer says persist="sheet") and resume_declutter (RAM pointer
    gone, e.g. after a Railway restart). Implements §4.2/§5/§6 of
    docs/DESIGN_sheet_backed_declutter.md:

    - freshly reads `decision` from the sheet every time (never trusts RAM —
      a human may have edited the sheet after the plan was printed);
    - CONSENT (docs/DESIGN_approval_gate.md) is the CALLER's job — both
      execute_declutter and resume_declutter run _require_consent(...) with
      the user's real user_reply BEFORE dispatching here; this function
      trusts that a genuine "yes" already happened and just does the work;
    - resolves a crashed `applying` lock by LIVE fact in TickTick (§5.4)
      before deciding whether to retry it, rather than trusting the stale
      flag;
    - write-throughs status=done|failed per row, judged from a FRESH
      post-apply read of live state (§5.6) rather than by parsing the
      sub-tool's text output;
    - never re-applies a `done` row (§5.2 — the core "don't delete twice"
      guarantee) — a row whose decision flipped to "rejected"/"pending"
      since the plan was printed is likewise simply excluded from
      `apply_rows` below, so nothing gets touched for it regardless of what
      N a stale plan-time message said."""
    err = _ensure_ready()
    if err:
        return err
    try:
        rows = declutter_sheet.read_manifest_rows(manifest_id)
    except declutter_sheet.DeclutterSheetError as e:
        return f"🛑 Не могу прочитать таблицу разбора: {_redact_for_user(e)}"
    if not rows:
        try:
            url = declutter_sheet.sheet_url()
        except declutter_sheet.DeclutterSheetError:
            url = "(таблица не настроена)"
        return (f"🛑 Манифест разбора {manifest_id} не найден в таблице "
                f"({url}). Сначала plan_declutter(persist=\"sheet\").")

    by_id = _open_by_id(fresh=True)

    # ---- §5.4: resolve any crashed `applying` lock by LIVE fact FIRST -----
    stuck = [r for r in rows if r.get("status") == "applying"
             and r.get("action") in _DC_MUTATING_ROW_ACTIONS]
    if stuck:
        if by_id is None:
            return _STATE_UNAVAILABLE_MSG
        resolved_updates = []
        for r in stuck:
            tid = r.get("task_id")
            if r.get("action") == "delete":
                done = tid not in by_id
            elif r.get("action") == "rename":
                live = by_id.get(tid)
                done = bool(live) and _names_agree(
                    r.get("proposed_value") or "", live.get("title") or "")
            else:  # group
                live = by_id.get(tid)
                done = bool(live) and live.get("parentId") == r.get("cluster_id")
            if done:
                r["status"] = "done"
                resolved_updates.append({"row_id": r.get("row_id"),
                                         "status": "done",
                                         "applied_ts": _dc_now_la_iso()})
            else:
                r["status"] = "failed"
                resolved_updates.append({
                    "row_id": r.get("row_id"), "status": "failed",
                    "error": "прерванное применение (лок завис) — переприменяю"})
        try:
            declutter_sheet.batch_update_rows(resolved_updates)
        except declutter_sheet.DeclutterSheetError as e:
            return f"🛑 Не могу разрешить зависший лок applying в таблице: {_redact_for_user(e)}"

    apply_rows = [r for r in rows if r.get("action") in _DC_MUTATING_ROW_ACTIONS
                  and r.get("decision") == "approved"
                  and r.get("status") in ("planned", "failed")]

    if not apply_rows:
        return ("Нечего применять — в таблице нет одобренных ещё-не-"
                f"применённых правок для манифеста {manifest_id}.")

    exec_input = _dc_rows_to_exec(apply_rows)

    # ---- lock BEFORE touching TickTick (§5.3) ------------------------------
    lock_updates = [{"row_id": r.get("row_id"), "status": "applying"}
                    for r in apply_rows]
    try:
        declutter_sheet.batch_update_rows(lock_updates)
    except declutter_sheet.DeclutterSheetError as e:
        return f"🛑 Не могу поставить лок applying в таблице: {_redact_for_user(e)}"

    summary = f"Разбор помойки (таблица) — манифест {manifest_id}"
    out_blocks: List[str] = []
    report_ids: set = set()
    row_by_task_action: Dict[tuple, Dict] = {
        (r.get("task_id"), r.get("action")): r for r in apply_rows}
    status_updates: List[Dict] = []

    try:
        if exec_input["delete"]:
            sub_mid = uuid.uuid4().hex[:12]
            items = [{"taskId": it["taskId"], "projectId": it["projectId"],
                      "title": it["title"], "project": it.get("project", ""),
                      "snapshot": it["snapshot"]} for it in exec_input["delete"]]
            _MANIFESTS[sub_mid] = {"kind": "delete", "items": items,
                                   "created": time.monotonic(),
                                   "summary": summary + " — дубликаты",
                                   "consumed": False}
            res = await _execute_task_deletion_impl(sub_mid)
            out_blocks.append("## 🗑 Удаление дубликатов\n" + res)
            report_ids.add(sub_mid)

        if exec_input["rename"]:
            res = await _update_tasks_impl(
                summary + " — SMART-переименования",
                [{"taskId": it["taskId"], "projectId": it["projectId"],
                  "title": it["title"], "new_title": it["new_title"]}
                 for it in exec_input["rename"]])
            out_blocks.append("## ✏️ Переименования\n" + res)

        for g in exec_input["group"]:
            res = await _set_task_parent_impl(
                summary + " — группировка",
                [{"taskId": c["taskId"], "title": c["title"]}
                 for c in g["children"]],
                g["parentId"], g["project_id"] or "", g.get("parent_title") or "")
            out_blocks.append(f"## 🔗 Группировка под `{g['parentId']}`\n" + res)

        # ---- §5.6: per-row write-through, judged from FRESH live state ----
        fresh = _open_by_id(fresh=True)
        for it in exec_input["delete"]:
            r = row_by_task_action.get((it["taskId"], "delete"))
            if not r:
                continue
            if fresh is None:
                status_updates.append({"row_id": r["row_id"], "status": "failed",
                                       "error": _UNVERIFIED_MSG})
                continue
            ok = it["taskId"] not in fresh
            status_updates.append({
                "row_id": r["row_id"],
                "status": "done" if ok else "failed",
                "applied_ts": _dc_now_la_iso() if ok else "",
                "error": "" if ok else "задача всё ещё в TickTick после удаления",
            })
        for it in exec_input["rename"]:
            r = row_by_task_action.get((it["taskId"], "rename"))
            if not r:
                continue
            if fresh is None:
                status_updates.append({"row_id": r["row_id"], "status": "failed",
                                       "error": _UNVERIFIED_MSG})
                continue
            live = fresh.get(it["taskId"])
            ok = bool(live) and _names_agree(it["new_title"], live.get("title") or "")
            status_updates.append({
                "row_id": r["row_id"],
                "status": "done" if ok else "failed",
                "applied_ts": _dc_now_la_iso() if ok else "",
                "error": "" if ok else "новое название не видно в живом состоянии",
            })
        for g in exec_input["group"]:
            for c in g["children"]:
                r = row_by_task_action.get((c["taskId"], "group"))
                if not r:
                    continue
                if fresh is None:
                    status_updates.append({"row_id": r["row_id"], "status": "failed",
                                           "error": _UNVERIFIED_MSG})
                    continue
                live = fresh.get(c["taskId"])
                ok = bool(live) and live.get("parentId") == g["parentId"]
                status_updates.append({
                    "row_id": r["row_id"],
                    "status": "done" if ok else "failed",
                    "applied_ts": _dc_now_la_iso() if ok else "",
                    "error": "" if ok else "parentId не применился",
                })
        # Rows refused by the keep∉deletes invariant (§5.9) never went into
        # exec_input — they were locked `applying` above, so must be
        # explicitly written back to `failed` here (not left stuck).
        for ref in exec_input["refused"]:
            status_updates.append({"row_id": ref["row_id"], "status": "failed",
                                   "error": ref["reason"]})

        try:
            declutter_sheet.batch_update_rows(status_updates)
        except declutter_sheet.DeclutterSheetError as e:
            out_blocks.append(
                f"⚠️ Правки применены, но не смог записать статусы обратно в "
                f"таблицу: {_redact_for_user(e)} — сверь фактический результат вручную "
                "(operation_report ниже) и поправь `status` в таблице.")

        if not out_blocks:
            return "Нечего применять — в манифесте не было правок."

        combined = "\n\n".join(out_blocks)
        for rid in re.findall(r'operation_report\(record_id="([\w-]+)"\)', combined):
            report_ids.add(rid)
        reports = [_build_operation_report(rid) for rid in report_ids]
        if reports:
            combined += "\n\n---\n### 🧾 Независимые отчёты\n\n" + "\n\n".join(reports)
        if exec_input["refused"]:
            try:
                url = declutter_sheet.sheet_url()
            except declutter_sheet.DeclutterSheetError:
                url = ""
            combined += (
                f"\n\n🛑 Отклонено по инварианту «оставить∉удаляемых»: "
                f"{len(exec_input['refused'])} правк(а/и) — задача-«оставить» "
                "сама была одобрена на удаление в этом же прогоне. Строки "
                f"помечены failed{(', поправь decision: ' + url) if url else ''}.")
        try:
            combined += f"\n\n📄 Таблица: {declutter_sheet.sheet_url()}"
        except declutter_sheet.DeclutterSheetError:
            pass
        return combined
    except Exception as e:
        logger.exception("Error in _execute_declutter_from_sheet")
        # Best-effort: release the applying lock back to failed so a retry is
        # possible instead of a permanently stuck row (mirrors the RAM
        # branch's "graceful error, manifest state already changed" contract
        # — see test_execute_declutter_returns_graceful_error_on_internal_exception).
        try:
            declutter_sheet.batch_update_rows(
                [{"row_id": r["row_id"], "status": "failed",
                  "error": f"внутренняя ошибка: {_redact_for_user(e)}"} for r in apply_rows])
        except Exception:
            pass
        return _tool_error("executing declutter manifest (sheet)", e)


# DISABLED 2026-08-04 по прямому указанию Максима ("Деклатер закоменть пока") —
# QA-инцидент: plan_declutter молча смешал реальные задачи с тестовыми в одном
# манифесте на исполнение. Держать выключенным, пока функция не настроена и
# не протестирована безопасно. Раскомментировать decorator ниже, чтобы вернуть.
# @mcp.tool(annotations=READONLY)
async def plan_declutter(scope: str = "", dry_run: bool = True,
                         max_tasks: int = 0, persist: str = "ram") -> str:
    """
    Phase 1 of the retroactive declutter ("разбор помойки"): READ every open
    task and propose how to tidy the EXISTING pile. Read-only — creates and
    changes NOTHING. This is the retro counterpart to ingest-time dedup: it
    works over what is ALREADY in TickTick.

    Analyses for: (1) DUPLICATE clusters — near-identical tasks; the redundant
    copies are proposed for deletion, the richest kept (uncertain merges default
    to KEEP-BOTH and are only flagged); (2) OBSOLETE — long-overdue + untouched
    tasks, FLAGGED for a human, never auto-completed/deleted; (3) GROUPABLE —
    umbrella tasks whose title is a header of others (proposed nesting);
    (4) non-SMART titles — a proposed clearer reformulation.

    When the CLAUDE_CLI shim is set it judges fuzzy duplicate merges (bias
    keep-both) and writes the SMART reformulations. When it is unset/unreachable
    the analysis DEGRADES to rule-based exact-title duplicates only and says so.

    IMPORTANT: reprint the returned manifest VERBATIM to the user and STOP —
    wait for their real reply, do not call execute in the same turn. Once
    they actually answer, call execute_declutter(manifest_id,
    user_reply=<their literal last message, verbatim>). Nothing mutates
    until then. Manifests are one-shot, expire in 1 h.

    Refuses cleanly (no analysis, no shim call) when the resolved scope has
    more than a few hundred open tasks — narrow `scope` (project/tag/date)
    instead of running this over the whole pile.

    persist="sheet" (opt-in, default stays "ram" — RAM behaviour is UNCHANGED):
    writes every proposed edit (and flag) as a row in the `Declutter Log`
    Google Sheet (env DECLUTTER_SHEET_ID/GSHEETS_SA_JSON — see
    docs/DESIGN_sheet_backed_declutter.md) instead of relying only on the
    in-process RAM manifest. The sheet becomes the durable source of truth:
    it survives a restart, a human can edit its `decision` column directly,
    and resume_declutter/execute_declutter re-read it fresh rather than
    trusting stale RAM. If the sheet is unreachable (missing creds/network)
    plan_declutter REFUSES explicitly — no silent fallback to RAM, since the
    entire point of persist="sheet" is a durable plan.

    Args:
        scope: optional narrowing — 'inbox', a project-name substring, or
            'Project/Column' to restrict to one kanban column of a project
            (e.g. 'Fix&Roll/TG Inbox'). Use list_project_columns to see a
            project's column names. Narrowing a big project to one column is
            the intended way to fit under the size cap.
        dry_run: retained for symmetry; plan_declutter is always read-only
        max_tasks: override the default size cap (_DC_MAX_TASKS = 300) for THIS
            call only. 0/unset keeps the default. Raise it when you deliberately
            want to declutter a bigger set and accept the extra time — the O(n^2)
            clustering and the bundled shim prompts grow with the set, and the
            MCP client's ~60s timeout still applies, so keep it sane.
        persist: "ram" (default, unchanged behaviour) or "sheet" (durable —
            see above).
    """
    err = _ensure_ready()
    if err:
        return err
    _prune_manifests()
    by_id = _open_by_id(fresh=True)
    if by_id is None:
        return _STATE_UNAVAILABLE_MSG
    names = _v2_project_names()
    col_names = await _dc_column_names_for_scope(scope, names)
    tasks = _dc_scope_filter(list(by_id.values()), names, scope, col_names)
    if not tasks:
        return ("Открытых задач в этой области нет — разбирать нечего."
                + (f" (scope='{scope}')" if scope else ""))
    # Refuse BEFORE any O(n^2) clustering/grouping or shim call when the
    # resolved scope is too big — see _DC_MAX_TASKS for the sizing rationale.
    # Read-only refusal: nothing above this point mutated anything, and
    # nothing below runs, so there is no partial analysis/journal entry.
    cap = max_tasks if isinstance(max_tasks, int) and max_tasks > 0 else _DC_MAX_TASKS
    if len(tasks) > cap:
        raise_hint = (
            "" if (isinstance(max_tasks, int) and max_tasks > 0)
            else f" Либо, если уверен в объёме, подними лимит: max_tasks={len(tasks)}.")
        return (f"🛑 Отказ: в этой области {len(tasks)} открытых задач — "
                f"больше капа {cap} (на {len(tasks) - cap} "
                "больше). Разбор такого объёма упрётся в таймаут ещё до "
                "готового плана. Сузь scope и попробуй снова — например "
                "scope='inbox', конкретный проект, или ОДНУ колонку большого "
                "проекта: scope='Проект/Колонка' (например 'Fix&Roll/TG Inbox'; "
                "названия колонок — list_project_columns)."
                + raise_hint
                + (f" (текущий scope='{scope}')" if scope else " (scope не задан — вся куча)"))

    shim = _dc_shim_available()
    # Track whether a shim call actually FAILED during this run (vs. simply
    # being unconfigured) so the manifest can warn accurately — "shim
    # unavailable" previously only reflected the env vars being unset, not a
    # real degraded call (timeout/bad response/malformed JSON) mid-analysis.
    shim_fail_tracker: list = []
    judge_fn = (lambda clusters: _dc_judge_fn(clusters, fail_tracker=shim_fail_tracker)) \
        if shim else None
    smart_fn = (lambda titles: _dc_smart_fn(titles, fail_tracker=shim_fail_tracker)) \
        if shim else None
    actions = await _dc_analyze(
        tasks, names,
        judge_fn=judge_fn,
        smart_fn=smart_fn,
        today=_today_local(), now=datetime.now(timezone.utc), fuzzy=shim)
    shim_call_failed = bool(shim_fail_tracker)

    n_mut = _dc_mutating_count(actions)
    n_flags = (len(actions["flag_obsolete"]) + len(actions["flag_dupe"])
               + len(actions["flag_nonsmart"]))

    mid = uuid.uuid4().hex[:12]
    now = time.monotonic()
    sheet_note = ""
    if persist == "sheet":
        # Durable-plan invariant (design doc §9.3, Maksim's decision): if the
        # sheet is unreachable, REFUSE explicitly — no silent fallback to a
        # RAM manifest the human never sees and that dies on restart.
        run_ts = _dc_now_la_iso()
        rows = _dc_actions_to_rows(actions, mid, run_ts, names)
        try:
            declutter_sheet.ensure_header()
            declutter_sheet.append_rows(rows)
            url = declutter_sheet.sheet_url()
        except declutter_sheet.DeclutterSheetError as e:
            return (f"🛑 Отказ: не могу сохранить план durable в Google Sheet "
                    f"(persist=\"sheet\") — {_redact_for_user(e)} Ничего не записано в TickTick "
                    "и ничего не записано в таблицу — план НЕ построен. "
                    "Проверь DECLUTTER_SHEET_ID/GSHEETS_SA_JSON и доступ к "
                    "сети, либо вызови без persist (RAM-режим, как раньше).")
        _MANIFESTS[mid] = {"kind": "declutter", "actions": actions,
                           "persist": "sheet", "spreadsheet_url": url,
                           "mutating_count": n_mut, "created": now,
                           "plan_shown_at": now,
                           "object_hash": _manifest_object_hash(
                               "declutter", _dc_object_ids(actions)),
                           "summary": f"Разбор помойки ({n_mut} правок)",
                           "consumed": False}
        sheet_note = (
            f"\n📄 _План записан durable в таблицу: {url} — можешь править "
            "колонку `decision` (approved/rejected) прямо там, либо "
            "`set_declutter_decision`. Затем `execute_declutter(...)` "
            "применит одобренное; после рестарта — "
            f"`resume_declutter(manifest_id=\"{mid}\", user_reply=...)`._")
    else:
        _MANIFESTS[mid] = {"kind": "declutter", "actions": actions,
                           "mutating_count": n_mut, "created": now,
                           "plan_shown_at": now,
                           "object_hash": _manifest_object_hash(
                               "declutter", _dc_object_ids(actions)),
                           "summary": f"Разбор помойки ({n_mut} правок)",
                           "consumed": False}

    when = datetime.now(_USER_TZ).strftime("%d.%m.%Y %H:%M")
    lines = [f"### 🧹 План разбора помойки — {when} ({_USER_TZ.key})",
             _plan_id_line(mid, f"проверено задач: {len(tasks)} · "
                                "ничего ещё не тронуто")]
    if not shim:
        lines.append("⚠️ _CLAUDE_CLI shim недоступен → только точные дубликаты "
                     "(по идентичному названию), без судьи слияний и без "
                     "SMART-переформулировок._")
    elif shim_call_failed:
        lines.append("⚠️ _CLAUDE_CLI shim настроен, но хотя бы один вызов во "
                     "время этого разбора не удался (сеть/таймаут/некорректный "
                     "ответ) — часть спорных дублей/названий могла остаться "
                     "без вердикта судьи и уйти в «на заметку»._")
    lines.append("")

    if actions["delete"]:
        lines.append(f"#### 🗑 Дубликаты на удаление — {len(actions['delete'])}")
        for it in actions["delete"]:
            lines.append(
                f"- **«{it['title']}»** — {it['project']} (`{it['taskId']}`) → "
                f"удалить, оставить **«{it['keep_title']}»** _({it['reason']})_")
        lines.append("")
    if actions["rename"]:
        lines.append(f"#### ✏️ SMART-переформулировки — {len(actions['rename'])}")
        for it in actions["rename"]:
            lines.append(
                f"- «{it['title']}» → **«{it['new_title']}»** — {it['project']} "
                f"(`{it['taskId']}`)"
                + (f" _({it['reason']})_" if it['reason'] else ""))
        lines.append("")
    if actions["group"]:
        total_kids = sum(len(g["children"]) for g in actions["group"])
        lines.append(f"#### 🔗 Группировка (родитель+подзадачи) — "
                     f"{len(actions['group'])} групп / {total_kids} задач")
        for g in actions["group"]:
            lines.append(f"- под **«{g['parent_title']}»** ({g['project']}, "
                         f"`{g['parentId']}`):")
            for c in g["children"]:
                lines.append(f"    - «{c['title']}» (`{c['taskId']}`)")
        lines.append("")

    if n_flags:
        lines.append("#### 🚩 Только на заметку (НЕ трогаю автоматически)")
        if actions["flag_obsolete"]:
            lines.append(f"- ⏳ Похоже на протухшие — просрочены и давно без "
                         f"движения ({len(actions['flag_obsolete'])}): "
                         "реши сам, добить или отпустить:")
            for it in actions["flag_obsolete"]:
                age = f"{it['age_days']}д без правок" if it['age_days'] is not None else "возраст неизв."
                lines.append(f"    - «{it['title']}» — {it['project']} · срок "
                             f"{it['due']} (просрочено {it['overdue_days']}д, {age})")
        if actions["flag_dupe"]:
            lines.append(f"- 🤔 Похожи, но слить не уверен — оставил обе "
                         f"({len(actions['flag_dupe'])}):")
            for it in actions["flag_dupe"]:
                lines.append("    - " + " / ".join(f"«{t}»" for t in it["titles"])
                             + f" _({it['reason']})_")
        if actions["flag_nonsmart"]:
            lines.append(f"- ✏️ Расплывчатые названия без готовой переформулировки "
                         f"({len(actions['flag_nonsmart'])}): "
                         + ", ".join(f"«{it['title']}»" for it in actions["flag_nonsmart"]))
        lines.append("")

    if n_mut == 0:
        lines.append("**Правок для применения нет** — либо всё чисто, либо всё "
                     "спорное ушло в «на заметку».")
        return "\n".join(lines) + sheet_note

    lines.append(f"**Итого к применению: {n_mut} правок** "
                 f"(🗑 {len(actions['delete'])} · ✏️ {len(actions['rename'])} · "
                 f"🔗 {sum(len(g['children']) for g in actions['group'])}). "
                 "Протухшие и спорные НЕ входят.")
    lines.append("")
    # Инструкция для модели — ОТДЕЛЬНО от `lines`/`sheet_note` (2026-08-06,
    # дефект №2): раньше уходила дословно в Telegram-карточку плана.
    agent_tail = ("Покажи этот план пользователю дословно и ДОЖДИСЬ его "
                 "отдельного ответа. Когда он явно согласится, вызови "
                 f"`execute_declutter(manifest_id=\"{mid}\", "
                 "user_reply=\"<дословная реплика пользователя>\")` — НЕ в "
                 "этом же ходе. Действует 1 час, одноразово. Каждая правка "
                 "пройдёт через штатные удаление/обновление/вложение "
                 "(guard + журнал + сверка).")
    return await _maybe_tg_notify_plan("execute_declutter", mid,
                                       "\n".join(lines) + sheet_note, agent_tail)


# DISABLED 2026-08-04 — см. пометку у plan_declutter выше.
# @mcp.tool()
async def execute_declutter(manifest_id: str, user_reply: str = "") -> str:
    """
    Phase 2 of the declutter: apply EXACTLY the mutating actions the manifest
    proposed and the user approved. Gated (🔴 docs/DESIGN_approval_gate.md):
    `user_reply` must be the user's VERBATIM last chat message, given ONLY
    after they actually saw the plan and replied — do not paraphrase or
    invent it, and do not call this in the same turn where you printed the
    plan.

    Nothing here is a fresh decision — every action is routed through the
    already-audited tools (the deletion engine / update_tasks /
    set_task_parent), so each mutation is identity-guarded, journalled and
    post-verified. Obsolete and uncertain-duplicate FLAGS are never touched.
    One-shot. Afterwards the independent operation reports are appended.

    For a manifest planned with persist="sheet": this dispatches into the
    sheet-backed engine instead — `decision`/`status` are freshly re-read
    from the `Declutter Log` sheet (never trusted from RAM — see
    docs/DESIGN_sheet_backed_declutter.md §6), and per-row results are
    written back to the sheet. If the RAM pointer from plan_declutter is gone
    (e.g. a restart) but the manifest_id still has rows in the sheet, use
    resume_declutter instead — same engine, explicit entry point.

    Args:
        manifest_id: id from plan_declutter
        user_reply: the user's literal last message approving the plan
    """
    err = _ensure_ready()
    if err:
        return err
    _prune_manifests()
    # План мог быть построен ДРУГИМ процессом (перезапуск между планом и
    # исполнением). Если этот его не знает — поднимаем из базы (#91).
    await _rehydrate_manifest(manifest_id)
    m = _MANIFESTS.get(manifest_id)
    if m and m.get("kind") == "declutter" and m.get("persist") == "sheet":
        cr = _require_consent(action="declutter", tier=2, manifest=m,
                              user_reply=user_reply,
                              object_ids=_dc_object_ids(m.get("actions") or {}),
                              tool="execute_declutter", manifest_id=manifest_id)
        if not cr.ok:
            return cr.reason
        return await _execute_declutter_from_sheet(manifest_id)
    if not m or m.get("kind") != "declutter":
        # RAM pointer missing/expired — it may still be a sheet-backed
        # manifest from an earlier process (Railway restart killed RAM but
        # the sheet is durable). Only probe the sheet when it's actually
        # configured, so a genuinely bad/expired id keeps today's exact text.
        if declutter_sheet.sheet_configured():
            try:
                if declutter_sheet.read_manifest_rows(manifest_id):
                    cr = _require_consent(action="declutter", tier=2,
                                          manifest=None, user_reply=user_reply,
                                          tool="execute_declutter", manifest_id=manifest_id)
                    if not cr.ok:
                        return cr.reason
                    return await _execute_declutter_from_sheet(manifest_id)
            except declutter_sheet.DeclutterSheetError:
                pass
        return (f"🛑 Манифест разбора {manifest_id} не найден/истёк/уже "
                "исполнен. Сначала plan_declutter.")
    cr = _require_consent(action="declutter", tier=2, manifest=m,
                          user_reply=user_reply,
                          object_ids=_dc_object_ids(m.get("actions") or {}),
                          tool="execute_declutter", manifest_id=manifest_id)
    if not cr.ok:
        return cr.reason
    return await _execute_declutter_ram_impl(manifest_id, m)


async def _execute_declutter_ram_impl(manifest_id: str, m: Dict) -> str:
    """Shared declutter-application engine for the RAM-manifest branch (both
    plain RAM plans and persist="sheet" plans whose RAM pointer is still
    alive — the sheet-gone-after-restart case is handled separately by
    _execute_declutter_from_sheet, called by the resume_declutter tool and by
    execute_declutter's own sheet-persist/RAM-missing branches above).
    Factored out of execute_declutter() 2026-08-05 so the TG auto-execute
    poller (server.py's _AUTO_EXECUTORS registry) can call the exact same
    mutation path a manually-confirmed «да» would have run, once consent has
    already been granted by the caller (_require_consent, or the poller's
    try_auto_execute — see tg_approval.py)."""
    _mark_manifest_consumed(m, manifest_id)
    try:
        actions = m["actions"]
        summary = m.get("summary") or "Разбор помойки"
        out_blocks: List[str] = []
        report_ids: set = set()

        # ---- Deletions: reuse the audited deletion manifest engine --------
        if actions["delete"]:
            sub_mid = uuid.uuid4().hex[:12]
            items = [{"taskId": it["taskId"], "projectId": it["projectId"],
                      "title": it["title"], "project": it.get("project", ""),
                      "snapshot": it["snapshot"]} for it in actions["delete"]]
            _MANIFESTS[sub_mid] = {"kind": "delete", "items": items,
                                   "created": time.monotonic(),
                                   "summary": summary + " — дубликаты",
                                   "consumed": False}
            res = await _execute_task_deletion_impl(sub_mid)
            out_blocks.append("## 🗑 Удаление дубликатов\n" + res)
            report_ids.add(sub_mid)

        # ---- Renames: reuse update_tasks (guard + journal + post-verify) --
        if actions["rename"]:
            res = await _update_tasks_impl(
                summary + " — SMART-переименования",
                [{"taskId": it["taskId"], "projectId": it["projectId"],
                  "title": it["title"], "new_title": it["new_title"]}
                 for it in actions["rename"]])
            out_blocks.append("## ✏️ Переименования\n" + res)

        # ---- Groups: reuse set_task_parent (guard + journal + post-verify) -
        for g in actions["group"]:
            res = await _set_task_parent_impl(
                summary + f" — под «{g['parent_title']}»",
                [{"taskId": c["taskId"], "title": c["title"]} for c in g["children"]],
                g["parentId"], g["project_id"], g["parent_title"])
            out_blocks.append(f"## 🔗 Группировка под «{g['parent_title']}»\n" + res)

        if not out_blocks:
            return "Нечего применять — в манифесте не было правок."

        combined = "\n\n".join(out_blocks)
        # Consolidated independent check: pull every journalled record id the
        # sub-tools referenced and append the server-built report for each.
        for rid in re.findall(r'operation_report\(record_id="([\w-]+)"\)', combined):
            report_ids.add(rid)
        reports = [_build_operation_report(rid) for rid in report_ids]
        if reports:
            combined += "\n\n---\n### 🧾 Независимые отчёты\n\n" + "\n\n".join(reports)
        return combined
    except Exception as e:
        logger.exception("Error in execute_declutter")
        return _tool_error("executing declutter manifest", e)


# DISABLED 2026-08-04 — см. пометку у plan_declutter выше.
# @mcp.tool()
async def resume_declutter(manifest_id: str, user_reply: str = "") -> str:
    """
    Resume a sheet-backed declutter manifest (plan_declutter(persist="sheet"))
    after a restart, timeout, or partial application — when the in-process RAM
    pointer plan_declutter left behind is gone. The Google Sheet IS the durable
    state; RAM was only ever a same-process cache (see
    docs/DESIGN_sheet_backed_declutter.md).

    Reconstructs the remaining work straight from the `Declutter Log` rows for
    this manifest_id: applies rows with decision="approved" AND status in
    (planned, failed) — a row already `done` is NEVER re-applied (so this is
    safe to call again after a partial run — it won't delete anything twice),
    and a row left `applying` by a crashed run is resolved against LIVE
    TickTick state first (task already gone → done; title already matches →
    done; otherwise → retried) rather than trusted at face value.

    Gated (🔴 docs/DESIGN_approval_gate.md): the RAM manifest is gone by
    definition here, so there is no plan_shown_at to time against — but
    `user_reply` must still be the user's VERBATIM real reply confirming they
    want the remaining approved rows applied NOW (not fabricated by you).
    If `decision` was edited in the sheet (or via set_declutter_decision)
    since the original plan, that's reflected automatically — this always
    re-reads `decision`/`status` fresh from the sheet.

    Args:
        manifest_id: id from the ORIGINAL plan_declutter(persist="sheet") call
        user_reply: the user's literal message confirming "yes, apply now"
    """
    err = _ensure_ready()
    if err:
        return err
    if not declutter_sheet.sheet_configured():
        return ("🛑 Sheet-режим declutter не настроен (нужны env "
                "DECLUTTER_SHEET_ID и GSHEETS_SA_JSON) — resume_declutter "
                "работает только для планов, сохранённых с persist=\"sheet\".")
    cr = _require_consent(action="declutter", tier=2, manifest=None,
                          user_reply=user_reply,
                          tool="resume_declutter", manifest_id=manifest_id)
    if not cr.ok:
        return cr.reason
    return await _execute_declutter_from_sheet(manifest_id)


# DISABLED 2026-08-04 — см. пометку у plan_declutter выше.
# @mcp.tool()
async def set_declutter_decision(manifest_id: str, row_ids: List[int],
                                 decision: str, user_reply: str = "") -> str:
    """
    Set the `decision` column (approved/rejected) for specific rows of a
    sheet-backed declutter manifest (plan_declutter(persist="sheet")) —
    instead of the human editing the Google Sheet by hand, Claude
    proposes/relays a decision here and the CODE writes it, so the row's
    task_id/decision never has to round-trip through the model's own context.

    WARNING: writing decision="approved" is EQUIVALENT to authorizing
    whatever execute_declutter/resume_declutter will do with those rows next
    (delete/rename/group) — it is NOT a harmless bookkeeping note. Gated
    (🔴 docs/DESIGN_approval_gate.md) for that reason: decision="approved"
    REQUIRES user_reply = the user's VERBATIM message approving exactly these
    rows — you may NOT set "approved" on your own judgement. decision=
    "rejected" is safe (it only prevents action) and is NOT gated.

    Does NOT touch TickTick and does NOT change `status` — purely an
    approval-bookkeeping write to column L. execute_declutter/resume_declutter
    read the freshly-written `decision` afterwards, as always.

    Args:
        manifest_id: id from plan_declutter(persist="sheet")
        row_ids: sheet row_id values (column A, printed in the plan / visible
            in the sheet) to update — NOT task ids
        decision: "approved" or "rejected"
        user_reply: REQUIRED when decision="approved" — the user's literal
            message approving these specific rows
    """
    if decision not in ("approved", "rejected"):
        return "🛑 decision должен быть \"approved\" или \"rejected\" — получено " \
               f"{decision!r}. Ничего не тронул."
    if not row_ids:
        return "🛑 Не передано ни одного row_id."
    if decision == "approved":
        cr = _require_consent(action="declutter_decision", tier=2,
                              manifest=None, user_reply=user_reply)
        if not cr.ok:
            return cr.reason
    if not declutter_sheet.sheet_configured():
        return ("🛑 Sheet-режим declutter не настроен (нужны env "
                "DECLUTTER_SHEET_ID и GSHEETS_SA_JSON).")
    try:
        rows = declutter_sheet.read_manifest_rows(manifest_id)
    except declutter_sheet.DeclutterSheetError as e:
        return f"🛑 Не могу прочитать таблицу разбора: {_redact_for_user(e)}"
    if not rows:
        return f"🛑 Манифест разбора {manifest_id} не найден в таблице."
    valid_ids = {int(r["row_id"]) for r in rows
                if str(r.get("row_id") or "").strip().isdigit()}
    apply_ids = [rid for rid in row_ids if rid in valid_ids]
    unknown = [rid for rid in row_ids if rid not in valid_ids]
    if not apply_ids:
        return (f"🛑 Ни один row_id не найден в манифесте {manifest_id}: "
                f"{row_ids}. Ничего не тронул.")
    try:
        declutter_sheet.batch_update_rows(
            [{"row_id": rid, "decision": decision} for rid in apply_ids])
    except declutter_sheet.DeclutterSheetError as e:
        return f"🛑 Не удалось записать decision в таблицу: {_redact_for_user(e)}"
    msg = f"✅ decision=\"{decision}\" проставлен для {len(apply_ids)} строк: {apply_ids}"
    if unknown:
        msg += f"\n⚠️ Не найдены в манифесте {manifest_id} (пропущены): {unknown}"
    return msg


# === MOVED-BLOCK END ===


# Две функции-заглушки исполнителя автоуборки и закомментированная
# регистрация в реестре: они существуют ТОЛЬКО ради автоуборки,
# поэтому уезжают вместе с ней (пункт 1.2.4.в, коммит 1).
# === MOVED-BLOCK BEGIN (server.py @ 0b3c04c62c93171106a294f7090f366b05ea43e7, lines 17659–17668) ===
def _rehash_declutter_manifest(m: Dict) -> str:
    return _manifest_object_hash("declutter", _dc_object_ids(m.get("actions") or {}))


async def _auto_execute_declutter(manifest_id: str, m: Dict) -> str:
    if m.get("persist") == "sheet":
        return await _execute_declutter_from_sheet(manifest_id)
    return await _execute_declutter_ram_impl(manifest_id, m)


# === MOVED-BLOCK END ===

# === MOVED-BLOCK BEGIN (server.py @ 0b3c04c62c93171106a294f7090f366b05ea43e7, lines 17749–17755) ===
# DISABLED 2026-08-04/05 together with the @mcp.tool() decorators above — the
# TG-button auto-execute poller (_tg_auto_execute_tick) dispatches through
# THIS registry directly, bypassing the MCP tool layer entirely. Commenting
# out only the decorators (first pass) left this path live: pressing the
# button on an already-computed declutter manifest would still have executed
# it for real. Both layers must stay disabled together.
# _register_auto_executor("execute_declutter", _rehash_declutter_manifest, _auto_execute_declutter)
# === MOVED-BLOCK END ===
