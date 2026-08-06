"""Хранилище долговечных планов (#91) — против НАСТОЯЩЕГО Postgres.

Самый важный файл в этой задаче: здесь проверяется одноразовость плана, то
есть единственное свойство, ошибка в котором стоит второго безвозвратного
удаления. Двойника базы тут нет намеренно — см. шапку tests/pg_helper.py.
"""
import threading
import time

import pytest

from tests.pg_helper import fresh_store, requires_pg

pytestmark = requires_pg


def _plan(**over):
    p = {"kind": "delete", "tool": "delete_tasks", "_gate": "batch",
         "tasks": [{"taskId": "t1", "title": "Задача"}],
         "summary": "Удаление", "consumed": False, "tg_notified": True,
         "_created_ms": int(time.time() * 1000)}
    p.update(over)
    return p


def _save(store, mid="m1", ttl_ms=3600_000, payload=None):
    now = int(time.time() * 1000)
    return store.save(mid, "delete_tasks", payload or _plan(),
                      created_at_ms=now, expires_at_ms=now + ttl_ms)


# ───────────────────────────── схема и базовый цикл ─────────────────────────

def test_migration_creates_the_table_and_is_idempotent():
    store = fresh_store()
    # Повторный init на уже поднятом пуле не должен ни падать, ни городить
    # второй пул (второй остался бы висеть с открытыми соединениями, которые
    # уже некому закрыть), а сама миграция обязана переживать повторный
    # прогон — она выполняется при КАЖДОМ старте процесса.
    pool_before = store._pg_pool
    from tests.pg_helper import pg_dsn
    store.init_store(pg_dsn())
    store._ensure_schema()
    assert store._pg_pool is pool_before
    with store._conn() as cur:
        cur.execute("SELECT to_regclass('mcp_manifests')")
        assert cur.fetchone()[0] == "mcp_manifests"


def test_save_then_load_returns_the_same_plan():
    store = fresh_store()
    assert _save(store) is True
    row = store.load("m1")
    assert row["payload"]["tasks"][0]["taskId"] == "t1"
    assert row["payload"]["tg_notified"] is True
    assert row["consumed_at"] is None


def test_claim_returns_the_plan_once_and_then_says_taken():
    store = fresh_store()
    _save(store)
    outcome, payload = store.claim("m1")
    assert outcome == store.CLAIM_WON
    assert payload["tasks"][0]["taskId"] == "t1"
    # Второй заход по тому же плану — уже занято, и НИКАКОГО плана обратно.
    assert store.claim("m1") == (store.CLAIM_TAKEN, None)


def test_claim_of_an_unknown_plan_is_absent_not_taken():
    """Разница не косметическая: «строки нет» означает, что план виден только
    памяти процесса и решать должна она, а «занято» — что решение уже принято
    и трогать план нельзя."""
    store = fresh_store()
    assert store.claim("нет-такого") == (store.CLAIM_ABSENT, None)


def test_mark_consumed_makes_a_later_claim_impossible():
    """Чат-путь погасил план → нажатие кнопки после перезапуска не воскресит
    отменённую операцию."""
    store = fresh_store()
    _save(store)
    assert store.mark_consumed("m1") is True
    assert store.mark_consumed("m1") is False  # второй раз гасить нечего
    assert store.claim("m1") == (store.CLAIM_TAKEN, None)


def test_expired_plan_is_never_claimed():
    """Срок жизни проверяется ВНУТРИ того же запроса, что и захват."""
    store = fresh_store()
    now = int(time.time() * 1000)
    store.save("m1", "delete_tasks", _plan(),
               created_at_ms=now - 7200_000, expires_at_ms=now - 1000)
    assert store.claim("m1") == (store.CLAIM_TAKEN, None)


def test_ttl_is_evaluated_with_the_same_now_as_the_write():
    """План, истекающий между «посмотрели» и «заняли», занят быть не может.

    Проверяем через явный `now_ms`: захват с моментом ПОСЛЕ истечения не
    проходит, хотя строка в базе цела и свободна."""
    store = fresh_store()
    now = int(time.time() * 1000)
    store.save("m1", "delete_tasks", _plan(),
               created_at_ms=now, expires_at_ms=now + 50)
    assert store.claim("m1", now_ms=now + 500)[0] == store.CLAIM_TAKEN
    # ...а до истечения — проходит (иначе тест выше был бы зелёным по любой
    # причине, включая просто сломанный claim).
    assert store.claim("m1", now_ms=now + 10)[0] == store.CLAIM_WON


def test_id_collision_never_overwrites_a_foreign_plan():
    """Совпадение id = под ним уже лежит ЧУЖОЙ план. Затирать его нельзя:
    владелец подтвердит один план, а исполнится другой."""
    store = fresh_store()
    _save(store, payload=_plan(summary="ПЕРВЫЙ"))
    assert _save(store, payload=_plan(summary="ВТОРОЙ")) is False
    assert store.load("m1")["payload"]["summary"] == "ПЕРВЫЙ"


def test_oversized_plan_is_refused_not_truncated():
    """`attach_file_to_task` кладёт в план содержимое файла (до 20 МБ). Такой
    план в базу не едет — но и не калечится: отказ честный."""
    store = fresh_store()
    huge = _plan(params={"content_base64": "A" * (600 * 1024)})
    assert _save(store, payload=huge) is False
    assert store.load("m1") is None


def test_purge_expired_respects_the_grace_window():
    store = fresh_store()
    now = int(time.time() * 1000)
    store.save("свежий", "t", _plan(), created_at_ms=now, expires_at_ms=now + 60_000)
    store.save("вчерашний", "t", _plan(), created_at_ms=now - 90_000,
               expires_at_ms=now - 60_000)
    # Отсрочка больше возраста просрочки — не трогаем ничего.
    assert store.purge_expired(grace_ms=120_000) == 0
    assert store.purge_expired(grace_ms=0) == 1
    assert store.load("свежий") is not None
    assert store.load("вчерашний") is None


def test_load_live_skips_consumed_and_expired():
    store = fresh_store()
    now = int(time.time() * 1000)
    store.save("живой", "t", _plan(), created_at_ms=now, expires_at_ms=now + 60_000)
    store.save("занятый", "t", _plan(), created_at_ms=now, expires_at_ms=now + 60_000)
    store.save("протухший", "t", _plan(), created_at_ms=now, expires_at_ms=now - 1)
    store.claim("занятый")
    got = store.load_live(["живой", "занятый", "протухший", "несуществующий"])
    assert set(got) == {"живой"}


def test_delete_removes_the_row():
    store = fresh_store()
    _save(store)
    store.delete("m1")
    assert store.load("m1") is None


# ──────────────────────────── ОДНОРАЗОВОСТЬ ПРИ ГОНКЕ ────────────────────────
#
# Ниже — то, ради чего этот файл требует настоящий Postgres. Потоки стартуют
# по общему барьеру, то есть заходят в `claim` действительно одновременно, и
# разруливает их СУБД, а не порядок вызовов в тесте.

def _race(store, mid, n_threads):
    ready = threading.Barrier(n_threads)
    results = []
    lock = threading.Lock()

    def worker():
        ready.wait()
        outcome, payload = store.claim(mid)
        with lock:
            results.append((outcome, payload))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "поток завис на claim"
    return results


def test_two_simultaneous_claims_produce_exactly_one_winner():
    """ДВА НАЖАТИЯ ОДНОВРЕМЕННО → РОВНО ОДНО ИСПОЛНЕНИЕ.

    Это буквально сценарий выкатки: старый контейнер ещё жив, новый уже
    поднялся, поллеры обоих в одну и ту же секунду видят APPROVED."""
    store = fresh_store()
    _save(store)
    results = _race(store, "m1", 2)
    won = [r for r in results if r[0] == store.CLAIM_WON]
    assert len(won) == 1, f"победителей должно быть ровно 1, а не {len(won)}"
    assert won[0][1]["tasks"][0]["taskId"] == "t1"
    assert [r[0] for r in results].count(store.CLAIM_TAKEN) == 1


def test_twenty_simultaneous_claims_produce_exactly_one_winner():
    """Тот же инвариант под настоящей нагрузкой: 20 претендентов, один план.

    Двадцать — заведомо больше пула соединений (5), так что заодно
    проверяется, что очередь за соединением не превращается в отказ."""
    store = fresh_store()
    _save(store)
    results = _race(store, "m1", 20)
    assert len(results) == 20
    outcomes = [r[0] for r in results]
    assert outcomes.count(store.CLAIM_WON) == 1, \
        f"победителей должно быть ровно 1, а не {outcomes.count(store.CLAIM_WON)}"
    # Проигравшим полагается именно TAKEN: ABSENT означал бы «строки нет» и
    # отправил бы вызывающего решать по памяти — то есть исполнить второй раз.
    assert outcomes.count(store.CLAIM_TAKEN) == 19


def test_claim_loses_to_a_concurrent_uncommitted_claim():
    """ЭТО — ГЛАВНЫЙ ТЕСТ АТОМАРНОСТИ, и он детерминированный.

    Тесты выше (два и двадцать потоков по барьеру) проверяют инвариант, но
    поймать нарушение могут только если потоки реально успеют наложиться —
    а `claim` выполняется за доли миллисекунды, и на практике они выстраиваются
    в очередь сами. Мутационная проверка это подтвердила: замена одного
    `UPDATE … RETURNING` на «прочитал → решил → записал» оставляла оба
    многопоточных теста ЗЕЛЁНЫМИ.

    Здесь окно раскрывается принудительно и без всякого тайминга: соседняя
    транзакция занимает строку и НЕ коммитится, пока наш `claim` не упрётся в
    неё. Что видит каждая из двух реализаций:
      • один оператор `UPDATE … WHERE consumed_at IS NULL` — ждёт снятия
        блокировки, ПЕРЕЧИТЫВАЕТ строку, видит уже проставленный `consumed_at`
        и честно уходит ни с чем;
      • «SELECT → проверил → UPDATE» — SELECT в READ COMMITTED не блокируется
        и видит СТАРУЮ версию строки (`consumed_at IS NULL`), то есть решает
        «план свободен», а потом дожидается блокировки и безусловно
        перезаписывает. Двойное исполнение.
    """
    import psycopg2

    from tests.pg_helper import pg_dsn

    store = fresh_store()
    _save(store)

    rival = psycopg2.connect(pg_dsn())
    rival.autocommit = False
    result = {}
    try:
        with rival.cursor() as cur:
            cur.execute(
                "UPDATE mcp_manifests SET consumed_at = %s "
                "WHERE server = %s AND manifest_id = %s AND consumed_at IS NULL",
                (int(time.time() * 1000), store.SERVER, "m1"),
            )
            assert cur.rowcount == 1  # соперник занял строку, но ещё не коммитил

        def worker():
            result["claim"] = store.claim("m1")[0]

        t = threading.Thread(target=worker)
        t.start()
        # Пока соперник держит транзакцию, наш claim обязан ЖДАТЬ — если он уже
        # ответил, значит он читал мимо блокировки.
        t.join(timeout=1.0)
        assert t.is_alive(), ("claim не стал ждать занятую строку — значит "
                              "проверка и захват разъехались")
        rival.commit()
        t.join(timeout=30)
        assert not t.is_alive()
    finally:
        rival.close()
    assert result["claim"] == store.CLAIM_TAKEN, \
        "план достался второму претенденту — это двойное исполнение"


def test_claim_is_exactly_one_statement():
    """Второй, структурный слой той же защиты: захват обязан быть ОДНИМ
    обращением к базе. Любое разделение на два запроса создаёт окно между
    проверкой и захватом — и здесь это видно сразу, без всякой гонки."""
    import contextlib

    store = fresh_store()
    _save(store)
    seen = []
    real_conn = store._conn

    class _CountingCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, query, *a, **k):
            seen.append(" ".join(str(query).split())[:60])
            return self._cur.execute(query, *a, **k)

        def __getattr__(self, name):
            return getattr(self._cur, name)

    @contextlib.contextmanager
    def counting_conn():
        with real_conn() as cur:
            yield _CountingCursor(cur)

    store._conn = counting_conn
    try:
        assert store.claim("m1")[0] == store.CLAIM_WON
    finally:
        store._conn = real_conn
    assert len(seen) == 1, f"захват сделал {len(seen)} запросов вместо одного: {seen}"


def test_race_between_claim_and_mark_consumed_still_executes_at_most_once():
    """Кнопка и чат-«нет» в одну секунду: победить может любой, но исполнение
    достаётся максимум одному."""
    store = fresh_store()
    _save(store)
    ready = threading.Barrier(2)
    out = {}

    def claimer():
        ready.wait()
        out["claim"] = store.claim("m1")[0]

    def refuser():
        ready.wait()
        out["mark"] = store.mark_consumed("m1")

    ts = [threading.Thread(target=claimer), threading.Thread(target=refuser)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert (out["claim"] == store.CLAIM_WON) != out["mark"], \
        "план достался обоим — это двойное исполнение"


def test_pool_survives_a_logical_error_without_leaking_connections():
    """Логическая ошибка (кривой SQL) НЕ ломает соединение и не должна
    съедать его из пула — иначе пять кривых запросов оставили бы сервер без
    базы до перезапуска."""
    import psycopg2

    store = fresh_store()
    for _ in range(store._POOL_MAX_CONN + 3):
        with pytest.raises(psycopg2.Error):
            with store._conn() as cur:
                cur.execute("SELECT * FROM таблицы-которой-нет")
    # После всех этих ошибок хранилище обязано остаться рабочим.
    assert _save(store) is True
    assert store.claim("m1")[0] == store.CLAIM_WON
