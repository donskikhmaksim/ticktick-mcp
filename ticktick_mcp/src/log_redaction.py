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

ВТОРОЙ ПОТРЕБИТЕЛЬ (2026-08-09, П7 follow-up). `redact()` больше не только
для логов: `server.py` зовёт её же (через `_redact_for_user`) для текста
исключений, который возвращается МОДЕЛИ в чат — второй канал утечки того
же класса, только без хендлера-фильтра. Отсюда и маски, которых логам
самим по себе не требовалось: токен Telegram-бота (`/bot<token>/` в URL
Telegram API), `Authorization: Bearer …`, логин:пароль в Postgres DSN,
PEM-блок приватного ключа (Google service account). Список не закрыт —
это набор известных форматов секретов в этом проекте, а не гарантия про
любой будущий.
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
# не секрет; позиционное правило (регэкспы ниже) его всё равно накроет —
# оно не смотрит на длину значения вообще.
_MIN_INLINE_SECRET_LEN = 8

# Позиционные правила: сегмент пути СРАЗУ после /mcp, /dl, /ul — это и есть
# пропуск. Работают даже если значение секрета фильтру неизвестно (например
# ссылка выдана другим процессом) и независимо от его длины.
#
# ИСПРАВЛЕНО (аудит 2026-08-09, П7 follow-up). До этой правки регэксп нёс
# `(?<![\w/])` перед слэшем — «символ прямо перед /mcp(/dl,/ul)/ не должен
# быть буквой/цифрой/ещё одним слэшем». Замысел был не пересекать чужой
# путь, но на практике это условие рвётся на ЛЮБОМ полном URL:
# "https://host.up.railway.app/dl/<токен>" — перед слэшем стоит "p" из
# ".app", буква, лукбехайнд не пускает совпадение, и маскировка НЕ
# СРАБАТЫВАЕТ НИКОГДА для полных ссылок — только для голого пути в
# access-логе ("GET /dl/<токен> HTTP/1.1", где перед слэшем пробел).
# Живой пример: исключение HTTP-клиента, в тексте которого лежит URL
# ретрая, — именно так секрет и уезжает модели, ровно та дыра, которую
# П7 должен был закрыть. Ложных срабатываний от снятия лукбехайнда не
# прибавляется: сам паттерн требует буквальный "/mcp/"/"/dl/"/"/ul/" —
# слэш перед именем уже задаёт границу сегмента пути, отдельно проверять
# предыдущий символ незачем.
_MCP_PATH_RE = re.compile(r"(/mcp/)([^/?\s\"'<>]+)")
_LINK_PATH_RE = re.compile(r"(/(?:dl|ul)/)([^/?\s\"'<>]+)")

#: Чем заменяем токен Telegram-бота в сегменте `/bot<token>/` URL Telegram API.
BOT_TOKEN_PLACEHOLDER = "<tg-bot-token>"
#: Чем заменяем значение заголовка `Authorization: Bearer <...>`.
BEARER_PLACEHOLDER = "<bearer-token>"
#: Чем заменяем учётные данные в Postgres DSN (`user:password@`).
DSN_CREDENTIALS_PLACEHOLDER = "<db-credentials>"
#: Чем заменяем PEM-блок приватного ключа (например, Google service account).
PRIVATE_KEY_PLACEHOLDER = "<private-key>"

# Добавлено аудитом 2026-08-09 (П7 follow-up): список масок писался для
# СЕГМЕНТОВ НАШЕГО ЖЕ пути (/mcp,/dl,/ul) и ничего не знал про секреты
# ДРУГИХ систем, которые тоже оказываются в тексте исключений —
# `str(e)` от `requests` внутри URL Telegram API содержит токен бота
# буквально (`_tg_call` в tg_approval.py собирает
# `f"{TELEGRAM_API}/bot{cfg.bot_token}/{method}"`); токен бота — это
# полный контроль над вторым фактором подтверждения удалений.
#
# Формат токена Telegram-бота: `<numeric id>:<~35 base64url-ish символов>`
# (например "123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") — не
# привязываемся к точной длине хвоста, маскируем по форме.
_BOT_TOKEN_RE = re.compile(r"(/bot)(\d+:[A-Za-z0-9_-]+)")
# `Authorization: Bearer <token>` — стандартный HTTP-заголовок, может
# попасть в текст исключения при логировании запроса целиком.
_BEARER_RE = re.compile(r"(?i)(Authorization:\s*Bearer\s+)(\S+)")
# `postgres://user:password@host/db` (и `postgresql://`) — DSN с логином и
# паролем в открытом виде; так возвращает ошибку psycopg при сбое подключения.
_PG_DSN_RE = re.compile(r"(postgres(?:ql)?://)([^:/?#\s]+):([^/?#\s]+)@")
# PEM-блок приватного ключа (Google service account и любой другой RSA/EC
# ключ в том же формате) — самая распознаваемая по форме секретная строка.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----.*?-----END (?:RSA |EC )?PRIVATE KEY-----",
    re.DOTALL,
)


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
    text = _BOT_TOKEN_RE.sub(lambda m: m.group(1) + BOT_TOKEN_PLACEHOLDER, text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + BEARER_PLACEHOLDER, text)
    text = _PG_DSN_RE.sub(lambda m: m.group(1) + DSN_CREDENTIALS_PLACEHOLDER + "@", text)
    text = _PRIVATE_KEY_RE.sub(PRIVATE_KEY_PLACEHOLDER, text)
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
