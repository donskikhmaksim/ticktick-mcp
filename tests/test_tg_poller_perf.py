"""Регрессии производительности фонового поллера авто-исполнения
(_tg_auto_execute_tick / _tg_auto_execute_poller_loop), 2026-08-06.

Симптом с живого прода: между нажатием «✅ Подтвердить» в Telegram и реальным
исполнением проходило до ~14 минут при заявленном интервале в 10 секунд.
Две причины в коде (обе чинятся здесь и закрываются этими тестами):

  1. ЛИНЕЙНОЕ число поездок в Postgres: на КАЖДЫЙ живой манифест шёл
     check_approval() и, для одобренного, ещё get_tg_approval() — 1–2 поездки
     на план. С 2 гейтованными тулами планов были единицы, с 22 — десятки, а
     база ходит через публичный прокси Railway (десятки-сотни мс на поездку).
  2. БЛОКИРОВКА event loop: psycopg2 и requests синхронные, а звали их прямо
     из корутины поллера — на время запроса вставал ВЕСЬ сервер, включая
     обычные MCP-вызовы модели.

Тесты ниже должны краснеть, если любое из двух вернётся: №1 ловит возврат к
поштучному чтению, №2 — снятие выноса в поток (_run_blocking).
Ни одного реального обращения к Postgres/Telegram здесь нет.
"""
import asyncio
import time

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


@pytest.fixture(autouse=True)
def _clean_manifests():
    """_MANIFESTS — общий глобал на всю сессию тестов (см. тот же приём в
    test_tg_auto_execute.py): снимок и восстановление делают файл независимым
    от порядка запуска."""
    before = dict(s._MANIFESTS)
    s._MANIFESTS.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)


def _enable_tg(monkeypatch, allowlist=None):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=allowlist, ttl_s=3600))


def _plant(n: int, prefix: str = "perf") -> list:
    """n живых манифестов удаления — форма, которую поллер обязан признать
    кандидатом. `object_hash` намеренно НЕ задан: binding-сверка тогда
    пропускается, и тест меряет ровно поход в базу, а не пересчёт хэшей."""
    ids = []
    for i in range(n):
        mid = f"{prefix}-{i}"
        s._MANIFESTS[mid] = {
            "kind": "delete", "consumed": False, "created": time.monotonic(),
            "summary": "test",
            "items": [{"taskId": f"t{i}", "projectId": "p1", "title": "X",
                       "project": "P", "snapshot": {}}],
        }
        ids.append(mid)
    return ids


def _row(message_id=7):
    return {"status": "APPROVED", "expires_at": tg._now_ms() + 3_600_000,
            "chat_id": "c1", "message_id": message_id}


def _count_db_reads(monkeypatch) -> dict:
    """Счётчик ВСЕХ путей чтения статусов из Postgres — и пакетного, и двух
    одиночных. Считать нужно все три: иначе возврат к поштучному чтению
    (check_approval/get_tg_approval в цикле) тест бы не заметил."""
    calls = {"batch": 0, "single": 0, "ids": 0}

    def _batch(ids):
        ids = list(ids)
        calls["batch"] += 1
        calls["ids"] += len(ids)
        return {mid: _row() for mid in ids}

    def _check(mid):
        calls["single"] += 1
        return "approved"

    def _single(mid):
        calls["single"] += 1
        return _row()

    monkeypatch.setattr(tg, "get_tg_approvals", _batch)
    monkeypatch.setattr(tg, "check_approval", _check)
    monkeypatch.setattr(tg, "get_tg_approval", _single)
    return calls


def _noop_executor(monkeypatch, record=None, delay=0.0, boom_on=None):
    """Подменяет исполнение delete_tasks: никакого TickTick, только запись
    факта вызова. `boom_on` — id, на котором исполнитель падает."""
    async def _exec(manifest_id, m):
        if boom_on is not None and manifest_id == boom_on:
            raise RuntimeError("kaboom")
        if delay:
            await asyncio.sleep(delay)
        if record is not None:
            record.append(manifest_id)
        return "### ✅ Готово"

    monkeypatch.setattr(s._AUTO_EXECUTORS["delete_tasks"], "execute", _exec)


def _silent_report(monkeypatch, sink=None, sleep_s=0.0):
    """Подменяет ОБОИХ получателей итога автоисполнения.

    С 2026-08-06 итог уходит двумя разными вызовами, а не одним: ПОЛНЫЙ
    markdown — в группу-архив (`post_report_to_group`), КОРОТКАЯ сводка — в то
    же личное сообщение с кнопками (`summarize_in_owner_chat`). Оба
    синхронные (requests + сон на 429), поэтому `sleep_s` вешается на
    групповую отправку — именно её вынос в поток и проверяет тест про
    незамороженный event loop. В `sink` кладётся полный текст отчёта: тесты
    ниже ищут в нём маркеры провала («🛑», «kaboom», «не уложилось»)."""
    def _post(cfg, manifest_id, report_md, *, tool, verdict):
        if sleep_s:
            time.sleep(sleep_s)      # синхронный, как настоящий requests.post
        if sink is not None:
            sink.append(report_md)
        return tg.ReportDelivery([1001], 1, 1, True)

    def _summarize(cfg, chat_id, message_id, short_md):
        return True

    monkeypatch.setattr(tg, "post_report_to_group", _post)
    monkeypatch.setattr(tg, "summarize_in_owner_chat", _summarize)


# ═══════════ 1. Поездок в базу за проход — константа, не O(планов) ═══════════

@pytest.mark.parametrize("n_plans", [1, 25])
async def test_db_roundtrips_per_pass_are_constant(n_plans, monkeypatch):
    """ГЛАВНЫЙ тест этого файла: сколько бы живых планов ни висело, проход
    поллера ходит в Postgres РОВНО ОДИН раз. Вернётся поштучное чтение —
    счётчик станет пропорционален числу планов, и тест покраснеет."""
    _enable_tg(monkeypatch)
    calls = _count_db_reads(monkeypatch)
    _noop_executor(monkeypatch)
    _silent_report(monkeypatch)
    _plant(n_plans)

    await s._tg_auto_execute_tick()

    assert calls["batch"] == 1, "проход должен читать статусы ОДНИМ запросом"
    assert calls["single"] == 0, ("поштучное чтение статусов вернулось — "
                                  "это и есть линейная деградация")
    assert calls["ids"] == n_plans, "в пакет должны попасть все живые планы"


async def test_db_roundtrips_identical_for_1_and_25_plans(monkeypatch):
    """Та же проверка «в лоб»: число походов в базу при 1 и при 25 планах
    буквально одинаково."""
    _enable_tg(monkeypatch)
    _noop_executor(monkeypatch)
    _silent_report(monkeypatch)

    calls_one = _count_db_reads(monkeypatch)
    _plant(1, "one")
    await s._tg_auto_execute_tick()
    total_one = calls_one["batch"] + calls_one["single"]

    s._MANIFESTS.clear()
    calls_many = _count_db_reads(monkeypatch)
    _plant(25, "many")
    await s._tg_auto_execute_tick()
    total_many = calls_many["batch"] + calls_many["single"]

    assert total_one == total_many == 1


async def test_no_db_read_at_all_when_nothing_is_pending(monkeypatch):
    """Пустой список живых манифестов — в базу не ходим вовсе (типичное
    состояние: поллер тикает круглосуточно, планов почти всегда нет)."""
    _enable_tg(monkeypatch)
    calls = _count_db_reads(monkeypatch)

    await s._tg_auto_execute_tick()

    assert calls == {"batch": 0, "single": 0, "ids": 0}


# ═══════════ 2. Проход поллера не морозит event loop ═══════════

async def _heartbeat_during(coro, beat_s: float = 0.01) -> int:
    """Считает, сколько раз успела провернуться ПОСТОРОННЯЯ корутина, пока
    выполнялась `coro`. Ноль = event loop был заблокирован синхронным вызовом
    (именно так вели себя psycopg2/requests, вызванные прямо из корутины)."""
    beats = {"n": 0}

    async def _beat():
        while True:
            await asyncio.sleep(beat_s)
            beats["n"] += 1

    hb = asyncio.create_task(_beat())
    try:
        await coro
    finally:
        hb.cancel()
    return beats["n"]


async def test_slow_db_read_does_not_block_the_event_loop(monkeypatch):
    """Медленный (0.2 c) СИНХРОННЫЙ поход в базу обязан уйти в поток: пока он
    идёт, соседняя корутина должна успеть отработать много раз. Убрать
    _run_blocking вокруг get_tg_approvals — и счётчик станет 0."""
    _enable_tg(monkeypatch)
    _noop_executor(monkeypatch)
    _silent_report(monkeypatch)
    ids = _plant(3)

    def _slow_batch(mids):
        time.sleep(0.2)
        return {mid: _row() for mid in mids}

    monkeypatch.setattr(tg, "get_tg_approvals", _slow_batch)

    beats = await _heartbeat_during(s._tg_auto_execute_tick())

    assert beats >= 5, (f"event loop стоял во время похода в базу "
                        f"(посторонняя корутина провернулась {beats} раз)")
    assert all(s._MANIFESTS[m]["consumed"] for m in ids)


async def test_slow_telegram_report_does_not_block_the_event_loop(monkeypatch):
    """То же для отчёта в Telegram: requests.post с таймаутом 8 c, вызванный
    из корутины, морозил весь сервер. Должен идти через _run_blocking."""
    _enable_tg(monkeypatch)
    _noop_executor(monkeypatch)
    _silent_report(monkeypatch, sleep_s=0.2)
    monkeypatch.setattr(tg, "get_tg_approvals",
                        lambda mids: {mid: _row() for mid in mids})
    _plant(1)

    beats = await _heartbeat_during(s._tg_auto_execute_tick())

    assert beats >= 5, (f"event loop стоял во время отправки отчёта в Telegram "
                        f"(посторонняя корутина провернулась {beats} раз)")


# ═══════════ 3. Один плохой кандидат не мешает остальным ═══════════

async def test_failing_candidate_does_not_stop_the_others(monkeypatch):
    _enable_tg(monkeypatch)
    monkeypatch.setattr(tg, "get_tg_approvals",
                        lambda mids: {mid: _row() for mid in mids})
    done, reports = [], []
    _plant(3, "mix")
    _noop_executor(monkeypatch, record=done, boom_on="mix-0")
    _silent_report(monkeypatch, sink=reports)

    await s._tg_auto_execute_tick()

    assert done == ["mix-1", "mix-2"], "падение первого съело остальных"
    assert len(reports) == 3, "об ошибке первого тоже должен уйти отчёт"
    assert "🛑" in reports[0] and "kaboom" in reports[0]


async def test_hung_candidate_is_cut_off_and_queue_moves_on(monkeypatch):
    """Зависший кандидат (сетевой вызов к TickTick, который не возвращается)
    раньше держал очередь бесконечно — теперь его режет таймаут, а остальные
    подтверждённые планы исполняются в этом же проходе."""
    _enable_tg(monkeypatch)
    monkeypatch.setattr(s, "_TG_AUTO_EXECUTE_CANDIDATE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(tg, "get_tg_approvals",
                        lambda mids: {mid: _row() for mid in mids})
    done, reports = [], []
    _plant(2, "hang")
    _silent_report(monkeypatch, sink=reports)

    async def _exec(manifest_id, m):
        if manifest_id == "hang-0":
            await asyncio.sleep(30)       # «завис» навсегда
        done.append(manifest_id)
        return "### ✅ Готово"

    monkeypatch.setattr(s._AUTO_EXECUTORS["delete_tasks"], "execute", _exec)

    await asyncio.wait_for(s._tg_auto_execute_tick(), 5)

    assert done == ["hang-1"], "зависший кандидат заблокировал очередь"
    assert any("не уложилось" in r for r in reports)


# ═══════════ 4. Проходы не накладываются друг на друга ═══════════

async def test_passes_never_overlap(monkeypatch):
    """Проход длится дольше интервала — второй не должен стартовать поверх
    первого. Заодно проверяется, что интервал отсчитывается ОТ НАЧАЛА прохода
    (иначе реальный период = интервал + длительность и «10 секунд»
    незаметно превращаются в минуты)."""
    monkeypatch.setattr(s, "_TG_AUTO_EXECUTE_INTERVAL_S", 0.0)
    state = {"now": 0, "max": 0, "runs": 0}

    async def _slow_tick():
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        state["runs"] += 1
        await asyncio.sleep(0.02)
        state["now"] -= 1

    monkeypatch.setattr(s, "_tg_auto_execute_tick", _slow_tick)
    task = asyncio.create_task(s._tg_auto_execute_poller_loop())
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert state["max"] == 1, "два прохода поллера шли одновременно"
    assert state["runs"] >= 2, "цикл вообще не провернулся — тест бессмысленен"


async def test_loop_survives_a_failing_tick(monkeypatch):
    """Падение одного прохода не убивает цикл (иначе авто-исполнение молча
    умирает до следующего деплоя)."""
    monkeypatch.setattr(s, "_TG_AUTO_EXECUTE_INTERVAL_S", 0.0)
    runs = {"n": 0}

    async def _boom_tick():
        runs["n"] += 1
        raise RuntimeError("tick boom")

    monkeypatch.setattr(s, "_tg_auto_execute_tick", _boom_tick)
    task = asyncio.create_task(s._tg_auto_execute_poller_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert runs["n"] >= 2


# ═══════════ 5. Слой выключен — ни базы, ни сети ═══════════

async def test_disabled_layer_touches_neither_db_nor_network(monkeypatch):
    """TG_APPROVAL_ENABLED=false: поллер в проде вообще не запускается, но и
    прямой вызов прохода не должен ни ходить в Postgres, ни дёргать Telegram
    — ни одной из правок это поведение менять не разрешено."""
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=False, bot_token="", owner_chat_id="", server="ticktick",
        tools_allowlist=None, ttl_s=3600))

    def _explode(*a, **k):
        raise AssertionError("выключенный слой полез в базу/сеть")

    for name in ("get_tg_approvals", "check_approval", "get_tg_approval",
                 "_tg_call", "report_auto_execution_result", "notify_plan",
                 "create_tg_approval",
                 # Новые получатели итога (2026-08-06) — их тоже надо
                 # заминировать, иначе выключенный слой мог бы пойти в сеть
                 # мимо старого имени и тест этого не заметил бы.
                 "post_report_to_group", "summarize_in_owner_chat",
                 "reap_expired"):
        monkeypatch.setattr(tg, name, _explode)
    _noop_executor(monkeypatch)
    _plant(5, "off")

    await s._tg_auto_execute_tick()               # не должно бросить
    assert s._find_tg_auto_execute_candidates() == []
    assert not any(s._MANIFESTS[m].get("consumed") for m in s._MANIFESTS)
