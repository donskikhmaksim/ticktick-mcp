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

Где берётся база:
  1. `TICKTICK_TEST_PG_DSN` — как в CI (там поднимается сервис postgres:16);
  2. иначе локальный Postgres по unix-сокету (у разработчика он обычно уже
     стоит через brew) — база `ticktick_mcp_test` создаётся при первом запуске;
  3. если ни то ни другое не отвечает — тесты пропускаются с внятной причиной,
     а не падают.
"""
import os
import socket

import pytest

_DEFAULT_DB = "ticktick_mcp_test"


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


def _try_dsn(dsn: str):
    """Возвращает рабочий DSN или None. База создаётся, если её ещё нет."""
    import psycopg2

    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.close()
        return dsn
    except psycopg2.OperationalError as e:
        if "does not exist" not in str(e):
            return None
    # Пробуем создать базу через служебную `postgres` на том же сервере.
    admin = dsn.replace(f"/{_DEFAULT_DB}", "/postgres")
    if admin == dsn:
        return None
    try:
        conn = psycopg2.connect(admin, connect_timeout=3)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{_DEFAULT_DB}"')
        conn.close()
        return dsn
    except Exception:  # noqa: BLE001
        return None


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
        socket.setdefaulttimeout(3)
        ok = _try_dsn(dsn)
        if ok:
            _resolved = ok
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
    from ticktick_mcp.src import manifest_store, tg_approval

    store = fresh_store()
    tg_approval._pg_pool = None
    tg_approval.init_store(pg_dsn())
    with manifest_store._conn() as cur:
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
