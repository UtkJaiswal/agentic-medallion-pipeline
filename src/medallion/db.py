"""Postgres access: a small connection-pool wrapper, transactional helpers and a migration runner."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from medallion.resilience.retry import RetryPolicy, TransientError, retry_call

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class Database:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 8) -> None:
        self.url = url
        self._pool = ConnectionPool(url, min_size=min_size, max_size=max_size, open=False,
                                    kwargs={"row_factory": dict_row, "autocommit": False})

    def open(self, wait_s: float = 60.0) -> Database:
        """Open the pool, retrying while the database comes up (e.g. right after `docker compose up`)."""
        def _try() -> None:
            try:
                self._pool.open(wait=True, timeout=5)
            except psycopg.OperationalError as exc:
                raise TransientError(str(exc)) from exc
            except Exception as exc:  # pool timeout
                raise TransientError(str(exc)) from exc

        attempts = max(2, int(wait_s // 5))
        retry_call(_try, RetryPolicy(max_attempts=attempts, base_delay_s=1, max_delay_s=5), label="db.open")
        return self

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """One unit of work: commits on success, rolls back on any exception."""
        with self._pool.connection() as conn, conn.transaction():
            yield conn


class LockNotAcquired(TransientError):
    pass


@contextmanager
def advisory_lock(conn: psycopg.Connection, name: str) -> Iterator[None]:
    """Session-level advisory lock so two pipeline runs can never interleave writes."""
    key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
    got = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,)).fetchone()["ok"]
    conn.commit()
    if not got:
        raise LockNotAcquired(f"lock '{name}' is held by another process")
    try:
        yield
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        conn.commit()


def migrate(db: Database, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending `NNN_name.sql` files in order. Applied files are checksummed: editing one after the
    fact is an error (write a new migration instead)."""
    applied: list[str] = []
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('medallion.migrate'))")
        conn.execute("""CREATE SCHEMA IF NOT EXISTS ops;
                        CREATE TABLE IF NOT EXISTS ops.schema_migrations (
                            version text PRIMARY KEY, checksum text NOT NULL,
                            applied_at timestamptz NOT NULL DEFAULT now())""")
        done = {r["version"]: r["checksum"] for r in conn.execute("SELECT * FROM ops.schema_migrations")}
        for path in sorted(directory.glob("*.sql")):
            sql = path.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if path.stem in done:
                if done[path.stem] != checksum:
                    raise RuntimeError(f"migration {path.name} was modified after being applied")
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO ops.schema_migrations (version, checksum) VALUES (%s, %s)",
                         (path.stem, checksum))
            applied.append(path.stem)
    if applied:
        log.info("db.migrated", extra={"applied": applied})
    return applied
