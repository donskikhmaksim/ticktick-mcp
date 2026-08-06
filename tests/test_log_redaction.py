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
