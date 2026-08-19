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
    """DDL из TZ_automation_key_hub.md (2026-08-11) — таблица теперь ОБЩАЯ на
    всю экосистему MCP-серверов, не только ticktick. Генерация переехала в
    gmail-mcp (он один держит вебхук после консолидации ботов) — ticktick-mcp
    только ЧИТАЕТ и проверяет, подходит ли окно для него по `scope`.

    `server` — старый столбец (одно-серверная схема до 2026-08-11), оставлен
    НЕТРОНУТЫМ ради уже выданных строк на боевой базе, но новый код по нему
    не фильтрует. `scope` — csv из канонических имён сервисов
    (gmail/calendar/drive/sheets/docs/ticktick) ИЛИ буквально `"all"`.
    `ADD COLUMN IF NOT EXISTS scope` + бэкафилл из `server` — идемпотентно,
    безопасно повторять при каждом старте (тот же приём, что у `label`
    раньше)."""
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
            "ALTER TABLE tg_automation_windows ADD COLUMN IF NOT EXISTS scope TEXT"
        )
        cur.execute(
            "UPDATE tg_automation_windows SET scope = server WHERE scope IS NULL"
        )
        # 2026-08-11 (TZ_automation_key_duration_labels.md, §1): срок жизни
        # окна теперь настраиваемый ВПЛОТЬ ДО «бессрочно» — NULL в
        # expires_at означает именно это, а не «забыли заполнить». Раньше
        # столбец был NOT NULL (фиксированный AUTOMATION_WINDOW_HOURS на
        # всё). DROP NOT NULL идемпотентен — повтор на уже nullable-столбце
        # не падает.
        cur.execute(
            "ALTER TABLE tg_automation_windows ALTER COLUMN expires_at DROP NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS tg_automation_windows_scope_idx "
            "ON tg_automation_windows (revoked_at, expires_at)"
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
    """УСТАРЕЛО для боевого пути (TZ_automation_key_hub.md, 2026-08-11):
    генерация временных окон теперь происходит В gmail-mcp (он один держит
    вебхук после консолидации ботов) — ticktick-mcp свою собственную команду/
    кнопку `/automation_key` в Telegram больше НЕ показывает (см.
    `tg_approval.py`, где обработчик выключен). Функция оставлена
    работоспособной (пишет `scope=SERVER`, то есть окно валидно только для
    ticktick) ради обратной совместимости с прямыми вызовами/тестами — не
    удалена, но живого пути к ней из Telegram больше нет.

    Выпускает НОВЫЙ временной токен как ОТДЕЛЬНУЮ строку (INSERT, не
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
                       expires_at, revoked_at, created_by_chat, scope)
                    VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s)
                    """,
                    (SERVER, window_id, _hash(token), label_val, now, expires,
                     str(chat_id), SERVER),
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


def _scope_covers_me(scope: Optional[str]) -> bool:
    """Разбирает `scope` строки таблицы и решает, действует ли она на МЕНЯ
    (`SERVER = "ticktick"`) — TZ_automation_key_hub.md, раздел "Проверка
    scope". `"all"` — покрывает всех. Иначе — точное совпадение токена ПОСЛЕ
    разбиения по запятой, НЕ подстрокой (`"ticktick2"` не обязан совпасть с
    `"ticktick"`). Пустой/NULL `scope` (старая строка до миграции, бэкафилл
    почему-то не сработал) — трактуется как НЕ покрывающий: fail-closed,
    молчаливое совпадение опаснее отказа."""
    if not scope:
        return False
    scope = scope.strip()
    if scope == "all":
        return True
    return SERVER in {part.strip() for part in scope.split(",") if part.strip()}


def find_window(provided: str) -> Optional[Dict[str, Any]]:
    """Какое ИМЕННО активное окно, ПОКРЫВАЮЩЕЕ МЕНЯ ПО SCOPE, совпало с
    присланным `provided` — словарь (`window_id`, `label`, `created_at`,
    `expires_at`, `created_by_chat`) или None, если ни одно такое окно не
    совпало (или `provided` пуст, или хранилище не поднято).

    Таблица ОБЩАЯ на всю экосистему (TZ_automation_key_hub.md, 2026-08-11):
    строку могло создать любой сервис (сейчас — только gmail-mcp, он один
    владеет генерацией), поэтому запрос больше НЕ фильтрует по `server` в
    SQL — читает ВСЕ активные окна и в Python отбрасывает те, чей `scope` не
    покрывает `ticktick` (`_scope_covers_me`). Каждое сравнение хэша —
    постоянное по времени (`_digest_matches`); порядок перебора — по
    `created_at` (детерминирован ради тестов)."""
    if not (provided and store_ready()):
        return None
    now = _now_ms()
    provided_hash = _hash(provided)
    with _conn() as cur:
        cur.execute(
            "SELECT window_id, token_hash, label, created_at, expires_at, "
            "created_by_chat, scope FROM tg_automation_windows "
            "WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > %s) "
            "ORDER BY created_at ASC",
            (now,),
        )
        rows = cur.fetchall()
    for window_id, token_hash, label, created_at, expires_at, created_by_chat, scope in rows:
        if not _scope_covers_me(scope):
            continue
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
    выключено / `window_id` пуст. Ни то, ни другое НЕ ошибка.

    SCOPE, НЕ server (QA-2 2026-08-19, добор №9): фильтр `server = %s`
    отбирал только окна, СОЗДАННЫЕ этим сервером, — а `find_window` (то, что
    реально открывает гейт) принимает окно ЛЮБОГО создателя, чей `scope`
    покрывает ticktick. После переезда генерации ключей в gmail-mcp боевые
    окна создаются с server='gmail' — локальный отзыв их не видел, то есть
    окно продолжало открывать гейт ПОСЛЕ «отзыва». Теперь отзыв/список ходят
    той же scope-логикой, что и допуск: гасится/показывается ровно то, что
    может открыть гейт этому серверу."""
    if not (store_ready() and window_id):
        return False
    now = _now_ms()
    with _conn() as cur:
        cur.execute(
            "SELECT scope FROM tg_automation_windows "
            "WHERE window_id = %s AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > %s)",
            (window_id, now),
        )
        row = cur.fetchone()
        if row is None or not _scope_covers_me(row[0]):
            return False
        cur.execute(
            "UPDATE tg_automation_windows SET revoked_at = %s "
            "WHERE window_id = %s AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > %s)",
            (now, window_id, now),
        )
        return cur.rowcount > 0


def revoke_all_windows(chat_id: str = "") -> int:
    """Гасит ВСЕ активные окна разом (TZ_multi_automation_windows.md, тест 3)
    — команда/кнопка «Выключить всё». Возвращает число реально погашенных
    (0, если активных окон и не было, или хранилище выключено — не ошибка).

    `chat_id` — по тому же интерфейсу, что `revoke_window` (задел на
    аудит/многотенантность, сегодня не фильтр).

    SCOPE, НЕ server (QA-2 2026-08-19, добор №9, см. `revoke_window`):
    «Выключить всё» обязано гасить ВСЁ, что способно открыть гейт этому
    серверу, — то есть все активные окна, чей `scope` покрывает ticktick,
    независимо от того, какой сервис их создал. Со старым фильтром
    `server = %s` кнопка отвечала «активных окон не было», а окно от
    gmail-mcp продолжало действовать."""
    if not store_ready():
        return 0
    now = _now_ms()
    with _conn() as cur:
        cur.execute(
            "SELECT window_id, scope FROM tg_automation_windows "
            "WHERE revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > %s)",
            (now,),
        )
        covering = [wid for wid, scope in cur.fetchall()
                    if _scope_covers_me(scope)]
        if not covering:
            return 0
        cur.execute(
            "UPDATE tg_automation_windows SET revoked_at = %s "
            "WHERE window_id = ANY(%s) AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > %s)",
            (now, covering, now),
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
    активных окон нет»).

    SCOPE, НЕ server (QA-2 2026-08-19, добор №9, см. `revoke_window`): в
    списке — все активные окна, чей `scope` покрывает ticktick (та же
    выборка, которой `find_window` решает про допуск), а не только те, что
    создал этот сервер, — иначе окно от gmail-mcp открывало бы гейт, будучи
    невидимым для «Списка»."""
    if not store_ready():
        return []
    now = _now_ms()
    with _conn() as cur:
        cur.execute(
            "SELECT window_id, label, created_at, expires_at, "
            "created_by_chat, scope "
            "FROM tg_automation_windows WHERE revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > %s) ORDER BY created_at ASC",
            (now,),
        )
        rows = [r[:5] for r in cur.fetchall() if _scope_covers_me(r[5])]
    return [
        {
            "window_id": window_id, "label": label,
            "created_at": created_at, "expires_at": expires_at,
            "created_by_chat": created_by_chat,
            # expires_at=NULL — бессрочно (TZ_automation_key_duration_labels.md
            # §1): "сколько осталось" не осмысленно, None — не 0 (0 читался
            # бы как "вот-вот истечёт", то есть противоположность правде).
            "remaining_s": None if expires_at is None else max(0, (expires_at - now) // 1000),
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
