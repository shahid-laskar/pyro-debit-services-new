import logging
from contextlib import contextmanager
from typing import Generator, Optional
import functools
import psycopg2
import psycopg2.pool

from app.config import settings

logger = logging.getLogger(__name__)

def _pg_retry(fn):
   
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except psycopg2.OperationalError as exc:
            logger.warning(
                "Postgres: %s failed with OperationalError (%s) — retrying once",
                fn.__name__, exc,
            )
            return fn(*args, **kwargs)
    return wrapper

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def init_pg_pool() -> None:
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=settings.pg_min_conn,
        maxconn=settings.pg_max_conn,
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    logger.info("Postgres pool initialised (min=%d max=%d)",
                settings.pg_min_conn, settings.pg_max_conn)


def close_pg_pool() -> None:
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("Postgres pool closed")


def is_pg_pool_ready() -> bool:
    return _pool is not None


@contextmanager
def get_pg_conn() -> Generator:
    conn = _pool.getconn()
   
    if conn.closed:
        logger.warning("Postgres: stale connection detected on checkout — replacing")
        _pool.putconn(conn, close=True)
        conn = _pool.getconn()
    discard = False
    try:
        yield conn
        conn.commit()
    except Exception as exc:
        discard = isinstance(exc, psycopg2.OperationalError) or bool(conn.closed)
        try:
            conn.rollback()
        except Exception:
            discard = True  # rollback itself failed — connection is unusable
            logger.warning("Postgres: rollback failed — discarding connection")
        raise
    finally:
        _pool.putconn(conn, close=discard)
        if discard:
            logger.warning("Postgres: broken connection discarded from pool")