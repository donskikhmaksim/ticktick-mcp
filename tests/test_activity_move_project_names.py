"""Дефект №3 (живая приёмка 2026-08-07): лента активности печатала событие
перемещения голыми идентификаторами проектов.

    you moved to another list 6a755ff58f08e34527a29b31 → 6a752d718f083125df116c9d

Оба проекта живые и резолвятся — соседний `get_changes` в этом же файле уже
переводит id в имена хелпером `_v2_project_names()`. То есть инструмент был
под рукой и применён рядом, а в `T_MOVE` подставлялись сырые
`fromProjectId`/`toProjectId`.

Лента активности — это запись, по которой люди восстанавливают, что и куда
делось. 24 hex-символа в ней не читает никто, и «куда переехала задача»
остаётся неизвестным ровно в том месте, где на этот вопрос и приходят
отвечать.

Отдельно фиксируется НЕУДАЧНЫЙ резолвинг: если имя установить не удалось,
это надо сказать вслух рядом с id, а не показывать id молча (то же правило,
что для карточек подтверждения — см. tests/test_completed_task_display_name.py).

Через стенд tests/read_stand.py: настоящий v2-клиент, подменён только
транспорт; `_v2_project_names` НЕ подменяется — иначе тест проверял бы мок.
"""
import pytest

from tests import read_stand as rs

MOVE_EVENT = {
    "id": "act-move", "action": "T_MOVE",
    "when": rs._stamp(rs.TODAY, 12),
    "whoProfile": {"isMyself": True, "displayName": "Максим"},
    "fromProjectId": rs.P_WORK, "toProjectId": rs.P_HOME,
}
GHOST_PROJECT = "6a99deadbeefdeadbeefdead"


@pytest.fixture
def activity_stand(monkeypatch):
    def _wire(events):
        return rs.wire(monkeypatch, v2_kwargs={"activity": events})
    return _wire


async def test_move_event_names_both_projects(activity_stand):
    """Главный тест: в строке перемещения — ИМЕНА проектов."""
    activity_stand([MOVE_EVENT])

    out = await rs.call("get_task_activity", task_id=rs.TASK_ROOT,
                        project_id=rs.P_WORK)

    assert "Работа" in out, out
    assert "Дом" in out, out


async def test_move_event_does_not_print_bare_ids(activity_stand):
    """И тот же факт с другой стороны: голых идентификаторов в строке нет —
    иначе «имя добавили, id оставили рядом» прошло бы первый тест, а читать
    ленту было бы всё так же нечем."""
    activity_stand([MOVE_EVENT])

    out = await rs.call("get_task_activity", task_id=rs.TASK_ROOT,
                        project_id=rs.P_WORK)

    move_line = next(ln for ln in out.splitlines() if "moved to another list" in ln)
    assert rs.P_WORK not in move_line, move_line
    assert rs.P_HOME not in move_line, move_line


async def test_unresolvable_project_says_so_out_loud(activity_stand):
    """Резолвинг не удался — молчать нельзя: id остаётся видимым, но рядом
    сказано, что имя неизвестно, иначе id снова читается как «название»."""
    activity_stand([{**MOVE_EVENT, "toProjectId": GHOST_PROJECT}])

    out = await rs.call("get_task_activity", task_id=rs.TASK_ROOT,
                        project_id=rs.P_WORK)

    move_line = next(ln for ln in out.splitlines() if "moved to another list" in ln)
    assert "Работа" in move_line, move_line
    assert GHOST_PROJECT in move_line, "неизвестный id обязан остаться видимым"
    assert "имя неизвестно" in move_line.lower() or "unknown" in move_line.lower(), (
        f"неудачный резолвинг остался молчаливым:\n{move_line}")


async def test_other_events_unchanged(activity_stand):
    """Контроль: остальные события ленты этой правкой не задеты."""
    activity_stand(list(rs.ACTIVITY))

    out = await rs.call("get_task_activity", task_id=rs.TASK_ROOT,
                        project_id=rs.P_WORK)

    assert "completed" in out
    assert "moved to another column" in out
    assert "changed due date" in out
