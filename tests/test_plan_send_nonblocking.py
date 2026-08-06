"""Отправка плана в Telegram не держит event loop (задача #115, 2026-08-06).

СИМПТОМ С ПРОДА. Фаза плана каждого гейтованного тула шлёт превью владельцу
одним или несколькими сообщениями. Telegram душит плотную серию сообщений в
один чат: на 12-13-м подряд он отвечает «429 Too Many Requests: retry after
N», и наш клиент честно отсиживает эти N секунд (`send_message_chunked`,
до `_MAX_SEND_WAIT_S` = 180 c на КАЖДЫЙ кусок). Пока это делалось прямо в
корутине, стоял ВЕСЬ сервер: другие вызовы инструментов, фоновый поллер
автоисполнения, /health. Снаружи — «сервис завис на несколько минут».

ПОЧЕМУ ЭТО ВЫЛЕЗЛО ИМЕННО СЕЙЧАС. Раньше план уходил в Telegram у двух тулов,
и оба звали хук через `_run_blocking` вручную. После перевода ВСЕХ мутирующих
действий на подтверждение кнопкой таких тулов стало 22, и 19 из них пошли
через общие гейты `_gate_batch`/`_gate_single`, где ручной обёртки не было.
Правильная починка усилила скрытую проблему — цена росла с успехом.

ПОЧЕМУ ДВОЙНИК TELEGRAM ЗДЕСЬ ЗЛОЙ, А НЕ ДОБРЫЙ. Тест, в котором отправка
мгновенна и всегда успешна, эту задачу не проверяет вообще: беда состоит
ровно в ожидании. Поэтому подменяется САМЫЙ НИЖНИЙ слой — `tg_approval._tg_call`
(единственное место, где живёт `requests.post`), а вся логика нарезки, пауз
между кусками и сна на 429 остаётся НАСТОЯЩЕЙ. Двойник отвечает так же, как
живой Bot API: `{"ok": false, "error_code": 429, "parameters": {"retry_after": 1}}`
и с задержкой сетевого round-trip. Сны в тесте настоящие — цена честности
несколько секунд на файл.

Postgres (`tg_approvals`) подменён журналом в памяти: он не участвует в беде,
но обязан фиксировать ПОРЯДОК обращений — на нём держится вторая половина
задачи (нажатие не должно приходить в пустоту).
"""
import asyncio
import re
import time

import pytest

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ───────────────────────────── общее окружение ─────────────────────────────

@pytest.fixture(autouse=True)
def _clean_manifests():
    """`_MANIFESTS` — глобал на всю сессию тестов; снимок/восстановление
    делают файл независимым от порядка запуска (тот же приём, что в
    test_tg_auto_execute.py и test_tg_poller_perf.py)."""
    before = dict(s._MANIFESTS)
    s._MANIFESTS.clear()
    yield
    s._MANIFESTS.clear()
    s._MANIFESTS.update(before)


def _enable_tg(monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", tg.TgApprovalConfig(
        enabled=True, bot_token="x", owner_chat_id="1", server="ticktick",
        tools_allowlist=None, ttl_s=3600))


class _FakeTelegram:
    """Двойник Bot API на уровне HTTP-вызова (`tg._tg_call`).

    Ведёт себя как настоящий сервер, а не как удобная заглушка:
      • каждый вызов стоит времени (round-trip), пусть и маленького;
      • на попытках из `flood_on` отвечает НАСТОЯЩИМ форматом 429 —
        `error_code` + `parameters.retry_after`, — из-за чего наш клиент
        честно спит retry_after секунд и повторяет кусок;
      • sendMessage возвращает растущие message_id, как живой Telegram.

    `log` — общий журнал с двойником Postgres, по нему проверяется порядок
    «строка → сообщение → обновление строки»."""

    def __init__(self, log, flood_on=(), rtt_s: float = 0.01,
                 retry_after: int = 1, hard_fail_on=()):
        self.log = log
        self.flood_on = set(flood_on)
        self.hard_fail_on = set(hard_fail_on)
        self.rtt_s = rtt_s
        self.retry_after = retry_after
        self.sends = 0            # сколько раз дёрнули sendMessage
        self.flood_replies = 0    # сколько раз ответили 429
        self.sent_texts = []      # тексты РЕАЛЬНО принятых сообщений
        self._next_id = 1000

    def __call__(self, cfg, method, body):
        time.sleep(self.rtt_s)
        if method != "sendMessage":
            self.log.append((method, body.get("message_id")))
            return {"ok": True, "result": {}, "description": None,
                    "error_code": None, "parameters": {}}
        self.sends += 1
        if self.sends in self.flood_on:
            self.flood_replies += 1
            self.log.append(("flood", self.sends))
            return {"ok": False, "result": None,
                    "description": f"Too Many Requests: retry after {self.retry_after}",
                    "error_code": 429,
                    "parameters": {"retry_after": self.retry_after}}
        if self.sends in self.hard_fail_on:
            self.log.append(("reject", self.sends))
            return {"ok": False, "result": None,
                    "description": "Bad Request: chat not found",
                    "error_code": 400, "parameters": {}}
        self._next_id += 1
        self.sent_texts.append(body.get("text") or "")
        self.log.append(("send", self._next_id))
        return {"ok": True, "result": {"message_id": self._next_id},
                "description": None, "error_code": None, "parameters": {}}


def _fake_store(monkeypatch, log):
    """Двойник строки подтверждения в Postgres. Ничего не «чинит» за
    вызывающего: просто записывает, ЧТО и в каком порядке его попросили
    сделать."""
    rows = {}

    def _create(manifest_id, chat_id, message_id, expires_at, extra_ids):
        log.append(("create_row", manifest_id))
        rows[manifest_id] = {"message_id": message_id,
                             "extra_message_ids": list(extra_ids or [])}

    def _attach(manifest_id, message_id, extra_ids):
        log.append(("attach_row", message_id))
        if manifest_id not in rows:
            return False
        rows[manifest_id].update(message_id=message_id,
                                 extra_message_ids=list(extra_ids or []))
        return True

    def _delete(manifest_id):
        log.append(("delete_row", manifest_id))
        rows.pop(manifest_id, None)

    def _check(manifest_id):
        """Тот же ответ, что дал бы живой Postgres в этот момент: строка
        вставлена первым шагом notify_plan и решения по ней ещё нет →
        "pending". Нет строки → "none"."""
        log.append(("check_row", manifest_id))
        return "pending" if manifest_id in rows else "none"

    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "create_tg_approval", _create)
    monkeypatch.setattr(tg, "attach_plan_messages", _attach)
    monkeypatch.setattr(tg, "delete_tg_approval", _delete)
    monkeypatch.setattr(tg, "check_approval", _check)
    return rows


class _FakeOfficialClient:
    """Минимальный клиент официального API — только для ЧИТАЮЩЕГО тула,
    которым проверяется, что сервер продолжает обслуживать запросы."""

    def __init__(self):
        self.calls = 0

    def get_project_with_data(self, project_id):
        self.calls += 1
        return {"project": {"name": "Покупки"},
                "tasks": [{"id": "t1", "title": "Купить молоко", "status": 0}]}


def _long_plan_tasks(n: int = 30):
    """Столько задач, чтобы превью гарантированно не влезло в одно сообщение
    Telegram (4096 символов) и пошло сериями — именно серия и ловит 429."""
    return [{"taskId": f"t{i}", "title": f"Задача номер {i} — " + "щ" * 150}
            for i in range(n)]


async def _wait_until(pred, timeout_s: float = 5.0):
    deadline = time.monotonic() + timeout_s
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("не дождались условия за отведённое время")
        await asyncio.sleep(0.005)


def _plan_id_from(text: str) -> str:
    """Идентификатор плана берётся ИЗ ТЕКСТА, который реально ушёл в Telegram
    — тем же путём, каким его видит человек и модель. Лазить за ним в
    `_MANIFESTS` (память процесса) тест не имеет права: снаружи такого пути
    нет, и проверка стала бы легче настоящей жизни."""
    # В Telegram текст уходит уже переведённым в HTML, поэтому id обёрнут в
    # <code>…</code>, а не в обратные кавычки исходного markdown.
    m = re.search(r"Манифест\s*<code>([0-9a-f]+)</code>", text)
    assert m, f"в отправленном тексте нет идентификатора плана: {text[:200]!r}"
    return m.group(1)


# ═══════════ 1. Беда целиком: флуд-отказы не морозят другие тулы ═══════════

async def test_flood_wait_on_plan_send_does_not_freeze_other_tools(monkeypatch):
    """ВОСПРОИЗВЕДЕНИЕ ЗАДАЧИ #115.

    Пока длинный план отсиживает серию «слишком много запросов», параллельный
    вызов ДРУГОГО инструмента обязан быть обслужен — а не встать в очередь за
    ним. Сломать вынос в поток (вернуть синхронный вызов `notify_plan` внутрь
    `_maybe_tg_notify_plan`) — и тест краснеет сразу двумя проверками: план
    успевает завершиться ещё до того, как тест доберётся до второго тула, и
    посторонняя корутина не проворачивается ни разу."""
    _enable_tg(monkeypatch)
    log = []
    fake_tg = _FakeTelegram(log, flood_on={1, 3})   # по отказу на первые два куска
    monkeypatch.setattr(tg, "_tg_call", fake_tg)
    _fake_store(monkeypatch, log)

    official = _FakeOfficialClient()
    monkeypatch.setattr(s, "_ensure_official", lambda: None)
    monkeypatch.setattr(s, "ticktick", official)

    beats = {"n": 0}

    async def _beat():
        while True:
            await asyncio.sleep(0.01)
            beats["n"] += 1

    hb = asyncio.create_task(_beat())
    started = time.monotonic()
    plan = asyncio.create_task(
        s.complete_tasks("Завершаю 30 задач", _long_plan_tasks()))
    try:
        # ждём, пока отправка реально началась (первый поход в Telegram)
        await _wait_until(lambda: fake_tg.sends >= 1)

        t0 = time.monotonic()
        other = await s.get_project_tasks("p1")
        other_took = time.monotonic() - t0

        assert not plan.done(), (
            "другой инструмент обслужен только ПОСЛЕ того, как отправка плана "
            "закончилась — значит она держала event loop")
        assert other_took < 0.5, (
            f"читающий тул ждал отправки плана {other_took:.2f} c")
        assert other_group_ok(other), other
        assert official.calls == 1

        out = await plan
    finally:
        hb.cancel()

    plan_took = time.monotonic() - started
    # Сама беда действительно воспроизведена: Telegram отказал по частоте, и
    # отправка честно отсидела паузы (иначе тест проверял бы пустоту).
    assert fake_tg.flood_replies == 2, fake_tg.flood_replies
    assert plan_took > 2 * 1.0, (
        f"отправка не отсиживала флуд-паузы ({plan_took:.2f} c) — сценарий "
        "выродился, тест ничего не проверяет")
    assert beats["n"] >= 5, (
        f"посторонняя корутина провернулась {beats['n']} раз — event loop стоял")
    assert "Telegram" in out and "Манифест" in out


def other_group_ok(text: str) -> bool:
    return "Купить молоко" in text


# ═══════════ 2. Порядок «строка → сообщение → обновление строки» ═══════════

async def test_row_is_written_before_the_first_message_and_updated_after_the_last(
        monkeypatch):
    """Порядок внутри `notify_plan` — не стилистика, а защита от «нажатия в
    пустоту»: кнопка видна человеку сразу, и если строки в Postgres ещё нет,
    вебхук обновлять нечего — нажатие теряется навсегда, а сообщение висит
    живым. Вынос отправки в поток обязан этот порядок сохранить: функция
    уезжает в поток ЦЕЛИКОМ, а не по кускам."""
    _enable_tg(monkeypatch)
    log = []
    fake_tg = _FakeTelegram(log, flood_on={2})   # отказ в СЕРЕДИНЕ серии
    monkeypatch.setattr(tg, "_tg_call", fake_tg)
    _fake_store(monkeypatch, log)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    out = await s.complete_tasks("Завершаю 30 задач", _long_plan_tasks())

    kinds = [k for k, _ in log]
    assert kinds[0] == "create_row", f"строка создана не первой: {kinds}"
    assert "send" in kinds, kinds
    assert kinds[-1] == "attach_row", f"строка обновлена не последней: {kinds}"
    # Между созданием строки и первым сообщением ничего в Telegram не уходило.
    assert kinds.index("create_row") < kinds.index("send")
    # Обновление — строго после ПОСЛЕДНЕГО доставленного сообщения.
    assert kinds.index("attach_row") > len(kinds) - 2 or \
        kinds.index("attach_row") > max(i for i, k in enumerate(kinds) if k == "send")
    # id, записанный в строку, — это id ПОСЛЕДНЕГО сообщения (на нём кнопки).
    attach_id = [v for k, v in log if k == "attach_row"][0]
    last_send_id = [v for k, v in log if k == "send"][-1]
    assert attach_id == last_send_id
    assert fake_tg.flood_replies == 1
    assert "Telegram" in out


# ═══════════ 3. Гонка, которую создаёт сам вынос в поток ═══════════

async def test_text_yes_is_refused_while_the_plan_is_still_being_sent(monkeypatch):
    """ГОНКА, ПОРОЖДЁННАЯ АСИНХРОННОСТЬЮ (и закрытая ранней пометкой).

    Пока отправка была синхронной, между «кнопки появились в Telegram» и
    «манифест помечен `tg_notified`» не мог влезть никто: event loop стоял.
    Теперь loop свободен — и в это окно попадает вызов execute с текстовым
    «да». Если пометки ещё нет, `_tg_button_only` пропускает, текстовый путь
    открыт, и операция выполняется БЕЗ второго фактора — ровно то, что
    закрывали кнопкой.

    Идентификатор плана тест берёт из УЖЕ ДОСТАВЛЕННОГО первого куска, а не
    из `_MANIFESTS`: именно так его видит владелец (и модель) в реальной
    жизни, когда длинный план ещё дописывается в чат.

    Убрать раннюю пометку в `_maybe_tg_notify_plan` (вернуть простановку
    ПОСЛЕ await) — и тест краснеет: `_complete_tasks_impl` окажется вызван."""
    _enable_tg(monkeypatch)
    log = []
    # Отказ на ВТОРОМ куске: первый уже в чате (id виден), отправка ещё идёт.
    fake_tg = _FakeTelegram(log, flood_on={2}, retry_after=1)
    monkeypatch.setattr(tg, "_tg_call", fake_tg)
    _fake_store(monkeypatch, log)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    executed = {"n": 0}

    async def _never(*a, **kw):
        executed["n"] += 1
        return "выполнено"

    monkeypatch.setattr(s, "_complete_tasks_impl", _never)

    plan = asyncio.create_task(
        s.complete_tasks("Завершаю 30 задач", _long_plan_tasks()))
    try:
        await _wait_until(lambda: fake_tg.sent_texts)
        mid = _plan_id_from(fake_tg.sent_texts[0])

        verdict = await s.complete_tasks("Завершаю 30 задач", _long_plan_tasks(),
                                         manifest_id=mid, user_reply="да, давай")

        assert executed["n"] == 0, (
            "текстовое «да» исполнило план, пока его ещё дописывали в "
            "Telegram — второй фактор обошли")
        assert "кнопк" in verdict.lower(), verdict
        assert not plan.done(), "отправка успела закончиться — окно не проверено"
        out = await plan
    finally:
        if not plan.done():
            plan.cancel()

    assert executed["n"] == 0
    assert "Telegram" in out


# ═══════════ 4. Провал отправки не оставляет требования кнопки ═══════════

async def test_failed_send_leaves_no_stale_button_requirement(monkeypatch):
    """Обратная сторона ранней пометки: если отправка ПРОВАЛИЛАСЬ, состояние
    манифеста обязано быть ровно тем же, что и до выноса в поток — план
    погашен (fail-closed), а следа `tg_notified` не осталось. Иначе мёртвый
    манифест выглядел бы как «ждёт кнопки, которой нет»."""
    _enable_tg(monkeypatch)
    log = []
    fake_tg = _FakeTelegram(log, hard_fail_on={1})   # 400, не 429 — повтор не поможет
    monkeypatch.setattr(tg, "_tg_call", fake_tg)
    _fake_store(monkeypatch, log)
    monkeypatch.setattr(s, "_ensure_official", lambda: None)

    out = await s.complete_tasks("Завершаю 30 задач", _long_plan_tasks())

    assert "🛑" in out and "Не смог отправить" in out
    manifests = list(s._MANIFESTS.values())
    assert len(manifests) == 1
    m = manifests[0]
    assert m["consumed"] is True, "провал отправки обязан гасить план"
    assert "tg_notified" not in m, (
        "предварительная пометка не откатилась — мёртвый план остался "
        "помеченным как ушедший в Telegram")
    # Строка-сирота в Postgres тоже прибрана.
    assert ("delete_row", [v for k, v in log if k == "create_row"][0]) in log
