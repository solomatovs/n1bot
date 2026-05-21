"""PostgresPool: тонкая обёртка над psycopg_pool.ConnectionPool + process-singleton."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from boba.db.postgres.config import PostgresConfig
from boba.db.postgres.errors import PostgresPoolClosedError

if TYPE_CHECKING:
    import psycopg

__all__ = ["PostgresPool"]

logger = logging.getLogger(__name__)


ConfigureConnection = Callable[["psycopg.Connection[Any]"], None]
"""Hook на каждое новое соединение pool'а (pgvector/hstore type registration)."""


class PostgresPool:
    """Read-only-обёртка над `psycopg_pool.ConnectionPool` с process-singleton'ом.

    Контракт read-only задаётся параметрами DSN
    (`default_transaction_read_only=on`, `statement_timeout=...`); pool сам
    ничего read-only не выставляет — это держит инфра-слой dumb.

    Опциональный `configure`-callback вызывается psycopg_pool'ом на каждое
    свежее соединение (type-codec registration: `pgvector.psycopg.register_vector`,
    hstore и т.п.). Идентичность кэширования по cfg НЕ учитывает callback —
    разные tool-пакеты с одинаковым DSN получают один и тот же pool. Если
    нужны разные configure-hook'и на одном DSN, разводите их через разные
    DSN (например `?application_name=...`).
    """

    _CacheKey = tuple[str, int, int, float]
    _CACHE: ClassVar[dict[_CacheKey, PostgresPool]] = {}

    def __init__(
        self,
        cfg: PostgresConfig,
        *,
        configure: ConfigureConnection | None = None,
    ) -> None:
        from psycopg_pool import ConnectionPool  # noqa: PLC0415

        self._cfg = cfg
        self._pool = ConnectionPool(
            conninfo=cfg.dsn,
            min_size=cfg.min_size,
            max_size=cfg.max_size,
            timeout=cfg.connect_timeout_sec,
            configure=configure,
            open=True,
        )
        self._closed = False
        logger.info(
            "PostgresPool opened min_size=%d max_size=%d configure=%s",
            cfg.min_size,
            cfg.max_size,
            "yes" if configure is not None else "no",
        )

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """Взять connection из pool'а; auto-return по выходу из контекста."""
        if self._closed:
            raise PostgresPoolClosedError("PostgresPool is closed")
        with self._pool.connection() as conn:
            yield conn

    def close(self) -> None:
        """Закрыть pool. Идемпотентно."""
        if self._closed:
            return
        self._closed = True
        self._pool.close()
        logger.info("PostgresPool closed")

    @classmethod
    def get(
        cls,
        cfg: PostgresConfig,
        *,
        configure: ConfigureConnection | None = None,
    ) -> PostgresPool:
        """Process-singleton по полному состоянию `cfg`; пересоздаёт закрытый.

        `configure` применяется при первом создании pool'а для данного cfg;
        при повторном `.get(...)` с тем же DSN значение игнорируется
        (cache-hit). См. docstring класса.
        """
        key: PostgresPool._CacheKey = (
            cfg.dsn,
            cfg.min_size,
            cfg.max_size,
            cfg.connect_timeout_sec,
        )
        pool = cls._CACHE.get(key)
        if pool is None or pool._closed:
            pool = cls(cfg, configure=configure)
            cls._CACHE[key] = pool
        return pool
