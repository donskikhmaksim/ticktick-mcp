"""restore_tasks / _restore_tasks_impl: докстринг обещает «defaults to each
task's original list», но ночная QA-кампания (QA — автоматический прогон
проверок) нашла, что задача фактически попадает в Inbox. Причина: старый
post-verify (пере-проверка после мутации СВЕЖИМ запросом к сервису) сверял
только «задача снова среди открытых», но никогда не сверял, В КАКОМ именно
проекте — поэтому потерянный/проигнорированный пункт назначения на вызове
TickTick /trash/restore проходил полностью незамеченным.

Фикс: пункт назначения (override, иначе — собственный projectId задачи из
записи в корзине) считается ДО восстановления, а после восстановления сервер
заново читает живую задачу и сверяет её реальный projectId с этим ожиданием.
Расхождение чинится одним явным дополнительным перемещением (тем же вызовом,
что использует move_tasks); расхождение, пережившее и это — уходит в отчёт
как явное расхождение, а не молча растворяется в строке «успех».

Без реальной сети — v2-клиент замокан (мок — поддельная замена реального
клиента для теста), с переключателем, воспроизводящим замеченный баг
(restore игнорирует запрошенный пункт назначения), чтобы путь починки
проверялся напрямую, а не только счастливый путь, где «сырой» API и так
ведёт себя правильно."""
import json

import ticktick_mcp.src.server as s
from tests import read_stand as stand


class _FakeV2Restore:
    """Поддельный v2-клиент, покрывающий ровно то, что трогает
    _restore_tasks_impl: get_trash / batch_restore_tasks / batch_move_tasks,
    плюс no-op invalidate_cache, который дёргает _guard_project при заданном
    to_project_id."""

    def __init__(self, live, trash, *, restore_drops_destination=False,
                fallback_project="inbox", move_is_noop=False):
        self.live = live
        self.trash = trash
        self.restore_drops_destination = restore_drops_destination
        self.fallback_project = fallback_project
        self.move_is_noop = move_is_noop
        self.calls = []

    def invalidate_cache(self):
        pass

    def get_trash(self, limit=500):
        return list(self.trash.values())[:limit]

    def batch_restore_tasks(self, ids, to_project_id=None):
        self.calls.append(("restore", list(ids), to_project_id))
        for tid in ids:
            entry = self.trash.get(tid)
            if not entry:
                continue
            restored = dict(entry)
            if self.restore_drops_destination:
                # Воспроизводит реальный баг: вызов /trash/restore
                # полностью игнорирует fromProjectId/toProjectId и всегда
                # кидает задачу в фиксированный проект независимо от того,
                # что было запрошено.
                restored["projectId"] = self.fallback_project
            elif to_project_id:
                restored["projectId"] = to_project_id
            self.live[tid] = restored
        return {}

    def batch_move_tasks_raw(self, rows):
        ids = [r["taskId"] for r in rows]
        to_project_id = rows[0]["toProjectId"] if rows else None
        self.calls.append(("move", ids, to_project_id))
        for r in rows:
            tid = r["taskId"]
            if tid in self.live and not self.move_is_noop:
                self.live[tid]["projectId"] = r["toProjectId"]
        return {}


def _wire(monkeypatch, live, trash, names=None, **fake_kwargs):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    # _open_by_id используется (fresh=True) и для первого post-verify, и для
    # повторной проверки после починочного перемещения — свежий снимок
    # каждый раз отражает то, что поддельный клиент только что изменил.
    monkeypatch.setattr(s, "_open_by_id", lambda fresh=False: dict(live))
    monkeypatch.setattr(s, "_v2_project_names", lambda: names or {
        "p1": "Работа", "p2": "Личное", "inbox": "Inbox"})
    fake = _FakeV2Restore(live, trash, **fake_kwargs)
    monkeypatch.setattr(s, "ticktick_v2", fake)
    return fake


# ---- счастливый путь: API и так уважает пункт назначения ------------------

async def test_restore_lands_in_original_project_when_api_honours_destination(
        monkeypatch):
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    live = {}
    fake = _wire(monkeypatch, live, trash)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])

    assert live["t1"]["projectId"] == "p1"
    assert "снова среди открытых, в нужном списке" in result
    assert "⚠️" not in result
    assert [c for c in fake.calls if c[0] == "move"] == []


# ---- собственно баг: restore роняет в Inbox, должна сработать починка -----

async def test_restore_dropped_to_inbox_is_auto_corrected_via_followup_move(
        monkeypatch):
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    live = {}
    fake = _wire(monkeypatch, live, trash, restore_drops_destination=True,
                fallback_project="inbox")

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])

    # Доказывает, что починка реально сработала: без неё задача застряла бы
    # в "inbox" (это и есть баг, ради которого весь этот файл).
    move_calls = [c for c in fake.calls if c[0] == "move"]
    assert move_calls == [("move", ["t1"], "p1")]
    assert live["t1"]["projectId"] == "p1"
    assert "снова среди открытых, в нужном списке" in result
    assert "⚠️" not in result


async def test_multiple_tasks_dropped_to_inbox_from_different_lists_are_all_fixed(
        monkeypatch):
    trash = {
        "t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"},
        "t2": {"id": "t2", "title": "Позвонить маме", "projectId": "p2"},
    }
    live = {}
    fake = _wire(monkeypatch, live, trash, restore_drops_destination=True,
                fallback_project="inbox")

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [
            {"taskId": "t1", "title": "Купить молоко"},
            {"taskId": "t2", "title": "Позвонить маме"},
        ])

    assert live["t1"]["projectId"] == "p1"
    assert live["t2"]["projectId"] == "p2"
    move_calls = {c[2]: set(c[1]) for c in fake.calls if c[0] == "move"}
    assert move_calls == {"p1": {"t1"}, "p2": {"t2"}}
    assert "Восстановлено из корзины 2" in result
    assert "⚠️" not in result


# ---- явный override побеждает "исходный список" даже против кривого API ---

async def test_explicit_override_wins_and_is_corrected_if_api_ignores_it(
        monkeypatch):
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    live = {}
    # API упрямо возвращает задачу в её ИСХОДНЫЙ список независимо от
    # override — override всё равно должен победить после починки.
    fake = _wire(monkeypatch, live, trash, restore_drops_destination=True,
                fallback_project="p1")

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}], "p2")

    move_calls = [c for c in fake.calls if c[0] == "move"]
    assert move_calls == [("move", ["t1"], "p2")]
    assert live["t1"]["projectId"] == "p2"
    assert "снова среди открытых, в нужном списке" in result


# ---- сама починка не сработала → явное расхождение, никогда ложный "ok" ---

async def test_move_fixup_failure_is_reported_as_discrepancy_not_success(
        monkeypatch):
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    live = {}
    _wire(monkeypatch, live, trash, restore_drops_destination=True,
                fallback_project="inbox", move_is_noop=True)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])

    assert live["t1"]["projectId"] == "inbox"  # починочное перемещение так и не применилось
    assert "НЕ в исходный/запрошенный список" in result
    assert "Inbox" in result
    assert "Работа" in result
    assert "снова среди открытых, в нужном списке" not in result


# ---- в записи корзины нет узнаваемого поля проекта -------------------------

async def test_missing_project_field_in_trash_entry_is_reported_not_silently_ok(
        monkeypatch):
    trash = {"t1": {"id": "t1", "title": "Купить молоко"}}  # нет projectId/projectID/listId
    live = {}
    fake = _wire(monkeypatch, live, trash)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])

    assert "исходный список не удалось определить" in result
    assert "снова среди открытых, в нужном списке" not in result
    assert [c for c in fake.calls if c[0] == "move"] == []


# ---- ранее существовавшие гарды не задеты переписыванием -------------------

async def test_title_mismatch_refuses_and_touches_nothing(monkeypatch):
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    live = {}
    fake = _wire(monkeypatch, live, trash)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Совсем другое"}])

    assert "🛑" in result
    assert live == {}
    assert fake.calls == []


async def test_task_absent_from_trash_is_reported_and_untouched(monkeypatch):
    fake = _wire(monkeypatch, {}, {})

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "ghost", "title": "Призрак"}])

    assert "Не найдены в корзине" in result
    assert fake.calls == []


# ---- журнал хранит ожидаемый проект, operation_report это подтверждает -----

async def test_journal_records_expected_project_and_report_confirms_success(
        monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    live = {}
    _wire(monkeypatch, live, trash)

    result = await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])
    assert "operation_report" in result

    journal_path = tmp_path / "deletion_journal.jsonl"
    rec = json.loads(journal_path.read_text(encoding="utf-8").strip())
    assert rec["op"] == "restore"
    assert rec["items"][0]["expect"] == {"projectId": "p1"}

    report = s._build_operation_report(rec["record"])
    assert "снова среди открытых, в нужном списке" in report
    # Формат строки «Итог» расширен правкой fix/qa-c-report: добавлен
    # счётчик ⚠️ «не проверено» плюс отдельная строка «Статус операции».
    assert "Итог: ✅ 1 подтверждено, ⚠️ 0 не проверено, ❌ 0 расхождений" in report
    assert "Статус операции:" in report


async def test_operation_report_flags_a_restore_that_ended_up_in_the_wrong_project(
        monkeypatch, tmp_path):
    """Даже если собственная починка _restore_tasks_impl каким-то образом
    это пропустила (или задачу потом переместили ещё раз) — независимая
    пере-проверка operation_report всё равно должна поймать живой projectId,
    не совпадающий с тем, что записано в журнале, а не просто повторить
    журнал слово в слово."""
    monkeypatch.setattr(s, "_JOURNAL_DIR", str(tmp_path))
    trash = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    live = {}
    _wire(monkeypatch, live, trash)

    await s._restore_tasks_impl(
        "Восстанавливаю", [{"taskId": "t1", "title": "Купить молоко"}])

    journal_path = tmp_path / "deletion_journal.jsonl"
    rec = json.loads(journal_path.read_text(encoding="utf-8").strip())

    # Имитируем, что задачу впоследствии переместили куда-то ещё.
    live["t1"]["projectId"] = "inbox"

    report = s._build_operation_report(rec["record"])
    assert "не тот список" in report
    assert "Inbox" in report
    assert "Работа" in report


# ---- ветка "restore" в _verify_item напрямую -------------------------------
#
# _verify_item возвращает КОРТЕЖ (status, line), где status — строго
# "ok"/"warn"/"bad" (правка из fix/qa-c-report: раньше статус пытались
# восстановить парсингом эмодзи в начале строки, и ⚠️-расхождения терялись —
# это и был баг «расхождений: 0» при 4 напечатанных пунктах). Поэтому тесты
# ниже проверяют И статус (по нему считается статистика), И текст строки.

def test_verify_item_flags_restore_that_landed_in_wrong_project():
    names = {"p1": "Работа", "inbox": "Inbox"}
    live_map = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "inbox"}}
    item = {"taskId": "t1", "title": "Купить молоко", "expect": {"projectId": "p1"}}

    status, line = s._verify_item("restore", item, live_map, names)

    assert status == "warn"
    assert "⚠️" in line
    assert "не тот список" in line
    assert "Inbox" in line
    assert "Работа" in line


def test_verify_item_confirms_restore_in_correct_project():
    names = {"p1": "Работа"}
    live_map = {"t1": {"id": "t1", "title": "Купить молоко", "projectId": "p1"}}
    item = {"taskId": "t1", "title": "Купить молоко", "expect": {"projectId": "p1"}}

    status, line = s._verify_item("restore", item, live_map, names)

    assert status == "ok"
    assert "✅" in line
    assert "нужном списке" in line


def test_verify_item_restore_not_reappeared_is_still_a_plain_failure(monkeypatch):
    """Задачи нет НИГДЕ — ни среди открытых, ни среди завершённых, ни в
    корзине: это по-прежнему ❌.

    Тест переписан 2026-08-07 вместе с фиксом «восстановление рапортует
    „не восстановлено" об успехе»: «нет среди открытых» само по себе больше
    не приговор (завершённая задача возвращается из корзины завершённой и в
    открытых не появляется НИКОГДА), поэтому проверка теперь идёт против
    живого состояния, а не против пустого словаря открытых. Приговор здесь
    выносится по РЕЗУЛЬТАТУ поиска во всех трёх лентах."""
    names = {"p1": "Работа"}
    item = {"taskId": "t1", "title": "Купить молоко", "expect": {"projectId": "p1"}}
    stand.wire(monkeypatch, v2_kwargs={"trash": [], "completed": []})

    status, line = s._verify_item("restore", item, {}, names)

    assert status == "bad"
    assert "❌" in line
    assert "не подтвердилось" in line


def test_verify_item_restore_says_unverified_when_state_cannot_be_read(monkeypatch):
    """А вот когда живое состояние прочитать НЕ УДАЛОСЬ — вердикт ⚠️ «не
    подтверждено», не ❌: «не смогли проверить» и «операция провалилась» —
    разные вещи (та же политика, что у ветки delete_project выше)."""
    names = {"p1": "Работа"}
    item = {"taskId": "t1", "title": "Купить молоко", "expect": {"projectId": "p1"}}
    monkeypatch.setattr(s, "ticktick_v2", None)

    status, line = s._verify_item("restore", item, {}, names)

    assert status == "warn"
    assert "НЕ ПОДТВЕРЖДЁН" in line
