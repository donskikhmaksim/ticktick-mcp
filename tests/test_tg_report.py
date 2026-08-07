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
import re

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


@pytest.fixture(autouse=True)
def _never_sleep_for_real(monkeypatch):
    """Между соседними кусками теперь есть профилактическая пауза
    (`_INTER_CHUNK_PAUSE_S`, защита от 429), а на 429 — сон на retry_after.
    Тесты не имеют права спать по-настоящему: отчёт на 13 сообщений добавил бы
    к прогону 6 секунд на ровном месте. Патчим САМ модуль time (именно его
    видит tg_approval), поэтому мок накрывает оба вида сна сразу.

    Тесты, которым важны конкретные значения сна, ставят свой monkeypatch
    поверх — он применяется позже и выигрывает."""
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)


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


def test_unbalanced_markup_storm_terminates_at_the_real_telegram_limit():
    """РЕГРЕССИЯ (найдено фаззингом 2026-08-06): строка из повторов «`**»
    растит стек открытых маркеров линейно (`` ` `` внутри `**` не образует
    пары), и дорезка переставала сходиться — сколько отрезали, столько же
    возвращало переоткрытие разметки. `split_for_telegram` крутилась ВЕЧНО
    на БОЕВОМ лимите 4096, съедая память. Прод-цена: `notify_plan` не
    возвращается (тул висит), поллер автоисполнения встаёт навсегда.

    Такой текст не экзотика: отчёт печатает названия задач дословно, а они
    приходят извне. Тест обязан завершиться — и соблюсти оба инварианта."""
    text = "`**" * 1000
    chunks = tg.split_for_telegram(text, tg.TELEGRAM_TEXT_LIMIT)
    assert chunks, "текст не должен исчезнуть"
    for chunk in chunks:
        assert _html_len(chunk) <= tg.TELEGRAM_TEXT_LIMIT
    # ничего не потеряно (маркеры разметки при переоткрытии могут
    # ДОБАВИТЬСЯ — это штатно, а вот исчезнуть текст не имеет права)
    assert len("".join(chunks)) >= len(text)


def test_unbalanced_markup_storm_inside_a_realistic_report_terminates():
    """Тот же вход, но спрятанный в НАЗВАНИИ задачи внутри обычного отчёта —
    ровно так он и попадёт в прод."""
    report = ("### 🧾 Независимый отчёт\n"
              + "\n".join(f"- ✅ **«Задача {i}»** — удалена" for i in range(50))
              + "\n- ❌ **«" + "`**" * 2000 + "»** — ВСЁ ЕЩЁ существует\n"
              + "**Итог: ✅ 50 подтверждено, ❌ 1 расхождений.**")
    chunks = tg.split_for_telegram(report, tg.TELEGRAM_TEXT_LIMIT)
    for chunk in chunks:
        assert _html_len(chunk) <= tg.TELEGRAM_TEXT_LIMIT
    assert "Итог: ✅ 50 подтверждено" in "".join(chunks)


def test_emergency_cut_never_returns_an_oversized_or_empty_head():
    head, tail = tg._emergency_cut("&" * 100, 20)   # каждый & = 5 символов HTML
    assert 0 < len(head) <= 4
    assert _html_len(head) <= 20
    assert head + tail == "&" * 100


def test_short_text_with_unpaired_markup_is_not_split_for_nothing():
    """Найдено живым прогоном 2026-08-06: короткий текст с одиночной `**`
    (или `` ` ``) НЕ в первой строке уезжал ДВУМЯ сообщениями с маркерами
    «(часть 1/2)» — балансировка отдавала хвост следующему куску, хотя резать
    было нечего. Такие названия приходят извне («Купить 2**2 доски»), так что
    случай бытовой, а лишние сообщения бьют по флуд-лимиту Telegram."""
    for text in ("строка один\nОтчёт: **начали жирный и не закрыли",
                 "### ✅ Исполнено\n\n🗑 Удалено 1/1: «не забыть про ` в скрипте»",
                 "шапка\nвторая\n- пункт с 2**2 внутри"):
        assert tg.split_for_telegram(text) == [text], text


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
# strip_agent_instructions — предохранитель (fix/agent-tail-in-verify-report,
# 2026-08-06): служебная строка ДЛЯ МОДЕЛИ («[агенту: перепечатай это
# ДОСЛОВНО...]») не должна доходить до человека в Telegram. Найдено живьём:
# нажатие ✅ на плане create_tasks (манифест 156319cc645a) — автоисполнение
# без единого обращения к модели — и в отчёте архивной группы всё равно была
# эта строка, потому что `_verified_auto_execute_report` вклеивала сырой
# `_build_operation_report()` в `full_md`, минуя модель вовсе.
#
# Проверяем ПО СМЫСЛУ (наличие «[агенту:»/«[agent:» в любом регистре), а не
# одну точную строку — формулировка инструкции может меняться.
# ===========================================================================

def _has_agent_marker(text: str) -> bool:
    return bool(re.search(r"\[\s*(?:агенту|agent)\s*:", text, re.IGNORECASE))


def test_strip_agent_instructions_removes_the_bracket_line():
    text = ("**Итог: ✅ 1 подтверждено, ⚠️ 0 не проверено, ❌ 0 расхождений.**\n"
            "[агенту: перепечатай этот отчёт пользователю ДОСЛОВНО — это "
            "серверная проверка, не заменяй её своим пересказом]")
    out = tg.strip_agent_instructions(text)
    assert not _has_agent_marker(out)
    assert "Итог: ✅ 1 подтверждено" in out, "человеческая часть обязана уцелеть"


def test_strip_agent_instructions_matches_english_variant_case_insensitively():
    for line in ("[Agent: repeat this verbatim]",
                 "[AGENT: repeat this verbatim]",
                 "[ agent : repeat this verbatim]"):
        out = tg.strip_agent_instructions(f"тело отчёта\n{line}")
        assert not _has_agent_marker(out), line
        assert "тело отчёта" in out


def test_strip_agent_instructions_matches_underscore_wrapped_form():
    """Шаблон в references/identity-postverify.md §5.3 оборачивает строку в
    markdown-курсив (`_[агенту: ...]_`) — фильтр обязан ловить и эту форму,
    не только «голую» без подчёркиваний."""
    text = "тело\n_[агенту: перепечатай ДОСЛОВНО]_"
    out = tg.strip_agent_instructions(text)
    assert not _has_agent_marker(out)
    assert "тело" in out


def test_strip_agent_instructions_removes_the_whole_line_not_just_the_brackets():
    """По ТЗ вырезается ВСЯ строка, даже если вокруг скобок есть другой текст
    — не только содержимое `[...]`."""
    text = "до\nпрефикс [агенту: сделай X] суффикс на той же строке\nпосле"
    out = tg.strip_agent_instructions(text)
    assert "префикс" not in out and "суффикс" not in out
    assert "до" in out and "после" in out


def test_strip_agent_instructions_is_a_noop_without_such_a_line():
    text = "### ✅ Готово\n\nОбычный отчёт без служебных строк."
    assert tg.strip_agent_instructions(text) == text


@pytest.mark.parametrize("text", ["", None])
def test_strip_agent_instructions_survives_empty_input(text):
    assert tg.strip_agent_instructions(text) == text


def test_strip_agent_instructions_does_not_touch_a_task_title_that_merely_mentions_agent():
    """Не должен вырезать легитимный текст, который просто содержит слово
    «агент»/«agent» без формы `[агенту: ...]`/`[agent: ...]` — иначе реальные
    названия задач («Позвонить агенту по недвижимости») теряли бы кусок
    отчёта."""
    text = "- ✅ **«Позвонить агенту по недвижимости»** — создана в «Входящие»"
    assert tg.strip_agent_instructions(text) == text


# ===========================================================================
# send_message_chunked — кнопки, plain-fallback, 429
# ===========================================================================

def _visible_len(html_text: str) -> int:
    """Длина так, как её считает Telegram: разметочные теги превращаются в
    entities и в лимит 4096 не входят, входит видимый текст."""
    return len(re.sub(r"<[^>]+>", "", html_text))


class _FakeTelegram:
    """Записывает тела запросов и отдаёт заранее заданные ответы.

    Единственное, в чём двойник обязан быть НЕ добрее живого Bot API: длина.
    Раньше он принимал `editMessageText`/`sendMessage` любого размера, и
    поэтому весь расчёт бюджета сводки (резерв под приписку «сводка
    сокращена») держался ни на чём — его можно было выкинуть, оставив пакет
    зелёным, и вернуть ровно тот инцидент, ради которого он написан: 400 →
    сводки нет → кнопки на исполненном плане висят дальше."""

    def __init__(self, responses=None):
        self.bodies = []
        self.responses = list(responses or [])
        self._next_id = 1000

    def __call__(self, cfg, method, body):
        self.bodies.append((method, body))
        text = body.get("text")
        if isinstance(text, str) and _visible_len(text) > tg.TELEGRAM_TEXT_LIMIT:
            return {"ok": False, "error_code": 400, "parameters": {},
                    "description": "Bad Request: message is too long"}
        if self.responses:
            res = self.responses.pop(0)
            if res is not None:
                return res
        self._next_id += 1
        return {"ok": True, "result": {"message_id": self._next_id}}


# ===========================================================================
# approval_status_of — единственная формула вердикта кнопки
# ===========================================================================
#
# Прямых тестов у неё не было вовсе: чат-путь во всех файлах подменяет
# `check_approval` готовой строкой, а поллеру подавали только APPROVED и
# PENDING. То есть «нажал 🛑» как СТРОКА БАЗЫ не превращалась в вердикт ни в
# одном тесте — при том что это единственное, что отделяет отказ владельца от
# исполнения.

def test_approval_status_of_classifies_every_row_shape():
    now = tg._now_ms()
    future, past = now + 3_600_000, now - 1

    assert tg.approval_status_of(None) == "none"
    assert tg.approval_status_of(
        {"status": "APPROVED", "expires_at": future}) == "approved"
    assert tg.approval_status_of(
        {"status": "REJECTED", "expires_at": future}) == "rejected"
    assert tg.approval_status_of(
        {"status": "PENDING", "expires_at": future}) == "pending"
    # TTL истёк — «как будто не спрашивали», гейт закрыт
    assert tg.approval_status_of(
        {"status": "PENDING", "expires_at": past}) == "none"
    # решение владельца НЕ протухает вместе с TTL: отказ остаётся отказом
    assert tg.approval_status_of(
        {"status": "REJECTED", "expires_at": past}) == "rejected"
    assert tg.approval_status_of(
        {"status": "APPROVED", "expires_at": past}) == "approved"


# ===========================================================================
# _tg_call — разбор ответа живого Bot API
# ===========================================================================
#
# До 2026-08-06 эта функция не исполнялась НИ ОДНИМ тестом: во всех файлах она
# подменялась двойником, который возвращал уже разобранный словарь. То есть
# весь пакет проверял контракт, который сам же и выдумывал, а код, который
# этот контракт обязан произвести из сырого ответа Telegram, не проверялся
# нигде. Убери из `_tg_call` `error_code`/`parameters` — и 429 перестанет
# распознаваться (`_retry_after_s`), а 400 «ok:false» при HTTP 200 начнёт
# читаться как успешная доставка. Оба следствия молчаливые.

class _FakeHTTPResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self.ok = status_ok

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _patch_post(monkeypatch, response):
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen.update(url=url, body=json, timeout=timeout)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(tg.requests, "post", fake_post)
    return seen


def test_tg_call_builds_the_bot_url_and_returns_the_result(monkeypatch):
    seen = _patch_post(monkeypatch, _FakeHTTPResponse(
        {"ok": True, "result": {"message_id": 42}}))

    res = tg._tg_call(_cfg(), "sendMessage", {"chat_id": "111", "text": "hi"})

    assert seen["url"] == f"{tg.TELEGRAM_API}/botfaketoken/sendMessage"
    assert seen["body"] == {"chat_id": "111", "text": "hi"}
    assert seen["timeout"] == tg.TG_TIMEOUT_S
    assert res["ok"] is True and res["result"] == {"message_id": 42}


def test_tg_call_passes_429_through_so_the_retry_can_read_it(monkeypatch):
    """429 узнаётся ТОЛЬКО по error_code/parameters. Потеряются здесь —
    длинный отчёт молча оборвётся на середине."""
    _patch_post(monkeypatch, _FakeHTTPResponse(
        {"ok": False, "error_code": 429, "description": "Too Many Requests",
         "parameters": {"retry_after": 37}}, status_ok=False))

    res = tg._tg_call(_cfg(), "sendMessage", {})

    assert res["ok"] is False
    assert res["error_code"] == 429
    assert res["parameters"] == {"retry_after": 37}
    assert tg._retry_after_s(res) == 37


def test_tg_call_keeps_the_parse_error_description(monkeypatch):
    """По этому описанию `send_message_chunked` решает повторить кусок без
    parse_mode. Стереть description — и сообщение потеряется навсегда."""
    _patch_post(monkeypatch, _FakeHTTPResponse(
        {"ok": False, "error_code": 400,
         "description": "Bad Request: can't parse entities: Unsupported start tag"},
        status_ok=False))

    res = tg._tg_call(_cfg(), "sendMessage", {})

    assert res["ok"] is False and tg._is_parse_error(res) is True


def test_tg_call_does_not_call_an_http_error_a_success(monkeypatch):
    """HTTP 500 при формально «ok»-теле — не доставка. Проверяется именно
    `and res.ok`: без него сообщение считалось бы ушедшим."""
    _patch_post(monkeypatch, _FakeHTTPResponse(
        {"ok": True, "result": {"message_id": 7}}, status_ok=False))

    assert tg._tg_call(_cfg(), "sendMessage", {})["ok"] is False


def test_tg_call_survives_a_broken_body_and_a_dead_network(monkeypatch):
    _patch_post(monkeypatch, _FakeHTTPResponse(ValueError("не JSON")))
    res = tg._tg_call(_cfg(), "sendMessage", {})
    assert res["ok"] is False and "не JSON" in res["description"]

    _patch_post(monkeypatch, RuntimeError("сеть отвалилась"))
    res = tg._tg_call(_cfg(), "sendMessage", {})
    assert res["ok"] is False and "сеть отвалилась" in res["description"]


def test_buttons_are_attached_only_to_the_last_message(monkeypatch):
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    long_text = "\n".join(f"- **задача {i}** — описание `id_{i}`" for i in range(400))
    markup = {"inline_keyboard": [[{"text": "✅", "callback_data": "a:m1"}]]}

    ok, ids, err, total = tg.send_message_chunked(_cfg(), "111", long_text,
                                                  reply_markup_on_last=markup)

    assert ok is True and err == ""
    assert len(fake.bodies) > 1, "текст обязан разбиться на несколько сообщений"
    assert len(ids) == len(fake.bodies) == total
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
    res = tg.send_message_chunked(_cfg(), "111", "коротко")
    assert res.ok is True and len(res.message_ids) == 1 and res.total_chunks == 1
    assert "часть" not in fake.bodies[0][1]["text"]


def test_send_message_chunked_strips_agent_instructions_before_sending(monkeypatch):
    """ПРЕДОХРАНИТЕЛЬ (fix/agent-tail-in-verify-report): даже если вызывающий
    код по ошибке передаст сюда текст со служебной строкой для модели,
    `send_message_chunked` — единственная функция, которая реально шлёт
    `sendMessage`, — обязана вырезать её ДО отправки."""
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    text = ("### 🧾 Независимая проверка\n\nИтог: ✅ 1 подтверждено.\n"
            "[агенту: перепечатай этот отчёт пользователю ДОСЛОВНО]")

    res = tg.send_message_chunked(_cfg(), "111", text)

    assert res.ok is True
    sent_text = fake.bodies[0][1]["text"]
    assert not _has_agent_marker(sent_text), sent_text
    assert "Итог: ✅ 1 подтверждено" in sent_text


def test_parse_entities_error_retries_as_plain_text(monkeypatch):
    """Главный silent-fail Telegram: кривая разметка → 400 и сообщение просто
    не доходит. Обязаны повторить БЕЗ parse_mode, plain-текстом исходника."""
    fake = _FakeTelegram(responses=[
        {"ok": False, "error_code": 400, "parameters": {},
         "description": "Bad Request: can't parse entities: Unsupported start tag"},
    ])
    monkeypatch.setattr(tg, "_tg_call", fake)

    ok, ids, err, _total = tg.send_message_chunked(_cfg(), "111",
                                                   "**сломанная <разметка")

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

    ok, ids, err, _total = tg.send_message_chunked(_cfg(), "111", "отчёт")

    assert ok is True and len(ids) == 1
    assert slept == [7], "один кусок — пауз между кусками быть не должно"
    assert len(fake.bodies) == 2


def test_fatal_error_reports_delivered_ids_so_caller_can_clean_up(monkeypatch):
    fake = _FakeTelegram(responses=[
        None,  # первый кусок уходит нормально
        {"ok": False, "error_code": 403, "parameters": {},
         "description": "Forbidden: bot was blocked by the user"},
    ])
    monkeypatch.setattr(tg, "_tg_call", fake)
    long_text = "\n".join(f"строка {i} " + "x" * 100 for i in range(200))

    ok, ids, err, total = tg.send_message_chunked(_cfg(), "111", long_text)

    assert ok is False
    assert len(ids) == 1, "id уже доставленных кусков обязан вернуться наружу"
    assert "Forbidden" in err
    assert total > 1, "вызывающий обязан узнать, СКОЛЬКО частей планировалось"


def test_empty_text_is_a_failure_not_a_silent_success(monkeypatch):
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    ok, ids, err, total = tg.send_message_chunked(_cfg(), "111", "   ")
    assert ok is False and ids == [] and "пуст" in err and total == 0
    assert fake.bodies == []


# ===========================================================================
# send_message_chunked — устойчивость к флуд-лимиту (429)
# ===========================================================================

def test_pause_between_chunks_is_taken_before_every_chunk_but_the_first(monkeypatch):
    """Профилактика 429: отчёт на 200 задач — это 9-13 сообщений подряд, и
    Telegram отвечает «retry after 37» примерно на 13-м. Дешевле выдержать
    паузу заранее, чем отсиживать штраф после отказа."""
    slept = []
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    monkeypatch.setattr(tg.time, "sleep", lambda s: slept.append(s))
    long_text = "\n".join(f"строка {i} " + "y" * 120 for i in range(200))

    res = tg.send_message_chunked(_cfg(), "111", long_text)

    assert res.ok is True and res.total_chunks > 1
    assert slept == [tg._INTER_CHUNK_PAUSE_S] * (res.total_chunks - 1)


def test_five_attempts_survive_a_streak_of_flood_limits(monkeypatch):
    """Было 3 попытки — три 429 подряд по ОДНОМУ куску обрывали весь отчёт."""
    too_many = {"ok": False, "error_code": 429, "parameters": {"retry_after": 5},
                "description": "Too Many Requests: retry after 5"}
    fake = _FakeTelegram(responses=[too_many, too_many, too_many, too_many])
    monkeypatch.setattr(tg, "_tg_call", fake)
    slept = []
    monkeypatch.setattr(tg.time, "sleep", lambda s: slept.append(s))

    res = tg.send_message_chunked(_cfg(), "111", "отчёт")

    assert tg._SEND_ATTEMPTS == 5
    assert res.ok is True, "пятая попытка обязана дойти"
    assert slept == [5, 5, 5, 5]


def test_total_wait_per_message_is_capped(monkeypatch):
    """Потолок суммарного ожидания на ОДИН кусок: без него пять подряд
    «retry after 60» держали бы поток пять минут."""
    huge = {"ok": False, "error_code": 429, "parameters": {"retry_after": 100},
            "description": "Too Many Requests: retry after 100"}
    fake = _FakeTelegram(responses=[huge, huge, huge, huge, huge])
    monkeypatch.setattr(tg, "_tg_call", fake)
    slept = []
    monkeypatch.setattr(tg.time, "sleep", lambda s: slept.append(s))

    res = tg.send_message_chunked(_cfg(), "111", "отчёт")

    assert res.ok is False
    assert sum(slept) <= tg._MAX_SEND_WAIT_S
    assert "ожидание" in res.error


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
# notify_plan — порядок «строка раньше сообщения», id последнего куска,
# fail-closed
# ===========================================================================

def _notify_harness(monkeypatch, send_result):
    """Подменяет три выхода notify_plan наружу: отправку, INSERT строки и
    последующий UPDATE её реальными message_id. Возвращает журнал вызовов В
    ПОРЯДКЕ их совершения — порядок здесь и есть предмет проверки."""
    calls = []
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, text, **kw: calls.append(
                            ("send", text, kw.get("reply_markup_on_last")))
                        or send_result)
    monkeypatch.setattr(tg, "create_tg_approval",
                        lambda mid, chat, msg, exp, extra=None: calls.append(
                            ("insert", mid, chat, msg, extra)))
    monkeypatch.setattr(tg, "attach_plan_messages",
                        lambda mid, msg, extra=None: calls.append(
                            ("attach", mid, msg, list(extra or []))) or True)
    monkeypatch.setattr(tg, "delete_tg_approval",
                        lambda mid: calls.append(("delete_row", mid)))
    return calls


def test_notify_plan_inserts_row_before_sending_the_message(monkeypatch):
    """РЕГРЕССИЯ (найдено ревью 2026-08-06): сообщение с кнопками уходило
    РАНЬШЕ, чем в БД появлялась строка. Нажатие в этот зазор пропадало
    впустую — вебхук gmail-mcp делает `UPDATE … WHERE manifest_id=… AND
    status='PENDING'`, строки не находил, а сообщение оставалось висеть с
    живыми кнопками. Порядок обязан быть INSERT → отправка → UPDATE."""
    calls = _notify_harness(monkeypatch, tg.SendResult(True, [10, 11, 12], "", 3))

    ok, err = tg.notify_plan(_cfg(), "m1", "### план", "delete_tasks")

    assert (ok, err) == (True, "")
    assert [c[0] for c in calls] == ["insert", "send", "attach"]
    # строка создаётся ДО отправки, поэтому message_id ещё не известен
    assert calls[0][1:] == ("m1", "111", None, [])
    # кнопки на последнем куске — его id и есть «тот самый» message_id
    assert calls[2] == ("attach", "m1", 12, [10, 11])


def test_notify_plan_puts_buttons_on_last_chunk_only(monkeypatch):
    calls = _notify_harness(monkeypatch, tg.SendResult(True, [7], "", 1))

    tg.notify_plan(_cfg(), "m1", "### план", "delete_tasks")

    _kind, text, markup = calls[1]
    buttons = markup["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == "a:m1"
    assert buttons[1]["callback_data"] == "r:m1"
    assert text.endswith("delete_tasks · ticktick")


def test_notify_plan_partial_send_deletes_stubs_row_and_fails_closed(monkeypatch):
    """Обрубок плана без кнопок опаснее пустоты: его убираем, гейт — закрыт.
    Вставленную первым шагом строку тоже сносим — иначе осталась бы сирота
    PENDING, подтверждающая план, которого владелец никогда не видел."""
    deleted = []
    calls = _notify_harness(monkeypatch, tg.SendResult(False, [5, 6], "boom", 3))
    monkeypatch.setattr(tg, "delete_message",
                        lambda cfg, chat, mid: deleted.append(mid) or True)

    ok, err = tg.notify_plan(_cfg(), "m1", "### план", "delete_tasks")

    assert ok is False and err == "boom"
    assert deleted == [5, 6]
    assert [c[0] for c in calls] == ["insert", "send", "delete_row"]
    assert calls[-1] == ("delete_row", "m1")


def test_notify_plan_survives_a_failed_update_of_message_ids(monkeypatch):
    """UPDATE упал: план доставлен, строка есть, кнопка сработает (вебхук
    снимает разметку по message_id из самого callback_query). Гейт валить из-за
    этого нельзя — потеряна только последующая уборка."""
    _calls = _notify_harness(monkeypatch, tg.SendResult(True, [9], "", 1))
    monkeypatch.setattr(tg, "attach_plan_messages", lambda *a, **k: False)

    assert tg.notify_plan(_cfg(), "m1", "### план", "delete_tasks") == (True, "")


def test_notify_plan_fails_closed_without_a_store(monkeypatch):
    """Нет Postgres — нет строки, значит нажатие кнопки физически не сможет
    ничего подтвердить. Явная проверка `store_ready()` не была покрыта: во
    ВСЕХ харнессах стор объявлен готовым, поэтому её можно было удалить, и
    корректность держалась бы на побочном эффекте (падении на `None.getconn`)
    вместо решения."""
    monkeypatch.setattr(tg, "store_ready", lambda: False)
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda *a, **k: pytest.fail("слать было нельзя"))
    monkeypatch.setattr(tg, "create_tg_approval",
                        lambda *a, **k: pytest.fail("строку писать было некуда"))

    ok, err = tg.notify_plan(_cfg(), "m1", "### план", "delete_tasks")

    assert ok is False and "Postgres" in err


def test_notify_plan_fails_closed_when_the_row_cannot_be_created(monkeypatch):
    """Нет строки — нет гейта: отправлять план с кнопками, которые физически
    не смогут ничего подтвердить, нельзя."""
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda *a, **k: pytest.fail("слать было нельзя"))

    def boom(*a, **k):
        raise RuntimeError("Postgres недоступен")

    monkeypatch.setattr(tg, "create_tg_approval", boom)

    ok, err = tg.notify_plan(_cfg(), "m1", "### план", "delete_tasks")

    assert ok is False and "Postgres недоступен" in err


# ===========================================================================
# post_report_to_group / summarize_in_owner_chat
# ===========================================================================

@pytest.mark.parametrize("verdict,marker", [
    ("ok", "✅ Исполнено и подтверждено"),
    ("partial", "⚠️ Исполнено частично"),
    ("mismatch", "❌ Исполнено, но результат расходится с ожиданием"),
    ("failed", "🛑 Ошибка исполнения"),
    # ❓, не ⚠️ (2026-08-06, дефект №1 части 1+2): раньше этот словарь и
    # server.py's _VERDICT_EMOJI расходились для "unverified" (⚠️ здесь, ❓
    # там) — теперь оба честного «не знаю» согласны.
    ("unverified", "❓ Исполнено, НО независимой перепроверкой не подтверждено"),
])
def test_report_header_matches_verdict(monkeypatch, verdict, marker):
    seen = {}

    def fake_send(cfg, chat, text, **kw):
        seen["chat"], seen["text"] = chat, text
        return tg.SendResult(True, [99], "", 1)

    monkeypatch.setattr(tg, "send_message_chunked", fake_send)
    monkeypatch.setattr(tg, "record_report_messages", lambda *a: None)

    out = tg.post_report_to_group(_cfg(), "m1", "тело отчёта",
                                  tool="delete_tasks", verdict=verdict)

    assert out.message_ids == [99] and out.ok is True
    assert (out.delivered, out.total_chunks) == (1, 1)
    assert seen["chat"] == "-1004357150083"
    assert marker in seen["text"]
    assert "delete_tasks" in seen["text"] and "m1" in seen["text"]
    assert "тело отчёта" in seen["text"]


def test_report_with_its_own_header_is_not_wrapped_in_a_second_one(monkeypatch):
    """2026-08-06, дефект №1 часть 2 (живой прогон на update_project): когда
    `report_md` УЖЕ несёт свой самодостаточный `###`-заголовок (ровно то, что
    строит `_verified_auto_execute_report` в server.py — output-format.md
    §7.1, правило 1), `post_report_to_group` не должен приклеивать ВТОРОЙ,
    независимо сформулированный заголовок про тот же исход поверх него. До
    этой правки оба заголовка печатались подряд одним сообщением, притом
    разными словами (а для "unverified" — и разным эмодзи)."""
    seen = {}

    def fake_send(cfg, chat, text, **kw):
        seen["text"] = text
        return tg.SendResult(True, [1], "", 1)

    monkeypatch.setattr(tg, "send_message_chunked", fake_send)
    monkeypatch.setattr(tg, "record_report_messages", lambda *a: None)

    own_header_report = ("### ✅ Автоисполнение «update_project» — "
                         "подтверждено живым чтением\n_manifest: `eb77e4e758c7`_\n\n"
                         "тело отчёта")
    tg.post_report_to_group(_cfg(), "eb77e4e758c7", own_header_report,
                            tool="update_project", verdict="ok")

    # РОВНО один "###"-заголовок в сообщении — это и есть заголовок самого
    # report_md, обёрточный из _VERDICT_HEADERS отсутствует.
    assert seen["text"].count("###") == 1
    assert "Автоисполнение «update_project»" in seen["text"]
    # Обёрточная формулировка ("✅ Исполнено и подтверждено" и т.п.) не
    # продублирована — её просто нет в тексте.
    for wrapper_marker in tg._VERDICT_HEADERS.values():
        assert wrapper_marker not in seen["text"], wrapper_marker
    assert "тело отчёта" in seen["text"]


def test_report_without_its_own_header_still_gets_the_wrapper(monkeypatch):
    """Обратная сторона: отчёты БЕЗ своего заголовка (например verdict="lost"
    — план не пережил перезапуск) по-прежнему получают заголовок от
    `_VERDICT_HEADERS` — им больше неоткуда."""
    seen = {}

    def fake_send(cfg, chat, text, **kw):
        seen["text"] = text
        return tg.SendResult(True, [1], "", 1)

    monkeypatch.setattr(tg, "send_message_chunked", fake_send)
    monkeypatch.setattr(tg, "record_report_messages", lambda *a: None)

    tg.post_report_to_group(_cfg(), "m2", "_manifest: `m2`_\n\nплан пропал",
                            tool="операция неизвестна", verdict="lost")

    assert seen["text"].count("###") == 1
    assert "Подтверждено, но исполнять было нечего" in seen["text"]


def test_report_timestamp_is_owner_timezone_not_utc(monkeypatch):
    """Жёсткое требование: время в отчёте — America/Los_Angeles."""
    seen = {}
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, text, **kw: seen.update(text=text)
                        or tg.SendResult(True, [1], "", 1))
    monkeypatch.setattr(tg, "record_report_messages", lambda *a: None)
    tg.post_report_to_group(_cfg(), "m1", "тело", tool="t", verdict="ok")
    assert any(tzname in seen["text"] for tzname in ("PDT", "PST")), seen["text"]


def test_report_records_message_ids_for_later_cleanup(monkeypatch):
    recorded = {}
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, text, **kw: tg.SendResult(True, [4, 5], "", 2))
    monkeypatch.setattr(tg, "record_report_messages",
                        lambda mid, chat, ids: recorded.update(
                            dict(mid=mid, chat=chat, ids=ids)))
    tg.post_report_to_group(_cfg(), "m9", "тело", tool="t", verdict="ok")
    assert recorded == {"mid": "m9", "chat": "-1004357150083", "ids": [4, 5]}


def test_partial_report_delivery_is_reported_as_partial_not_success(monkeypatch):
    """ГЛАВНОЕ свойство честности (2026-08-06): на затяжном 429 в группу
    ложатся, например, 2 части из 6. Раньше наружу отдавался просто непустой
    список id, и вызывающий читал это как полный успех — в личку уходило
    бодрое «Подробный отчёт — в группе». Теперь неполнота видна явно."""
    monkeypatch.setattr(tg, "send_message_chunked",
                        lambda cfg, chat, text, **kw: tg.SendResult(
                            False, [1, 2], "Too Many Requests: retry after 37", 6))
    recorded = {}
    monkeypatch.setattr(tg, "record_report_messages",
                        lambda mid, chat, ids: recorded.update(ids=ids))

    out = tg.post_report_to_group(_cfg(), "m1", "тело", tool="t", verdict="ok")

    assert out.ok is False
    assert (out.delivered, out.total_chunks) == (2, 6)
    assert out.message_ids == [1, 2]
    # id доставленных кусков всё равно записываются — иначе reaper их не найдёт
    assert recorded == {"ids": [1, 2]}


def test_report_never_raises_even_if_transport_explodes(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("сеть отвалилась")

    monkeypatch.setattr(tg, "send_message_chunked", boom)
    out = tg.post_report_to_group(_cfg(), "m1", "тело", tool="t", verdict="ok")
    assert out.ok is False and out.message_ids == [] and out.delivered == 0


def test_summary_edits_message_and_strips_buttons(monkeypatch):
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    assert tg.summarize_in_owner_chat(_cfg(), "111", 55,
                                      "**Готово** — 3 задачи удалены") is True
    method, body = fake.bodies[0]
    assert method == "editMessageText"
    assert body["message_id"] == 55
    assert body["reply_markup"] == {"inline_keyboard": []}
    assert "<b>Готово</b>" in body["text"]


def test_summarize_in_owner_chat_strips_agent_instructions_before_editing(monkeypatch):
    """Тот же предохранитель на ВТОРОМ (и последнем) пути текста в Telegram —
    `editMessageText` в `summarize_in_owner_chat`, который не проходит через
    `send_message_chunked` вовсе."""
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    short = "**Готово** — 1 задача\n[agent: repeat this verbatim to the user]"

    assert tg.summarize_in_owner_chat(_cfg(), "111", 55, short) is True

    sent_text = fake.bodies[0][1]["text"]
    assert not _has_agent_marker(sent_text), sent_text
    assert "Готово" in sent_text


def test_summary_reports_failure_so_caller_keeps_the_plan_chunks(monkeypatch):
    """Возврат — не косметика: по нему вызывающий решает, можно ли удалять
    предыдущие куски плана. Пока итог не вписан, стирать контекст нельзя."""
    monkeypatch.setattr(tg, "_tg_call", lambda cfg, m, b: {
        "ok": False, "description": "Bad Request: message to edit not found"})
    assert tg.summarize_in_owner_chat(_cfg(), "111", 55, "сводка") is False


def test_summary_longer_than_the_limit_is_cut_and_still_delivered(monkeypatch):
    """Ветка `len(chunks) > 1` в summarize_in_owner_chat не была покрыта ни
    одним тестом, а двойник Telegram принимал текст любой длины — то есть
    резерв бюджета под приписку можно было выкинуть, и пакет остался бы
    зелёным. Живой Bot API на этом отвечает 400, сводка не появляется, кнопки
    на исполненном плане висят дальше.

    Вход намеренно плотный (много коротких строк): нарезка заполняет первый
    кусок почти под самый лимит, поэтому именно приписка и решает, влезет
    сообщение или нет."""
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    long_summary = "\n".join(f"строка {i}" for i in range(900))

    assert tg.summarize_in_owner_chat(_cfg(), "111", 55, long_summary) is True

    method, body = fake.bodies[0]
    assert method == "editMessageText"
    assert _visible_len(body["text"]) <= tg.TELEGRAM_TEXT_LIMIT
    assert "сводка сокращена" in body["text"], (
        "обрезали молча — человек не узнает, что видит не весь итог")
    assert body["reply_markup"] == {"inline_keyboard": []}


def test_summary_without_message_id_is_a_noop(monkeypatch):
    fake = _FakeTelegram()
    monkeypatch.setattr(tg, "_tg_call", fake)
    assert tg.summarize_in_owner_chat(_cfg(), "111", None, "сводка") is False
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


def test_reap_never_touches_an_archive_of_a_decided_operation(monkeypatch):
    """ГЛАВНОЕ свойство архива (Максим сказал это отдельно и прямо): отчёт об
    УЖЕ ИСПОЛНЕННОЙ операции не удаляется НИКОГДА — удаляется только план, на
    который так и не ответили.

    Проверяем на мини-эмуляции Postgres, а не на строке SQL: фейковый курсор
    применяет ровно ту семантику, которую декларирует запрос (status IN
    ('PENDING','EXPIRED') AND expires_at <= now), и падает, если фильтр из
    запроса исчезнет. Так тест ловит и «забыли WHERE», и «добавили APPROVED в
    список», а не только опечатку в тексте."""
    now = tg._now_ms()
    store = _FakeStatusStore([
        # (manifest_id, server, status, expires_at, chat, msg, extra, rchat, rids)
        ("m-pending", "ticktick", "PENDING", now - 1, "111", 900, [898, 899],
         "-100777", [500]),
        ("m-expired", "ticktick", "EXPIRED", now - 1, "111", 910, [], None, None),
        ("m-approved", "ticktick", "APPROVED", now - 1, "111", 920, [918],
         "-100777", [600, 601]),
        ("m-rejected", "ticktick", "REJECTED", now - 1, "111", 930, [928],
         "-100777", [700]),
        # чужие строки в ОБЩЕЙ таблице: просрочены и неотвечены — то есть
        # подходят под все условия, кроме принадлежности серверу
        ("m-gmail", "gmail", "PENDING", now - 1, "111", 940, [938], None, None),
        ("m-calendar", "calendar", "EXPIRED", now - 1, "111", 950, [], None, None),
    ])
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "_pg_pool", store)
    deleted = []
    monkeypatch.setattr(tg, "delete_message",
                        lambda cfg, chat, mid: deleted.append((chat, mid)) or True)

    assert tg.reap_expired(_cfg()) == 2

    # неотвеченный план сносится целиком: кнопки + куски + сообщения отчёта
    assert deleted == [("111", 900), ("111", 898), ("111", 899),
                       ("-100777", 500), ("111", 910)]
    # ни одного удаления в архиве решённых операций и в чужих сообщениях
    archive_ids = {918, 920, 928, 930, 600, 601, 700}
    assert not [d for d in deleted if d[1] in archive_ids]
    foreign_ids = {938, 940, 950}
    assert not [d for d in deleted if d[1] in foreign_ids], (
        "стёрли сообщение чужого MCP-сервера из лички владельца")
    # и сами строки решённых операций (а также чужие) остались в таблице
    assert {r[0] for r in store.rows} == {"m-approved", "m-rejected",
                                          "m-gmail", "m-calendar"}


def test_reap_survives_a_row_without_a_message_id(monkeypatch):
    """`message_id IS NULL` — штатная строка, а не битая: `notify_plan`
    вставляет её ДО отправки, и если отправка не состоялась (упал процесс),
    убирать в Telegram нечего. Удаляем только строку, без вызовов и без
    исключений."""
    rows = [("m-null", "111", None, None, None, None)]
    monkeypatch.setattr(tg, "store_ready", lambda: True)
    monkeypatch.setattr(tg, "_pg_pool", _FakePool(rows))
    monkeypatch.setattr(tg, "delete_message",
                        lambda *a, **k: pytest.fail("удалять было нечего"))
    assert tg.reap_expired(_cfg()) == 1


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


# --- мини-эмуляция таблицы со СТАТУСАМИ (для проверки, что архив не трогают) -
#
# Обычный _FakePool отдаёт заранее заготовленные строки и не смотрит на SQL —
# на нём нельзя доказать, что решённые операции уцелели: он вернул бы что
# угодно. Этот стор хранит строки со статусом и ПРИМЕНЯЕТ фильтр запроса.
# Семантику не «парсим» (это был бы свой недо-Postgres), а требуем дословно:
# исчезнет условие — упадёт assert прямо в execute.

class _StatusAwareCursor:
    """Строка стора: (manifest_id, server, status, expires_at, chat_id,
    message_id, extra_message_ids, report_chat_id, report_message_ids).

    Колонка `server` здесь НЕ формальность: таблица `tg_approvals` общая с
    пятью TS-серверами (gmail/drive/calendar/docs/sheets), и все шесть ходят
    ОДНИМ ботом. Раньше её в этом двойнике не было вовсе — то есть двойник
    физически не мог заметить, если бы уборка ticktick-mcp начала сносить
    чужие PENDING-строки и стирать чужие сообщения с кнопками из лички
    владельца."""

    _COLUMNS = ("manifest_id", "chat_id", "message_id", "extra_message_ids",
                "report_chat_id", "report_message_ids")

    def __init__(self, store):
        self._store = store
        self.sql = ""
        self._returned = []

    def execute(self, sql, params=None):
        self.sql = sql
        flat = " ".join(sql.split())
        assert flat.startswith("DELETE FROM tg_approvals"), flat
        assert "server = 'ticktick'" in flat, (
            "уборка обязана ограничиваться СВОИМИ строками: таблица общая с "
            "gmail/drive/calendar/docs/sheets-mcp")
        assert "status IN ('PENDING', 'EXPIRED')" in flat, (
            "уборка обязана ограничиваться неотвеченными планами")
        assert "expires_at <= %s" in flat
        now = (params or (0,))[0]
        keep, taken = [], []
        for row in self._store.rows:
            _mid, server, status, expires_at = row[0], row[1], row[2], row[3]
            if (server == "ticktick" and status in ("PENDING", "EXPIRED")
                    and expires_at <= now):
                taken.append(row)
            else:
                keep.append(row)
        self._store.rows = keep
        # RETURNING отдаёт колонки в порядке из запроса (см. _COLUMNS)
        self._returned = [(r[0], r[4], r[5], r[6], r[7], r[8]) for r in taken]

    def fetchall(self):
        return self._returned

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeStatusStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self.cursor_obj = _StatusAwareCursor(self)
        self.conn = _FakeConn(self.cursor_obj)

    def getconn(self):
        return self.conn

    def putconn(self, conn):
        pass
