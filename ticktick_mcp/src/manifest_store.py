"""Долговечное хранилище планов подтверждения (манифестов) в Postgres.

ЗАЧЕМ ЭТОТ МОДУЛЬ ВООБЩЕ ЕСТЬ (задача #91, 2026-08-06)
──────────────────────────────────────────────────────
План подтверждения (`_MANIFESTS` в server.py) — это ЧТО именно будет сделано,
когда владелец нажмёт ✅ в Telegram. До этого модуля он жил ТОЛЬКО в памяти
процесса, а решение по кнопке — в общем Postgres (`tg_approvals`). Разные
сроки жизни у двух половин одного механизма давали четыре следствия, все
подтверждённые на живом сервисе:

  1. перезапуск сервиса = все висящие планы исчезли; владелец жмёт кнопку —
     исполнять нечего;
  2. авто-деплой из `main` перезапускает процесс в произвольный момент, то
     есть выкатка ГАРАНТИРОВАННО обесценивала кнопки, висящие в этот момент;
  3. план, построенный одной репликой, невидим другой — при нескольких
     экземплярах кнопка не работает вовсе;
  4. механизм «подтверждено, но исполнять нечего» (`_tg_lost_manifest_rows`)
     мог только ИЗВИНИТЬСЯ за потерю, но не предотвратить её.

Здесь план переезжает в ту же базу, где живёт решение по нему. После рестарта
нажатие находит и строку `tg_approvals`, и сам план — и действительно
исполняется.

ЧТО ЗДЕСЬ САМОЕ ОПАСНОЕ — ОДНОРАЗОВОСТЬ
────────────────────────────────────────
В памяти одноразовость держалась даром: между проверкой `consumed` и его
простановкой в однопоточном event loop'е физически нет точки переключения.
В базе этой бесплатной гарантии НЕТ, а цена ошибки несимметрична: потерянный
план — это «повтори ещё раз», двойное исполнение — это второе УДАЛЕНИЕ. Поэтому
захват плана здесь — ОДИН запрос `UPDATE … WHERE consumed_at IS NULL …
RETURNING` (`claim`), а не «прочитал → решил → записал». Postgres блокирует
строку на время UPDATE: из двух одновременных нажатий второе дождётся коммита
первого, увидит уже проставленный `consumed_at` и не получит ничего.

ПОЧЕМУ СВОЯ ТАБЛИЦА, А НЕ КОЛОНКА В `tg_approvals`
──────────────────────────────────────────────────
`tg_approvals` — общая с пятью TS-серверами (gmail/sheets/calendar/docs/drive),
её базовый DDL там помечен FROZEN и меняется только согласованно. Содержимое
плана — сугубо наше: другим серверам оно не нужно и читать его они не умеют.
`mcp_manifests` заводится и обслуживается только этим сервером; колонка
`server` и составной первичный ключ оставляют дверь открытой, если завтра такой
же переезд понадобится соседям.
"""
import contextlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Имя сервера в общей базе — то же, что этот сервер пишет в `tg_approvals`.
SERVER = "ticktick"

_pg_pool = None
_init_lock = threading.Lock()

# Те же таймауты и та же мотивация, что у `tg_approval`: база живёт за ПУБЛИЧНЫМ
# прокси Railway, и без явных лимитов подвисшее соединение ждёт дефолтный
# TCP-таймаут ОС — это минуты. Переменные окружения общие с тем модулем
# намеренно: это одна и та же база, разводить два набора ручек не за чем.
_PG_CONNECT_TIMEOUT_S = int(os.environ.get("CONSENT_PG_CONNECT_TIMEOUT_S", "10"))
_PG_STATEMENT_TIMEOUT_MS = int(os.environ.get("CONSENT_PG_STATEMENT_TIMEOUT_MS", "15000"))

# Размер пула. Обращения сюда идут из `asyncio.to_thread`, а его пул потоков
# по умолчанию заметно шире пяти — при всплеске подтверждений соединений может
# не хватить, и psycopg2 в этом случае не ждёт, а СРАЗУ бросает
# `PoolError: connection pool exhausted`. Семафор ниже превращает этот мгновенный
# отказ в честную очередь с потолком ожидания.
_POOL_MAX_CONN = int(os.environ.get("MANIFEST_PG_POOL_MAX", "5"))
_POOL_WAIT_S = float(os.environ.get("MANIFEST_PG_POOL_WAIT_S", "10"))

# Потолок на размер одного плана в базе (см. `save`).
_PAYLOAD_MAX_BYTES = int(os.environ.get("MANIFEST_PAYLOAD_MAX_BYTES", str(512 * 1024)))
_pool_slots = threading.BoundedSemaphore(_POOL_MAX_CONN)


class StoreBusy(RuntimeError):
    """Все соединения заняты дольше `_POOL_WAIT_S`. Отдельный тип, чтобы
    вызывающий мог отличить «база перегружена» от «запрос не удался»."""


@contextlib.contextmanager
def _conn():
    """Соединение на ОДНУ операцию, с двумя правилами, которые дороже всего
    обходятся, если их забыть:

      1. СЛОМАННОЕ соединение (`OperationalError`/`InterfaceError` — обрыв,
         таймаут, перезапуск базы) в пул НЕ возвращается, а закрывается.
         Вернув его, мы бы уронили запрос СЛЕДУЮЩЕГО потока, который это
         соединение получит, — и так по кругу, пока не перезапустят процесс.
      2. ЛОГИЧЕСКАЯ ошибка (кривой SQL, нарушение ограничения) соединение НЕ
         ломает: транзакция откатывается, соединение здоровое и обязано
         вернуться в пул. Выбрасывать его — значит на каждой такой ошибке
         терять соединение из пяти.

    `with conn` (транзакция psycopg2) внутри: коммит при выходе без
    исключения, откат при исключении."""
    import psycopg2

    if not _pool_slots.acquire(timeout=_POOL_WAIT_S):
        raise StoreBusy(f"пул соединений занят дольше {_POOL_WAIT_S:.0f} c")
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
        _pool_slots.release()


def init_store(database_url: str) -> None:
    """Поднимает пул и применяет миграцию. Зовётся один раз при старте из
    `server.main()`, сразу после `tg_approval.init_store` и на том же DSN.

    ПУЛ ИМЕННО `ThreadedConnectionPool`, а не `SimpleConnectionPool`: все
    обращения сюда идут из `_run_blocking` (`asyncio.to_thread`), то есть из
    РАЗНЫХ потоков пула потоков — Simple-версия по документации psycopg2 для
    этого не предназначена (её `getconn`/`putconn` не защищены блокировкой и
    два потока могут получить одно соединение). Заплатить за это нечем:
    Threaded-версия отличается ровно наличием `threading.Lock` внутри.

    `sslmode` НЕ задаётся жёстко (в отличие от `tg_approval.init_store`,
    который прибит к `require`): тесты этого модуля работают против настоящего
    локального Postgres по unix-сокету, где TLS нет вовсе, а в проде sslmode
    приезжает в самой строке подключения от Railway."""
    global _pg_pool
    import psycopg2.pool

    # Лок + повторная проверка внутри: инициализация обязана быть
    # ИДЕМПОТЕНТНОЙ и потокобезопасной. Без него два потока создали бы два
    # пула, второй затёр бы ссылку на первый, и первый остался бы висеть с
    # открытыми соединениями, которые уже никто не закроет.
    with _init_lock:
        if _pg_pool is not None:
            return
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            1, _POOL_MAX_CONN, dsn=database_url,
            connect_timeout=_PG_CONNECT_TIMEOUT_S,
            options=f"-c statement_timeout={_PG_STATEMENT_TIMEOUT_MS}",
        )
        _ensure_schema()


def close_store() -> None:
    """Закрывает пул. Нужен тестам (каждый прогон поднимает свой) и никогда не
    зовётся в проде — там пул живёт столько же, сколько процесс."""
    global _pg_pool
    if _pg_pool is not None:
        try:
            _pg_pool.closeall()
        except Exception as e:  # noqa: BLE001 — закрытие best-effort
            logger.debug(f"manifest_store: closeall failed: {e}")
        _pg_pool = None


def store_ready() -> bool:
    """Есть ли вообще долговечное хранилище. False — это ШТАТНЫЙ режим, а не
    поломка: без `TG_APPROVAL_ENABLED` базы нет, и весь сервер работает ровно
    как до этой задачи (планы живут в памяти процесса)."""
    return _pg_pool is not None


def _ensure_schema() -> None:
    """Миграция применяется КОДОМ ПРИ СТАРТЕ — той же дисциплиной, что и
    `tg_approval._ensure_schema` (руками схему прода никто не правит).

    `payload` — JSONB, а не текст: это позволяет при разборе инцидента
    посмотреть план прямо запросом (`payload->>'tool'`), не поднимая Python.
    Индекс по (server, expires_at) обслуживает уборку просроченных."""
    with _conn() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_manifests (
              server      TEXT   NOT NULL,
              manifest_id TEXT   NOT NULL,
              tool        TEXT,
              payload     JSONB  NOT NULL,
              created_at  BIGINT NOT NULL,
              expires_at  BIGINT NOT NULL,
              consumed_at BIGINT,
              PRIMARY KEY (server, manifest_id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS mcp_manifests_cleanup_idx "
            "ON mcp_manifests (server, expires_at)"
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _dumps(payload: Dict[str, Any]) -> str:
    """Сериализация плана. Сначала СТРОГО: если в плане завёлся объект, который
    JSON не берёт, это надо ЗАМЕТИТЬ, а не тихо превратить в строку — иначе
    после рестарта план восстановится изувеченным и исполнится не так, как его
    показали человеку. Запасной проход с `default=str` оставлен только чтобы не
    терять план целиком из-за одного экзотического поля, и он всегда пишет
    предупреждение в лог."""
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.warning(f"manifest_store: план содержит несериализуемое поле "
                       f"({e}) — сохраняю с приведением к строке")
        return json.dumps(payload, ensure_ascii=False, default=str)


def save(manifest_id: str, tool: str, payload: Dict[str, Any],
         created_at_ms: int, expires_at_ms: int) -> bool:
    """Кладёт план в базу. Возвращает True, если строка на месте.

    `ON CONFLICT DO NOTHING`, а НЕ `DO UPDATE` — и это не мелочь. Совпадение
    первичного ключа означает, что под этим id уже лежит ЧУЖОЙ план (id
    выдаётся `uuid4().hex[:12]` на каждый план заново, повторный `save` того
    же плана этим путём невозможен). Затереть чужую строку своим содержимым —
    худший исход из всех: владелец подтвердит кнопкой один план, а исполнится
    другой. Поэтому при коллизии мы НИЧЕГО не пишем и честно возвращаем False
    («долговечным этот план не стал»), а не делаем вид, что всё хорошо.

    Ошибку НЕ бросаем, а возвращаем False: не сохранившийся план — это ровно
    то поведение, что было до этой задачи (живёт в памяти процесса, рестарта
    не переживёт). Ронять из-за этого показ плана человеку несоразмерно."""
    if not store_ready():
        return False
    body = _dumps(payload)
    if len(body.encode("utf-8")) > _PAYLOAD_MAX_BYTES:
        # Долговечность — не любой ценой. `attach_file_to_task` кладёт в план
        # `content_base64` целиком (до 20 МБ), и такой план поехал бы в JSONB
        # как есть, а потом ещё и читался бы оттуда на каждом проходе поллера.
        # Отказ означает ровно прежнее поведение для этого одного плана: живёт
        # в памяти процесса, рестарта не переживёт.
        logger.warning(f"manifest_store: план {manifest_id} слишком велик "
                       f"({len(body)} байт > {_PAYLOAD_MAX_BYTES}) — "
                       "долговечным не делаю, остаётся только в памяти процесса")
        return False
    try:
        with _conn() as cur:
            cur.execute(
                "INSERT INTO mcp_manifests "
                "(server, manifest_id, tool, payload, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (server, manifest_id) DO NOTHING",
                (SERVER, manifest_id, tool or None, body,
                 int(created_at_ms), int(expires_at_ms)),
            )
            written = cur.rowcount > 0
        if not written:
            logger.error(f"manifest_store: план {manifest_id} НЕ сохранён — под "
                         "этим id в базе уже есть строка (коллизия id); "
                         "перезаписывать чужой план нельзя")
        return written
    except Exception as e:  # noqa: BLE001
        logger.warning(f"manifest_store: не смог сохранить план {manifest_id}: {e}")
        return False


def _row_to_dict(row: tuple) -> Dict[str, Any]:
    return {"manifest_id": row[0], "tool": row[1], "payload": row[2] or {},
            "created_at": row[3], "expires_at": row[4], "consumed_at": row[5]}


def load(manifest_id: str) -> Optional[Dict[str, Any]]:
    """Читает план по id. None — «такого плана в базе нет».

    Возвращает строку КАК ЕСТЬ, включая израсходованные и просроченные:
    решение, что с ней делать, принимает server.py (ему нужно отличать «плана
    не было» от «план уже исполнен», чтобы ответить модели по-разному)."""
    if not store_ready():
        return None
    try:
        with _conn() as cur:
            cur.execute(
                "SELECT manifest_id, tool, payload, created_at, expires_at, "
                "consumed_at FROM mcp_manifests "
                "WHERE server = %s AND manifest_id = %s",
                (SERVER, manifest_id),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"manifest_store: не смог прочитать план {manifest_id}: {e}")
        return None
    return _row_to_dict(row) if row else None


def load_live(manifest_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Пакетно — ТОЛЬКО живые планы (не израсходованные и не просроченные) из
    переданного списка id. ОДИН запрос на весь список: этим пользуется фоновый
    поллер, а он обязан укладываться в свой 10-секундный тик (см. тот же довод
    в `tg_approval.get_tg_approvals`)."""
    ids = [str(i) for i in manifest_ids]
    if not ids or not store_ready():
        return {}
    try:
        with _conn() as cur:
            cur.execute(
                "SELECT manifest_id, tool, payload, created_at, expires_at, "
                "consumed_at FROM mcp_manifests "
                "WHERE server = %s AND manifest_id = ANY(%s) "
                "AND consumed_at IS NULL AND expires_at > %s",
                (SERVER, ids, _now_ms()),
            )
            rows: List[tuple] = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"manifest_store: пакетное чтение планов не удалось: {e}")
        return {}
    return {r[0]: _row_to_dict(r) for r in rows}


def list_live(tool: str = "", window_ms: int = 0) -> List[Dict[str, Any]]:
    """ВСЕ живые планы сервера за окно времени — в отличие от `load_live`
    выше, которому список id надо знать заранее (1.3.3/изм-9, дизайн раздел 8,
    рубеж 2).

    Зачем это вообще нужно. Живой случай: владелец подтвердил ДВА похожих
    манифеста подряд и получил пять задач дважды. Защита «один манифест — одно
    исполнение» такого не ловит: манифестов было два, каждый исполнился ровно
    один раз. Единственное место, где второй план можно было увидеть ДО
    подтверждения, — перечень ещё не исполненных чужих планов, а получить его
    было нечем: `load_live` принимает готовые идентификаторы, а тот, кто
    строит новый план, идентификаторов чужих планов не знает по определению.

    ОДИН запрос с тем же условием живости, что у `load_live`
    (`consumed_at IS NULL AND expires_at > now`), и тем же способом, каким она
    бережёт десятисекундный тик поллера.

    `tool` — сузить до планов одного инструмента (пусто = любые).
    `window_ms` — не старше стольких миллисекунд по `created_at` (0 = без
    ограничения снизу; сверху и так режет `expires_at`)."""
    if not store_ready():
        return []
    now = _now_ms()
    sql = ("SELECT manifest_id, tool, payload, created_at, expires_at, "
           "consumed_at FROM mcp_manifests "
           "WHERE server = %s AND consumed_at IS NULL AND expires_at > %s")
    args: List[Any] = [SERVER, now]
    if tool:
        sql += " AND tool = %s"
        args.append(tool)
    if window_ms:
        sql += " AND created_at >= %s"
        args.append(now - int(window_ms))
    sql += " ORDER BY created_at"
    try:
        with _conn() as cur:
            cur.execute(sql, tuple(args))
            rows: List[tuple] = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"manifest_store: перечень живых планов не удался: {e}")
        return []
    return [_row_to_dict(r) for r in rows]


# Исходы попытки захвата. Три, а не два, потому что «этой строки в базе нет»
# и «строка есть, но её уже забрали» требуют РАЗНОГО поведения у вызывающего:
# в первом случае план виден только текущему процессу и одноразовость обязана
# решаться по памяти (как до этой задачи), во втором — уже решена, и трогать
# ничего нельзя.
CLAIM_WON = "claimed"
CLAIM_TAKEN = "taken"
CLAIM_ABSENT = "absent"


def claim(manifest_id: str, *, now_ms: Optional[int] = None
          ) -> Tuple[str, Optional[Dict[str, Any]]]:
    """АТОМАРНО занимает право исполнить план — САМОЕ ОПАСНОЕ МЕСТО всей этой
    машинерии, поэтому разберём построчно.

    Возвращает пару (исход, план):
      * (CLAIM_WON,    payload) — заняли МЫ, исполнять нам;
      * (CLAIM_TAKEN,  None)    — строка есть, но её занял кто-то другой
                                  (соседний тик поллера, вторая реплика,
                                  старый контейнер во время выкатки);
      * (CLAIM_ABSENT, None)    — строки в базе нет вовсе (план старше этой
                                  фичи, база отключена или сохранить его не
                                  удалось) — решение остаётся за памятью
                                  процесса.

    Почему ОДИН запрос, а не «SELECT → проверил → UPDATE». Между чтением и
    записью в двух разных запросах помещается второй такой же захват: оба
    прочитают `consumed_at IS NULL`, оба решат «мой» и операция выполнится
    ДВАЖДЫ. Для удаления задач это второе безвозвратное удаление. Здесь же
    проверка и установка — одно и то же выражение: `UPDATE … WHERE consumed_at
    IS NULL`. Postgres берёт на строку эксклюзивную блокировку на время
    UPDATE, поэтому второй захват физически ждёт коммита первого и на
    перечитанной версии строки видит `consumed_at` уже проставленным — его
    WHERE не совпадает, `RETURNING` не отдаёт ничего. Так ведёт себя обычный
    READ COMMITTED, никаких особых уровней изоляции не требуется.

    Ветка `EXISTS` живёт в ТОМ ЖЕ операторе (CTE), а не отдельным SELECT'ом —
    иначе между «UPDATE ничего не вернул» и «а есть ли вообще строка» снова
    появился бы зазор, и вдобавок это была бы вторая поездка в базу на каждый
    промах. Внутри одного оператора обе ветки видят один снимок.

    СРОК ЖИЗНИ ПРОВЕРЯЕТСЯ ЗДЕСЬ ЖЕ (`expires_at > now`), а не отдельным
    чтением до захвата. Разнесённые проверка и захват означали бы, что план,
    истёкший между ними, всё-таки будет исполнен: одна и та же величина `now`
    идёт и в сравнение TTL, и в записываемый `consumed_at`. Просроченный план
    даёт исход CLAIM_TAKEN («трогать нечего»), а не CLAIM_ABSENT: строка-то
    есть, и падать на память процесса, которая про TTL уже забыла, нельзя."""
    if not store_ready():
        return CLAIM_ABSENT, None
    stamp = _now_ms() if now_ms is None else int(now_ms)
    try:
        with _conn() as cur:
            cur.execute(
                """
                WITH claimed AS (
                    UPDATE mcp_manifests SET consumed_at = %(now)s
                     WHERE server = %(srv)s AND manifest_id = %(mid)s
                       AND consumed_at IS NULL AND expires_at > %(now)s
                    RETURNING payload
                )
                SELECT (SELECT payload FROM claimed),
                       EXISTS (SELECT 1 FROM claimed),
                       EXISTS (SELECT 1 FROM mcp_manifests
                                WHERE server = %(srv)s AND manifest_id = %(mid)s)
                """,
                {"now": stamp, "srv": SERVER, "mid": manifest_id},
            )
            payload, won, exists = cur.fetchone()
    except Exception as e:  # noqa: BLE001 — не смогли занять = НЕ исполняем.
        # Fail-closed именно в эту сторону: пропущенное исполнение владелец
        # увидит (кнопка не сработала, отчёта нет) и повторит, а лишнее
        # исполнение необратимо.
        logger.warning(f"manifest_store: захват плана {manifest_id} не удался: {e}")
        return CLAIM_TAKEN, None
    if won:
        return CLAIM_WON, dict(payload or {})
    return (CLAIM_TAKEN if exists else CLAIM_ABSENT), None


def mark_consumed(manifest_id: str) -> bool:
    """Гасит план, когда его израсходовал НЕ кнопочный путь: обычное
    исполнение по чат-«да», явный отказ пользователя, истёкший TTL, откат
    неудачной отправки в Telegram. Тот же атомарный `UPDATE … WHERE
    consumed_at IS NULL`, просто результат никому не нужен.

    True = погасили именно мы. False = гасить было нечего (строки нет, база
    отключена или план уже израсходован) — это НЕ ошибка."""
    if not store_ready():
        return False
    try:
        with _conn() as cur:
            cur.execute(
                "UPDATE mcp_manifests SET consumed_at = %s "
                "WHERE server = %s AND manifest_id = %s AND consumed_at IS NULL",
                (_now_ms(), SERVER, manifest_id),
            )
            return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"manifest_store: не смог погасить план {manifest_id}: {e}")
        return False


def delete(manifest_id: str) -> None:
    """Убирает строку целиком. Нужна ровно там же, где `tg_approval.
    delete_tg_approval`, — откат наполовину состоявшейся отправки плана:
    строку вставили, а сообщение в Telegram уйти не смогло. Best-effort:
    оставшаяся строка всё равно умрёт по TTL."""
    if not store_ready():
        return
    try:
        with _conn() as cur:
            cur.execute(
                "DELETE FROM mcp_manifests WHERE server = %s AND manifest_id = %s",
                (SERVER, manifest_id),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"manifest_store: не смог удалить план {manifest_id}: {e}")


def purge_expired(grace_ms: int = 0) -> int:
    """Уборка просроченных планов — иначе таблица растёт вечно.

    `grace_ms` — насколько ПЕРЕЖИВАЮТ свой `expires_at` строки, прежде чем их
    снести. Ноль здесь был бы ошибкой: разбор «подтверждено, но исполнять
    нечего» смотрит на строки `tg_approvals` ещё час после их истечения
    (`_TG_LOST_LOOKBACK_S`), и если план к этому моменту уже стёрт, владелец
    получит «неизвестная операция» вместо внятного объяснения. Поэтому
    server.py передаёт сюда тот же час.

    Возвращает число снесённых строк."""
    if not store_ready():
        return 0
    try:
        with _conn() as cur:
            cur.execute(
                "DELETE FROM mcp_manifests "
                "WHERE server = %s AND expires_at < %s",
                (SERVER, _now_ms() - int(grace_ms)),
            )
            return cur.rowcount or 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"manifest_store: уборка просроченных планов не удалась: {e}")
        return 0
