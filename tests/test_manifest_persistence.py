"""Стык «план в памяти ⇄ план в базе» (#91) — та его часть, которой база не
нужна: перевод меток времени, восстановление формы плана и поведение сервера
при ВЫКЛЮЧЕННОЙ долговечности.

Работа с настоящим Postgres — в tests/test_manifest_store_pg.py, сквозной
перезапуск процесса — в tests/test_manifest_restart.py.
"""
import asyncio
import time

import pytest

import ticktick_mcp.src.server as s
from ticktick_mcp.src import manifest_store


@pytest.fixture(autouse=True)
def isolate_manifests():
    """`_MANIFESTS` — глобал модуля, общий на всю сессию тестов (тот же приём,
    что в test_silent_failures.py)."""
    before = dict(s._MANIFESTS)
    s._MANIFESTS.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)


@pytest.fixture(autouse=True)
def no_store(monkeypatch):
    """По умолчанию база ВЫКЛЮЧЕНА, и любое обращение к ней — провал теста.

    Так проверяется главный инвариант совместимости: без `CONSENT_DATABASE_URL`
    сервер обязан вести себя ровно как до #91, не делая ни одного похода в
    Postgres."""
    monkeypatch.setattr(manifest_store, "store_ready", lambda: False)
    for name in ("save", "load", "load_live", "claim", "mark_consumed",
                 "delete", "purge_expired"):
        monkeypatch.setattr(manifest_store, name, _forbidden(name))


def _forbidden(name):
    def boom(*a, **k):
        raise AssertionError(f"без базы manifest_store.{name} звать нельзя")
    return boom


def _plan(age_s=0.0):
    now = time.monotonic() - age_s
    return {"kind": "create_tag", "tool": "create_tag", "_gate": "single",
            "params": {"name": "тег"}, "created": now, "plan_shown_at": now,
            "consumed": False, "tg_notified": True, "_tg_tool": "create_tag"}


# ───────────────────── ВОЗРАСТ ПЛАНА ПЕРЕЖИВАЕТ РЕСТАРТ ─────────────────────
#
# `created`/`plan_shown_at` — это `time.monotonic()`, отсчёт от старта КОНКРЕТНОГО
# процесса. В другом процессе то же число означает совсем другой момент.

def test_round_trip_preserves_the_age_of_a_plan():
    """План 50-минутной давности обязан остаться 50-минутным и в новом
    процессе. Подставь тут «сейчас» — и протухший план (TTL 1 час) снова
    стал бы исполнимым."""
    m = _plan(age_s=3000)
    restored = s._manifest_from_payload(s._durable_payload(m))
    age = time.monotonic() - restored["created"]
    assert 2995 < age < 3005, f"возраст поехал: {age:.0f} c вместо ~3000"


def test_round_trip_preserves_the_moment_the_plan_was_shown():
    """Анти-дуплетный зазор (§4.3.4) сравнивает `plan_shown_at` с «сейчас».
    Сбросив его, мы бы ОТКАЗЫВАЛИ живому подтверждению: сервер решил бы, что
    план показан секунду назад и человек не мог успеть его прочитать."""
    m = _plan(age_s=600)
    m["plan_shown_at"] = time.monotonic() - 300
    restored = s._manifest_from_payload(s._durable_payload(m))
    shown_ago = time.monotonic() - restored["plan_shown_at"]
    assert 295 < shown_ago < 305


def test_expired_plan_stays_expired_after_a_restart():
    """Сквозная проверка того же через настоящий гейт: восстановленный
    протухший план `_require_consent` обязан отбраковать."""
    restored = s._manifest_from_payload(s._durable_payload(_plan(age_s=7200)))
    s._MANIFESTS["m1"] = restored
    cr = s._require_consent(action="create_tag", tier=1, manifest=restored,
                            user_reply="да", manifest_id="m1")
    assert not cr.ok
    assert "истёк" in cr.reason


def test_round_trip_keeps_the_button_only_flag():
    """`tg_notified` закрывает для плана текстовый путь исполнения. Потеряйся
    она при переносе — после перезапуска ту же операцию можно было бы
    подтвердить одним чат-«да», то есть требование кнопки молча ослабло бы."""
    restored = s._manifest_from_payload(s._durable_payload(_plan()))
    assert restored["tg_notified"] is True
    assert s._tg_button_only(restored) is True


def test_restored_plan_is_alive_regardless_of_the_saved_flag():
    """В payload мог попасть снимок с `consumed: True` (план гасили уже после
    записи). Отдают нам строку только если в базе она НЕ израсходована, так
    что источник истины — база, а не снимок."""
    payload = s._durable_payload(_plan())
    payload["consumed"] = True
    assert s._manifest_from_payload(payload)["consumed"] is False


def test_durable_payload_drops_monotonic_fields():
    """Монотонные метки в базу ехать не должны вовсе: в чужом процессе они
    бессмысленны, а лежащее рядом одноимённое поле однажды кто-нибудь прочтёт
    как настоящее."""
    payload = s._durable_payload(_plan(age_s=10))
    assert "created" not in payload and "plan_shown_at" not in payload
    assert payload["_created_ms"] > 0 and payload["_plan_shown_ms"] > 0


# ───────────────── БЕЗ БАЗЫ ВСЁ РАБОТАЕТ РОВНО КАК РАНЬШЕ ────────────────────

def test_without_a_store_nothing_touches_postgres():
    """`no_store` роняет тест на любом обращении к базе — значит достаточно
    просто пройти обычный путь гашения и восстановления."""
    m = _plan()
    s._MANIFESTS["m1"] = m
    s._mark_manifest_consumed(m, "m1")
    assert m["consumed"] is True
    asyncio.run(s._rehydrate_manifest("m1"))
    asyncio.run(s._rehydrate_manifest("нет-такого"))


def test_consume_falls_back_to_memory_when_the_row_is_absent(monkeypatch):
    """План, которого нет в базе (долговечность выключена или запись не
    удалась), виден только этому процессу — и одноразовость для него обязана
    решаться по памяти, как до #91."""
    monkeypatch.setattr(manifest_store, "claim",
                        lambda mid, **k: (manifest_store.CLAIM_ABSENT, None))
    s._MANIFESTS["m1"] = _plan()
    assert s._consume_manifest_for_auto_execute("m1") is not None
    assert s._consume_manifest_for_auto_execute("m1") is None  # уже погашен


def test_consume_refuses_when_the_row_is_taken(monkeypatch):
    """База сказала «занято» (соседняя реплика/старый контейнер) — исполнять
    нельзя, даже если в памяти план выглядит живым."""
    monkeypatch.setattr(manifest_store, "claim",
                        lambda mid, **k: (manifest_store.CLAIM_TAKEN, None))
    s._MANIFESTS["m1"] = _plan()
    assert s._consume_manifest_for_auto_execute("m1") is None
    assert s._MANIFESTS["m1"]["consumed"] is False  # чужой захват нас не гасит


def test_consume_trusts_memory_over_the_database(monkeypatch):
    """Расхождение «в базе свободен, в памяти погашен» разрешается в пользу
    памяти: пропустить исполнение безопаснее, чем выполнить дважды."""
    monkeypatch.setattr(manifest_store, "claim",
                        lambda mid, **k: (manifest_store.CLAIM_WON, {"kind": "x"}))
    m = _plan()
    m["consumed"] = True
    s._MANIFESTS["m1"] = m
    assert s._consume_manifest_for_auto_execute("m1") is None


def test_consume_restores_a_plan_this_process_never_saw(monkeypatch):
    """Захват выиграли, а плана в памяти нет — это и есть «нажали после
    перезапуска». Работаем по восстановленной из базы копии."""
    payload = s._durable_payload(_plan())
    monkeypatch.setattr(manifest_store, "claim",
                        lambda mid, **k: (manifest_store.CLAIM_WON, payload))
    got = s._consume_manifest_for_auto_execute("m1")
    assert got is not None and got["params"] == {"name": "тег"}
    assert got["consumed"] is True


# ───────────────────────── связка «план ⇄ исполнитель» ───────────────────────

def test_the_same_predicate_selects_candidates_from_ram_and_from_the_database():
    """Отбор «этот план сервер умеет исполнить сам» обязан быть ОДНИМ на оба
    источника. Разойдись они — поднятый из базы план молча не попал бы в
    кандидаты, и рестарт снова терял бы операции, только тише."""
    monkey_cfg = s.tg_approval.TgApprovalConfig(
        enabled=True, bot_token="t", owner_chat_id="1", server="ticktick",
        tools_allowlist={"create_tag"}, ttl_s=3600)
    old = s._TG_CFG
    s._TG_CFG = monkey_cfg
    try:
        alive = _plan()
        s._MANIFESTS["m1"] = alive
        assert s._auto_executable_tool(alive) == "create_tag"
        assert ("m1", "create_tag") in s._tg_auto_execute_pending()
        # ...и тот же предикат на копии, поднятой из базы.
        restored = s._manifest_from_payload(s._durable_payload(alive))
        assert s._auto_executable_tool(restored) == "create_tag"
    finally:
        s._TG_CFG = old


def test_a_consumed_plan_is_never_a_candidate():
    m = _plan()
    m["consumed"] = True
    assert s._auto_executable_tool(m) == ""
    assert s._auto_executable_tool(None) == ""
