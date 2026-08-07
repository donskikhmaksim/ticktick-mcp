"""own_bot (`TG_BOT_TOKEN_OVERRIDE`) — собственный Telegram-бот/вебхук вместо
общего (см. блок-докстринг в шапке tg_approval.py). Нет реальной сети, нет
реального Postgres — Telegram HTTP и хранилище фейковые/monkeypatch'нутые,
та же дисциплина, что у tests/test_tg_approval.py.

Инвариант обратной совместимости, который держит весь этот файл: БЕЗ
TG_BOT_TOKEN_OVERRIDE (own_bot=False, дефолт) всё поведение — конфиг, роут
`/tg/webhook`, main() — обязано быть побитово прежним. Позитивные тесты на
own_bot=True идут ОТДЕЛЬНО и не подменяют негативные (own_bot=False всё ещё
проверяется явно ниже)."""
import pytest
from starlette.testclient import TestClient

import ticktick_mcp.src.server as s
import ticktick_mcp.src.tg_approval as tg


# ═══════════════════════════════════════════════════════════════════════════
# 1. load_tg_approval_config — TG_BOT_TOKEN_OVERRIDE парсинг
# ═══════════════════════════════════════════════════════════════════════════

def test_without_override_behaves_as_before(monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN_OVERRIDE", raising=False)
    monkeypatch.delenv("TG_APPROVAL_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("TG_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("TG_BOT_TOKEN", "shared-token")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "123")
    cfg = tg.load_tg_approval_config()
    assert cfg.own_bot is False
    assert cfg.bot_token == "shared-token"
    assert cfg.webhook_secret == ""


def test_override_wins_over_shared_token_and_sets_own_bot(monkeypatch):
    monkeypatch.setenv("TG_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("TG_BOT_TOKEN", "shared-token")
    monkeypatch.setenv("TG_BOT_TOKEN_OVERRIDE", "own-token")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "123")
    monkeypatch.setenv("TG_APPROVAL_WEBHOOK_SECRET", "whsecret")
    cfg = tg.load_tg_approval_config()
    assert cfg.own_bot is True
    assert cfg.bot_token == "own-token"  # НЕ shared-token
    assert cfg.webhook_secret == "whsecret"


def test_own_bot_without_webhook_secret_raises(monkeypatch):
    monkeypatch.setenv("TG_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("TG_BOT_TOKEN_OVERRIDE", "own-token")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "123")
    monkeypatch.delenv("TG_APPROVAL_WEBHOOK_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="TG_APPROVAL_WEBHOOK_SECRET"):
        tg.load_tg_approval_config()


def test_own_bot_flag_is_independent_of_enabled(monkeypatch):
    """own_bot вычисляется из одной непустоты TG_BOT_TOKEN_OVERRIDE — так же,
    как у TS-референса (`ownBot = !!botTokenOverride`). Проверка секрета при
    этом гейтуется ТОЛЬКО через `enabled` (см. следующий тест) — тот же
    паттерн, что и у существующей проверки bot_token/owner_chat_id."""
    monkeypatch.delenv("TG_APPROVAL_ENABLED", raising=False)
    monkeypatch.setenv("TG_BOT_TOKEN_OVERRIDE", "own-token")
    monkeypatch.delenv("TG_APPROVAL_WEBHOOK_SECRET", raising=False)
    cfg = tg.load_tg_approval_config()
    assert cfg.enabled is False
    assert cfg.own_bot is True


def test_disabled_layer_with_override_set_does_not_raise(monkeypatch):
    """TG_APPROVAL_ENABLED=false (дефолт) — ни один из новых fail-fast чеков
    не срабатывает, даже если TG_BOT_TOKEN_OVERRIDE явно оставлен в env
    (частый случай: переменную один раз задали, потом временно выключили
    TG_APPROVAL_ENABLED для отладки)."""
    monkeypatch.delenv("TG_APPROVAL_ENABLED", raising=False)
    monkeypatch.setenv("TG_BOT_TOKEN_OVERRIDE", "own-token")
    monkeypatch.delenv("TG_APPROVAL_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("TG_OWNER_CHAT_ID", raising=False)
    cfg = tg.load_tg_approval_config()  # не бросает
    assert cfg.enabled is False


def test_missing_bot_token_error_still_mentions_plain_var(monkeypatch):
    """Регрессия для test_tg_approval.py::test_enabled_without_bot_token_raises
    (matches on "TG_BOT_TOKEN") — сообщение расширено про TG_BOT_TOKEN_OVERRIDE,
    но обязано по-прежнему содержать голое имя TG_BOT_TOKEN."""
    monkeypatch.setenv("TG_APPROVAL_ENABLED", "true")
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TG_BOT_TOKEN_OVERRIDE", raising=False)
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "123")
    with pytest.raises(RuntimeError, match="TG_BOT_TOKEN"):
        tg.load_tg_approval_config()


def test_config_dataclass_still_constructible_the_old_way():
    """Существующие вызовы (десятки в tests/, server.py) конструируют
    TgApprovalConfig без own_bot/webhook_secret — оба поля ОБЯЗАНЫ иметь
    дефолты, иначе это TypeError на каждом таком вызове."""
    cfg = tg.TgApprovalConfig(enabled=True, bot_token="x", owner_chat_id="1",
                              server="ticktick", tools_allowlist=None, ttl_s=3600)
    assert cfg.own_bot is False
    assert cfg.webhook_secret == ""


# ═══════════════════════════════════════════════════════════════════════════
# 2. secret_token_matches — constant-time, non-ASCII-safe
# ═══════════════════════════════════════════════════════════════════════════

def test_secret_token_matches_empty_sides_never_match():
    assert tg.secret_token_matches("", "x") is False
    assert tg.secret_token_matches("x", "") is False
    assert tg.secret_token_matches("", "") is False


def test_secret_token_matches_mismatch_and_match():
    assert tg.secret_token_matches("abc", "xyz") is False
    assert tg.secret_token_matches("abc", "abc") is True


def test_secret_token_matches_non_ascii_does_not_raise():
    """Регрессия того же класса, что у server.py's _automation_key_matches:
    hmac.compare_digest на СЫРЫХ str требует ASCII с обеих сторон, иначе
    TypeError вместо честного False. Заголовок — вход снаружи (атака может
    прислать что угодно), не-ASCII не должен ронять вебхук исключением."""
    assert tg.secret_token_matches("секрет-с-кириллицей", "abc") is False
    assert tg.secret_token_matches("секрет", "секрет") is True
    assert tg.secret_token_matches("🔑emoji", "🔑emoji") is True


# ═══════════════════════════════════════════════════════════════════════════
# 3. consume_tg_decision — атомарный server-scoped consume
# ═══════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, sink, row):
        self.sink, self._row = sink, row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, sink, row):
        self.sink, self._row = sink, row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self.sink, self._row)


class _FakePool:
    def __init__(self, sink, row):
        self.sink, self._row = sink, row

    def getconn(self):
        return _FakeConn(self.sink, self._row)

    def putconn(self, conn):
        pass


def test_consume_tg_decision_without_store_is_a_noop(monkeypatch):
    monkeypatch.setattr(tg, "_pg_pool", None)
    assert tg.consume_tg_decision("m1", "APPROVED") is None


def test_consume_tg_decision_sql_is_server_scoped_atomic_and_returns_row(monkeypatch):
    sink = []
    monkeypatch.setattr(tg, "_pg_pool", _FakePool(sink, ("c1", 42)))
    out = tg.consume_tg_decision("m1", "APPROVED")
    assert out == {"chat_id": "c1", "message_id": 42}
    sql, params = sink[0]
    assert sql.startswith("UPDATE tg_approvals SET status")
    assert "server = 'ticktick'" in sql
    assert "status = 'PENDING'" in sql
    assert "RETURNING chat_id, message_id" in sql
    # params[1] (decided_at) — epoch-миллисекунды текущего момента,
    # недетерминированные по построению; проверяем только то, что стабильно.
    assert params[0] == "APPROVED" and params[2] == "m1"


def test_consume_tg_decision_replay_or_unknown_manifest_returns_none(monkeypatch):
    """Строка уже не 'PENDING' (второй тап/ретрай Telegram) или её нет вовсе —
    UPDATE … RETURNING ничего не находит, fetchone() отдаёт None."""
    sink = []
    monkeypatch.setattr(tg, "_pg_pool", _FakePool(sink, None))
    assert tg.consume_tg_decision("m1", "APPROVED") is None


def test_consume_tg_decision_query_error_is_caught_and_conn_released(monkeypatch):
    class _RaisingCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            raise RuntimeError("boom")

    class _RaisingConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _RaisingCursor()

    class _RaisingPool:
        def __init__(self):
            self.released = False

        def getconn(self):
            return _RaisingConn()

        def putconn(self, conn):
            self.released = True

    pool = _RaisingPool()
    monkeypatch.setattr(tg, "_pg_pool", pool)
    assert tg.consume_tg_decision("m1", "APPROVED") is None
    assert pool.released is True


# ═══════════════════════════════════════════════════════════════════════════
# 4. handle_webhook — разбор апдейта, owner-only, anti-replay
# ═══════════════════════════════════════════════════════════════════════════

def _cfg(owner="1", webhook_secret="whsecret"):
    return tg.TgApprovalConfig(enabled=True, bot_token="own-token", owner_chat_id=owner,
                               server="ticktick", tools_allowlist=None, ttl_s=3600,
                               own_bot=True, webhook_secret=webhook_secret)


def _cq_update(data="a:m1", from_id="1", chat_id="1", message_id=42, cq_id="cbq1",
              include_message=True):
    cq = {}
    if cq_id is not None:
        cq["id"] = cq_id
    if from_id is not None:
        cq["from"] = {"id": from_id}
    if data is not None:
        cq["data"] = data
    if include_message:
        cq["message"] = {"chat": {"id": chat_id}, "message_id": message_id}
    return {"callback_query": cq}


def test_handle_webhook_ignores_updates_without_callback_query(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "consume_tg_decision", lambda *a: calls.append(a))
    tg.handle_webhook(_cfg(), {"message": {"text": "hi"}})
    assert calls == []


def test_handle_webhook_ignores_non_owner_sender(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "consume_tg_decision", lambda *a: calls.append(a))
    tg.handle_webhook(_cfg(owner="999"), _cq_update(from_id="1"))
    assert calls == []


def test_handle_webhook_ignores_missing_from(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "consume_tg_decision", lambda *a: calls.append(a))
    tg.handle_webhook(_cfg(), _cq_update(from_id=None))
    assert calls == []


def test_handle_webhook_ignores_unparseable_callback_data(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "consume_tg_decision", lambda *a: calls.append(a))
    tg.handle_webhook(_cfg(), _cq_update(data="garbage"))
    assert calls == []
    tg.handle_webhook(_cfg(), _cq_update(data=None))
    assert calls == []


def test_handle_webhook_approve_consumes_clears_buttons_and_answers(monkeypatch):
    consume_calls, clear_calls, answer_calls = [], [], []
    monkeypatch.setattr(tg, "consume_tg_decision",
                        lambda mid, status: consume_calls.append((mid, status))
                        or {"chat_id": "1", "message_id": 42})
    monkeypatch.setattr(tg, "clear_inline_keyboard",
                        lambda cfg, chat_id, message_id: clear_calls.append((chat_id, message_id)))
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: answer_calls.append((method, body)) or {"ok": True})
    tg.handle_webhook(_cfg(), _cq_update(data="a:m1"))
    assert consume_calls == [("m1", "APPROVED")]
    assert clear_calls == [("1", 42)]
    assert answer_calls == [("answerCallbackQuery",
                            {"callback_query_id": "cbq1", "text": "Подтверждено"})]


def test_handle_webhook_reject_maps_r_prefix_to_rejected(monkeypatch):
    consume_calls, answer_calls = [], []
    monkeypatch.setattr(tg, "consume_tg_decision",
                        lambda mid, status: consume_calls.append((mid, status))
                        or {"chat_id": "1", "message_id": 42})
    monkeypatch.setattr(tg, "clear_inline_keyboard", lambda *a: None)
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: answer_calls.append(body) or {"ok": True})
    tg.handle_webhook(_cfg(), _cq_update(data="r:m2"))
    assert consume_calls == [("m2", "REJECTED")]
    assert answer_calls[0]["text"] == "Отклонено"


def test_handle_webhook_replay_does_not_clear_buttons_again(monkeypatch):
    """consume_tg_decision вернул None (строка уже не PENDING — повторный тап
    или ретрай доставки) — кнопки НЕ трогаем повторно, отвечаем «Уже
    обработано», а не выдаём себя за новое решение."""
    monkeypatch.setattr(tg, "consume_tg_decision", lambda mid, status: None)
    clear_calls, answer_calls = [], []
    monkeypatch.setattr(tg, "clear_inline_keyboard", lambda *a: clear_calls.append(a))
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: answer_calls.append(body) or {"ok": True})
    tg.handle_webhook(_cfg(), _cq_update(data="a:m1"))
    assert clear_calls == []
    assert answer_calls[0]["text"] == "Уже обработано"


def test_handle_webhook_falls_back_to_consumed_row_when_message_missing(monkeypatch):
    """callback_query без `message` (в теории возможно у старых/инлайн-кнопок)
    — снятие кнопок использует chat_id/message_id из самой строки БД."""
    monkeypatch.setattr(tg, "consume_tg_decision",
                        lambda mid, status: {"chat_id": "9", "message_id": 77})
    clear_calls = []
    monkeypatch.setattr(tg, "clear_inline_keyboard",
                        lambda cfg, c, m: clear_calls.append((c, m)))
    monkeypatch.setattr(tg, "_tg_call", lambda *a, **k: {"ok": True})
    tg.handle_webhook(_cfg(), _cq_update(data="a:m1", include_message=False))
    assert clear_calls == [("9", 77)]


def test_handle_webhook_never_touches_manifest_execution_machinery():
    """СТРУКТУРНАЯ защита от двойного исполнения (пункт 5 задания). Вебхук
    ТОЛЬКО переводит статус approval-строки — единственный путь исполнения
    мутации остаётся server.py's _tg_auto_execute_poller_loop через
    manifest_store.claim() (атомарный UPDATE … WHERE consumed_at IS NULL …
    RETURNING), который own_bot не трогает вовсе: ни одна из новых функций
    этого файла не ИСПОЛЬЗУЕТ (не просто «не упоминает в докстринге» —
    докстринги как раз ОБЪЯСНЯЮТ это разделение прозой и законно содержат эти
    слова) manifest_store/_MANIFESTS/consume_manifest/try_auto_execute.

    Проверка через `__code__.co_names` (реальные глобальные имена/атрибуты,
    на которые ссылается СКОМПИЛИРОВАННЫЙ байт-код функции), а не через
    substring-поиск по исходнику: последний ловил бы и честные упоминания в
    docstring'ах (как и случилось при первой версии этого теста — см. правку
    рядом), а байт-код такими строками в принципе не интересуется. Если
    будущая правка добавит сюда РЕАЛЬНЫЙ вызов исполнения — этот тест
    упадёт, а честное объяснение в докстринге падать не заставит."""
    forbidden = {"manifest_store", "_MANIFESTS", "consume_manifest",
                "try_auto_execute", "_consume_manifest_for_auto_execute"}
    for fn in (tg.handle_webhook, tg.consume_tg_decision, tg.register_webhook,
              tg.secret_token_matches):
        names = set(fn.__code__.co_names)
        hit = names & forbidden
        assert not hit, f"{fn.__name__} реально ссылается на {hit}"


# ═══════════════════════════════════════════════════════════════════════════
# 5. register_webhook — setWebhook при старте
# ═══════════════════════════════════════════════════════════════════════════

def test_register_webhook_noop_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_tg_call", lambda *a, **k: calls.append(1) or {"ok": True})
    cfg = tg.TgApprovalConfig(enabled=False, bot_token="x", owner_chat_id="1",
                              server="ticktick", tools_allowlist=None, ttl_s=3600,
                              own_bot=True, webhook_secret="s")
    tg.register_webhook(cfg, "https://x.example.com")
    assert calls == []


def test_register_webhook_noop_when_not_own_bot(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_tg_call", lambda *a, **k: calls.append(1) or {"ok": True})
    cfg = tg.TgApprovalConfig(enabled=True, bot_token="x", owner_chat_id="1",
                              server="ticktick", tools_allowlist=None, ttl_s=3600,
                              own_bot=False, webhook_secret="")
    tg.register_webhook(cfg, "https://x.example.com")
    assert calls == []


def test_register_webhook_without_public_base_url_logs_error_and_skips(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(tg, "_tg_call", lambda *a, **k: calls.append(1) or {"ok": True})
    with caplog.at_level("ERROR"):
        tg.register_webhook(_cfg(), None)
    assert calls == []
    assert "PUBLIC_BASE_URL" in caplog.text


def test_register_webhook_calls_setwebhook_with_secret_and_allowed_updates(monkeypatch):
    calls = []
    monkeypatch.setattr(tg, "_tg_call",
                        lambda cfg, method, body: calls.append((method, body)) or {"ok": True})
    tg.register_webhook(_cfg(webhook_secret="topsecret"), "https://tt.example.com/")
    method, body = calls[0]
    assert method == "setWebhook"
    assert body["url"] == "https://tt.example.com/tg/webhook"
    assert body["secret_token"] == "topsecret"
    assert body["allowed_updates"] == ["callback_query"]


def test_register_webhook_logs_error_on_telegram_failure_without_raising(monkeypatch, caplog):
    monkeypatch.setattr(tg, "_tg_call", lambda *a, **k: {"ok": False, "description": "boom"})
    with caplog.at_level("ERROR"):
        tg.register_webhook(_cfg(), "https://tt.example.com")  # не бросает
    assert "boom" in caplog.text


def test_register_webhook_swallows_exceptions(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(tg, "_tg_call", _boom)
    tg.register_webhook(_cfg(), "https://tt.example.com")  # не бросает


# ═══════════════════════════════════════════════════════════════════════════
# 6. /tg/webhook — сам HTTP-роут (server.py)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def client():
    return TestClient(s.mcp.streamable_http_app())


def _shared_bot_cfg():
    """own_bot=False — сегодняшнее (дефолтное) поведение сервера."""
    return tg.TgApprovalConfig(enabled=True, bot_token="shared", owner_chat_id="1",
                               server="ticktick", tools_allowlist=None, ttl_s=3600,
                               own_bot=False, webhook_secret="")


def test_route_404_when_own_bot_disabled(client, monkeypatch):
    """БЕЗ TG_BOT_TOKEN_OVERRIDE — побитово как сейчас: роут смонтирован (та
    же дисциплина, что у /health/dl/ul), но снаружи неотличим от отсутствия —
    404, и handle_webhook не зовётся вовсе."""
    monkeypatch.setattr(s, "_TG_CFG", _shared_bot_cfg())
    monkeypatch.setattr(s, "TRANSPORT", "streamable-http")
    called = []
    monkeypatch.setattr(tg, "handle_webhook", lambda *a, **k: called.append(1))
    r = client.post("/tg/webhook", json={"callback_query": {"id": "x"}})
    assert r.status_code == 404
    assert called == []


def test_route_404_when_transport_is_stdio_even_with_own_bot(client, monkeypatch):
    """own_bot=True, но транспорт stdio (нет HTTP-сервера физически) — роут
    тоже отказывает. main() в этом случае логирует явное предупреждение
    (отдельно проверено ниже) вместо тихого молчания."""
    monkeypatch.setattr(s, "_TG_CFG", _cfg())
    monkeypatch.setattr(s, "TRANSPORT", "stdio")
    called = []
    monkeypatch.setattr(tg, "handle_webhook", lambda *a, **k: called.append(1))
    r = client.post("/tg/webhook", json={"callback_query": {"id": "x"}},
                    headers={"x-telegram-bot-api-secret-token": "whsecret"})
    assert r.status_code == 404
    assert called == []


def test_route_401_on_wrong_secret(client, monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", _cfg())
    monkeypatch.setattr(s, "TRANSPORT", "streamable-http")
    called = []
    monkeypatch.setattr(tg, "handle_webhook", lambda *a, **k: called.append(1))
    r = client.post("/tg/webhook", json={"callback_query": {"id": "x"}},
                    headers={"x-telegram-bot-api-secret-token": "wrong"})
    assert r.status_code == 401
    assert called == []


def test_route_401_on_missing_secret_header(client, monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", _cfg())
    monkeypatch.setattr(s, "TRANSPORT", "streamable-http")
    r = client.post("/tg/webhook", json={"callback_query": {"id": "x"}})
    assert r.status_code == 401


def test_route_200_and_dispatches_parsed_body_when_active(client, monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", _cfg())
    monkeypatch.setattr(s, "TRANSPORT", "streamable-http")
    received = []
    monkeypatch.setattr(tg, "handle_webhook", lambda cfg, update: received.append(update))
    payload = {"callback_query": {"id": "x", "from": {"id": "1"}, "data": "a:m1"}}
    r = client.post("/tg/webhook", json=payload,
                    headers={"x-telegram-bot-api-secret-token": "whsecret"})
    assert r.status_code == 200
    assert received == [payload]


def test_route_malformed_json_body_still_200_with_empty_dict(client, monkeypatch):
    monkeypatch.setattr(s, "_TG_CFG", _cfg())
    monkeypatch.setattr(s, "TRANSPORT", "streamable-http")
    received = []
    monkeypatch.setattr(tg, "handle_webhook", lambda cfg, update: received.append(update))
    r = client.post("/tg/webhook", content=b"not json",
                    headers={"x-telegram-bot-api-secret-token": "whsecret",
                             "content-type": "application/json"})
    assert r.status_code == 200
    assert received == [{}]


def test_route_handle_webhook_exception_still_answers_200(client, monkeypatch):
    """Telegram ретраит не-2xx; ошибка внутри обработки — намеренный no-op
    снаружи, не то, что стоит заставлять Telegram повторять."""
    monkeypatch.setattr(s, "_TG_CFG", _cfg())
    monkeypatch.setattr(s, "TRANSPORT", "streamable-http")

    def _boom(cfg, update):
        raise RuntimeError("boom")

    monkeypatch.setattr(tg, "handle_webhook", _boom)
    r = client.post("/tg/webhook", json={"callback_query": {"id": "x"}},
                    headers={"x-telegram-bot-api-secret-token": "whsecret"})
    assert r.status_code == 200


def test_route_replayed_update_only_clears_buttons_once(client, monkeypatch):
    """Сквозной идемпотентности-тест (пункт 5 задания): один и тот же апдейт
    доставлен ДВАЖДЫ (Telegram ретраит недоставленные вебхуки) — оба запроса
    отвечают 200 (Telegram не должен продолжать ретраить), но фактическое
    решение (снятие кнопок) применяется РОВНО один раз — вторая доставка
    видит строку уже не PENDING и становится no-op на уровне consume_tg_
    decision, как в реальном Postgres (WHERE status = 'PENDING')."""
    monkeypatch.setattr(s, "_TG_CFG", _cfg())
    monkeypatch.setattr(s, "TRANSPORT", "streamable-http")
    state = {"n": 0}

    def _consume(mid, status):
        state["n"] += 1
        return {"chat_id": "1", "message_id": 42} if state["n"] == 1 else None

    monkeypatch.setattr(tg, "consume_tg_decision", _consume)
    clear_calls = []
    monkeypatch.setattr(tg, "clear_inline_keyboard",
                        lambda cfg, c, m: clear_calls.append((c, m)))
    monkeypatch.setattr(tg, "_tg_call", lambda *a, **k: {"ok": True})
    payload = {"callback_query": {"id": "x", "from": {"id": "1"}, "data": "a:m1",
                                   "message": {"chat": {"id": "1"}, "message_id": 42}}}
    headers = {"x-telegram-bot-api-secret-token": "whsecret"}
    r1 = client.post("/tg/webhook", json=payload, headers=headers)
    r2 = client.post("/tg/webhook", json=payload, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert state["n"] == 2  # оба запроса дошли до слоя решения
    assert clear_calls == [("1", 42)]  # но эффект применился только один раз
