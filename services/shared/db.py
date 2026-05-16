"""Asyncpg pool management -- singleton pattern."""
import logging

import asyncpg

from .config import Config

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

POOL_MIN_SIZE = 2
POOL_MAX_SIZE = 10
COMMAND_TIMEOUT_SEC = 30


async def get_pool(config: Config | None = None) -> asyncpg.Pool:
    """Return singleton asyncpg pool, creating it on first call.

    Args:
        config: Service config. Required on first call, ignored after.

    Returns:
        Asyncpg connection pool.

    Raises:
        RuntimeError: If called without config before pool is initialized.
    """
    global _pool
    if _pool is not None:
        return _pool

    if config is None:
        raise RuntimeError(
            "get_pool() called without config before pool initialization"
        )

    dsn = config.get_pg_dsn()
    logger.info(
        "Creating asyncpg pool: host=%s db=%s user=%s min=%d max=%d",
        dsn.get("host"),
        dsn.get("database"),
        dsn.get("user"),
        POOL_MIN_SIZE,
        POOL_MAX_SIZE,
    )
    _pool = await asyncpg.create_pool(
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        command_timeout=COMMAND_TIMEOUT_SEC,
        **dsn,
    )
    return _pool


async def close_pool() -> None:
    """Close the singleton pool if it exists."""
    global _pool
    if _pool is not None:
        logger.info("Closing asyncpg pool")
        await _pool.close()
        _pool = None
