"""Cross-process mutual exclusion for debit batches and stuck cleanup.

The asyncio.Lock in processor.py and the in-memory ownership tracker only work
inside one process. With more than one worker/replica (or overlapping zone
containers), two batches could run for the same service, and one replica's stuck
cleanup could reset rows another replica is still debiting.

A PostgreSQL session-level advisory lock per service closes that gap: the debit
batch and the stuck cleanup of a service take the same lock, so at most one of
them runs at a time across every process that shares the database. A lock that is
busy is never waited for: the caller skips this run (the next scheduler tick tries
again). If PostgreSQL cannot be reached the caller also skips (fail closed).
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Tuple

import app.db.postgres as pg

logger = logging.getLogger(__name__)

LOCK_NAMESPACE = "pyro-debit"


def lock_key(service_type: str) -> str:
    return f"{LOCK_NAMESPACE}:{service_type.strip().upper()}"


def _try_acquire(key: str) -> Tuple[Optional[object], bool]:
    pool = pg._pool
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,))
            row = cur.fetchone()
        conn.commit()          # end the transaction; the session-level lock persists
        acquired = bool(row[0]) if row else False
    except Exception:
        pool.putconn(conn, close=True)
        raise
    if not acquired:
        pool.putconn(conn)
        return None, False
    return conn, True


def _release(conn, key: str) -> None:
    pool = pg._pool
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        conn.commit()
        pool.putconn(conn)
    except Exception as exc:
        # Closing the session releases every advisory lock it holds.
        logger.error("Advisory unlock failed for %s (%s) — closing the session", key, exc)
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass


@asynccontextmanager
async def service_lock(service_type: str, purpose: str) -> AsyncIterator[bool]:
    """Yield True if this process now holds the service's lock, False if it must skip."""
    key = lock_key(service_type)
    if not pg.is_pg_pool_ready():
        # Unit tests / startup without Postgres: nothing to coordinate with.
        logger.debug("Advisory lock %s not enforced (Postgres pool not initialised)", key)
        yield True
        return

    try:
        conn, acquired = await asyncio.to_thread(_try_acquire, key)
    except Exception as exc:
        logger.error("[%s] %s skipped: cannot take advisory lock %s (%s: %s)",
                     service_type, purpose, key, type(exc).__name__, exc)
        yield False
        return

    if not acquired:
        logger.warning("[%s] %s skipped: advisory lock %s is held by another worker/replica",
                       service_type, purpose, key)
        yield False
        return

    try:
        yield True
    finally:
        await asyncio.to_thread(_release, conn, key)


async def locked_reset_stuck(adapter, stuck_minutes: int, context=None) -> int:
    """Run adapter.reset_stuck_processing under the service's advisory lock.

    Returns 0 (nothing reset) when another worker holds the lock.
    """
    async with service_lock(adapter.service_type, "stuck cleanup") as got:
        if not got:
            return 0
        return await asyncio.to_thread(
            adapter.reset_stuck_processing, stuck_minutes, context=context,
        )
