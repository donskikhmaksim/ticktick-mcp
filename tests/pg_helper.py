"""Настоящий Postgres для тестов долговечных планов (#91) — и ничего кроме.

ПОЧЕМУ ЗДЕСЬ НЕТ ДВОЙНИКА БАЗЫ. Главное, что проверяют эти тесты, —
одноразовость плана при ОДНОВРЕМЕННЫХ попытках его занять. Она целиком стоит
на поведении настоящего движка: `UPDATE … WHERE consumed_at IS NULL` берёт на
строку блокировку, и второй претендент ЖДЁТ коммита первого, а потом видит уже
изменённую строку. Любой самодельный двойник (словарь под локом, sqlite,
«фейковый курсор») исполняет запросы по очереди сам по себе — и тест на гонку
в нём зелёный ВСЕГДА, даже если атомарности в коде нет вовсе. В этом
репозитории уже был случай, когда добрый тестовый двойник годами прятал
дефект; повторять его на самом опасном месте механизма нельзя.

БАЗА — СВОЯ НА КАЖДЫЙ ПРОГОН (2026-08-07, починка флейка)
──────────────────────────────────────────────────────────
Раньше все прогоны ходили в ОДНУ базу `ticktick_mcp_test`, а `fresh_stores()`
готовила её к тесту через `DELETE FROM …`. Пока прогон один, это работает;
как только рядом идёт второй (несколько агентов, фоновый прогон в том же
терминале, `pytest` в соседнем worktree), два процесса чистят и наполняют одни
и те же две таблицы — и тесты падают там, где ничего не сломано. Замер: два
одновременных `pytest tests/test_manifest_restart.py
tests/test_automation_key_first_call.py` на общей базе — 20 падений из 20
процессов; те же прогоны на разных базах — 0 из 30. Красный цвет при таком
устройстве не значит ничего, а именно на нём держится проверка тестов
мутациями.

Поэтому КАЖДЫЙ прогон pytest заводит СЕБЕ отдельную базу
`ticktick_mcp_test_run<pid>_<rand>` и сносит её на выходе (`atexit`). Схема в
ней создаётся тем же кодом миграции, что и в проде (`init_store` →
`_ensure_schema`), так что проверяется она по-прежнему. `DELETE` в
`fresh_store`/`fresh_stores` остаются — они разделяют СОСЕДНИЕ ТЕСТЫ одного
прогона, а от соседнего ПРОЦЕССА теперь защищает сама база.

Где берётся сервер:
  1. `TICKTICK_TEST_PG_DSN` — как в CI (там поднимается сервис postgres:16);
  2. иначе локальный Postgres по unix-сокету (у разработчика он обычно уже
     стоит через brew);
  3. если ни то ни другое не отвечает — тесты пропускаются с внятной причиной,
     а не падают.
Имя базы из DSN при этом используется только как «куда подключаться, чтобы
проверить, что сервер жив»: сами данные всегда уезжают в базу этого прогона.
"""
import atexit
import os
import sys
import uuid

import pytest

_DEFAULT_DB = "ticktick_mcp_test"

# Префикс баз-однодневок. В имени стоит pid прогона — по нему уборка отличает
# базу живого соседа от сироты, оставшейся после убитого прогона.
_RUN_DB_PREFIX = "ticktick_mcp_test_run"


def _candidate_dsns() -> list:
    env = os.environ.get("TICKTICK_TEST_PG_DSN", "").strip()
    if env:
        return [env]
    out = []
    user = os.environ.get("USER") or "postgres"
    for sock_dir in ("/tmp", "/var/run/postgresql"):
        if os.path.isdir(sock_dir):
            out.append(f"postgresql://{user}@/{_DEFAULT_DB}"
                       f"?host={sock_dir}&sslmode=disable")
    out.append(f"postgresql://postgres:postgres@127.0.0.1:5432/{_DEFAULT_DB}"
               "?sslmode=disable")
    return out


def _with_db(dsn: str, dbname: str) -> str:
    """Тот же DSN, но на другую базу — через разбор libpq, а не подстроку:
    имя базы встречается в строке подключения не только в своей позиции."""
    from psycopg2.extensions import make_dsn, parse_dsn

    parts = parse_dsn(dsn)
    parts["dbname"] = dbname
    return make_dsn(**parts)


def _admin_conn(dsn: str):
    """Соединение со служебной базой `postgres` того же сервера.

    `CREATE DATABASE`/`DROP DATABASE` не работают внутри транзакции, отсюда
    autocommit."""
    import psycopg2

    conn = psycopg2.connect(_with_db(dsn, "postgres"), connect_timeout=3)
    conn.autocommit = True
    return conn


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True     # чужой процесс — значит живой
    except OSError:
        return True     # не смогли выяснить — считаем живым, сносить нельзя
    return True


def _drop_db(conn, name: str) -> bool:
    """Снести базу. Сначала с `FORCE` (PG13+) — иначе одно забытое соединение
    оставляет базу навсегда; на старом сервере откатываемся на обычный DROP."""
    for sql in (f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
                f'DROP DATABASE IF EXISTS "{name}"'):
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            return True
        except Exception:  # noqa: BLE001 — уборка best-effort
            continue
    return False


def _drop_orphans(conn) -> None:
    """Базы прошлых прогонов, чьи процессы уже мертвы (Ctrl-C, kill, падение
    интерпретатора — `atexit` в таких случаях не отрабатывает). Без этой уборки
    сервер медленно зарастал бы базами-однодневками."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT datname FROM pg_database WHERE datname LIKE %s",
                        (_RUN_DB_PREFIX + "%",))
            names = [r[0] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return
    for name in names:
        tail = name[len(_RUN_DB_PREFIX):].split("_")[0]
        if not tail.isdigit() or _pid_alive(int(tail)):
            continue
        _drop_db(conn, name)


def _close_pools() -> None:
    """Закрыть пулы обоих хранилищ перед сносом базы.

    Нужно не только для DROP: `tg_approval.init_store` пул не закрывает, а
    `fresh_stores` до этой правки просто обнуляла ссылку — соединения жили до
    сборки мусора."""
    try:
        from ticktick_mcp.src import manifest_store, tg_approval
    except Exception:  # noqa: BLE001 — сервер мог и не импортироваться
        return
    try:
        manifest_store.close_store()
    except Exception:  # noqa: BLE001
        pass
    pool = getattr(tg_approval, "_pg_pool", None)
    if pool is not None:
        try:
            pool.closeall()
        except Exception:  # noqa: BLE001
            pass
    tg_approval._pg_pool = None


def _cleanup_run_db(dsn: str, name: str) -> None:
    _close_pools()
    try:
        conn = _admin_conn(dsn)
    except Exception:  # noqa: BLE001 — сервер уже мог уехать
        return
    try:
        _drop_db(conn, name)
    finally:
        conn.close()


def _make_run_db(dsn: str) -> str:
    """Своя база для ЭТОГО прогона; возвращает DSN на неё."""
    name = f"{_RUN_DB_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:6]}"
    conn = _admin_conn(dsn)
    try:
        _drop_orphans(conn)
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    atexit.register(_cleanup_run_db, dsn, name)
    return _with_db(dsn, name)


def _shared_db_fallback(dsn: str):
    """Крайний случай: своей базы завести не дали (нет прав на CREATE
    DATABASE). Тогда работаем в общей — ровно как раньше, — но ГРОМКО об этом
    говорим: молча вернувшийся флейк хуже, чем шумный прогон."""
    import psycopg2

    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.close()
    except psycopg2.Error:
        return None
    print(f"ВНИМАНИЕ: не удалось завести отдельную базу для этого прогона — "
          f"тесты идут в общую ({dsn.split('@')[-1]}). Параллельный прогон "
          f"pytest будет ронять тесты долговечных планов.", file=sys.stderr)
    return dsn


_resolved = None
_resolved_done = False


def pg_dsn():
    """DSN живого Postgres или None (тогда тест пропускается)."""
    global _resolved, _resolved_done
    if _resolved_done:
        return _resolved
    _resolved_done = True
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return None
    for dsn in _candidate_dsns():
        try:
            _resolved = _make_run_db(dsn)
            break
        except Exception:  # noqa: BLE001 — сервера нет или прав не хватает
            fallback = _shared_db_fallback(dsn)
            if fallback:
                _resolved = fallback
                break
    return _resolved


requires_pg = pytest.mark.skipif(
    pg_dsn() is None,
    reason="нужен настоящий Postgres: задайте TICKTICK_TEST_PG_DSN "
           "(в CI это делает сервис postgres) или поднимите локальный")


def fresh_stores():
    """Оба хранилища на настоящей базе: планы (#91) И строки подтверждений.

    Тест перезапуска работает с обеими таблицами сразу — план без строки
    подтверждения некому одобрить, — поэтому поднимаются они вместе, тем же
    кодом миграции, что и в проде."""
    from ticktick_mcp.src import tg_approval

    store = fresh_store()
    # Пул закрываем, а не просто теряем ссылку: `tg_approval.init_store`
    # своего `close_store` не имеет, и брошенный пул держал соединения до
    # сборки мусора — за один файл тестов их набегали десятки.
    pool = getattr(tg_approval, "_pg_pool", None)
    if pool is not None:
        try:
            pool.closeall()
        except Exception:  # noqa: BLE001
            pass
    tg_approval._pg_pool = None
    tg_approval.init_store(pg_dsn())
    with store._conn() as cur:
        cur.execute("DELETE FROM tg_approvals WHERE server = 'ticktick'")
    return store


def fresh_store(monkeypatch=None):
    """Чистое хранилище на настоящей базе: пул поднят, таблица пуста.

    Пустая таблица, а не пустая база: `mcp_manifests` создаёт та же миграция,
    что и в проде (`init_store` → `_ensure_schema`), — проверяется заодно и
    она."""
    from ticktick_mcp.src import manifest_store

    manifest_store.close_store()
    manifest_store.init_store(pg_dsn())
    with manifest_store._conn() as cur:
        cur.execute("DELETE FROM mcp_manifests")
    return manifest_store
