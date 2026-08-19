"""Single-flight на /batch/check/0 и уважение Retry-After (2026-08-19).

Прод-логи ночного QA: 63 раза `429 Client Error` на
`api.ticktick.com/api/v2/batch/check/0` за ~2 часа. Механика самоподдержки
rate-limit'а: почти каждый инструмент читает полный снимок через
`get_state(force=True)`; вызовы разъезжаются по потокам `_run_blocking`, и у
КАЖДОГО потока был свой независимый fetch — десяток параллельных инструментов
= десяток тяжёлых запросов подряд, из которых TickTick режет большинство.
Плюс на 429 клиент спал слепые 1с/2с, игнорируя присланный сервером
Retry-After, и повторял — продлевая полосу 429.

Фиксы, которые проверяет этот файл (ticktick_v2_client.py):
  * get_state — single-flight: одновременные вызовы (включая force=True)
    складываются в ОДИН HTTP-запрос; force удовлетворён любым снимком, чей
    запрос стартовал не раньше вызова;
  * сбой полёта не подвешивает ждущих (следующий пробует сам);
  * `_retry_delay_s` — пауза ретрая берётся из Retry-After (с потолком
    RETRY_AFTER_CAP_S), а не только из экспоненты.
"""
import threading
import time

from ticktick_mcp.src.ticktick_v2_client import (
    RETRY_AFTER_CAP_S, TickTickV2Client, _retry_delay_s)
from ticktick_mcp.src import ticktick_client as v1


def _client_with_slow_fetch(delay=0.15, state=None):
    c = TickTickV2Client(token="tok")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, time.monotonic()))
        time.sleep(delay)
        return dict(state if state is not None else
                    {"inboxId": "inbox1", "syncTaskBean": {"update": []}})

    c._request = fake_request
    return c, calls


def test_concurrent_force_reads_collapse_into_one_fetch():
    """8 одновременных get_state(force=True) — не 8 HTTP-запросов. Потоки,
    пришедшие до старта полёта, удовлетворяются его результатом; максимум
    возможен один добор (поток, проскочивший после старта)."""
    c, calls = _client_with_slow_fetch()
    barrier = threading.Barrier(8)
    results = []

    def worker():
        barrier.wait()
        results.append(c.get_state(force=True))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert all(r.get("inboxId") == "inbox1" for r in results)
    assert len(calls) <= 2, (
        f"{len(calls)} параллельных fetch'а /batch/check/0 вместо "
        "single-flight — ровно так прод сам себе устраивал 429")


def test_force_after_finished_snapshot_refetches():
    """Force-семантика не ослабла: force ПОСЛЕ завершённого снимка обязан
    перечитать (снимок стартовал раньше вызова — мог не увидеть свежую
    запись)."""
    c, calls = _client_with_slow_fetch(delay=0)
    c.get_state(force=True)
    assert len(calls) == 1
    c.get_state(force=True)
    assert len(calls) == 2, "force доволен доисторическим снимком"


def test_nonforce_within_ttl_uses_cache():
    c, calls = _client_with_slow_fetch(delay=0)
    c.get_state()
    c.get_state()
    assert len(calls) == 1, "TTL-кэш перестал работать"


def test_fetch_failure_wakes_waiters_no_deadlock():
    """Полёт, упавший исключением, будит ждущих; следующий пробует сам.
    Главное — никто не виснет навечно."""
    c = TickTickV2Client(token="tok")
    calls = []
    fail_first = threading.Event()

    def fake_request(method, path, **kwargs):
        calls.append(path)
        time.sleep(0.1)
        if not fail_first.is_set():
            fail_first.set()
            raise RuntimeError("simulated 5xx")
        return {"inboxId": "inbox1"}

    c._request = fake_request
    outcomes = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            outcomes.append(("ok", c.get_state(force=True)))
        except RuntimeError as e:
            outcomes.append(("err", str(e)))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "deadlock в single-flight"
    assert len(outcomes) == 2
    # Ровно один упал (тот, чей полёт был первым), второй дожил до данных.
    kinds = sorted(k for k, _ in outcomes)
    assert kinds == ["err", "ok"], outcomes


class _Resp:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_retry_delay_uses_retry_after_header():
    assert _retry_delay_s(_Resp({"Retry-After": "7"}), 0) == 7.0
    # потолок: щедрый Retry-After не должен занимать воркер пула минутами
    assert _retry_delay_s(_Resp({"Retry-After": "999"}), 0) == RETRY_AFTER_CAP_S
    # нет заголовка / мусор в нём — прежняя экспонента
    assert _retry_delay_s(_Resp(), 0) == 1.0
    assert _retry_delay_s(_Resp(), 1) == 2.0
    assert _retry_delay_s(_Resp({"Retry-After": "soon"}), 1) == 2.0


def test_v1_retry_delay_uses_retry_after_header():
    """Официальный клиент (ticktick_client.py) — та же логика."""
    assert v1._retry_delay_s(_Resp({"Retry-After": "7"}), 0) == 7.0
    assert v1._retry_delay_s(_Resp({"Retry-After": "999"}), 1) == v1.RETRY_AFTER_CAP_S
    assert v1._retry_delay_s(_Resp(), 1) == 2.0


def test_v2_request_sleeps_by_retry_after(monkeypatch):
    """Сквозная проверка _request: на 429 c Retry-After спим ровно его, а не
    экспоненту."""
    from ticktick_mcp.src import ticktick_v2_client as mod
    c = TickTickV2Client(token="tok")
    slept = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))

    class _R:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.headers = headers or {}
            self.text = "{}"

        def json(self):
            return {}

        def raise_for_status(self):
            pass

    responses = [_R(429, {"Retry-After": "7"}), _R(200)]
    c.session = type("S", (), {
        "request": lambda self_, *a, **kw: responses.pop(0),
        "cookies": c.session.cookies,
    })()
    c._request("GET", "/batch/check/0")
    assert slept == [7.0], f"спали {slept}, а сервер просил 7с"
