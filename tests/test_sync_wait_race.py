"""Гонка синхронного ожидания подтверждения (`_sync_wait_for_decision`) —
приёмка правки 2026-08-14 «повторные запросы подтверждения».

СЦЕНАРИЙ БАГА (наблюдался живьём). Владелец подтверждает план в веб-портале
за пару секунд → манифест погашен → портал тут же дёргает
`GET /pending-consents` → тот зовёт `_prune_manifests()` → погашенная запись
УДАЛЯЕТСЯ из `_MANIFESTS` → тик ожидания видит `None` и делает `continue`
(вместо `break`, как написано в исходном ТЗ веб-хаба: `if (!row || row.status
!== "AWAITING_CONSENT") break;`) → крутится до конца окна и возвращает
ОБЫЧНОЕ превью плана с хвостом «вызови этот инструмент снова» → модель просит
подтверждение по второму кругу. И так несколько раз подряд.

Три независимых половины правки, каждая со своим тестом ниже:
  1. исчезнувшая запись = решение принято → выходим и объясняем исход
     (из надгробия), а не крутимся;
  2. пока по плану идёт ожидание, `_prune_manifests` его не удаляет
     (`_sync_waiters`);
  3. в `POST /pending-consents/decide` метка решения и признак «исполняю»
     ставятся ОДНОВРЕМЕННО с погашением, а отчёт — последним шагом, поэтому
     ожидание отдаёт настоящий результат, а не обобщённое «решено где-то».
"""
import asyncio
import re

import pytest
from starlette.testclient import TestClient

import ticktick_mcp.src.consent as consent
import ticktick_mcp.src.server as s
from ticktick_mcp.src.tg_auto_execute import _consume_manifest_for_auto_execute

_MID_RE = re.compile(r"Манифест `([0-9a-f]{6,})`")


def _mid_of(text: str) -> str:
    m = _MID_RE.search(text)
    assert m, f"в тексте нет id манифеста:\n{text}"
    return m.group(1)


@pytest.fixture(autouse=True)
def _isolate_manifests():
    before = dict(consent._MANIFESTS)
    tombstones = dict(consent._MANIFEST_TOMBSTONES)
    yield
    consent._MANIFESTS.clear()
    consent._MANIFESTS.update(before)
    consent._MANIFEST_TOMBSTONES.clear()
    consent._MANIFEST_TOMBSTONES.update(tombstones)


@pytest.fixture(autouse=True)
def _fast_wait(monkeypatch):
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 600)
    monkeypatch.setattr(consent, "CONSENT_SYNC_POLL_MS", 20)


class _FakeTicktickV2:
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


def _describe_tag(params):
    return f"Тег «{params.get('name')}»"


# ===========================================================================
# 1. Исчезнувшая запись = решение принято (а не «крутись дальше»)
# ===========================================================================

def test_vanished_manifest_ends_the_wait_with_an_execution_report():
    """Манифест погашен И удалён из памяти прямо во время ожидания (то, что
    делал `_prune_manifests` по звонку портала). Ожидание обязано закончиться
    ОТЧЁТОМ ОБ ИСПОЛНЕНИИ, а не None — None означает «никто не решил» и даёт
    вызывающему обычное превью плана с приглашением позвать инструмент
    снова."""
    mid = "vanished-mid"
    consent._MANIFESTS[mid] = {"consumed": False, "created": 0.0}

    async def _vanish():
        await asyncio.sleep(0.05)
        consent._tombstone_manifest(mid, consent._TOMBSTONE_EXECUTED)
        consent._MANIFESTS.pop(mid, None)

    async def _both():
        return await asyncio.gather(
            consent._sync_wait_for_decision(mid), _vanish())

    result, _ = asyncio.run(_both())
    assert result is not None, "ожидание не должно возвращать None по решённому плану"
    assert "УЖЕ исполнен" in result
    assert "снова" not in result.lower()


def test_vanished_manifest_without_a_tombstone_is_still_not_a_plan_preview():
    """Надгробия нет (план убрали мимо обычных путей) — всё равно честное
    «решение принято», НИКОГДА не None."""
    mid = "vanished-no-tombstone"
    consent._MANIFESTS[mid] = {"consumed": False, "created": 0.0}

    async def _vanish():
        await asyncio.sleep(0.05)
        consent._MANIFESTS.pop(mid, None)

    async def _both():
        return await asyncio.gather(
            consent._sync_wait_for_decision(mid), _vanish())

    result, _ = asyncio.run(_both())
    assert result is not None
    assert "уже принято" in result
    assert "НЕ нужно" in result


# ===========================================================================
# 2. Prune не выдёргивает план из-под живого ожидания
# ===========================================================================

def test_prune_keeps_a_manifest_while_a_sync_wait_holds_it():
    """`_prune_manifests()` (его зовёт КАЖДЫЙ `GET /pending-consents` и каждый
    гейт) не имеет права удалить погашенный план, пока по нему идёт
    ожидание: исход решения читается ТОЛЬКО из самого манифеста."""
    mid = "held-mid"
    consent._MANIFESTS[mid] = {"consumed": False, "created": 0.0}

    async def _decide_then_prune():
        await asyncio.sleep(0.05)
        m = consent._MANIFESTS[mid]
        m["consumed"] = True
        m["_web_decision"] = "confirmed"
        m["_web_result"] = "✅ Отчёт об исполнении (веб)"
        consent._prune_manifests()          # ← ровно то, что делал портал
        assert mid in consent._MANIFESTS, "план вырвали из-под ожидания"

    async def _both():
        return await asyncio.gather(
            consent._sync_wait_for_decision(mid), _decide_then_prune())

    result, _ = asyncio.run(_both())
    assert result == "✅ Отчёт об исполнении (веб)"


def test_hold_is_released_and_prune_collects_the_manifest_afterwards():
    """Задержка удаления ограничена окном ожидания: как только оно кончилось,
    обычный prune убирает план как раньше (утечки памяти нет)."""
    mid = "released-mid"
    consent._MANIFESTS[mid] = {"consumed": True, "created": 0.0,
                               "_web_decision": "confirmed",
                               "_web_result": "✅ готово"}
    assert asyncio.run(consent._sync_wait_for_decision(mid)) == "✅ готово"
    assert "_sync_waiters" not in consent._MANIFESTS[mid]
    consent._prune_manifests()
    assert mid not in consent._MANIFESTS


# ===========================================================================
# 3. Порядок записи в /pending-consents/decide: метка и результат видны
#    ожиданию вместе с фактом погашения
# ===========================================================================

def test_confirmed_but_report_not_written_yet_waits_for_the_real_result():
    """Между погашением и записью отчёта проходит настоящее исполнение
    (секунды). Тик ожидания в это окно обязан ЖДАТЬ отчёт, а не отдавать
    обобщённое «решение принято через другой канал»."""
    mid = "in-flight-mid"
    consent._MANIFESTS[mid] = {"consumed": False, "created": 0.0}

    async def _web_decide():
        m = consent._MANIFESTS[mid]
        await asyncio.sleep(0.05)
        m["_web_decision"] = "confirmed"     # метка ДО погашения
        m["_web_in_flight"] = True
        m["consumed"] = True
        await asyncio.sleep(0.15)            # «исполняем»
        m["_web_result"] = "### 🧾 Отчёт: создан тег «X»"
        m["_web_in_flight"] = False

    async def _both():
        return await asyncio.gather(
            consent._sync_wait_for_decision(mid), _web_decide())

    result, _ = asyncio.run(_both())
    assert result == "### 🧾 Отчёт: создан тег «X»"
    assert "другой канал" not in result


def test_in_flight_execution_outliving_the_window_is_not_a_plan_preview(monkeypatch):
    """Отчёт не успел дописаться до конца окна — возвращается честное
    «подтверждено, исполняется», а не None (иначе модель показала бы превью и
    попросила подтвердить уже подтверждённое)."""
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 120)
    mid = "slow-exec-mid"
    consent._MANIFESTS[mid] = {"consumed": True, "created": 0.0,
                               "_web_decision": "confirmed",
                               "_web_in_flight": True}
    result = asyncio.run(consent._sync_wait_for_decision(mid))
    assert result is not None
    assert "ПРЯМО СЕЙЧАС" in result


@pytest.fixture
def hub_secret(monkeypatch):
    secret = "race-hub-secret"
    monkeypatch.setattr(s, "_CONSENT_HUB_SECRET", secret)
    return secret


@pytest.fixture
def client():
    return TestClient(s.mcp.streamable_http_app())


def test_full_web_confirm_during_wait_returns_the_execution_report_not_a_plan(
        client, hub_secret, monkeypatch):
    """СКВОЗНОЙ сценарий бага целиком: фаза плана ждёт, портал в это же время
    читает список ожидающих (внутри — `_prune_manifests`) и подтверждает
    план. Инструмент обязан вернуть отчёт об исполнении, а не превью с
    «вызови снова»."""
    fake_v2 = _FakeTicktickV2()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 3000)
    monkeypatch.setattr(consent, "CONSENT_SYNC_POLL_MS", 20)

    seen = {}

    async def _plan_side():
        outcome = await consent._gate_single(
            "create_tag", "create_tag", {"name": "гоночный", "color": None},
            "", "", _describe_tag)
        return outcome

    async def _portal_side():
        # Дожидаемся, пока манифест появится (фаза плана его уже создала).
        for _ in range(200):
            await asyncio.sleep(0.01)
            mids = [k for k, v in consent._MANIFESTS.items()
                    if (v.get("params") or {}).get("name") == "гоночный"]
            if mids:
                seen["mid"] = mids[0]
                break
        assert "mid" in seen, "план так и не появился"
        # Портал: читает список (внутри `_prune_manifests`) и подтверждает.
        r = client.get("/pending-consents",
                       headers={"x-consent-hub-secret": hub_secret})
        assert r.status_code == 200
        r = client.post("/pending-consents/decide",
                        headers={"x-consent-hub-secret": hub_secret},
                        json={"manifestId": seen["mid"], "decision": "confirm"})
        assert r.status_code == 200, r.text
        # И ещё раз дёргает список — именно этот вызов раньше выбрасывал
        # погашенный манифест из памяти прямо под ожиданием.
        client.get("/pending-consents",
                   headers={"x-consent-hub-secret": hub_secret})

    async def _both():
        return await asyncio.gather(_plan_side(), _portal_side())

    outcome, _ = asyncio.run(_both())
    assert outcome.proceed is False          # мутацию делает веб-путь, не гейт
    assert fake_v2.created == [("гоночный", None)], "исполнение ровно одно"
    assert "гоночный" in outcome.message
    assert "вызови этот же инструмент снова" not in outcome.message.lower()
    assert "manifest_id=" not in outcome.message, \
        "это должен быть отчёт об исполнении, а не превью плана"


def test_decide_marks_the_decision_atomically_with_consuming(
        client, hub_secret, monkeypatch):
    """Третья гонка, точечно: `_consume_manifest_for_auto_execute` уходит в
    рабочий поток и ставит `consumed` ТАМ. Пометка `_web_decision` обязана
    быть видна тику ожидания ОДНОВРЕМЕННО с фактом погашения — иначе окно
    «погашено, метки нет» отдаёт обобщённое «решено другим каналом» вместо
    результата этого самого веб-подтверждения.

    Проверка смотрит на состояние манифеста РОВНО в момент возврата из
    захвата, до любой следующей строки обработчика."""
    fake_v2 = _FakeTicktickV2()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 0)

    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "атомарный", "color": None},
        "", "", _describe_tag))
    mid = _mid_of(outcome.message)

    real_run_blocking = s._run_blocking
    observed = {}

    async def _spy(fn, *args, **kwargs):
        result = await real_run_blocking(fn, *args, **kwargs)
        if getattr(fn, "__name__", "") == "_consume_manifest_for_auto_execute":
            m = consent._MANIFESTS[mid]
            observed["consumed"] = m.get("consumed")
            observed["decision"] = m.get("_web_decision")
            observed["in_flight"] = m.get("_web_in_flight")
        return result

    monkeypatch.setattr(s, "_run_blocking", _spy)
    r = client.post("/pending-consents/decide",
                    headers={"x-consent-hub-secret": hub_secret},
                    json={"manifestId": mid, "decision": "confirm"})
    assert r.status_code == 200, r.text
    assert observed["consumed"] is True
    assert observed["decision"] == "confirmed", \
        "метка решения обязана стоять уже в момент погашения, а не после него"
    assert observed["in_flight"] is True, \
        "признак «исполняю прямо сейчас» тоже обязан быть виден сразу"
    # А по завершении — готовый отчёт и снятый признак исполнения.
    m = consent._MANIFESTS[mid]
    assert m.get("_web_in_flight") is False
    assert "атомарный" in (m.get("_web_result") or "")


# ===========================================================================
# 4. Двойного исполнения нет: веб и Telegram-кнопка одновременно
# ===========================================================================

def test_telegram_button_wins_the_race_and_web_decide_does_not_execute_twice(
        client, hub_secret, monkeypatch):
    """Кнопка в Telegram захватила план первой (тот же атомарный захват, что у
    фонового поллера) — веб-подтверждение обязано ответить `already_decided`,
    не исполнив мутацию во второй раз."""
    fake_v2 = _FakeTicktickV2()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 0)

    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "дубль", "color": None},
        "", "", _describe_tag))
    mid = _mid_of(outcome.message)

    claimed = _consume_manifest_for_auto_execute(mid)     # «нажали кнопку»
    assert claimed is not None

    r = client.post("/pending-consents/decide",
                    headers={"x-consent-hub-secret": hub_secret},
                    json={"manifestId": mid, "decision": "confirm"})
    assert r.status_code == 409
    assert r.json() == {"ok": False, "error": "already_decided"}
    assert fake_v2.created == [], "веб-путь не имел права исполнять захваченный план"


def test_losing_web_decide_leaves_no_confirmed_marker_behind(
        client, hub_secret, monkeypatch):
    """Проигравший гонку веб-путь снимает свои пометки: иначе ожидание
    рассказало бы про «подтверждено и исполнено через веб» операцию, которую
    веб не исполнял."""
    fake_v2 = _FakeTicktickV2()
    monkeypatch.setattr(s, "ticktick_v2", fake_v2)
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 0)

    outcome = asyncio.run(consent._gate_single(
        "create_tag", "create_tag", {"name": "проигравший", "color": None},
        "", "", _describe_tag))
    mid = _mid_of(outcome.message)
    _consume_manifest_for_auto_execute(mid)

    client.post("/pending-consents/decide",
                headers={"x-consent-hub-secret": hub_secret},
                json={"manifestId": mid, "decision": "confirm"})

    m = consent._MANIFESTS[mid]
    assert "_web_decision" not in m
    assert "_web_in_flight" not in m
    # А ожидание по такому плану честно говорит «решено другим каналом».
    monkeypatch.setattr(consent, "CONSENT_SYNC_WAIT_MS", 60)
    monkeypatch.setattr(consent, "CONSENT_SYNC_POLL_MS", 10)
    text = asyncio.run(consent._sync_wait_for_decision(mid))
    assert text is not None and "другой канал" in text
