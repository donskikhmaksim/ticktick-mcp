"""Маскировка секретов пути в строках лога (#119).

ЗАЧЕМ. Доступ к этому серверу закрыт секретом, который живёт В ПУТИ URL:
`STREAMABLE_PATH = f"/mcp/{SECRET}"` (server.py). Uvicorn печатает полный
путь в КАЖДОЙ строке access-лога:

    10.0.0.1:0 - "POST /mcp/<секрет> HTTP/1.1" 200

То есть секрет открытым текстом лежит в логах сервиса за всю историю, а
логи видит всякий, у кого есть доступ к проекту Railway; они же попадают в
выгрузки и в скриншоты при отладке. Секрет не истекает и не ротируется —
значит доступ к логам = полный доступ к серверу. То же касается ссылок
`/dl/<токен>` и `/ul/<токен>`: токен в пути И ЕСТЬ пропуск к вложению.

ЧТО ДЕЛАЕМ. Ставим logging-фильтр, который ПЕРЕД выводом подменяет
секретные сегменты пути на заглушку. Снаружи не меняется ничего: URL тот
же, клиенты (Claude Code, коннекторы claude.ai, n8n) ничего не замечают —
меняется только текст, попадающий в лог:

    10.0.0.1:0 - "POST /mcp/<mcp-secret> HTTP/1.1" 200
    10.0.0.1:0 - "GET /dl/<link-token> HTTP/1.1" 200

Отладка по логам сохраняется полностью: остаются адрес клиента, метод,
код ответа, время (его подставляет formatter) и сам путь — маскируется
ровно один сегмент, тот, который является паролем.

ЧЕГО ЭТО НЕ ДЕЛАЕТ. Секреты, уже попавшие в старые логи, остаются там —
их надо считать скомпрометированными и ротировать отдельно. И это не
замена переносу секрета из пути в заголовок (ломающее изменение, отдельно).
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Iterable, Optional

#: Чем заменяем сам MCP_SECRET.
SECRET_PLACEHOLDER = "<mcp-secret>"
#: Чем заменяем подписанные токены ссылок /dl и /ul.
LINK_TOKEN_PLACEHOLDER = "<link-token>"

# Ниже этой длины подстроку НЕ вырезаем по всему тексту: короткая строка
# («ok», «id») может встретиться где угодно в осмысленном сообщении, и
# слепая замена сделает логи бесполезными. Секрет короче 8 символов и так
# не секрет; позиционное правило (регэкспы ниже) его всё равно накроет.
_MIN_INLINE_SECRET_LEN = 8

# Позиционные правила: сегмент пути СРАЗУ после /mcp, /dl, /ul — это и есть
# пропуск. Работают даже если значение секрета фильтру неизвестно (например
# ссылка выдана другим процессом) и независимо от его длины.
_MCP_PATH_RE = re.compile(r"(?<![\w/])(/mcp/)([^/?\s\"']+)")
_LINK_PATH_RE = re.compile(r"(?<![\w/])(/(?:dl|ul)/)([^/?\s\"']+)")


def redact(text: str, secret: Optional[str] = None) -> str:
    """Вернуть `text` с замаскированными секретами пути.

    Идемпотентна: повторный вызов на уже замаскированной строке ничего не
    меняет (заглушки не совпадают ни с одним правилом на второй проход).
    """
    if not text:
        return text
    if secret and len(secret) >= _MIN_INLINE_SECRET_LEN:
        text = text.replace(secret, SECRET_PLACEHOLDER)
        # uvicorn пропускает путь через urllib.parse.quote (см.
        # uvicorn.protocols.utils.get_path_with_query_string), поэтому
        # секрет с не-URL-безопасными символами доедет до лога в
        # процентной кодировке и на сырое значение уже не похож.
        quoted = urllib.parse.quote(secret)
        if quoted != secret:
            text = text.replace(quoted, SECRET_PLACEHOLDER)
    text = _MCP_PATH_RE.sub(lambda m: m.group(1) + SECRET_PLACEHOLDER, text)
    text = _LINK_PATH_RE.sub(lambda m: m.group(1) + LINK_TOKEN_PLACEHOLDER, text)
    return text


def _redact_any(value: Any, secret: Optional[str]) -> Any:
    return redact(value, secret) if isinstance(value, str) else value


class SecretPathFilter(logging.Filter):
    """Фильтр, маскирующий секреты пути в сообщении и в его аргументах.

    Uvicorn логирует access-строку как ШАБЛОН с аргументами:

        access_logger.info('%s - "%s %s HTTP/%s" %d',
                           client_addr, method, path, http_version, status)

    — то есть путь лежит в `record.args`, а не в `record.msg`, и правка
    только `msg` ничего бы не дала. Правим и то, и другое.

    Никогда не отбрасывает записи (всегда True) — задача фильтра здесь
    отредактировать текст, а не решать, что печатать.
    """

    def __init__(self, secret: Optional[str] = None) -> None:
        super().__init__()
        self.secret = secret or None

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if isinstance(record.msg, str):
            record.msg = redact(record.msg, self.secret)
        args = record.args
        if isinstance(args, dict):
            record.args = {k: _redact_any(v, self.secret) for k, v in args.items()}
        elif isinstance(args, tuple):
            record.args = tuple(_redact_any(a, self.secret) for a in args)
        return True


#: Логгеры, на которые фильтр вешается напрямую. uvicorn.* конфигурируются
#: своим dictConfig с propagate=False, поэтому root-хендлеры их записей не
#: видят и фильтр обязан висеть на самих этих логгерах.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def install(secret: Optional[str] = None,
            logger_names: Iterable[str] = _UVICORN_LOGGERS) -> SecretPathFilter:
    """Повесить фильтр на uvicorn-логгеры и на корневые хендлеры.

    Две точки крепления не дублирование, а разное покрытие:

    * фильтр НА ЛОГГЕРЕ `uvicorn.access` — единственный способ достать
      access-строки: uvicorn ставит им `propagate=False`, до корневых
      хендлеров они не доходят;
    * фильтр НА ХЕНДЛЕРАХ корневого логгера — покрывает всё остальное, что
      печатает сам сервер (`logging.getLogger(__name__)` в server.py и
      соседних модулях): фильтры родительских логгеров к таким записям НЕ
      применяются, а хендлеры — применяются.

    Переживает `uvicorn.Config.configure_logging()`, который вызывается
    позже нас (внутри `mcp.run_streamable_http_async()`): его dictConfig
    пересоздаёт хендлеры перечисленных логгеров, но НЕ снимает с них
    фильтры, а корневой логгер не описывает вовсе. Тест
    `test_log_redaction.py::test_filter_survives_uvicorn_logging_config`
    держит это утверждение под проверкой, а не на веру.

    Идемпотентна: повторный вызов не навешивает второй фильтр.
    """
    existing = _installed_filter()
    if existing is not None:
        return existing

    filt = SecretPathFilter(secret)
    for name in logger_names:
        logging.getLogger(name).addFilter(filt)
    root = logging.getLogger()
    root.addFilter(filt)
    for handler in root.handlers:
        handler.addFilter(filt)
    return filt


def _installed_filter() -> Optional[SecretPathFilter]:
    for f in logging.getLogger().filters:
        if isinstance(f, SecretPathFilter):
            return f
    return None


def uninstall() -> None:
    """Снять фильтр отовсюду (нужен тестам, чтобы не тащить его между ними)."""
    targets = [logging.getLogger(n) for n in _UVICORN_LOGGERS]
    root = logging.getLogger()
    targets.append(root)
    for logger in targets:
        for f in list(logger.filters):
            if isinstance(f, SecretPathFilter):
                logger.removeFilter(f)
        for handler in list(logger.handlers):
            for f in list(handler.filters):
                if isinstance(f, SecretPathFilter):
                    handler.removeFilter(f)
