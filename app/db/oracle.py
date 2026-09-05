import logging
from contextlib import contextmanager
from typing import Generator, Optional

import oracledb as cx_Oracle

from app.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[cx_Oracle.SessionPool] = None


def init_oracle_pool() -> None:
    global _pool
    _pool = cx_Oracle.SessionPool(
        user=settings.oracle_user,
        password=settings.oracle_password,
        dsn=settings.oracle_dsn,
        min=1, max=5, increment=1,
        encoding="UTF-8",
    )
    logger.info("Oracle pool initialised (min=1 max=5)")


def close_oracle_pool() -> None:
    global _pool
    if _pool:
        _pool.close()
        _pool = None
        logger.info("Oracle pool closed")


def is_oracle_pool_ready() -> bool:
    return _pool is not None


@contextmanager
def get_oracle_conn() -> Generator[cx_Oracle.Connection, None, None]:
    conn = _pool.acquire()
    try:
        yield conn
    finally:
        _pool.release(conn)
