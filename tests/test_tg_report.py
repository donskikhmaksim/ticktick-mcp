"""Тесты транспортного слоя Telegram-отчётов (ticktick_mcp/src/tg_approval.py):
чанкинг под лимит 4096, конвертация markdown→HTML, отправка с честным
fallback'ом и уборка по TTL.

Ни сети, ни Postgres: `_tg_call` подменяется монкейпатчем, стор — заглушками.
Проверяется ЛОГИКА, а не Telegram.

Главный инвариант, ради которого этот файл существует: ни один кусок,
возвращённый split_for_telegram(), не должен после md_to_telegram_html()
оказаться длиннее лимита — иначе Telegram отвергнет сообщение (400), и оно
пропадёт молча.
"""
import random

import pytest

import ticktick_mcp.src.tg_approval as tg


def _cfg(**over):
    base = dict(enabled=True, bot_token="faketoken", owner_chat_id="111",
                server="ticktick", tools_allowlist=None, ttl_s=3600,
                reports_chat_id="-1004357150083", reap_enabled=True)
    base.update(over)
    return tg.TgApprovalConfig(**base)


def _html_len(chunk):
    return len(tg.md_to_telegram_html(chunk))


# ===========================================================================
# split_for_telegram — границы, инвариант длины, целостность разметки
# ===========================================================================

def test_empty_and_blank_input_produce_nothing():
    assert tg.split_for_telegram("") == []
    assert tg.split_for_telegram("   \n\n\t ") == []


def test_short_text_is_a_single_chunk_unchanged():
    assert tg.split_for_telegram("короткий план на 3 задачи") == [
        "короткий план на 3 задачи"]


def test_default_limit_is_the_real_telegram_one():
    """4096 — настоящий лимит Bot API. Искусственный PREVIEW_CAP=3500 с
    обрезкой «…» убран сознательно: план нельзя показывать урезанным."""
    assert tg.TELEGRAM_TEXT_LIMIT == 4096


def _random_report(rnd):
    """Текст, похожий на реальный отчёт ticktick-mcp: разметка, эмодзи,
    кавычки-ёлочки, id с подчёркиваниями, HTML-опасные символы и изредка
    гигантские «слова» (ссылки), которые придётся резать по символам."""
    words = ["задача", "проект", "**жирный**", "`id_42`", "«Купить молоко»",
             "R&D", "<none>", "_курсив_", "🗑", "✅", "⚠️", "❌",
             "n8n_email_algo_report_2026-08-06.md", "—",
             "https://ticktick.com/webapp/#p/" + "a" * 250]
    lines = []
    for _ in range(rnd.randint(1, 50)):
        lines.append(" ".join(rnd.choice(words) for _ in range(rnd.randint(1, 40))))
    return "\n".join(lines)


def test_html_length_invariant_on_generated_reports():
    rnd = random.Random(20260806)
    for _ in range(60):
        text = _random_report(rnd)
        limit = rnd.choice([80, 200, 600, 1500, tg.TELEGRAM_TEXT_LIMIT])
        for chunk in tg.split_for_telegram(text, limit):
            assert _html_len(chunk) <= limit, (
                f"кусок после конвертации длиннее лимита {limit}: "
                f"{_html_len(chunk)}")


def test_words_are_never_torn_apart():
    """Слова рвать нельзя (кроме случая «одно слово длиннее лимита» — здесь
    таких нет). Проверяем: каждый токен каждого куска — целое слово оригинала."""
    text = " ".join(f"слово{i:04d}" for i in range(2000))
    original = set(text.split())
    for chunk in tg.split_for_telegram(text, 300):
        for token in chunk.split():
            assert token in original, f"слово порвано: {token!r}"


def test_paired_markup_is_never_left_open_inside_a_chunk():
    text = "\n".join(
        f"- **задача номер {i}** с `id_{i}` и «кавычками»" for i in range(300))
    for chunk in tg.split_for_telegram(text, 400):
        assert tg._open_markers(chunk) == [], f"незакрытая разметка: {chunk[-60:]!r}"


def test_multiline_bold_pair_moves_to_next_chunk_instead_of_breaking():
    """Предпочтительный путь по ТЗ: пару, не влезающую целиком, уносим в
    следующий кусок, а не закрываем посреди."""
    text = "строка один заполнитель\n**жирное начало через строки\nи конец жирного**\nхвост"
    chunks = tg.split_for_telegram(text, 55)
    assert chunks[0] == "строка один заполнитель"
    assert "**жирное начало через строки\nи конец жирного**" in chunks[1]
    for chunk in chunks:
        assert tg._open_markers(chunk) == []


def test_originally_broken_markup_is_not_silently_fixed():
    """Дозакрываем только СВОИ разрывы. Незакрытая `**` в самом исходнике —
    это текст автора отчёта, и превращать его в жирный (меняя смысл) мы не
    вправе; голая `**` в HTML остаётся обычным текстом и ничего не ломает."""
    assert tg.split_for_telegram("**без пары в исходнике") == [
        "**без пары в исходнике"]


def test_single_giant_line_is_split_by_chars_with_markup_reopened():
    """Последний рубеж: одно слово длиннее лимита. Разметка закрывается в
    конце куска и переоткрывается в начале следующего — иначе Telegram
    отвергнет первый кусок как невалидный HTML."""
    chunks = tg.split_for_telegram("**" + "z" * 400 + "**", 60)
    assert len(chunks) > 1
    for chunk in chunks:
        assert tg._open_markers(chunk) == []
        assert _html_len(chunk) <= 60
    # содержимое не потеряно
    assert "".join(c.replace("**", "") for c in chunks) == "z" * 400


def test_long_lines_are_split_on_line_boundaries_when_possible():
    text = "\n".join(f"строка номер {i} с небольшим текстом" for i in range(200))
    chunks = tg.split_for_telegram(text, 500)
    assert len(chunks) > 1
    # ни один кусок не начинается/заканчивается посреди строки
    rebuilt = "\n".join(chunks)
    assert rebuilt == text


# ===========================================================================
# md_to_telegram_html — экранирование и реальные символы отчётов
# ===========================================================================

def test_escapes_html_specials_before_inserting_tags():
    assert tg.md_to_telegram_html("<b>&x</b>") == "&lt;b&gt;&amp;x&lt;/b&gt;"


def test_markup_forms_render():
    out = tg.md_to_telegram_html("### Заголовок\n**жирный** и `код` и _курсив_")
    assert out == "<b>Заголовок</b>\n<b>жирный</b> и <code>код</code> и <i>курсив</i>"


_ALLOWED_TAGS = ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>")


def test_real_report_symbols_produce_valid_html():
    """Реальный отчёт ticktick: ёлочки, эмодзи, `<`, `&`, id с подчёркиваниями.
    После конвертации не должно остаться НИ ОДНОГО голого `<` или `>` —
    только наши теги (иначе Telegram вернёт 400 и сообщение потеряется)."""
    src = ("### 🗑 Удалено 3 задачи ✅\n"
           "- «Позвонить в R&D» — проект <Входящие>\n"
           "- `task_id_663f<>&` ⚠️ дубль\n"
           "- **n8n_email_algo_report_2026-08-06.md** ❌ не найдено")
    out = tg.md_to_telegram_html(src)
    stripped = out
    for tag in _ALLOWED_TAGS:
        stripped = stripped.replace(tag, "")
    assert "<" not in stripped and ">" not in stripped
    assert "&amp;" in out and "&lt;" in out
    assert "🗑" in out and "⚠️" in out
    # intraword-подчёркивания не превращаются в курсив
    assert "n8n_email_algo_report_2026-08-06.md" in out


# ===========================================================================
# send_message_chunked — кнопки, plain-fallback, 429
# ===========================================================================

class _FakeTelegram:
    """Записывает тела запросов и отдаёт заранее заданные ответы."""

    def __init__(self, responses=None):
        self.bodies = []
        self.responses = list(responses or [])
        self._next_id = 1000

    def __call__(self, cfg, method, body):
        self.bodies.append((method, body))
        if self.responses:
            res = self.responses.pop(0)
            if res is not None:
                return res
        self._next_id += 1
        return {"ok": True, "result": {"message_id": self._next_id}}


def test_buttons_are_attached_only_to_the_last_message(monkeypatch):
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    long_text = "\n".join(f"- **задача {i}** — описание `id_{i}`" for i in range(400))
    markup = {"inline_keyboard": [[{"text": "✅", "callback_data": "a:m1"}]]}

    ok, ids, err = tg.send_message_chunked(_cfg(), "111", long_text,
                                           reply_markup_on_last=markup)

    assert ok is True and err == ""
    assert len(fake.bodies) > 1, "текст обязан разбиться на несколько сообщений"
    assert len(ids) == len(fake.bodies)
    for _, body in fake.bodies[:-1]:
        assert "reply_markup" not in body
    assert fake.bodies[-1][1]["reply_markup"] == markup
    # маркер части и соблюдение реального лимита Telegram
    total = len(fake.bodies)
    for i, (_, body) in enumerate(fake.bodies, start=1):
        assert f"(часть {i}/{total})" in body["text"]
        assert len(body["text"]) <= tg.TELEGRAM_TEXT_LIMIT


def test_single_chunk_gets_no_part_marker(monkeypatch):
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    ok, ids, err = tg.send_message_chunked(_cfg(), "111", "коротко")
    assert ok is True and len(ids) == 1
    assert "часть" not in fake.bodies[0][1]["text"]


def test_parse_entities_error_retries_as_plain_text(monkeypatch):
    """Главный silent-fail Telegram: кривая разметка → 400 и сообщение просто
    не доходит. Обязаны повторить БЕЗ parse_mode, plain-текстом исходника."""
    fake = _FakeTelegram(responses=[
        {"ok": False, "error_code": 400, "parameters": {},
         "description": "Bad Request: can't parse entities: Unsupported start tag"},
    ])
    monkeypatch.setattr(tg, "_tg_call", fake)

    ok, ids, err = tg.send_message_chunked(_cfg(), "111", "**сломанная <разметка")

    assert ok is True, "сообщение обязано дойти вторым заходом"
    assert len(ids) == 1
    assert len(fake.bodies) == 2
    assert fake.bodies[0][1]["parse_mode"] == "HTML"
    assert "parse_mode" not in fake.bodies[1][1]
    # plain-повтор шлёт ИСХОДНЫЙ markdown, а не HTML-версию
    assert fake.bodies[1][1]["text"] == "**сломанная <разметка"


def test_rate_limit_waits_retry_after_and_retries(monkeypatch):
    slept = []
    fake = _FakeTelegram(responses=[
        {"ok": False, "error_code": 429, "parameters": {"retry_after": 7},
         "description": "Too Many Requests: retry after 7"},
    ])
    monkeypatch.setattr(tg, "_tg_call", fake)
    monkeypatch.setattr(tg.time, "sleep", lambda s: slept.append(s))

    ok, ids, err = tg.send_message_chunked(_cfg(), "111", "отчёт")

    assert ok is True and len(ids) == 1
    assert slept == [7]
    assert len(fake.bodies) == 2


def test_fatal_error_reports_delivered_ids_so_caller_can_clean_up(monkeypatch):
    fake = _FakeTelegram(responses=[
        None,  # первый кусок уходит нормально
        {"ok": False, "error_code": 403, "parameters": {},
         "description": "Forbidden: bot was blocked by the user"},
    ])
    monkeypatch.setattr(tg, "_tg_call", fake)
    long_text = "\n".join(f"строка {i} " + "x" * 100 for i in range(200))

    ok, ids, err = tg.send_message_chunked(_cfg(), "111", long_text)

    assert ok is False
    assert len(ids) == 1, "id уже доставленных кусков обязан вернуться наружу"
    assert "Forbidden" in err


def test_empty_text_is_a_failure_not_a_silent_success(monkeypatch):
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    ok, ids, err = tg.send_message_chunked(_cfg(), "111", "   ")
    assert ok is False and ids == [] and "пуст" in err
    assert fake.bodies == []


def test_delete_message_is_best_effort(monkeypatch):
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, m, b: {
        "ok": False, "description": "Bad Request: message to delete not found"})
    assert tg.delete_message(_cfg(), "111", 42) is False  # не бросает
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, m, b: {"ok": True, "result": True})
    assert tg.delete_message(_cfg(), "111", 42) is True


# ===========================================================================
# Конфиг: группа отчётов и выключатель уборщика
# ===========================================================================

def _base_env(monkeypatch):
    monkeypatch.setenv("TG_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("TG_BOT_TOKEN", "faketoken")
    monkeypatch.setenv("TG_OWNER_CHAT_ID", "111")
    monkeypatch.delenv("TG_APPROVAL_TOOLS", raising=False)


def test_reports_chat_falls_back_to_owner_chat(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("TG_REPORTS_CHAT_ID", raising=False)
    cfg = tg.load_tg_approval_config()
    assert cfg.reports_chat_id == "111"


def test_reports_chat_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TG_REPORTS_CHAT_ID", "-1004357150083")
    cfg = tg.load_tg_approval_config()
    assert cfg.reports_chat_id == "-1004357150083"


def test_reaper_is_on_by_default_and_off_only_on_explicit_false(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("TG_REAP_ENABLED", raising=False)
    assert tg.load_tg_approval_config().reap_enabled is True
    monkeypatch.setenv("TG_REAP_ENABLED", "true")
    assert tg.load_tg_approval_config().reap_enabled is True
    monkeypatch.setenv("TG_REAP_ENABLED", "FALSE")
    assert tg.load_tg_approval_config().reap_enabled is False


# ===========================================================================
# notify_plan — чанкинг, id последнего сообщения, fail-closed
# ===========================================================================

def test_notify_plan_stores_last_message_id_and_extras(monkeypatch):
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, text, **kw: (True, [10, 11, 12], ""))
    saved = {}
    monkeypatch.setattr(tg, "create_tg_approval",
                        lambda mid, chat, msg, exp, extra=None: saved.update(
                            dict(mid=mid, chat=chat, msg=msg, extra=extra)))

    ok, err = tg.notify_plan(_cfg(), "m1", "### план", "delete_tasks")

    assert (ok, err) == (True, "")
    assert saved["msg"] == 12, "кнопки на последнем — его id и есть message_id"
    assert saved["extra"] == [10, 11]


def test_notify_plan_puts_buttons_on_last_chunk_only(monkeypatch):
    seen = {}

    def fake_send(cfg, chat, text, **kw):
        seen["markup"] = kw.get("reply_markup_on_last")
        seen["text"] = text
        return True, [7], ""

    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "send_message_chunked", fake_send)
    monkeypatch.setattr(tg, "create_tg_approval", lambda *a, **k: None)

    tg.notify_plan(_cfg(), "m1", "### план", "delete_tasks")

    buttons = seen["markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "a:m1"
    assert buttons[1]["callback_data"] == "r:m1"
    assert seen["text"].endswith("delete_tasks · ticktick")


def test_notify_plan_partial_send_deletes_stubs_and_fails_closed(monkeypatch):
    """Обрубок плана без кнопок опаснее пустоты: его убираем, гейт — закрыт."""
    deleted = []
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, text, **kw: (False, [5, 6], "boom"))
    monkeypatch.setattr(tg, "delete_message",
                        lambda cfg, chat, mid: deleted.append(mid) or True)
    monkeypatch.setattr(tg, "create_tg_approval",
                        lambda *a, **k: pytest.fail("строку писать нельзя"))

    ok, err = tg.notify_plan(_cfg(), "m1", "### план", "delete_tasks")

    assert ok is False and err == "boom"
    assert deleted == [5, 6]


# ===========================================================================
# post_report_to_group / summarize_in_owner_chat
# ===========================================================================

@pytest.mark.parametrize("verdict,marker", [
    ("ok", "✅ Исполнено и подтверждено"),
    ("partial", "⚠️ Исполнено частично"),
    ("failed", "🛑 Ошибка исполнения"),
    ("unverified", "⚠️ Исполнено, НО независимой перепроверкой не подтверждено"),
])
def test_report_header_matches_verdict(monkeypatch, verdict, marker):
    seen = {}

    def fake_send(cfg, chat, text, **kw):
        seen["chat"], seen["text"] = chat, text
        return True, [99], ""

    monkeypatch.setattr(tg, "send_message_chunked", fake_send)
    monkeypatch.setattr(tg, "record_report_messages", lambda *a: None)

    ids = tg.post_report_to_group(_cfg(), "m1", "тело отчёта",
                                  tool="delete_tasks", verdict=verdict)

    assert ids == [99]
    assert seen["chat"] == "-1004357150083"
    assert marker in seen["text"]
    assert "delete_tasks" in seen["text"] and "m1" in seen["text"]
    assert "тело отчёта" in seen["text"]


def test_report_timestamp_is_owner_timezone_not_utc(monkeypatch):
    """Жёсткое требование: время в отчёте — America/Los_Angeles."""
    seen = {}
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, text, **kw: seen.update(text=text) or (True, [1], ""))
    monkeypatch.setattr(tg, "record_report_messages", lambda *a: None)
    tg.post_report_to_group(_cfg(), "m1", "тело", tool="t", verdict="ok")
    assert any(tzname in seen["text"] for tzname in ("PDT", "PST")), seen["text"]


def test_report_records_message_ids_for_later_cleanup(monkeypatch):
    recorded = {}
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, text, **kw: (True, [4, 5], ""))
    monkeypatch.setattr(tg, "record_report_messages",
                        lambda mid, chat, ids: recorded.update(
                            dict(mid=mid, chat=chat, ids=ids)))
    tg.post_report_to_group(_cfg(), "m9", "тело", tool="t", verdict="ok")
    assert recorded == {"mid": "m9", "chat": "-1004357150083", "ids": [4, 5]}


def test_report_never_raises_even_if_transport_explodes(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("сеть отвалилась")

    monkeypatch.setattr(tg, "send_message_chunked", boom)
    assert tg.post_report_to_group(_cfg(), "m1", "тело", tool="t", verdict="ok") == []


def test_summary_edits_message_and_strips_buttons(monkeypatch):
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    tg.summarize_in_owner_chat(_cfg(), "111", 55, "**Готово** — 3 задачи удалены")
    method, body = fake.bodies[0]
    assert method == "editMessageText"
    assert body["message_id"] == 55
    assert body["reply_markup"] == {"inline_keyboard": []}
    assert "<b>Готово</b>" in body["text"]


def test_summary_without_message_id_is_a_noop(monkeypatch):
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    tg.summarize_in_owner_chat(_cfg(), "111", None, "сводка")
    assert fake.bodies == []


def test_deprecated_wrapper_still_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(tg, "summarize_in_owner_chat",
                        lambda cfg, chat, mid, text: seen.update(
                            chat=chat, mid=mid, text=text))
    tg.report_auto_execution_result(_cfg(), "111", 5, "итог")
    assert seen == {"chat": "111", "mid": 5, "text": "итог"}


# ===========================================================================
# reap_expired — предохранители до похода в Postgres
# ===========================================================================

def test_reap_disabled_by_config_does_nothing(monkeypatch):
    monkeypatch.setattr(tg, "store_ready", lambda: pytest.fail("стор трогать не должны"))
    assert tg.reap_expired(_cfg(reap_enabled=False)) == 0


def test_reap_without_store_does_nothing(monkeypatch):
    monkeypatch.setattr(tg, "store_ready", lambda: False)
    monkeypatch.setattr(tg, "_tg_call", lambda *a, **k: pytest.fail("сети быть не должно"))
    assert tg.reap_expired(_cfg()) == 0


def test_reap_deletes_plan_extras_and_report_messages(monkeypatch):
    """Уборка должна снести ВЕСЬ след манифеста: сообщение с кнопками,
    предыдущие куски плана и сообщения отчёта (в их собственном чате)."""
    rows = [("m1", "111", 900, [898, 899], "-100777", [500, 501])]
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "_pg_pool", _FakePool(rows))
    deleted = []
    monkeypatch.setattr(tg, "delete_message",
                        lambda cfg, chat, mid: deleted.append((chat, mid)) or True)

    assert tg.reap_expired(_cfg()) == 1
    assert deleted == [("111", 900), ("111", 898), ("111", 899),
                       ("-100777", 500), ("-100777", 501)]


def test_reap_claims_rows_atomically_and_spares_decided_ones(monkeypatch):
    """Строки APPROVED/REJECTED не трогаем (их архив должен жить), а claim
    делаем одним DELETE … RETURNING, чтобы два тика не подрались."""
    pool = _FakePool([])
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "_pg_pool", pool)
    tg.reap_expired(_cfg())
    sql = " ".join(pool.cursor_obj.sql.split())
    assert sql.startswith("DELETE FROM tg_approvals")
    assert "RETURNING" in sql
    assert "status IN ('PENDING', 'EXPIRED')" in sql
    assert "APPROVED" not in sql and "REJECTED" not in sql


def test_reap_survives_a_broken_row(monkeypatch):
    """Ошибка на одном манифесте не должна оставить неубранными остальные."""
    rows = [("m1", "111", 1, None, None, None), ("m2", "111", 2, None, None, None)]
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "_pg_pool", _FakePool(rows))
    seen = []

    def flaky(cfg, chat, mid):
        seen.append(mid)
        if mid == 1:
            raise RuntimeError("Telegram недоступен")
        return True

    monkeypatch.setattr(tg, "delete_message", flaky)
    assert tg.reap_expired(_cfg()) == 2
    assert seen == [1, 2]


# --- минимальная заглушка psycopg2-пула (без Postgres) ----------------------

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    def __init__(self, rows):
        self.cursor_obj = _FakeCursor(rows)
        self.conn = _FakeConn(self.cursor_obj)

    def getconn(self):
        return self.conn

    def putconn(self, conn):
        pass
