"""automation_key.py — отдельный статический AUTOMATION_KEY + временные окна
по кнопке в Telegram (docs/TZ/TZ_temp_automation_key.md, 2026-08-10;
docs/TZ/TZ_multi_automation_windows.md, 2026-08-11 — НЕСКОЛЬКО одновременно
активных окон вместо одного, см. ниже).

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
зовёт функции отсюда: `matches_static`, `generate_window`, `check_window`,
`find_window` — деталей схемы таблицы `tg_automation_windows` он не видит и
видеть не должен, ровно как `manifest_store`/`tg_approval` инкапсулируют
свою часть уже сегодня. Никакой Telegram-логики (отправка сообщений, кнопки,
разбор апдейтов) здесь тоже нет — это ответственность `tg_approval.py` (он
уже владеет транспортом Telegram, и он же зовёт `revoke_window`/
`revoke_all_windows`/`list_windows` по командам/кнопкам владельца); этот
модуль — «минисервис» ТОЛЬКО жизненного цикла ключей, как и попросил
владелец (TZ §1).

НЕСКОЛЬКО ОДНОВРЕМЕННЫХ ОКОН (docs/TZ/TZ_multi_automation_windows.md,
2026-08-11). Владелец использует временные ключи в НЕСКОЛЬКИХ разных чатах
одновременно — первая версия этого модуля хранила РОВНО ОДНО окно на сервер
(`_WINDOW_ID = SERVER` фиксирован, повторная генерация UPSERT'ила ту же
строку и инвалидировала предыдущий токен), что молча ломало этот сценарий:
сгенерировал ключ для чата А → вставил → сгенерировал ключ для чата Б → ключ
чата А перестал работать. Теперь `window_id` — случайный (`secrets.
token_hex(6)`) на КАЖДУЮ генерацию, `generate_window` — INSERT новой строки
(не UPSERT существующей), и произвольное число окон активно параллельно,
каждое истекает по своему собственному `expires_at`. `window_status`
(алиас «последнего» окна) убран — при множестве окон концепция «последнее»
не осмысленна для вызывающего; ему на замену `find_window` (какое ИМЕННО
окно совпало с присланным токеном — нужно для аудита) и `list_windows`
(все активные, для команды/кнопки «Список»).

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
from typing import Any, Dict, List, Optional
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

# Сколько раз generate_window пробует новый случайный window_id, если он
# внезапно совпал с уже существующим (PRIMARY KEY conflict) — 12 hex-символов
# (secrets.token_hex(6)) значит 48 бит энтропии, реальная коллизия практически
# невозможна, но код не должен молча упасть в том редком случае, когда она
# всё же случится.
_WINDOW_ID_INSERT_ATTEMPTS = 5

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
    """DDL из TZ_multi_automation_windows.md — `server`/`window_id` составной
    смысл (одна таблица может завтра обслуживать несколько серверов), PRIMARY
    KEY только `window_id`, но теперь это СЛУЧАЙНЫЙ id на каждую генерацию
    (`secrets.token_hex(6)`, см. `generate_window`), а не фиксированный
    `SERVER` — поэтому несколько строк ОДНОВРЕМЕННО активны для одного
    `server`, там где раньше была ровно одна.

    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS label` — на случай, если таблица
    уже существует в проде со старой (одно-оконной) схемой без `label`:
    `CREATE TABLE IF NOT EXISTS` её не тронет, а старая единственная строка
    (`window_id = 'ticktick'`) остаётся как есть и участвует в выдаче наравне
    с новыми — не теряем то, что уже было выдано до этой миграции."""
    with _conn() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tg_automation_windows (
                server          TEXT NOT NULL,
                window_id       TEXT PRIMARY KEY,
                token_hash      TEXT NOT NULL,
                label           TEXT,
                created_at      BIGINT NOT NULL,
                expires_at      BIGINT NOT NULL,
                revoked_at      BIGINT,
                created_by_chat TEXT NOT NULL
            )
            """
        )
        cur.execute(
            "ALTER TABLE tg_automation_windows ADD COLUMN IF NOT EXISTS label TEXT"
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


def generate_window(chat_id: str, label: Optional[str] = None) -> str:
    """Выпускает НОВЫЙ временной токен как ОТДЕЛЬНУЮ строку (INSERT, не
    UPSERT — TZ_multi_automation_windows.md, «повторная генерация НЕ должна
    убивать более раннюю»): все ранее выданные и всё ещё активные окна
    продолжают работать как ни в чём не бывало. `window_id` — случайный
    (`secrets.token_hex(6)`), не связан с чат-идентификатором и не
    последовательный (owner попросил явно: «не предсказуемый»).

    Возвращает СЫРОЙ токен ровно один раз — вызывающий (Telegram-обработчик
    в tg_approval.py) обязан показать его владельцу немедленно и не сохранять
    нигде, кроме этого возврата.

    `label` — необязательная человекочитаемая метка («чат с Х»), NULL по
    умолчанию; сегодня её некому передавать (ни команда, ни кнопки её не
    спрашивают), но `list_windows` уже умеет её показывать, если она когда-то
    появится.

    Пустая строка, если хранилище не поднято (TG_APPROVAL_ENABLED=false или
    CONSENT_DATABASE_URL не задан) — вызывающий обязан явно сказать об этом,
    а не тихо выдать токен, который негде проверить."""
    if not store_ready():
        return ""
    import psycopg2

    token = secrets.token_urlsafe(32)
    now = _now_ms()
    expires = now + int(AUTOMATION_WINDOW_HOURS * 3600 * 1000)
    label_val = (label or "").strip() or None
    for _attempt in range(_WINDOW_ID_INSERT_ATTEMPTS):
        window_id = secrets.token_hex(6)
        try:
            with _conn() as cur:
                cur.execute(
                    """
                    INSERT INTO tg_automation_windows
                      (server, window_id, token_hash, label, created_at,
                       expires_at, revoked_at, created_by_chat)
                    VALUES (%s, %s, %s, %s, %s, %s, NULL, %s)
                    """,
                    (SERVER, window_id, _hash(token), label_val, now, expires,
                     str(chat_id)),
                )
            return token
        except psycopg2.IntegrityError:
            # window_id уже занят — статистически почти невозможно (48 бит
            # энтропии), но не молчим: пробуем ещё раз с новым случайным id,
            # а не роняем выдачу ключа владельцу.
            continue
    raise RuntimeError(
        f"automation_key.generate_window: не подобрал свободный window_id "
        f"за {_WINDOW_ID_INSERT_ATTEMPTS} попыток"
    )


def find_window(provided: str) -> Optional[Dict[str, Any]]:
    """Какое ИМЕННО активное окно совпало с присланным `provided` — словарь
    (`window_id`, `label`, `created_at`, `expires_at`, `created_by_chat`) или
    None, если ни одно активное окно не совпало (или `provided` пуст, или
    хранилище не поднято).

    Ищет СРЕДИ ВСЕХ активных окон (`revoked_at IS NULL AND expires_at >
    now`), не одного фиксированного, как раньше (TZ_multi_automation_
    windows.md) — при множестве одновременно открытых окон это единственный
    способ узнать, КАКОЕ из них сейчас используется (нужно для аудита —
    `server.py`'s `_automation_key_channel` несёт `created_at` совпавшего
    окна в метку журнала). Каждое сравнение хэша — постоянное по времени
    (`_digest_matches`); порядок перебора — по `created_at` (детерминирован
    ради тестов), но сам перебор НЕ constant-time по количеству активных
    окон — то же самое тайминг-допущение, что фиксирует ТЗ («все
    constant-time» — про КАЖДОЕ сравнение по отдельности, не про общее число
    окон, которое и так не секрет)."""
    if not (provided and store_ready()):
        return None
    now = _now_ms()
    provided_hash = _hash(provided)
    with _conn() as cur:
        cur.execute(
            "SELECT window_id, token_hash, label, created_at, expires_at, "
            "created_by_chat FROM tg_automation_windows "
            "WHERE server = %s AND revoked_at IS NULL AND expires_at > %s "
            "ORDER BY created_at ASC",
            (SERVER, now),
        )
        rows = cur.fetchall()
    for window_id, token_hash, label, created_at, expires_at, created_by_chat in rows:
        if _digest_matches(token_hash, provided_hash):
            return {
                "window_id": window_id, "label": label,
                "created_at": created_at, "expires_at": expires_at,
                "created_by_chat": created_by_chat,
            }
    return None


def check_window(provided: str) -> bool:
    """Есть ли СРЕДИ ВСЕХ активных окон хоть одно, чей хэш совпал с `provided`
    (TZ §3.4, пункт 2, расширено TZ_multi_automation_windows.md на множество
    окон). Пустой `provided` или отключённое хранилище → False.

    Тонкая обёртка над `find_window` — тот же единственный SQL-запрос и та
    же логика перебора, здесь просто отбрасывается КАКОЕ окно совпало (это
    нужно только аудиту в server.py, не самой проверке допуска)."""
    return find_window(provided) is not None


def revoke_window(window_id: str, chat_id: str = "") -> bool:
    """Гасит ОДНО конкретное окно по его `window_id` немедленно (`revoked_at
    = now`) — НЕ удаляет строку (TZ §3.6/тест 7, та же дисциплина, что
    `manifest_store.mark_consumed`: гасить, не стирать, история остаётся
    видна в базе). Остальные активные окна (если есть) не затрагивает —
    TZ_multi_automation_windows.md, тест 2.

    `chat_id` — кто нажал «Отозвать» (для будущей многотенантности / аудита;
    owner-only проверка САМА уже сделана вызывающим до этой функции, здесь
    второй раз не переспрашивается — см. TZ «никаких новых путей авторизации
    не изобретать»).

    Возвращает True, если было что гасить (окно с таким id существовало и
    было активно); False — такого окна нет / оно уже неактивно / хранилище
    выключено / `window_id` пуст. Ни то, ни другое НЕ ошибка."""
    if not (store_ready() and window_id):
        return False
    with _conn() as cur:
        cur.execute(
            "UPDATE tg_automation_windows SET revoked_at = %s "
            "WHERE server = %s AND window_id = %s AND revoked_at IS NULL "
            "AND expires_at > %s",
            (_now_ms(), SERVER, window_id, _now_ms()),
        )
        return cur.rowcount > 0


def revoke_all_windows(chat_id: str = "") -> int:
    """Гасит ВСЕ активные окна разом (TZ_multi_automation_windows.md, тест 3)
    — команда/кнопка «Выключить всё». Возвращает число реально погашенных
    (0, если активных окон и не было, или хранилище выключено — не ошибка).

    `chat_id` — по тому же интерфейсу, что `revoke_window` (задел на
    аудит/многотенантность, сегодня не фильтр)."""
    if not store_ready():
        return 0
    with _conn() as cur:
        cur.execute(
            "UPDATE tg_automation_windows SET revoked_at = %s "
            "WHERE server = %s AND revoked_at IS NULL AND expires_at > %s",
            (_now_ms(), SERVER, _now_ms()),
        )
        return cur.rowcount


def list_windows(chat_id: str = "") -> List[Dict[str, Any]]:
    """Все АКТИВНЫЕ окна (TZ_multi_automation_windows.md, тест 4) — для
    команды/кнопки «Список». Каждый элемент: `window_id`, `label`,
    `created_at`, `expires_at`, `created_by_chat`, `remaining_s` — БЕЗ
    хэшей и без сырых токенов (те не восстановимы принципиально, см.
    докстринг файла про «хэш, не сам токен»). Погашенные/истёкшие окна не
    попадают в список (они всё ещё есть в базе — просто не активны, см.
    `revoke_window`), отсортированы по `created_at` (старые сверху).

    Пустой список — активных окон нет (никогда не выдавались / все
    погашены-истекли) ИЛИ хранилище выключено; вызывающий не различает эти
    два случая (для «Списка» разница не имеет значения — в обоих «сейчас
    активных окон нет»)."""
    if not store_ready():
        return []
    now = _now_ms()
    with _conn() as cur:
        cur.execute(
            "SELECT window_id, label, created_at, expires_at, created_by_chat "
            "FROM tg_automation_windows WHERE server = %s AND revoked_at IS NULL "
            "AND expires_at > %s ORDER BY created_at ASC",
            (SERVER, now),
        )
        rows = cur.fetchall()
    return [
        {
            "window_id": window_id, "label": label,
            "created_at": created_at, "expires_at": expires_at,
            "created_by_chat": created_by_chat,
            "remaining_s": max(0, (expires_at - now) // 1000),
        }
        for window_id, label, created_at, expires_at, created_by_chat in rows
    ]


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
