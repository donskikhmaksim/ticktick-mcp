"""automation_key.py — отдельный статический AUTOMATION_KEY + временные окна
по кнопке в Telegram (docs/TZ/TZ_temp_automation_key.md, 2026-08-10).

ЗАЧЕМ ЭТОТ МОДУЛЬ ВООБЩЕ ЕСТЬ
─────────────────────────────
До этой правки `automation_key` (аргумент headless-инструментов, снимающий
требование план→кнопка для автоматики с верным ключом) сверялся с
`MCP_SECRET` — а у того уже есть ДВЕ ДРУГИЕ роли (путь `/mcp/<secret>` и
корень ключа подписи вложений, см. `.env.template`). Три обязанности на одном
секрете значат, что ротация ради одной ломает остальные две.

Владелец явно попросил (2026-08-10, TZ §1) три НЕЗАВИСИМЫХ канала:
  1. `MCP_SECRET`   — как раньше, для своих двух старых ролей; роль
                       automation_key у него ЗАБИРАЕТСЯ (остаётся только
                       временно, ради обратной совместимости — см. server.py's
                       `_automation_key_matches`).
  2. `AUTOMATION_KEY` — новый отдельный статический секрет (env), меняется
                       редко и осознанно, зашивается кодом во внешние
                       автоматизации (n8n и подобные).
  3. Временные окна  — случайная строка (`secrets.token_urlsafe`), выдаётся
                       по кнопке/команде `/automation_key` в Telegram, живёт
                       `AUTOMATION_WINDOW_HOURS` (default 24) часов, хранится
                       ХЭШЕМ (не сырым токеном) в Postgres.

ПОРТИРУЕМОСТЬ (TZ §7). Этот модуль написан по образцу `tg_approval.py` —
самодостаточный, без импорта других модулей этого репозитория (только
stdlib + psycopg2), чтобы его можно было скопировать byte-for-byte в другие
MCP-репозитории этой же экосистемы (gmail-mcp, calendar-mcp, drive-mcp,
docs-mcp, sheets-mcp), когда это понадобится — НЕ сейчас (TZ §7 явно выносит
это за рамки). Ради той же самостоятельности здесь СВОЙ пул соединений и
СВОЯ функция подсчёта часового пояса, хотя они дословно похожи на то, что уже
есть в `manifest_store.py`/`tg_approval.py`, — сознательное дублирование, а
не забытый рефакторинг.

ЧТО СЕРВЕР (server.py) ДОЛЖЕН ЗНАТЬ, А ЧТО — НЕТ (TZ §3.5). `server.py`
зовёт РОВНО пять функций отсюда: `matches_static`, `generate_window`,
`check_window`, `revoke_window`, `window_status` — деталей схемы таблицы
`tg_automation_windows` он не видит и видеть не должен, ровно как
`manifest_store`/`tg_approval` инкапсулируют свою часть уже сегодня. Никакой
Telegram-логики (отправка сообщений, кнопки, разбор апдейтов) здесь тоже
нет — это ответственность `tg_approval.py` (он уже владеет транспортом
Telegram); этот модуль — «минисервис» ТОЛЬКО жизненного цикла ключей,
как и попросил владелец (TZ §1).

ХРАНИМ ХЭШ, НЕ САМ ТОКЕН — тот же принцип, что и у `tg_approvals`/
`mcp_manifests` не хранят пароли в открытом виде нигде, кроме DSN подключения
самого. Токен виден владельцу РОВНО ОДИН РАЗ — в сообщении Telegram сразу
после генерации.
"""
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Имя сервера в общей базе — то же, что у tg_approval/manifest_store.
SERVER = "ticktick"

# Новый статический ключ — читается ОДИН РАЗ при импорте (тот же стиль, что
# `SECRET = os.getenv("MCP_SECRET", "")` в server.py): смена значения в
# окружении требует рестарта процесса, как и у остальных секретов этого
# уровня. Пусто = канал выключен (matches_static всегда False — тот же
# fail-closed принцип, что у _automation_key_matches на пустом MCP_SECRET).
AUTOMATION_KEY = os.environ.get("AUTOMATION_KEY", "").strip()

# Срок жизни временного окна. float, а не int: тесты используют дробные часы
# (секунды/3600), чтобы не ждать реальные сутки для проверки истечения.
AUTOMATION_WINDOW_HOURS = float(os.environ.get("AUTOMATION_WINDOW_HOURS", "24"))

# window_id стабилен (= SERVER): у одного владельца одновременно активно НЕ
# БОЛЬШЕ ОДНОГО временного окна, и повторная генерация — это UPSERT той же
# строки (TZ §3.6.4: «повторное нажатие ПРОДЛЕВАЕТ — перезаписывает
# expires_at, генерирует новый токен, старый хэш инвалидируется ЗАМЕНОЙ
# СТРОКИ»). Отдельного «пришедшего вторым» окна поэтому не бывает — ровно то
# поведение, которое требует ТЗ, без attrition правил вида «гасить прежние
# активные окна перед вставкой новой строки».
_WINDOW_ID = SERVER

_pg_pool = None
_init_lock = threading.Lock()

# Те же таймауты/мотивация, что у manifest_store.py и tg_approval.py — общая
# база сидит за публичным прокси Railway, без явных лимитов подвисшее
# соединение ждёт дефолтный TCP-таймаут ОС (минуты). Собственные переменные
# окружения НЕ заводим — общие с остальным этим слоем, незачем плодить ручки
# для одной и той же базы.
_PG_CONNECT_TIMEOUT_S = int(os.environ.get("CONSENT_PG_CONNECT_TIMEOUT_S", "10"))
_PG_STATEMENT_TIMEOUT_MS = int(os.environ.get("CONSENT_PG_STATEMENT_TIMEOUT_MS", "15000"))
_POOL_MAX_CONN = int(os.environ.get("AUTOMATION_KEY_PG_POOL_MAX", "5"))


@contextlib.contextmanager
def _conn():
    """Соединение на одну операцию — тот же контракт, что и
    `manifest_store._conn()`: сломанное соединение (обрыв/таймаут) в пул НЕ
    возвращается, логическая ошибка (кривой SQL) — возвращается, транзакция
    коммитится на выходе без исключения, откатывается при исключении."""
    import psycopg2

    conn = None
    broken = False
    try:
        conn = _pg_pool.getconn()
        with conn, conn.cursor() as cur:
            yield cur
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    finally:
        if conn is not None:
            _pg_pool.putconn(conn, close=broken or bool(getattr(conn, "closed", 0)))


def init_store(database_url: str) -> None:
    """Поднимает пул и применяет миграцию — тот же ДСН, что у
    `tg_approval.init_store`/`manifest_store.init_store` (`CONSENT_DATABASE_URL`),
    отдельная таблица. Идемпотентна и потокобезопасна (лок + повторная
    проверка внутри), тот же приём, что у `manifest_store.init_store`."""
    global _pg_pool
    import psycopg2.pool

    with _init_lock:
        if _pg_pool is not None:
            return
        extra = {} if "sslmode=" in database_url else {"sslmode": "require"}
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            1, _POOL_MAX_CONN, dsn=database_url,
            connect_timeout=_PG_CONNECT_TIMEOUT_S,
            options=f"-c statement_timeout={_PG_STATEMENT_TIMEOUT_MS}",
            **extra,
        )
        _ensure_schema()


def close_store() -> None:
    """Закрывает пул. Нужен тестам (каждый прогон поднимает свой) — в проде
    не зовётся, пул живёт столько же, сколько процесс."""
    global _pg_pool
    if _pg_pool is not None:
        try:
            _pg_pool.closeall()
        except Exception as e:  # noqa: BLE001 — закрытие best-effort
            logger.debug(f"automation_key: closeall failed: {e}")
        _pg_pool = None


def store_ready() -> bool:
    """False — штатный режим без CONSENT_DATABASE_URL/TG_APPROVAL_ENABLED:
    временные окна недоступны, но matches_static (не требует базы) и
    остальной сервер работают как прежде."""
    return _pg_pool is not None


def _ensure_schema() -> None:
    """DDL из TZ §3.1, дословно. `server`/`window_id` — составной смысл (одна
    таблица может завтра обслуживать несколько серверов), но PRIMARY KEY —
    только `window_id` (см. `_WINDOW_ID` выше: одна активная строка на
    сервер, генерация — UPSERT)."""
    with _conn() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tg_automation_windows (
                server          TEXT NOT NULL,
                window_id       TEXT PRIMARY KEY,
                token_hash      TEXT NOT NULL,
                created_at      BIGINT NOT NULL,
                expires_at      BIGINT NOT NULL,
                revoked_at      BIGINT,
                created_by_chat TEXT NOT NULL
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS tg_automation_windows_lookup_idx "
            "ON tg_automation_windows (server, revoked_at, expires_at)"
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hash(token: str) -> str:
    """sha256 hex-дайджест utf-8 байтов токена. Хранится и сравнивается ЭТО
    значение, никогда не сырой токен — тот же принцип, что и у остальных
    секретов этого слоя (см. `tg_approval.secret_token_matches`'s докстринг)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _digest_matches(a: str, b: str) -> bool:
    """Постоянное по времени сравнение ДВУХ hex-дайджестов (уже ASCII, так
    что `hmac.compare_digest` на голых `str` здесь безопасен — в отличие от
    сравнения ПРИСЛАННОГО значения напрямую, см. `_automation_key_matches`'s
    докстринг в server.py про non-ASCII)."""
    return hmac.compare_digest(a, b)


def matches_static(provided: str) -> bool:
    """Сравнение с `AUTOMATION_KEY` за постоянное время. Пустой
    `AUTOMATION_KEY` (канал не настроен) или пустой `provided` → всегда
    False — тот же fail-closed принцип, что у остальных сравнений этого
    слоя: пустое никогда не совпадает с пустым."""
    if not (AUTOMATION_KEY and provided):
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(AUTOMATION_KEY.encode("utf-8")).digest(),
    )


def generate_window(chat_id: str) -> str:
    """Выпускает новый временной токен, кладёт его ХЭШ в базу (UPSERT той же
    строки — TZ §3.6.4, см. `_WINDOW_ID`), возвращает СЫРОЙ токен ровно один
    раз — вызывающий (Telegram-обработчик кнопки в tg_approval.py) обязан
    показать его владельцу немедленно и не сохранять нигде, кроме этого
    возврата.

    Пустая строка, если хранилище не поднято (TG_APPROVAL_ENABLED=false или
    CONSENT_DATABASE_URL не задан) — вызывающий обязан явно сказать об этом,
    а не тихо выдать токен, который негде проверить."""
    if not store_ready():
        return ""
    token = secrets.token_urlsafe(32)
    now = _now_ms()
    expires = now + int(AUTOMATION_WINDOW_HOURS * 3600 * 1000)
    with _conn() as cur:
        cur.execute(
            """
            INSERT INTO tg_automation_windows
              (server, window_id, token_hash, created_at, expires_at,
               revoked_at, created_by_chat)
            VALUES (%s, %s, %s, %s, %s, NULL, %s)
            ON CONFLICT (window_id) DO UPDATE SET
              token_hash      = EXCLUDED.token_hash,
              created_at      = EXCLUDED.created_at,
              expires_at      = EXCLUDED.expires_at,
              revoked_at      = NULL,
              created_by_chat = EXCLUDED.created_by_chat
            """,
            (SERVER, _WINDOW_ID, _hash(token), now, expires, str(chat_id)),
        )
    return token


def check_window(provided: str) -> bool:
    """Непросроченный, неотозванный хэш совпал с присланным значением (TZ
    §3.4, пункт 2). Пустой `provided` или отключённое хранилище → False.

    Один SELECT: строка либо активна (`revoked_at IS NULL AND expires_at >
    now`), либо для этой проверки её как будто нет — истёкшая/отозванная
    запись НЕ стирается (см. `revoke_window`/TZ §3.6/тест 7), но и не
    участвует в сравнении."""
    if not (provided and store_ready()):
        return False
    now = _now_ms()
    with _conn() as cur:
        cur.execute(
            "SELECT token_hash FROM tg_automation_windows "
            "WHERE server = %s AND window_id = %s "
            "AND revoked_at IS NULL AND expires_at > %s",
            (SERVER, _WINDOW_ID, now),
        )
        row = cur.fetchone()
    if not row:
        return False
    return _digest_matches(row[0], _hash(provided))


def revoke_window(chat_id: str) -> bool:
    """Гасит активное окно немедленно (`revoked_at = now`) — НЕ удаляет
    строку (TZ §3.6/тест 7, та же дисциплина, что `manifest_store.
    mark_consumed`: гасить, не стирать, история остаётся видна в базе).

    `chat_id` — кто нажал «Выключить» (для будущей многотенантности /
    аудита; owner-only проверка САМА уже сделана вызывающим до этой функции,
    здесь второй раз не переспрашивается — см. TZ «никаких новых путей
    авторизации не изобретать»).

    Возвращает True, если было что гасить (строка существовала и была
    активна); False — окна и так не было / оно уже неактивно / хранилище
    выключено. И то, и другое НЕ ошибка."""
    if not store_ready():
        return False
    with _conn() as cur:
        cur.execute(
            "UPDATE tg_automation_windows SET revoked_at = %s "
            "WHERE server = %s AND window_id = %s AND revoked_at IS NULL "
            "AND expires_at > %s",
            (_now_ms(), SERVER, _WINDOW_ID, _now_ms()),
        )
        return cur.rowcount > 0


def window_status(chat_id: str = "") -> Optional[Dict[str, Any]]:
    """Текущее состояние окна — для кнопки/команды «Статус» (TZ §3.3) И для
    аудит-пометки в отчёте (TZ §4: «открыт <когда>»).

    None — окно НИКОГДА не создавалось (строки в базе нет вовсе, или
    хранилище выключено). Иначе — словарь с исходом ПОСЛЕДНЕЙ генерации,
    даже если оно уже отозвано/истекло: `active` — единственное поле, на
    которое стоит смотреть, чтобы решить «работает ли оно сейчас».

    `chat_id` в сигнатуре — по интерфейсу TZ §3.5 (симметрично
    `revoke_window`); сегодня НЕ используется как фильтр (одна строка на
    сервер, см. `_WINDOW_ID`), задел на будущую многотенантность."""
    if not store_ready():
        return None
    with _conn() as cur:
        cur.execute(
            "SELECT created_at, expires_at, revoked_at, created_by_chat "
            "FROM tg_automation_windows WHERE server = %s AND window_id = %s",
            (SERVER, _WINDOW_ID),
        )
        row = cur.fetchone()
    if not row:
        return None
    created_at, expires_at, revoked_at, created_by_chat = row
    now = _now_ms()
    active = revoked_at is None and expires_at > now
    return {
        "active": active,
        "revoked": revoked_at is not None,
        "expired": revoked_at is None and expires_at <= now,
        "created_at": created_at,
        "expires_at": expires_at,
        "revoked_at": revoked_at,
        "created_by_chat": created_by_chat,
        "remaining_s": max(0, (expires_at - now) // 1000) if active else 0,
    }


# ───────────────────────── форматирование для людей ─────────────────────────
# Своя копия часового пояса владельца — та же причина дублирования, что у
# `tg_approval._resolve_owner_tz`: модуль самодостаточен (см. докстринг
# файла про портируемость), резолвится лениво и под try, чтобы отсутствие
# пакета tzdata не роняло импорт всего модуля.
OWNER_TZ_NAME = "America/Los_Angeles"
_owner_tz = None


def _resolve_owner_tz():
    global _owner_tz
    if _owner_tz is None:
        try:
            _owner_tz = ZoneInfo(OWNER_TZ_NAME)
        except Exception as e:  # noqa: BLE001 — нет базы часовых поясов
            logger.warning(f"automation_key: не нашёл зону {OWNER_TZ_NAME} "
                           f"({e}) — время пойдёт с фиксированным -08:00")
            _owner_tz = timezone(timedelta(hours=-8), "PST")
    return _owner_tz


def format_ms(ms: Optional[int]) -> str:
    """`ms` (unix-миллисекунды) → «дд.мм ЧЧ:ММ» в America/Los_Angeles — тот
    же формат, что у отчётов `_build_operation_report_data` в server.py.
    None/некорректное значение → «?» (не бросает)."""
    if not ms:
        return "?"
    try:
        from datetime import datetime
        return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                .astimezone(_resolve_owner_tz()).strftime("%d.%m %H:%M"))
    except Exception:  # noqa: BLE001
        return "?"
