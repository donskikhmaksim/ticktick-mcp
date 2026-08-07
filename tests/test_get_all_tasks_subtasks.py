"""get_all_tasks must not silently swallow subtasks.

Live acceptance (2026-08-07) caught it: the header promised 1477 tasks while
the body held 1234 lines and not a single `↳` nesting marker; project
«🎯 Goals» showed 18 tasks here against 87 from get_project_tasks, and a real
subtask («🗻 Q2 2026») was findable via search_tasks but absent from this
tool's output. Root cause: the v2 branch pre-filtered the per-project list to
parent-less tasks (`top = [t for t in ptasks if not t.get("parentId")]`) and
handed only THAT to format_task_tree — which is itself perfectly capable of
nesting children under parents, but never got the children.

The pre-existing regression test (tests/test_slice2_reads.py) could not catch
this: every task in its fixture was parent-less, so the dropping filter was a
no-op there.
"""
import pytest

import ticktick_mcp.src.server as s


class FakeV2:
    """Minimal stand-in for TickTickV2Client: only what get_all_tasks touches."""

    def __init__(self, tasks, project_profiles=None, inbox_id="inbox1"):
        self._tasks = tasks
        self._project_profiles = project_profiles or []
        self._inbox_id = inbox_id

    def get_open_tasks(self):
        return self._tasks

    def get_state(self):
        return {"projectProfiles": self._project_profiles, "inboxId": self._inbox_id}


@pytest.fixture(autouse=True)
def _fake_official_client(monkeypatch):
    # _ensure_official() only needs a truthy `ticktick`; the v2 path exercised
    # here makes no network calls.
    monkeypatch.setattr(s, "ticktick", object())


def _tasks_with_subtree():
    """One project: a parent, its child, its grandchild, plus a flat sibling."""
    return [
        {"id": "p", "title": "Родитель", "projectId": "p1"},
        {"id": "c", "title": "Ребёнок", "projectId": "p1", "parentId": "p"},
        {"id": "g", "title": "Внук", "projectId": "p1", "parentId": "c"},
        {"id": "s", "title": "Одиночка", "projectId": "p1"},
    ]


def _install(monkeypatch, tasks):
    monkeypatch.setattr(
        s, "ticktick_v2",
        FakeV2(tasks, project_profiles=[{"id": "p1", "name": "Работа"}]),
    )


async def test_subtasks_are_present_in_output(monkeypatch):
    _install(monkeypatch, _tasks_with_subtree())

    out = await s.get_all_tasks()

    for title in ("Родитель", "Ребёнок", "Внук", "Одиночка"):
        assert title in out, f"«{title}» пропала из вывода get_all_tasks"


async def test_subtasks_are_rendered_nested(monkeypatch):
    _install(monkeypatch, _tasks_with_subtree())

    out = await s.get_all_tasks()

    assert "↳" in out, "нет ни одного маркера вложенности — дерево не построено"
    child_line = next(ln for ln in out.splitlines() if "Ребёнок" in ln)
    grand_line = next(ln for ln in out.splitlines() if "Внук" in ln)
    parent_line = next(ln for ln in out.splitlines() if "Родитель" in ln)
    assert child_line.lstrip().startswith("↳")
    assert grand_line.lstrip().startswith("↳")
    indent = lambda ln: len(ln) - len(ln.lstrip())  # noqa: E731
    assert indent(parent_line) < indent(child_line) < indent(grand_line)


async def test_header_count_matches_rendered_lines(monkeypatch):
    """The promised totals must equal what is actually printed — the live
    symptom was a 1477-vs-1234 gap between the header and the body."""
    tasks = _tasks_with_subtree()
    _install(monkeypatch, tasks)

    out = await s.get_all_tasks()

    assert f"All open tasks ({len(tasks)}):" in out
    # per-project header count
    assert f"Работа ({len(tasks)} tasks)" in out
    task_lines = [ln for ln in out.splitlines() if "(id:" in ln]
    assert len(task_lines) == len(tasks), (
        f"обещано {len(tasks)} задач, напечатано {len(task_lines)}"
    )


async def test_subtask_whose_parent_lives_in_another_project_is_kept(monkeypatch):
    """A cross-project parentId must not make the child vanish either: it is
    shown at top level in its own project, exactly like format_task_tree's
    orphan handling."""
    tasks = [
        {"id": "p", "title": "Родитель", "projectId": "p1"},
        {"id": "c", "title": "Ребёнок в другом проекте", "projectId": "p2", "parentId": "p"},
    ]
    monkeypatch.setattr(
        s, "ticktick_v2",
        FakeV2(tasks, project_profiles=[{"id": "p1", "name": "Работа"},
                                        {"id": "p2", "name": "Дом"}]),
    )

    out = await s.get_all_tasks()

    assert "Ребёнок в другом проекте" in out
    assert "Дом (1 tasks)" in out
