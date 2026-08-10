"""Три ТИХИХ отказа — сервер отвечал так, будто всё хорошо, а действие не
происходило (аудит 2026-08-06, находки #104 / #100 / #102).

Общее у всех трёх: неудача не видна ни в логе, ни модели, ни человеку.
Поэтому здесь проверяется не «функция вернула что-то», а именно ОТЛИЧИМОСТЬ
неуспеха от успеха.

  #104 — отметка «исполнено» ставилась ДО исполнения. Надгробие (tombstone)
         манифеста писалось с причиной "tg_auto_executed" в момент ЗАХВАТА
         плана поллером; упавшее следом исполнение ничего не переписывало, и
         следующий вызов получал «✅ УЖЕ исполнен, ничего не потеряно» на
         операцию, которой не было. Теперь три состояния различимы:
         ждёт нажатия → захвачен (исход неизвестен) → выполнено / НЕ выполнено.

  #100 — «вынуть проект из папки» не работало никогда: сентинел "NONE"
         уходил в TickTick буквальной строкой вместо `groupId: null`.
         Единственный тест, покрывавший цепочку, использовал фейк, который
         сам переводил "NONE" в None (tests/test_tier0_gate_conversion.py) —
         то есть делал за клиента ровно ту работу, которой в клиенте не было.

  #102 — сверка `automation_key` падала на не-ASCII: `hmac.compare_digest`
         с двумя `str` требует ASCII-only и иначе бросает TypeError. Русская
         буква или эмодзи в присланном ключе рушили гейт вместо того, чтобы
         дать по нему решение; не-ASCII в самом MCP_SECRET убивал headless-
         автоматику целиком.

Ни сети, ни Postgres: Telegram/Postgres-функции `tg_approval` и HTTP-слой
v2-клиента подменяются, как в остальных тестах этого репозитория.
"""
import asyncio
import re
import time

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg
from ticktick_mcp.src.ticktick_v2_client import TickTickV2Client


_MID_RE = re.compile(r"Манифест `([0-9a-f]{6,})`")


def _mid_of(text):
    m = _MID_RE.search(text)
    assert m, f"в превью нет id манифеста:\n{text}"
    return m.group(1)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Жёсткий запрет на живой TickTick из этого файла. `_ensure_official()`
    /`_ensure_ready()` при незаполненном клиенте ЛЕНИВО зовут
    `initialize_client()`, а тот реально ходит в api.ticktick.com — забыть
    подменить один хелпер достаточно, чтобы юнит-тест начал стучаться в
    боевой API. Здесь это падение теста, а не тихий сетевой вызов."""
    def _forbidden():
        raise AssertionError(
            "тест попытался инициализировать живой TickTick-клиент — "
            "подмени _ensure_official/_ensure_ready в самом тесте")
    monkeypatch.setattr(s, "initialize_client", _forbidden)


@pytest.fixture(autouse=True)
def _isolate_manifests():
    """_MANIFESTS и _MANIFEST_TOMBSTONES — глобалы модуля, общие на всю
    сессию тестов. Снимок/восстановление держит файл самодостаточным при
    любом порядке запуска."""
    before = dict(s._MANIFESTS)
    tombs = dict(s._MANIFEST_TOMBSTONES)
    s._MANIFESTS.clear()
    s._MANIFEST_TOMBSTONES.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)
    s._MANIFEST_TOMBSTONES.clear()
    s._MANIFEST_TOMBSTONES.update(tombs)


def _tg_on(monkeypatch, allowlist=None):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=allowlist, ttl_s=3600))


def _notify_ok(monkeypatch):
    monkeypatch.setattr(tg, "notify_plan", lambda cfg, mid, preview, tool: (True, ""))


def _approved(monkeypatch, mid, message_id=7):
    """Пакетное чтение статусов (единственный путь поллера в базу): наш план
    подтверждён кнопкой, чужие — «строки нет».

    Второй параметр (`lost_scan_since_ms`) появился вместе с поиском
    ПОТЕРЯННЫХ планов — строк, решение по которым в базе есть, а манифеста в
    памяти уже нет (перезапуск сервиса). Поллер теперь зовёт эту функцию с
    двумя аргументами ВСЕГДА, поэтому заглушка обязана их принимать: с одним
    она бросала TypeError, поллер ловил его как «база недоступна» и выходил
    из прохода — тесты этого файла падали не по своей теме, а на обвязке.
    Здесь потерянных планов нет, поэтому аргумент игнорируется."""
    monkeypatch.setattr(tg, "get_tg_approvals", lambda ids, lost_scan_since_ms=None: {
        i: {"status": "APPROVED", "expires_at": tg._now_ms() + 3_600_000,
            "chat_id": "c1", "message_id": message_id}
        for i in ids if i == mid})
    monkeypatch.setattr(tg, "check_approval",
                        lambda i: "approved" if i == mid else "none")


def _collect_reports(monkeypatch):
    """Перехват ИТОГА, который поллер публикует после исполнения.

    Каналов публикации стало три (ветка отчётов в группу «MCP Отчёты»):
    ПОЛНЫЙ отчёт уходит в группу-архив (`post_report_to_group`), при неудаче —
    фолбэком в личку (`send_message_chunked`), а в исходное сообщение с
    кнопками вписывается КОРОТКАЯ сводка (`summarize_in_owner_chat`).
    Возвращает (сводки, архив): подробности (текст исключения, трассировку
    смысла) теперь надо искать во ВТОРОМ, потому что в личку намеренно уходит
    только «выполнено / не выполнено» без деталей — личный чат не захламляют.
    Сетевые каналы подменяются все, иначе тест реально стучится в
    api.telegram.org (это было видно в логах: «Not Found» от editMessageText).
    """
    reports = []
    archive = []
    monkeypatch.setattr(tg, "report_auto_execution_result",
                        lambda cfg, chat_id, message_id, text: reports.append(text))
    monkeypatch.setattr(tg, "summarize_in_owner_chat",
                        lambda cfg, chat_id, message_id, text:
                        reports.append(text) or True)
    monkeypatch.setattr(tg, "post_report_to_group",
                        lambda cfg, mid, md, *, tool, verdict:
                        archive.append(md)
                        or tg.ReportDelivery([2], 1, 1, True))
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, md, **kw: tg.SendResult(True, [1], "", 1))
    return reports, archive


def _plant_delete_manifest(mid="silent-104"):
    s._MANIFESTS[mid] = {
        "kind": "delete", "consumed": False, "created": time.monotonic(),
        "object_hash": s._manifest_object_hash("delete", ["t1"]),
        "summary": "test", "items": [{
            "taskId": "t1", "projectId": "p1", "title": "Купить молоко",
            "project": "Покупки", "snapshot": {},
        }],
    }
    return mid


def _reason(mid):
    return (s._MANIFEST_TOMBSTONES.get(mid) or {}).get("reason")


# ═══════════════════════════════════════════════════════════════════════════
# #104 — «исполнено» пишется ТОЛЬКО после подтверждённого успеха
# ═══════════════════════════════════════════════════════════════════════════

def test_executor_raises_after_the_press_is_not_marked_executed(monkeypatch):
    """САМЫЙ ВАЖНЫЙ сценарий находки: кнопка нажата, поллер забрал план,
    исполнение УПАЛО. Отметка «исполнено» появляться не должна."""
    _tg_on(monkeypatch)
    mid = _plant_delete_manifest()
    _approved(monkeypatch, mid)
    reports, archive = _collect_reports(monkeypatch)

    async def boom(manifest_id, m):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(s._AUTO_EXECUTORS["delete_tasks"], "execute", boom)

    asyncio.run(s._tg_auto_execute_tick())

    assert _reason(mid) != s._TOMBSTONE_EXECUTED, (
        "план помечен исполненным, хотя исполнение упало")
    assert _reason(mid) == s._TOMBSTONE_FAILED
    # и человеку/модели это видно словами, а не только статусом
    msg = s._manifest_gone_msg(mid, "DEFAULT")
    assert "УЖЕ исполнен" not in msg
    assert "НЕ завершилось успехом" in msg
    assert "kaboom" in msg
    # Отчёт в Telegram по-прежнему уходит и по-прежнему говорит об ошибке —
    # но с появлением группы «MCP Отчёты» он РАЗДЕЛЁН на два канала: в личку
    # владельцу идёт короткое «не выполнено» (личный чат намеренно не
    # захламляют деталями), а полный текст с самим исключением ложится в
    # архив. Проверяем оба, иначе «где-то же написано» осталось бы
    # непроверенным ровно в том канале, который для разбора и предназначен.
    assert len(reports) == 1 and "не выполнено" in reports[0]
    assert len(archive) == 1 and "kaboom" in archive[0]


def test_executor_refusing_with_a_string_is_not_marked_executed(monkeypatch):
    """Исполнители умеют отказываться СТРОКОЙ, без исключения («🛑 Автоис-
    полнение отменено: …»). Это тоже не исполнение."""
    _tg_on(monkeypatch)
    mid = _plant_delete_manifest("silent-104-refusal")
    _approved(monkeypatch, mid)
    _collect_reports(monkeypatch)   # каналы публикации замолчали, содержимое не проверяем

    async def refuse(manifest_id, m):
        return ("🛑 Автоисполнение отменено: манифест не в формате плана — "
                "ничего не удалено.")

    monkeypatch.setattr(s._AUTO_EXECUTORS["delete_tasks"], "execute", refuse)

    asyncio.run(s._tg_auto_execute_tick())

    assert _reason(mid) == s._TOMBSTONE_FAILED
    assert "УЖЕ исполнен" not in s._manifest_gone_msg(mid, "DEFAULT")


def test_executor_timeout_after_the_press_is_not_marked_executed(monkeypatch):
    """Таймаут — тот же класс: нажато, но НЕ подтверждено, что выполнено."""
    _tg_on(monkeypatch)
    monkeypatch.setattr(s, "_TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S", 0.01)
    mid = _plant_delete_manifest("silent-104-timeout")
    _approved(monkeypatch, mid)
    _collect_reports(monkeypatch)   # каналы публикации замолчали, содержимое не проверяем

    async def slow(manifest_id, m):
        await asyncio.sleep(5)
        return "готово"

    monkeypatch.setattr(s._AUTO_EXECUTORS["delete_tasks"], "execute", slow)

    asyncio.run(s._tg_auto_execute_tick())

    assert _reason(mid) == s._TOMBSTONE_FAILED
    assert "УЖЕ исполнен" not in s._manifest_gone_msg(mid, "DEFAULT")


def test_successful_execution_is_marked_executed(monkeypatch):
    """Обратная сторона: настоящий успех обязан по-прежнему читаться как
    «уже исполнено», иначе фикс просто ломает полезный ответ."""
    _tg_on(monkeypatch)
    mid = _plant_delete_manifest("silent-104-ok")
    _approved(monkeypatch, mid)
    reports, archive = _collect_reports(monkeypatch)

    async def ok(manifest_id, m):
        return "### ✅ Удалено 1 задание (проверено)."

    monkeypatch.setattr(s._AUTO_EXECUTORS["delete_tasks"], "execute", ok)

    asyncio.run(s._tg_auto_execute_tick())

    assert _reason(mid) == s._TOMBSTONE_EXECUTED
    assert "УЖЕ исполнен" in s._manifest_gone_msg(mid, "DEFAULT")
    assert len(reports) == 1


def test_claimed_state_differs_from_both_waiting_and_executed():
    """«Нажато, но ещё не выполнено» — самостоятельное состояние: не «ждёт
    нажатия» (там надгробия нет вовсе) и не «выполнено»."""
    mid = _plant_delete_manifest("silent-104-claimed")
    waiting = s._manifest_gone_msg(mid, "DEFAULT")
    assert waiting == "DEFAULT"          # ждёт нажатия: сказать нечего

    assert s._consume_manifest_for_auto_execute(mid) is not None
    claimed = s._manifest_gone_msg(mid, "DEFAULT")

    assert _reason(mid) == s._TOMBSTONE_CLAIMED
    assert claimed != waiting
    assert "УЖЕ исполнен" not in claimed
    assert "исполняется" in claimed


async def test_model_calling_execute_after_a_failed_auto_run_is_not_told_done(
        monkeypatch):
    """Сквозной путь целиком, как это видит модель: план → кнопка → поллер
    упал → модель честно зовёт тот же инструмент. Раньше здесь приходило
    «✅ УЖЕ исполнен, ничего не потеряно» — то есть человеку сообщили бы
    «сделано» про несделанное."""
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    _tg_on(monkeypatch)
    _notify_ok(monkeypatch)
    _collect_reports(monkeypatch)   # каналы публикации замолчали, содержимое не проверяем

    async def boom(summary, tasks, **extra):
        raise RuntimeError("TickTick вернул 500")

    monkeypatch.setattr(s, "_complete_tasks_impl", boom)

    preview = await s.complete_tasks.direct(
        summary="Закрываю 1",
        tasks=[{"taskId": "t1", "title": "A", "projectId": "p1"}])
    mid = _mid_of(preview)
    _approved(monkeypatch, mid)

    await s._tg_auto_execute_tick()

    out = await s.complete_tasks.direct(
        summary="Закрываю 1",
        tasks=[{"taskId": "t1", "title": "A", "projectId": "p1"}],
        manifest_id=mid, user_reply="да")

    assert "УЖЕ исполнен" not in out, f"модели сказали «сделано»:\n{out}"
    assert "НЕ завершилось успехом" in out
    assert "500" in out


# ═══════════════════════════════════════════════════════════════════════════
# #100 — разгруппировка проекта реально доходит до TickTick
# ═══════════════════════════════════════════════════════════════════════════

class _FakeTickTickHttp:
    """Мини-подделка v2-эндпоинта, ведущая себя как валидирующий сервер:
    `groupId: null` = «без папки» и принимается; ссылка на НЕсуществующую
    группу игнорируется (проект остаётся там, где был) — ровно та ситуация,
    в которой буквальная строка "NONE" превращалась в тихий отказ."""

    def __init__(self):
        self.projects = [{"id": "p1", "name": "Проект", "groupId": "g1"}]
        self.groups = [{"id": "g1", "name": "Папка"}]
        self.seen_updates = []

    def request(self, method, path, **kwargs):
        if method == "GET":
            return {"projectProfiles": [dict(p) for p in self.projects],
                    "projectGroups": [dict(g) for g in self.groups],
                    "tags": [], "syncTaskBean": {"update": []}}
        body = kwargs.get("json") or {}
        for upd in body.get("update", []):
            self.seen_updates.append(dict(upd))
            live = next((p for p in self.projects if p["id"] == upd.get("id")), None)
            if live is None:
                continue
            gid = upd.get("groupId")
            if gid is None:
                live["groupId"] = None
            elif any(g["id"] == gid for g in self.groups):
                live["groupId"] = gid
            # иначе: ссылка в никуда — поле игнорируется, папка не меняется
        return {}


def _client_on(fake):
    c = TickTickV2Client(token="t")
    c._request = fake.request
    return c


def test_ungroup_sends_null_group_id_not_the_literal_sentinel():
    fake = _FakeTickTickHttp()
    c = _client_on(fake)

    c.move_project_to_group("p1", "NONE")

    assert fake.seen_updates, "запрос вообще не ушёл"
    assert fake.seen_updates[-1]["groupId"] is None, (
        "сентинел ушёл в TickTick как есть — разгруппировки не будет")
    assert fake.projects[0]["groupId"] is None


def test_empty_group_id_also_means_ungroup():
    for empty in ("", None):
        fake = _FakeTickTickHttp()
        _client_on(fake).move_project_to_group("p1", empty)
        assert fake.seen_updates[-1]["groupId"] is None, (
            f"пустое значение {empty!r} не сработало как «без папки»")


def test_real_group_id_still_moves_into_that_group():
    fake = _FakeTickTickHttp()
    fake.groups.append({"id": "g2", "name": "Другая"})
    c = _client_on(fake)

    c.move_project_to_group("p1", "g2")

    assert fake.seen_updates[-1]["groupId"] == "g2"
    assert fake.projects[0]["groupId"] == "g2"


async def test_tool_level_ungroup_reports_success_only_when_it_happened(monkeypatch):
    """Сквозь настоящий `_move_project_to_group_impl` и настоящий клиент —
    подделан только HTTP. До фикса пост-проверка честно ловила, что проект
    остался в папке, и тул отвечал «❌ НЕ переместился»: операция была
    невыполнима в принципе."""
    fake = _FakeTickTickHttp()
    monkeypatch.setattr(s, "ticktick_v2", _client_on(fake))
    monkeypatch.setattr(s, "_guard_project", lambda *a, **kw: None)
    monkeypatch.setattr(s, "_v2_project_names", lambda: {"p1": "Проект"})

    out = await s._move_project_to_group_impl("Проект", "p1", "NONE")

    assert fake.projects[0]["groupId"] is None, "проект остался в папке"
    assert "❌" not in out and "🛑" not in out, out
    assert "без папки" in out and "проверено" in out


# ═══════════════════════════════════════════════════════════════════════════
# #102 — сверка ключа автоматизации переживает кириллицу и эмодзи
# ═══════════════════════════════════════════════════════════════════════════

def test_non_ascii_key_is_rejected_not_crashed():
    """Кириллица/эмодзи в присланном ключе — это «ключ не подошёл», а не
    исключение из гейта."""
    for junk in ("ключ", "секрет🔑", "пароль-как-строка"):
        assert s._automation_key_matches(junk) is False


def test_non_ascii_secret_still_authenticates_automation(monkeypatch):
    """И наоборот: если сам MCP_SECRET не-ASCII, автоматика обязана
    проходить. Раньше падала ЛЮБАЯ проверка ключа на таком сервере."""
    monkeypatch.setattr(s, "SECRET", "секретный-ключ-🔑")

    assert s._automation_key_matches("секретный-ключ-🔑") is True
    assert s._automation_key_matches("другой-ключ") is False
    assert s._automation_key_matches("") is False


def test_consent_gate_survives_a_non_ascii_key():
    """Гейт должен ВЕРНУТЬ решение, а не бросить TypeError."""
    m = {"kind": "delete", "consumed": False}
    cr = s._require_consent(action="delete", tier=2, manifest=m,
                            user_reply="", automation_key="ключ-🔑")
    assert cr.ok is False


def test_consent_gate_accepts_a_non_ascii_key_that_matches(monkeypatch):
    monkeypatch.setattr(s, "SECRET", "секрет-🔑")
    cr = s._require_consent(action="delete", tier=2, manifest=None,
                            user_reply="", automation_key="секрет-🔑")
    assert cr.ok is True and cr.reason == "automation_key"


async def test_direct_create_with_non_ascii_key_answers_instead_of_raising(
        monkeypatch):
    """Сквозной путь тула: не-ASCII ключ даёт человеко-читаемый отказ, а не
    исключение наружу."""
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    out = await s.create_tasks("Создаю", [{"title": "A", "projectId": "p1"}],
                               automation_key="ключ-🔑")
    assert out.startswith("🛑")
    assert "автоматики" in out


async def test_direct_create_with_matching_non_ascii_key_is_allowed(monkeypatch):
    monkeypatch.setattr(s, "SECRET", "секрет-🔑")
    called = []

    async def _impl(summary, tasks):
        called.append((summary, tasks))
        return "### ✅ создано"

    monkeypatch.setattr(s, "_create_tasks_impl", _impl)

    out = await s.create_tasks.direct("Создаю", [{"title": "A", "projectId": "p1"}],
                               automation_key="секрет-🔑")

    assert called, f"валидный не-ASCII ключ не пропустили: {out}"


# ═══════════════════════════════════════════════════════════════════════════
# Д7 (2026-08-09) — надгробие ставится по ВЕРДИКТУ, а не по первому символу
# ═══════════════════════════════════════════════════════════════════════════
#
# Отчёт ручного разбора начинается с нейтрального «### 🧾», а внутри него
# строка «✅ Выполнено 0 из 3» — с галочкой РЯДОМ С НУЛЁМ. Признак «первый
# символ отчёта не 🛑/❌» на таком тексте означает ровно ничего, но именно по
# нему раньше решалось, писать ли «выполнено».


_ZERO_OF_THREE = "\n".join([
    "### 🧾 Ручной разбор — итог",
    "_Разбираю входящие_",
    "",
    "✅ Выполнено 0 из 3",
    "✏️ Изменено 0 · ↪ Перенесено 0 · ✅ Закрыто 0 · 🗑 Удалено 0 · 🔗 Объединено 0",
    "❌ Не подтверждено сверкой: 3",
])


def _run_auto_execute(monkeypatch, mid, report):
    async def _exec(manifest_id, m):
        return report

    monkeypatch.setattr(s._AUTO_EXECUTORS["delete_tasks"], "execute", _exec)
    asyncio.run(s._tg_auto_execute_tick())


def test_zero_confirmed_of_three_is_not_marked_executed(monkeypatch):
    """Три мутации отправлены, независимое чтение не подтвердило ни одной.
    Надгробие «выполнено» здесь — прямая ложь, а следующий вызов слышал от
    неё «Повторять нечего, ничего не потеряно»."""
    _tg_on(monkeypatch)
    mid = _plant_delete_manifest("d7-zero")
    _approved(monkeypatch, mid)
    _collect_reports(monkeypatch)
    monkeypatch.setattr(s, "_build_operation_report", lambda m: "\n".join([
        "### 🔍 Независимая проверка",
        "- ❌ **«Купить молоко»** — расхождение",
        "**Итог: ✅ 0 подтверждено, ⚠️ 0 не проверено, ❌ 3 расхождения**"]))

    _run_auto_execute(monkeypatch, mid, _ZERO_OF_THREE)

    assert _reason(mid) != s._TOMBSTONE_EXECUTED, \
        "нуль подтверждённых помечен как «выполнено»"
    assert _reason(mid) == s._TOMBSTONE_FAILED
    msg = s._manifest_gone_msg(mid, "DEFAULT")
    assert "УЖЕ исполнен" not in msg
    assert "ничего не потеряно" not in msg


def test_an_unprovable_outcome_is_neither_executed_nor_failed(monkeypatch):
    """Тот же отчёт, но подтвердить его нечем: журнала для этого класса
    операций нет вовсе. Это НЕ «выполнено» (никто ничего не доказал) и НЕ
    «не выполнено» (мутации ушли, часть могла примениться) — отдельное
    четвёртое состояние, иначе выбор был бы между двумя враньём."""
    _tg_on(monkeypatch)
    mid = _plant_delete_manifest("d7-unproven")
    _approved(monkeypatch, mid)
    _collect_reports(monkeypatch)
    monkeypatch.setattr(s, "_build_operation_report",
                        lambda m: "Журнал не найден")

    _run_auto_execute(monkeypatch, mid, _ZERO_OF_THREE)

    assert _reason(mid) == s._TOMBSTONE_UNCONFIRMED
    msg = s._manifest_gone_msg(mid, "DEFAULT")
    assert "УЖЕ исполнен" not in msg and "ничего не потеряно" not in msg
    assert "подтвердила НЕ ВСЁ" in msg
    assert "«сделано» НЕЛЬЗЯ" in msg
    # …и не выдаёт неподтверждённое за провал: «ничего не произошло» тоже ложь
    assert "могла примениться" in msg
    assert "unverified" in (s._MANIFEST_TOMBSTONES[mid].get("detail") or "")


def test_a_confirmed_success_is_still_marked_executed(monkeypatch):
    """Зеркало ко всем трём: настоящий подтверждённый успех по-прежнему
    читается как «уже исполнено» — правка не имеет права глушить полезный
    ответ."""
    _tg_on(monkeypatch)
    mid = _plant_delete_manifest("d7-ok")
    _approved(monkeypatch, mid)
    _collect_reports(monkeypatch)
    monkeypatch.setattr(s, "_build_operation_report", lambda m: "\n".join([
        "### 🔍 Независимая проверка",
        "- ✅ **«Купить молоко»** — удалена",
        "**Итог: ✅ 1 подтверждено, ⚠️ 0 не проверено, ❌ 0 расхождений**"]))

    _run_auto_execute(monkeypatch, mid, "### ✅ Удалено 1 задание (проверено).")

    assert _reason(mid) == s._TOMBSTONE_EXECUTED
    assert "УЖЕ исполнен" in s._manifest_gone_msg(mid, "DEFAULT")


def test_the_tombstone_follows_the_verdict_and_the_stricter_of_two_signals():
    """Таблица решений целиком — все пять вердиктов, оба сигнала.

    Самоотчёт участвует ровно одним способом: он может исход УХУДШИТЬ («я
    ничего не сделал» перебивает даже "ok"), но никогда не улучшить."""
    by_verdict = {v: s._tombstone_reason_for_verdict(v, False)
                  for v in ("ok", "partial", "mismatch", "failed", "unverified")}

    assert by_verdict == {
        "ok": s._TOMBSTONE_EXECUTED,
        "partial": s._TOMBSTONE_UNCONFIRMED,
        "unverified": s._TOMBSTONE_UNCONFIRMED,
        "mismatch": s._TOMBSTONE_FAILED,
        "failed": s._TOMBSTONE_FAILED,
    }
    # Строжайший из двух: отказ исполнителя строкой перебивает любой вердикт.
    for v in ("ok", "partial", "unverified", "mismatch", "failed"):
        assert s._tombstone_reason_for_verdict(v, True) == s._TOMBSTONE_FAILED
    # Незнакомое значение вердикта не считается успехом (fail-closed).
    assert s._tombstone_reason_for_verdict("нечто новое", False) \
        == s._TOMBSTONE_UNCONFIRMED
