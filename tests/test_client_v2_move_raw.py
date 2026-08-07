"""Второй живой инцидент 2026-08-07 00:26 PT, манифест `ed81c44f3018`:
identity-guard's official-API fallback (fix 0d59191) СРАБОТАЛ ПРАВИЛЬНО —
нашёл задачу «__AUTOTEST__dup-src», отсутствующую в v2-снимке «открытых
задач», разрешил move_tasks (`found` был непустым). Но живая проверка
показала: целевой проект остался пуст, перемещение НЕ состоялось, и
операция отчиталась «❌ НЕ перемещено 1 (остались на месте)».

Настоящая причина — глубже, чем identity-guard: `batch_move_tasks`
(`ticktick_v2_client.py`) сама СТРОИТ тело HTTP-запроса, заново разыскивая
`fromProjectId` каждой задачи через `self.get_open_tasks()` — тот же самый
v2-снимок, из которого задача уже была не найдена. Если её там всё ещё нет —
`if not t: continue` (см. batch_move_tasks) молча ВЫКИДЫВАЕТ эту задачу из
тела запроса вообще, БЕЗ единой ошибки: TickTick о ней даже не спрашивают, а
`id2error` остаётся пустым, так что вызывающий код в server.py не видит
никакого явного отказа — просто задача остаётся на месте.

Значит одного identity-guard'а (проверки ПЕРЕД мутацией) недостаточно: сама
МУТАЦИЯ должна использовать УЖЕ подтверждённый identity-guard'ом
`fromProjectId`, а не заново искать его тем же дефектным способом. Фикс:
`batch_move_tasks_raw(rows)` — caller передаёт `fromProjectId` явно (как уже
делает `set_task_parents(rows)` для вложения) — тело запроса строится БЕЗ
единого обращения к `get_open_tasks()`.

Тесты ниже:
  * доказательство бага в СТАРОМ `batch_move_tasks` — задача, отсутствующая
    в open_tasks, тихо выпадает из тела запроса (0 задач ушло в API);
  * доказательство фикса в `batch_move_tasks_raw` — та же задача, с явным
    fromProjectId, УХОДИТ в тело запроса без единого обращения к
    get_open_tasks()/get_state();
  * `batch_move_tasks_raw` по-прежнему не шлёт no-op перемещения (fromId ==
    toId пропускается — как и старый метод для собственной консистентности);
  * пустой список rows не делает HTTP-запрос вовсе."""
import pytest

import ticktick_mcp.src.server as s
from ticktick_mcp.src.ticktick_v2_client import TickTickV2Client


@pytest.fixture
def client():
    c = TickTickV2Client(token="tok")
    return c


def test_batch_move_tasks_silently_drops_a_task_missing_from_the_v2_snapshot(
        client, monkeypatch):
    """Живой баг, подтверждённый напрямую на клиенте: задача не в open_tasks
    → 0 запросов к TickTick для неё, БЕЗ единой ошибки."""
    monkeypatch.setattr(client, "get_open_tasks", lambda: [])  # тот самый пустой снимок

    posted = {}

    def fake_request(method, path, **kwargs):
        posted["called"] = (method, path, kwargs.get("json"))
        return {"id2etag": {}, "id2error": {}}

    client._request = fake_request

    resp = client.batch_move_tasks(["t1"], "p_new")

    assert "called" not in posted  # TickTick вообще не был спрошен
    assert "No tasks to move" in resp.get("message", "")


def test_batch_move_tasks_raw_sends_the_task_without_consulting_open_tasks(
        client, monkeypatch):
    """Фикс: raw-вариант отправляет задачу, используя ТОЛЬКО переданный
    fromProjectId — ни разу не обращаясь к get_open_tasks()/get_state()."""
    def _boom():
        raise AssertionError(
            "batch_move_tasks_raw НЕ ДОЛЖЕН обращаться к get_open_tasks() — "
            "весь смысл raw-варианта в том, чтобы не зависеть от того же "
            "снимка, из которого задача уже не была найдена")
    monkeypatch.setattr(client, "get_open_tasks", _boom)
    monkeypatch.setattr(client, "get_state", _boom)

    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        return {"id2etag": {}, "id2error": {}}

    client._request = fake_request

    resp = client.batch_move_tasks_raw(
        [{"taskId": "t1", "fromProjectId": "p_old", "toProjectId": "p_new"}])

    assert calls == [("POST", "/batch/taskProject",
                      [{"fromProjectId": "p_old", "toProjectId": "p_new",
                        "taskId": "t1"}])]
    assert resp == {"id2etag": {}, "id2error": {}}


def test_batch_move_tasks_raw_skips_noop_rows_already_in_target(client, monkeypatch):
    calls = []
    client._request = lambda method, path, **kw: (
        calls.append((method, path, kw.get("json"))) or {"id2etag": {}, "id2error": {}})

    resp = client.batch_move_tasks_raw([
        {"taskId": "already-there", "fromProjectId": "p1", "toProjectId": "p1"},
        {"taskId": "t2", "fromProjectId": "p1", "toProjectId": "p2"},
    ])

    (call,) = calls
    _, _, body = call
    assert [r["taskId"] for r in body] == ["t2"]
    assert resp["id2error"] == {}


def test_batch_move_tasks_raw_makes_no_request_when_rows_are_empty(client, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("не должно быть HTTP-запроса для пустого списка")
    client._request = _boom

    resp = client.batch_move_tasks_raw([])

    assert "No tasks to move" in resp["message"]


def test_batch_move_tasks_raw_makes_no_request_when_every_row_is_a_noop(
        client, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("не должно быть HTTP-запроса, когда все строки — no-op")
    client._request = _boom

    resp = client.batch_move_tasks_raw(
        [{"taskId": "t1", "fromProjectId": "p1", "toProjectId": "p1"}])

    assert "No tasks to move" in resp["message"]


# ---------------------------------------------------------------------------
# Сквозной тест на РЕАЛЬНОМ TickTickV2Client (не fake) через _move_tasks_impl
# — доказывает, что фикс реально долетает до самого HTTP-запроса, а не
# только до вызова с правильными аргументами на fake-объекте.
# ---------------------------------------------------------------------------

async def test_move_tasks_impl_posts_to_ticktick_for_a_task_the_v2_snapshot_never_had(
        monkeypatch):
    """Ровно ВТОРОЙ живой инцидент (2026-08-07 00:26 PT, манифест
    ed81c44f3018): identity-guard's официальный fallback находит задачу
    (found непустой), но /batch/check/0 (v2) НИКОГДА её не содержит — ни на
    guard'е, ни на пост-проверке. Со СТАРЫМ batch_move_tasks() ни один POST
    в TickTick не ушёл бы вовсе (задача тихо выпала бы из тела запроса) —
    именно так задача осталась на месте, а api_fail остался пустым (никакой
    явной ошибки от TickTick, потому что TickTick вообще не спросили).
    С фиксом (batch_move_tasks_raw, fromProjectId от identity-guard) POST
    реально уходит с правильным телом."""
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    monkeypatch.setattr(s, "_guard_project", lambda *a, **k: None)
    monkeypatch.setattr(s, "_v2_project_names",
                        lambda: {"p_new": "Новый", "p_old": "Старый"})
    monkeypatch.setattr(s, "_op_journal", lambda *a, **k: "move-test0003")
    monkeypatch.setattr(s.time, "sleep", lambda *a, **k: None)

    class _FakeOfficialClient:
        def get_task(self, project_id, task_id):
            if (project_id, task_id) == ("p_new", "t1"):
                return {"id": "t1", "title": "__AUTOTEST__dup-src",
                        "projectId": "p_new", "status": 0}
            return {"error": "404 Not Found"}

        def get_projects(self):
            return [{"id": "p_old"}, {"id": "p_new"}]
    monkeypatch.setattr(s, "ticktick", _FakeOfficialClient())

    real_v2 = TickTickV2Client(token="tok")
    posted = []

    def fake_request(method, path, **kwargs):
        if method == "GET" and path == "/batch/check/0":
            # v2's own sync snapshot NEVER has this task — the exact live
            # symptom (not a transient lag that a retry would outlast).
            return {"syncTaskBean": {"update": []}, "projectProfiles": [],
                    "tags": [], "inboxId": "inbox"}
        if method == "POST" and path == "/batch/taskProject":
            posted.append(kwargs.get("json"))
            return {"id2etag": {}, "id2error": {}}
        raise AssertionError(f"unexpected call {method} {path}")
    real_v2._request = fake_request
    monkeypatch.setattr(s, "ticktick_v2", real_v2)

    result = await s._move_tasks_impl(
        "Перемещаю", [{"taskId": "t1", "title": "__AUTOTEST__dup-src"}],
        "p_old", "Старый")

    # Главное доказательство: реальный POST ушёл в TickTick с явным
    # fromProjectId — СТАРЫЙ batch_move_tasks() с этим же (вечно пустым)
    # снимком не отправил бы вообще ничего (posted осталось бы []).
    assert posted == [[{"fromProjectId": "p_new", "toProjectId": "p_old",
                        "taskId": "t1"}]]
    assert "↷ Не найдены" not in result
