"""#119: секрет доступа не должен попадать в логи.

Каждый тест смотрит на то, что видно СНАРУЖИ — на реально напечатанную
строку лога (через настоящий logging-хендлер), а не на внутренние
переменные фильтра. Секрет здесь ВЫДУМАННЫЙ: боевое значение не должно
появляться ни в тестах, ни в отчётах.
"""
import io
import logging
import logging.config

import pytest

from ticktick_mcp.src import log_redaction

# Выдуманное значение, похожее по форме на настоящее (длинное, url-safe).
FAKE_SECRET = "fake0secret0do0not0use0abcdef123456"
FAKE_LINK_TOKEN = "eyJwIjoiMSIsInQiOiIyIn0.ZmFrZXNpZ25hdHVyZQ"


@pytest.fixture(autouse=True)
def _restore_logging():
    """Вернуть логирование в исходное состояние после каждого теста."""
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    yield
    log_redaction.uninstall()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    # test_filter_survives_uvicorn_logging_config прогоняет uvicorn'овский
    # dictConfig, который навешивает свои хендлеры и ставит propagate=False.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
        logger.setLevel(logging.NOTSET)


def _attach_stream(logger_name: str) -> tuple[io.StringIO, logging.Handler, logging.Logger]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return stream, handler, logger


def _log_access_line(logger: logging.Logger, path: str, status: int = 200) -> None:
    """Позвать логгер РОВНО так, как это делает uvicorn.

    Формат и порядок аргументов скопированы с
    uvicorn/protocols/http/h11_impl.py: путь приезжает не в тексте
    сообщения, а отдельным аргументом %s.
    """
    logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:0", "POST", path, "1.1", status)


def test_access_log_line_has_no_secret_but_keeps_everything_else():
    log_redaction.install(FAKE_SECRET)
    stream, handler, logger = _attach_stream("uvicorn.access")
    try:
        _log_access_line(logger, f"/mcp/{FAKE_SECRET}")
    finally:
        logger.removeHandler(handler)

    printed = stream.getvalue()
    assert FAKE_SECRET not in printed
    assert "/mcp/<mcp-secret>" in printed
    # Отладка по логам обязана остаться возможной: клиент, метод, версия,
    # код ответа и сам путь на месте.
    assert "10.0.0.1:0" in printed
    assert '"POST' in printed
    assert "HTTP/1.1" in printed
    assert "200" in printed


def test_access_log_line_with_query_string_keeps_query():
    log_redaction.install(FAKE_SECRET)
    stream, handler, logger = _attach_stream("uvicorn.access")
    try:
        _log_access_line(logger, f"/mcp/{FAKE_SECRET}?probe=1")
    finally:
        logger.removeHandler(handler)

    printed = stream.getvalue()
    assert FAKE_SECRET not in printed
    assert "/mcp/<mcp-secret>?probe=1" in printed


def test_attachment_link_token_is_redacted():
    """`/dl/<токен>` — тоже пропуск: он один даёт скачать вложение."""
    log_redaction.install(FAKE_SECRET)
    stream, handler, logger = _attach_stream("uvicorn.access")
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:0", "GET",
                    f"/dl/{FAKE_LINK_TOKEN}", "1.1", 200)
    finally:
        logger.removeHandler(handler)

    printed = stream.getvalue()
    assert FAKE_LINK_TOKEN not in printed
    assert "/dl/<link-token>" in printed
    assert "200" in printed


def test_server_startup_line_is_redacted():
    """server.main() печатает полный URL с секретом при старте — под фильтр
    попадает и он (запись идёт через хендлер корневого логгера)."""
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    log_redaction.install(FAKE_SECRET)

    logging.getLogger("ticktick_mcp.src.server").info(
        "Starting TickTick MCP server (streamable-http) on "
        f"http://0.0.0.0:8000/mcp/{FAKE_SECRET}")

    printed = stream.getvalue()
    assert FAKE_SECRET not in printed
    assert "Starting TickTick MCP server" in printed
    assert "http://0.0.0.0:8000/mcp/<mcp-secret>" in printed


def test_filter_survives_uvicorn_logging_config(capsys):
    """Порядок в бою: install() зовётся в main(), а uvicorn настраивает
    логирование позже, уже внутри run_streamable_http_async(). Его dictConfig
    пересоздаёт хендлеры uvicorn-логгеров — фильтр обязан это пережить,
    иначе защита в проде не работает вообще, а юнит-тесты этого не видят."""
    import uvicorn.config

    log_redaction.install(FAKE_SECRET)
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)

    _log_access_line(logging.getLogger("uvicorn.access"), f"/mcp/{FAKE_SECRET}")

    captured = capsys.readouterr()
    printed = captured.out + captured.err
    assert printed.strip(), "uvicorn-хендлер вообще ничего не напечатал — тест бесполезен"
    assert FAKE_SECRET not in printed
    assert "/mcp/<mcp-secret>" in printed
    assert "200" in printed


def test_server_main_installs_the_filter(monkeypatch):
    """Фильтр обязан ставиться самим сервером, а не только тестами.

    Проверяем поведение, а не наличие строчки в исходнике: гоняем настоящий
    `server.main()` (с заглушенным запуском event loop) и после этого
    смотрим, что access-строка печатается уже замаскированной."""
    from ticktick_mcp.src import server

    monkeypatch.setattr(server, "initialize_client", lambda: True)
    monkeypatch.setattr(server.anyio, "run", lambda *a, **kw: None)
    monkeypatch.setattr(server, "SECRET", FAKE_SECRET)

    server.main()

    stream, handler, logger = _attach_stream("uvicorn.access")
    try:
        _log_access_line(logger, f"/mcp/{FAKE_SECRET}")
    finally:
        logger.removeHandler(handler)

    printed = stream.getvalue()
    assert FAKE_SECRET not in printed
    assert "/mcp/<mcp-secret>" in printed


def test_ordinary_lines_pass_through_untouched():
    """Фильтр не должен глотать или калечить полезное."""
    log_redaction.install(FAKE_SECRET)
    stream, handler, logger = _attach_stream("uvicorn.access")
    try:
        _log_access_line(logger, "/health", status=503)
        logger.info("Batch finished: 12 tasks, 3 skipped")
    finally:
        logger.removeHandler(handler)

    printed = stream.getvalue()
    assert '"POST /health HTTP/1.1" 503' in printed
    assert "Batch finished: 12 tasks, 3 skipped" in printed


def test_plain_mcp_path_without_secret_is_untouched():
    """Без MCP_SECRET путь — просто `/mcp`, маскировать нечего."""
    assert log_redaction.redact('"POST /mcp HTTP/1.1" 200') == '"POST /mcp HTTP/1.1" 200'


def test_redact_is_idempotent():
    once = log_redaction.redact(f"/mcp/{FAKE_SECRET}", FAKE_SECRET)
    assert log_redaction.redact(once, FAKE_SECRET) == once


def test_short_secret_is_not_blindly_stripped_from_text():
    """Короткая строка не вырезается по всему тексту (иначе фильтр
    испортил бы осмысленные сообщения), но позиционно в пути — маскируется."""
    assert log_redaction.redact("task ok, project ok", "ok") == "task ok, project ok"
    assert log_redaction.redact("/mcp/ok", "ok") == "/mcp/<mcp-secret>"


def test_percent_encoded_secret_is_redacted():
    """uvicorn пропускает путь через quote(): секрет с не-URL-безопасными
    символами доезжает до лога в процентной кодировке."""
    tricky = "fake secret/with+chars"
    printed = log_redaction.redact("/mcp/fake%20secret/with%2Bchars", tricky)
    assert "fake%20secret" not in printed
    assert "<mcp-secret>" in printed


def test_percent_encoded_secret_tail_past_a_literal_slash_is_also_redacted():
    """2026-08-09 (независимый аудит): тест выше проверяет ОТСУТСТВИЕ ТОЛЬКО
    ПЕРВОГО куска секрета ("fake%20secret") — а его ловит уже отдельный,
    независимый от quote()-блока механизм: позиционный regex `_MCP_PATH_RE`
    (см. redact()), который стопорится на первом литеральном "/" и один
    маскирует "/mcp/fake%20secret" целиком, что бы ни было дальше. Секрет
    здесь ("fake secret/with+chars") сам содержит "/", а `quote()` по
    умолчанию его НЕ кодирует (safe="/") — значит закодированное значение
    после этого "/" ("with%2Bchars") остаётся ВТОРЫМ сегментом пути, и его
    маскирует ИСКЛЮЧИТЕЛЬНО блок `quoted = urllib.parse.quote(secret); ...
    text.replace(quoted, ...)`. Удаление этого блока оставляет "with%2Bchars"
    в логе нетронутым, а прежний тест этого не видит — он не заглядывает за
    первый "/" секрета вообще. Здесь — смотрим за оба."""
    tricky = "fake secret/with+chars"
    printed = log_redaction.redact("/mcp/fake%20secret/with%2Bchars", tricky)
    assert "with%2Bchars" not in printed, (
        "хвост секрета после встроенного '/' остался в логе в процентной "
        "кодировке — блок квотирования секрета в redact() снят")
    assert tricky not in printed
    # Ровно одна маска на весь секрет — не два обрубка вокруг его "/".
    assert printed == "/mcp/<mcp-secret>"


# ─────────────────── Аудит 2026-08-09 (П7 follow-up) ───────────────────

def test_link_token_is_redacted_inside_a_full_url():
    """Живой дефект, найденный независимым аудитом: `/dl/<токен>` внутри
    ПОЛНОЙ ссылки (`https://хост/dl/<токен>`) — ровно так строится сама
    ссылка (server.py: `f"{base}/dl/{token}"`), и ровно так текст исключения
    HTTP-клиента доносит её до `_redact_for_user`. Старый регэксп с
    `(?<![\\w/])` перед слэшем никогда не срабатывал здесь: перед "/dl/"
    стоит буква домена ("...app" -> "p"), лукбехайнд блокировал совпадение
    на КАЖДОЙ настоящей ссылке — маскировка работала только для голого пути
    в access-логе ("GET /dl/<токен> HTTP/1.1", там перед слэшем пробел)."""
    url = f"https://foo.up.railway.app/dl/{FAKE_LINK_TOKEN}"
    out = log_redaction.redact(url)
    assert FAKE_LINK_TOKEN not in out
    assert "https://foo.up.railway.app/dl/<link-token>" == out


def test_upload_link_token_is_redacted_inside_a_full_url():
    url = f"https://foo.up.railway.app/ul/{FAKE_LINK_TOKEN}"
    out = log_redaction.redact(url)
    assert FAKE_LINK_TOKEN not in out
    assert "<link-token>" in out


def test_mcp_secret_positional_rule_works_inside_a_full_url_too():
    """Тот же класс дефекта, что и для /dl,/ul — для /mcp/ значение обычно
    маскируется по значению SECRET, но позиционное правило (второй, не
    зависящий от значения слой защиты) обязано срабатывать и без него."""
    out = log_redaction.redact(f"https://host.example/mcp/{FAKE_SECRET}",
                               secret=None)
    assert FAKE_SECRET not in out
    assert "https://host.example/mcp/<mcp-secret>" == out


def test_multiline_text_is_still_redacted():
    """Угроза-мутация: `if '\\n' in text: return text` (пропустить
    многострочный вход как «слишком сложный») прошла бы мимо тестов, если
    бы все примеры были однострочными — а вложенные исключения
    (traceback-подобные тексты, тела HTTP-ошибок) многострочны почти
    всегда."""
    multiline = (
        "Traceback (most recent call last):\n"
        f"  requests.exceptions.ConnectionError: /mcp/{FAKE_SECRET}\n"
        f"  see also /dl/{FAKE_LINK_TOKEN} for the retried attachment\n"
        "ConnectionError: [Errno 61] Connection refused"
    )
    out = log_redaction.redact(multiline, FAKE_SECRET)
    assert FAKE_SECRET not in out
    assert FAKE_LINK_TOKEN not in out
    assert "<mcp-secret>" in out
    assert "<link-token>" in out
    assert "Traceback (most recent call last):" in out
    assert "ConnectionError: [Errno 61] Connection refused" in out


def test_telegram_bot_token_is_redacted():
    """Живой дефект, найденный независимым аудитом: `tg_approval._tg_call`
    строит `f"{TELEGRAM_API}/bot{cfg.bot_token}/{method}"`; при сетевом
    сбое `str(e)` от requests несёт этот URL целиком, токен бота уезжает
    модели как есть — токен даёт полный контроль над вторым фактором
    подтверждения удалений."""
    exc_text = ("HTTPSConnectionPool(host='api.telegram.org', port=443): "
                "Max retries exceeded with url: "
                "/bot123456789:AAHfake0bot0token0DO0NOT0USE/sendMessage")
    out = log_redaction.redact(exc_text)
    assert "AAHfake0bot0token0DO0NOT0USE" not in out
    assert "/bot<tg-bot-token>" in out
    assert "api.telegram.org" in out  # диагностика не должна пострадать


def test_bearer_header_is_redacted():
    out = log_redaction.redact("failed request, header: Authorization: Bearer sekrit.jwt.value")
    assert "sekrit.jwt.value" not in out
    assert "Authorization: Bearer <bearer-token>" in out


def test_postgres_dsn_credentials_are_redacted():
    """tg_approval.py возвращает `f'...в Postgres: {e}'` при сбое подключения
    — сообщение psycopg может содержать DSN целиком, включая логин:пароль."""
    out = log_redaction.redact(
        "could not connect to server: postgres://ttuser:hunter2@10.0.0.5:5432/tg")
    assert "hunter2" not in out
    assert "ttuser" not in out
    assert "postgres://<db-credentials>@10.0.0.5:5432/tg" in out


def test_google_private_key_pem_block_is_redacted():
    """Ключ service-account (declutter_sheet.py) — PEM-блок, самый
    узнаваемый по форме секрет, который в принципе может оказаться в тексте
    исключения (например, если ключ повреждён)."""
    body = ("-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC\n"
            "-----END PRIVATE KEY-----")
    out = log_redaction.redact(f"GSHEETS_SA_JSON нечитаем: {body}")
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" not in out
    assert "<private-key>" in out
