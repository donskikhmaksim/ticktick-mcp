"""ТЗ_consent_web_hub.md (2026-08-12) — веб-хаб подтверждений, портированный
на ticktick-mcp: часть 1 (гибридное короткое ожидание в `_gate_batch`/
`_gate_single`) и часть 2 (`GET /pending-consents`, `POST /pending-consents/
decide`). Покрывает тестовый план из задачи (пункты 1-11 применимо к этому
репозиторию — пункты 12-14 относятся к странице хаба и macOS-демону, которые
здесь не реализуются, см. отчёт).

Ни сети, ни реального Postgres: `manifest_store.store_ready()` остаётся
False (conftest.py не задаёт `CONSENT_DATABASE_URL`), поэтому
`_consume_manifest_for_auto_execute` идёт RAM-путём (CLAIM_ABSENT →
проверка/простановка `consumed` в памяти) — тем же путём, что и до задачи
#91 (переезд манифестов в базу). Telegram-слой выключен по умолчанию
(conftest не задаёт `TG_APPROVAL_ENABLED`), поэтому `_maybe_tg_notify_plan`
не делает сетевых вызовов.
"""
import asyncio
import re
import time

import pytest
from starlette.testclient import TestClient

import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s

_MID_RE = re.compile(r"Манифест `([0-9a-f]{6,})`")


def _mid_of(text: str) -> str:
    m = _MID_RE.search(text)
    assert m, f"в тексте нет id манифеста:\n{text}"
    return m.group(1)


@pytest.fixture(autouse=True)
def _isolate_manifests():
    """`_MANIFESTS` — глобал модуля consent.py, общий на всю тестовую сессию
    (тот же приём, что в test_manifest_persistence.py/test_button_only_execution.py)."""
    before = dict(consent._MANIFESTS)
    yield
    consent._MANIFESTS.clear()
    consent._MANIFESTS.update(before)


@pytest.fixture(autouse=True)
def _sync_wait_defaults(monkeypatch):
    """Дефолт из conftest (CONSENT_SYNC_WAIT_MS=0) уже применён на импорте
    модуля — здесь просто гарантируем, что каждый тест явно решает, нужно ли
    ему включать ожидание, а не наследует состояние соседнего теста."""
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 0)
    monkeypatch.setattr(consent, "CONSENT_SYNC_POLL_MS", 1000)
    yield


# ===========================================================================
# Часть 1 — гибридное короткое ожидание (`consent._sync_wait_for_decision`)
# ===========================================================================
# Юнит-тесты на саму функцию, а не через полный `_gate_single`/`_gate_batch`
# (эталон — как TS-репозитории тестируют requireConsent напрямую, а не через
# HTTP): она принимает только `manifest_id` и читает `_MANIFESTS[manifest_id]`
# — воспроизвести "мок-стор меняет статус на 2-й итерации опроса" (ТЗ, тест 2)
# проще и надёжнее прямой мутацией словаря из параллельной корутины, чем
# гонкой через реальный event loop нескольких тулов.


def test_1_wait_disabled_is_no_op_without_a_single_await():
    """ТЗ, тест 1: CONSENT_SYNC_WAIT_MS=0 (дефолт conftest) — функция
    возвращает None НЕМЕДЛЕННО, ни одной задержки, ни одного обращения к
    _MANIFESTS сверх банальной проверки условия."""
    assert consent.CONSENT_SYNC_WAIT_MS == 0
    start = time.monotonic()
    result = asyncio.run(consent._sync_wait_for_decision("does-not-exist"))
    assert result is None
    assert time.monotonic() - start < 0.05


async def _run_with_decision_after_one_tick(monkeypatch, decision_fn):
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 500)
    monkeypatch.setattr(consent, "CONSENT_SYNC_POLL_MS", 50)
    mid = "sync-wait-test-mid"
    consent._MANIFESTS[mid] = {"consumed": False}

    async def _decide():
        await asyncio.sleep(0.08)  # после первой итерации опроса (~50мс)
        decision_fn(consent._MANIFESTS[mid])

    result, _ = await asyncio.gather(
        consent._sync_wait_for_decision(mid), _decide())
    return result


def test_2_confirmed_in_window_returns_result_without_second_preview(monkeypatch):
    """ТЗ, тест 2: манифест подтверждён «человеком» (веб-хаб) в середине окна
    ожидания — функция возвращает готовый положительный текст с первой же
    итерации ПОСЛЕ решения, а не None (то есть вызывающий `_gate_batch`/
    `_gate_single` вернёт это как `outcome.message`, без второго превью)."""

    def _confirm(m):
        m["consumed"] = True
        m["_web_decision"] = "confirmed"
        m["_web_result"] = "✅ Подтверждено и исполнено через веб."

    result = asyncio.run(_run_with_decision_after_one_tick(monkeypatch, _confirm))
    assert result == "✅ Подтверждено и исполнено через веб."


def test_3_rejected_in_window_refuses_no_mutation(monkeypatch):
    """ТЗ, тест 3: отклонено в окне — отказ, мутации (в этом слое —
    исполнения) не было: результат — текст отказа, а не "confirmed"."""

    def _reject(m):
        m["consumed"] = True
        m["_web_decision"] = "rejected"
        m["_web_reject_comment"] = "передумал"

    result = asyncio.run(_run_with_decision_after_one_tick(monkeypatch, _reject))
    assert result is not None
    assert "Отменено пользователем" in result
    assert "передумал" in result
    assert "✅" not in result


def test_4_nobody_decided_falls_through_to_normal_planned_path(monkeypatch):
    """ТЗ, тест 4: никто не подтвердил/отклонил за окно — функция возвращает
    None (обычный planned-путь ниже в _gate_batch/_gate_single), ничего не
    потеряно."""
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 120)
    monkeypatch.setattr(consent, "CONSENT_SYNC_POLL_MS", 40)
    mid = "sync-wait-timeout-mid"
    consent._MANIFESTS[mid] = {"consumed": False}
    result = asyncio.run(consent._sync_wait_for_decision(mid))
    assert result is None
    # Регресс — манифест остаётся AWAITING (не тронут этой функцией).
    assert consent._MANIFESTS[mid]["consumed"] is False


def test_decided_via_some_other_channel_without_web_marker_is_safe_generic_text(monkeypatch):
    """Манифест погашен ДРУГИМ путём без пометки _web_decision (например,
    кнопка в Telegram сработала в это же окно) — безопасный общий ответ
    "уже решено где-то", НЕ "confirmed"-подобный текст, который мог бы
    навести вызывающий код на мысль повторить мутацию."""

    def _consume_without_marker(m):
        m["consumed"] = True

    result = asyncio.run(
        _run_with_decision_after_one_tick(monkeypatch, _consume_without_marker))
    assert result is not None
    assert "уже принято через другой канал" in result


# ===========================================================================
# ТЗ, тест 6 — automation_key + sync одновременно: валидный ключ исполняет
# СРАЗУ, ни одной итерации опроса не происходит (automation_key-ветка стоит
# ПЕРЕД гибридным ожиданием в _gate_single/_gate_batch и возвращает раньше).
# ===========================================================================

def _describe_tag(params):
    return f"Тег «{params.get('name')}»"


def test_6_automation_key_bypasses_sync_wait_entirely(monkeypatch):
    calls = {"n": 0}
    real_wait = consent._sync_wait_for_decision

    async def _counting_wait(mid):
        calls["n"] += 1
        return await real_wait(mid)

    monkeypatch.setattr(consent, "_sync_wait_for_decision", _counting_wait)
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 500)
    monkeypatch.setattr(consent, "_automation_key_channel",
                        lambda provided: "test" if provided == "validkey" else "")

    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "авто-тег", "color": None},
        "", "", _describe_tag, automation_key="validkey"))

    assert outcome.proceed is True
    assert outcome.extra == {"name": "авто-тег", "color": None}
    assert calls["n"] == 0, "гибридное ожидание не должно вызываться на automation_key-пути"


# ===========================================================================
# Часть 2 — HTTP: GET /pending-consents, POST /pending-consents/decide
# ===========================================================================


class _FakeTicktickV2:
    """Минимальный двойник ticktick_v2 для create_tag — без сети."""

    def __init__(self):
        self.created = []
        self._tags = []

    def create_tag(self, name, color=None):
        self.created.append((name, color))
        self._tags.append({"name": name})
        return {}

    def get_state(self, force=False):
        return {}

    def get_tags(self):
        return list(self._tags)


@pytest.fixture
def hub_secret(monkeypatch):
    secret = "test-hub-secret-xyz"
    monkeypatch.setattr(s, "_CONSENT_HUB_SECRET", secret)
    return secret


@pytest.fixture
def client():
    app = s.mcp.streamable_http_app()
    return TestClient(app)


def _headers(secret):
    return {"x-consent-hub-secret": secret}


def test_7_no_secret_or_wrong_secret_is_404_not_401_403(client, hub_secret):
    """ТЗ, тест 7: без секрета / с неверным секретом → 404 (не 401/403 —
    не подтверждаем существование роута)."""
    r1 = client.get("/pending-consents")
    assert r1.status_code == 404

    r2 = client.get("/pending-consents", headers=_headers("wrong-secret"))
    assert r2.status_code == 404

    r3 = client.post("/pending-consents/decide", json={"manifestId": "x", "decision": "confirm"})
    assert r3.status_code == 404


def test_8_secret_unset_disables_both_routes_rest_of_server_still_works(client, monkeypatch):
    """ТЗ, тест 8: CONSENT_HUB_SECRET не задан (пусто) ⇒ оба роута 404,
    остальной сервис работает."""
    monkeypatch.setattr(s, "_CONSENT_HUB_SECRET", "")
    assert client.get("/health").status_code == 200
    assert client.get("/pending-consents", headers=_headers("anything")).status_code == 404
    r = client.post("/pending-consents/decide", headers=_headers("anything"),
                    json={"manifestId": "x", "decision": "confirm"})
    assert r.status_code == 404


def test_get_pending_consents_lists_awaiting_manifest_with_title_summary_preview(client, hub_secret):
    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "список-тег", "color": None},
        "", "", _describe_tag))
    assert outcome.proceed is False  # фаза плана — превью, ничего не создано
    mid = _mid_of(outcome.message)

    r = client.get("/pending-consents", headers=_headers(hub_secret))
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "ticktick"
    item = next((it for it in body["items"] if it["manifestId"] == mid), None)
    assert item is not None, body["items"]
    assert item["tool"] == "create_tag"
    assert item["title"]
    assert item["summary"]
    assert "preview" in item and item["preview"]
    assert item["createdAt"] <= item["expiresAt"]
    assert "accountLabel" in item


def test_get_pending_consents_excludes_consumed_manifests(client, hub_secret):
    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "погашенный", "color": None},
        "", "", _describe_tag))
    mid = _mid_of(outcome.message)
    consent._MANIFESTS[mid]["consumed"] = True

    r = client.get("/pending-consents", headers=_headers(hub_secret))
    assert all(it["manifestId"] != mid for it in r.json()["items"])


def test_9_decide_confirm_executes_and_second_decide_is_already_decided(
        client, hub_secret, monkeypatch):
    """ТЗ, тест 9: decide confirm реально исполняет (тот же путь, что кнопка
    в Telegram — общий реестр исполнителей), повторный decide на тот же
    манифест — machine-readable already_decided, второй мутации НЕТ."""
    fake_v2 = _FakeTicktickV2()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)

    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "веб-тег", "color": "#FF6161"},
        "", "", _describe_tag))
    mid = _mid_of(outcome.message)

    r = client.post("/pending-consents/decide", headers=_headers(hub_secret),
                    json={"manifestId": mid, "decision": "confirm"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["outcome"] == "confirmed"
    assert "result" in body and "веб-тег" in body["result"]
    assert fake_v2.created == [("веб-тег", "#FF6161")], "исполнитель должен был реально создать тег ровно один раз"

    r2 = client.post("/pending-consents/decide", headers=_headers(hub_secret),
                     json={"manifestId": mid, "decision": "confirm"})
    assert r2.status_code == 409
    assert r2.json() == {"ok": False, "error": "already_decided"}
    assert len(fake_v2.created) == 1, "повторный decide НЕ должен исполнить мутацию ещё раз"


def test_10_decide_reject_with_comment_invalidates_and_records_comment(client, hub_secret):
    """ТЗ, тест 10: decide reject с комментарием — манифест отклонён,
    комментарий записан (здесь — как `_web_reject_comment`, см. отчёт по
    задаче про отсутствие отдельной audit-таблицы user_reply в ticktick-mcp)."""
    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "отклонённый", "color": None},
        "", "", _describe_tag))
    mid = _mid_of(outcome.message)

    r = client.post("/pending-consents/decide", headers=_headers(hub_secret),
                    json={"manifestId": mid, "decision": "reject", "comment": "плохая идея"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "outcome": "refused"}
    assert consent._MANIFESTS[mid]["consumed"] is True
    assert consent._MANIFESTS[mid]["_web_decision"] == "rejected"
    assert consent._MANIFESTS[mid]["_web_reject_comment"] == "плохая идея"


def test_decide_binding_mismatch_refuses_without_consuming(client, hub_secret, monkeypatch):
    fake_v2 = _FakeTicktickV2()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "дрейф", "color": None},
        "", "", _describe_tag))
    mid = _mid_of(outcome.message)
    consent._MANIFESTS[mid]["object_hash"] = "not-the-real-hash-anymore"

    r = client.post("/pending-consents/decide", headers=_headers(hub_secret),
                    json={"manifestId": mid, "decision": "confirm"})
    assert r.status_code == 409
    assert r.json() == {"ok": False, "error": "binding_mismatch"}
    assert consent._MANIFESTS[mid]["consumed"] is False
    assert fake_v2.created == []


def test_decide_unknown_manifest_is_not_found(client, hub_secret):
    r = client.post("/pending-consents/decide", headers=_headers(hub_secret),
                    json={"manifestId": "does-not-exist-at-all", "decision": "confirm"})
    assert r.status_code == 404
    assert r.json() == {"ok": False, "error": "not_found"}


def test_decide_expired_manifest_is_expired_error(client, hub_secret):
    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "протухший", "color": None},
        "", "", _describe_tag))
    mid = _mid_of(outcome.message)
    # Отодвигаем `created` в прошлое дальше TTL — тот же приём, что и в
    # test_manifest_persistence.py.
    consent._MANIFESTS[mid]["created"] -= (consent._MANIFEST_TTL + 60)

    r = client.post("/pending-consents/decide", headers=_headers(hub_secret),
                    json={"manifestId": mid, "decision": "confirm"})
    assert r.status_code == 409
    assert r.json() == {"ok": False, "error": "expired"}


def test_decide_bad_request_shapes(client, hub_secret):
    r1 = client.post("/pending-consents/decide", headers=_headers(hub_secret), json={})
    assert r1.status_code == 400
    assert r1.json()["error"] == "bad_request"

    r2 = client.post("/pending-consents/decide", headers=_headers(hub_secret),
                     json={"manifestId": "x", "decision": "maybe"})
    assert r2.status_code == 400
    assert r2.json()["error"] == "bad_request"


# ===========================================================================
# Регресс: обычный второй вызов тула (manifest_id + user_reply, чат-путь)
# по-прежнему работает как раньше — веб-хаб ничего не отбирает у чат-пути.
# ===========================================================================

def test_normal_chat_confirm_still_works_after_adding_preview_field(monkeypatch):
    monkeypatch.setattr(s, "_ensure_ready", lambda: None)
    fake_v2 = _FakeTicktickV2()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)

    plan_text = asyncio.run(s.create_tag(name="чат-тег", color=None))
    mid = _mid_of(plan_text)

    result = asyncio.run(s.create_tag(name="чат-тег", color=None,
                                      manifest_id=mid, user_reply="да"))
    assert "создан" in result.lower() or "✅" in result
    assert fake_v2.created == [("чат-тег", None)]
