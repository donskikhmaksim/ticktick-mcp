"""checkin_habit had a real gap (docs/ALL_METHODS_MECHANICS.md §11): unlike
the journaled task tools, it never independently re-read TickTick after
writing — it just echoed back whatever it had sent. A "successful" call could
silently not persist (lag, wrong stamp, API swallow) and the tool would still
say "done" with zero verification. This locks in the fix: after the write,
checkin_habit does a SEPARATE, fresh get_habit_checkins call (not a reuse of
the pre-write duplicate-check read) and only claims success if the fresh read
actually shows the expected status/value. Output follows the unified markdown
template from docs/DESIGN_output_format.md.

No real network — the v2 client is faked.

2026-08-05: checkin_habit is now gated 🟡 (two calls, same tool name — see
docs/DESIGN_approval_gate.md and the removed tier-🟢 exemption). Every test
below now does call #1 (plan, no mutation) then call #2 (manifest_id +
user_reply="да") before asserting on the outcome that used to come straight
back from a single call."""
import re

import ticktick_mcp.src.server as s


def _extract_manifest_id(preview: str) -> str:
    m = re.search(r'manifest_id="([0-9a-f]+)"', preview)
    assert m, f"no manifest_id found in preview: {preview!r}"
    return m.group(1)


async def _checkin(*args, **kwargs):
    """Runs checkin_habit's full plan->execute gate cycle and returns the
    execute-phase result (what all the pre-existing assertions expect)."""
    preview = await s.checkin_habit(*args, **kwargs)
    assert "🛑" not in preview, f"plan phase unexpectedly refused: {preview!r}"
    mid = _extract_manifest_id(preview)
    return await s.checkin_habit(*args, manifest_id=mid, user_reply="да", **kwargs)


class FakeHabitsV2:
    """Minimal stand-in for TickTickV2Client: only what checkin_habit touches.

    write_effect controls what happens when checkin_habit() is called:
      - "normal":   persists exactly what was requested (post-verify should pass)
      - "silent":   write call succeeds but nothing is persisted (post-verify
                    must NOT find the entry -> "not confirmed")
      - "mismatch": persists a DIFFERENT status/value than requested
                    (post-verify must catch the discrepancy)
    raise_on_call, if set, makes the Nth call (1-indexed) to
    get_habit_checkins raise instead of returning.
    """

    def __init__(self, habits, checkins=None, write_effect="normal",
                 raise_on_call=None):
        self._habits = habits
        self._checkins = {k: list(v) for k, v in (checkins or {}).items()}
        self._write_effect = write_effect
        self._raise_on_call = raise_on_call
        self.get_habit_checkins_calls = 0
        self.checkin_habit_calls = 0

    def get_habits(self):
        return self._habits

    def get_habit_checkins(self, ids, after_stamp):
        self.get_habit_checkins_calls += 1
        if self._raise_on_call == self.get_habit_checkins_calls:
            raise RuntimeError("network blip")
        out = {}
        for hid in ids:
            out[hid] = [e for e in self._checkins.get(hid, [])
                        if e["checkinStamp"] > after_stamp]
        return out

    def checkin_habit(self, habit_id, date=None, status=2, value=None, goal=1.0):
        self.checkin_habit_calls += 1
        stamp = int(date.replace("-", ""))
        if self._write_effect == "silent":
            return {"ok": True}
        entries = self._checkins.setdefault(habit_id, [])
        if self._write_effect == "mismatch":
            written_status = 1 if status != 1 else 0
            entries.append({"checkinStamp": stamp, "status": written_status,
                            "value": 0.0, "goal": goal})
        else:
            v = value if value is not None else (goal if status == 2 else 0.0)
            entries.append({"checkinStamp": stamp, "status": status,
                            "value": v, "goal": goal})
        return {"ok": True}


HABITS = [{"id": "h1", "name": "Медитация", "goal": 1.0}]


def _wire(monkeypatch, fake):
    monkeypatch.setattr(s, "ticktick_v2", fake)


# ---------------------------------------------------------------------------
# post-verify: success
# ---------------------------------------------------------------------------

async def test_checkin_habit_success_is_post_verified(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-07-28")

    assert result.startswith("### ✅ Чек-ин привычки «Медитация»")
    assert "выполнено" in result
    assert "🧾" in result
    assert "подтверждено независимым чтением" in result
    # one read for the dup-check, one SEPARATE fresh read for post-verify
    assert fake.get_habit_checkins_calls == 2
    assert fake.checkin_habit_calls == 1


# ---------------------------------------------------------------------------
# post-verify: write silently didn't persist -> must not claim success
# ---------------------------------------------------------------------------

async def test_checkin_habit_silent_write_is_not_confirmed(monkeypatch):
    fake = FakeHabitsV2(HABITS, write_effect="silent")
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-07-28")

    assert result.startswith("### ⚠️")
    assert "НЕ подтверждён" in result
    assert "НЕ нашлось" in result
    # must NOT claim success anywhere in the response
    assert "### ✅" not in result
    assert "🧾" not in result


# ---------------------------------------------------------------------------
# post-verify: fresh read disagrees with what was requested
# ---------------------------------------------------------------------------

async def test_checkin_habit_mismatch_is_flagged(monkeypatch):
    fake = FakeHabitsV2(HABITS, write_effect="mismatch")
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-07-28", status=2)

    assert result.startswith("### ❌")
    assert "разошёлся с подтверждением" in result
    assert "запрошено" in result
    assert "при независимом перечитывании" in result


# ---------------------------------------------------------------------------
# post-verify: the independent re-read itself fails (network error etc)
# ---------------------------------------------------------------------------

async def test_checkin_habit_postverify_fetch_error_is_unconfirmed(monkeypatch):
    # 1st get_habit_checkins call = dup-check (must succeed), 2nd = post-verify
    # (must raise) so we can tell the two reads apart.
    fake = FakeHabitsV2(HABITS, raise_on_call=2)
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-07-28")

    assert result.startswith("### ⚠️")
    assert "проверка не выполнена" in result
    assert "НЕ подтверждён" in result
    assert fake.checkin_habit_calls == 1


# ---------------------------------------------------------------------------
# existing duplicate protection must survive untouched
# ---------------------------------------------------------------------------

async def test_checkin_habit_duplicate_is_refused_without_writing(monkeypatch):
    fake = FakeHabitsV2(HABITS, checkins={
        "h1": [{"checkinStamp": 20260728, "status": 2, "value": 1.0, "goal": 1.0}],
    })
    _wire(monkeypatch, fake)

    result = await _checkin("Медитация", "h1", date="2026-07-28")

    assert result.startswith("### ↷")
    assert "уже есть" in result
    assert fake.checkin_habit_calls == 0


# ---------------------------------------------------------------------------
# identity guard (wrong name for the id) must survive untouched
# ---------------------------------------------------------------------------

async def test_checkin_habit_identity_guard_blocks_wrong_name(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await _checkin("Бег", "h1", date="2026-07-28")

    assert result.startswith("🛑")
    assert fake.checkin_habit_calls == 0


async def test_checkin_habit_invalid_date_format_refused(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await s.checkin_habit("Медитация", "h1", date="2026-7-4")

    assert result.startswith("🛑")
    assert fake.checkin_habit_calls == 0


async def test_checkin_habit_invalid_status_refused(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await s.checkin_habit("Медитация", "h1", date="2026-07-28", status=5)

    assert result.startswith("🛑")
    assert fake.checkin_habit_calls == 0


# ---------------------------------------------------------------------------
# 2026-08-05: the gate itself — checkin_habit used to be tier-🟢 exempt from
# _require_consent entirely. These lock in that the two-phase gate is real:
# call #1 never mutates, call #2 hard-enforces user_reply, and the manifest
# is one-shot.
# ---------------------------------------------------------------------------

async def test_checkin_habit_call1_previews_nothing_written(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    preview = await s.checkin_habit("Медитация", "h1", date="2026-07-28")

    assert "manifest_id" in preview
    assert "«Медитация»" in preview
    assert fake.checkin_habit_calls == 0
    assert fake.get_habit_checkins_calls == 0


async def test_checkin_habit_call2_without_reply_is_refused_and_retryable(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    preview = await s.checkin_habit("Медитация", "h1", date="2026-07-28")
    mid = _extract_manifest_id(preview)

    refused = await s.checkin_habit("Медитация", "h1", date="2026-07-28",
                                    manifest_id=mid, user_reply="")
    assert "🛑" in refused
    assert fake.checkin_habit_calls == 0

    # manifest survives an empty (not explicitly negative) reply — retryable
    result = await s.checkin_habit("Медитация", "h1", date="2026-07-28",
                                   manifest_id=mid, user_reply="да, отметь")
    assert fake.checkin_habit_calls == 1
    assert "🛑" not in result


async def test_checkin_habit_explicit_no_burns_the_manifest(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    preview = await s.checkin_habit("Медитация", "h1", date="2026-07-28")
    mid = _extract_manifest_id(preview)

    refused = await s.checkin_habit("Медитация", "h1", date="2026-07-28",
                                    manifest_id=mid, user_reply="нет, погоди")
    assert "🛑" in refused
    assert fake.checkin_habit_calls == 0

    dead = await s.checkin_habit("Медитация", "h1", date="2026-07-28",
                                 manifest_id=mid, user_reply="да")
    assert "🛑" in dead
    assert fake.checkin_habit_calls == 0


async def test_checkin_habit_manifest_is_one_shot(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    preview = await s.checkin_habit("Медитация", "h1", date="2026-07-28")
    mid = _extract_manifest_id(preview)
    await s.checkin_habit("Медитация", "h1", date="2026-07-28",
                          manifest_id=mid, user_reply="да")
    assert fake.checkin_habit_calls == 1

    second = await s.checkin_habit("Медитация", "h1", date="2026-07-28",
                                   manifest_id=mid, user_reply="да")
    assert "🛑" in second
    assert fake.checkin_habit_calls == 1  # nothing new happened


async def test_checkin_habit_unknown_manifest_is_refused(monkeypatch):
    fake = FakeHabitsV2(HABITS)
    _wire(monkeypatch, fake)

    result = await s.checkin_habit("Медитация", "h1", date="2026-07-28",
                                   manifest_id="does-not-exist", user_reply="да")
    assert "🛑" in result
    assert fake.checkin_habit_calls == 0

    assert result.startswith("🛑")
    assert fake.checkin_habit_calls == 0
