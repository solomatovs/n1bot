"""AsyncPostgresPool: async-обёртка над psycopg_pool.AsyncConnectionPool.

Ошибки: PostgresPoolClosedError — обращение к закрытому пулу;
KeytabError/KerberosError — соединению не выдан TGT из keytab.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, ClassVar

import psycopg
from psycopg.rows import DictRow, dict_row

from boba.cancellation import current_cancellation
from boba.db.postgres.profile import PostgresConfig
from boba.kerberos import KerberosAuthBase
from boba.krb import ClientCredentials, KerberosCredentials

__all__ = [
    "AsyncPostgresPool",
    "CancellablePool",
    "KerberosConnection",
    "PostgresError",
    "PostgresPoolClosedError",
    "PostgresPoolLoopError",
]


class PostgresError(Exception):
    """Базовая ошибка PG-инфры (pool/connection/timeout)."""


class PostgresPoolClosedError(PostgresError):
    """Попытка взять connection из уже закрытого pool'а."""


class PostgresPoolLoopError(PostgresError):
    """Обращение к пулу из event loop, отличного от того, в котором он открыт."""


logger = logging.getLogger(__name__)


class KerberosConnection(psycopg.AsyncConnection[Any]):
    """Соединение, само получающее TGT из своего keytab перед подключением.

    libpq не принимает keytab параметром и читает KRB5*-переменные процесса на
    каждом connect, поэтому окружение подставляется под процессным локом
    KerberosEnv на всё время установления соединения — GSSAPI-обмен идёт внутри
    connect, а не после него.
    """

    credentials: ClassVar[KerberosCredentials | None] = None

    @classmethod
    def bound_to(cls, credentials: KerberosCredentials) -> type[KerberosConnection]:
        """Подтип, привязанный к кредам одного пула."""
        name = f"{cls.__name__}[{credentials.principal}]"
        return type(name, (cls,), {"credentials": credentials})

    @classmethod
    async def connect(cls, conninfo: str = "", **kwargs: Any) -> KerberosConnection:
        if cls.credentials is None:
            msg = "KerberosConnection is not bound to credentials (use bound_to)"
            raise PostgresPoolClosedError(msg)

        async with cls.credentials.applied_async():
            return await super().connect(conninfo, **kwargs)  # type: ignore[return-value]


class AsyncPostgresPool:
    """
    Единственная точка работы с postgres: async-пул поверх AsyncConnectionPool
    """

    Configure = Callable[[psycopg.AsyncConnection[Any]], Awaitable[None]]
    """Hook на каждое новое соединение пула (регистрация типов pgvector/hstore)."""

    _CacheKey = tuple[str, tuple[tuple[str, str], ...]]
    _CACHE: ClassVar[dict[_CacheKey, AsyncPostgresPool]] = {}
    _CACHE_LOCK: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(
        self,
        cfg: PostgresConfig,
        *,
        override_options: dict[str, str] | None = None,
        configure: Configure | None = None,
    ) -> None:
        from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

        self._cfg = cfg
        self._pool = AsyncConnectionPool(
            connection_class=self._connection_class(cfg),
            kwargs=cfg.conn_settings(override_options),
            **cfg.pool_settings(),
            configure=configure,
            open=False,
        )
        self._closed = False
        self._loop_id: int | None = None
        self._loop_reported = False

        logger.info(
            "AsyncPostgresPool created db=%s auth=%s min_size=%d max_size=%s",
            cfg.dbname,
            cfg.auth.method,
            cfg.pool.min_size,
            cfg.pool.max_size,
        )

    @staticmethod
    def _connection_class(cfg: PostgresConfig) -> type[psycopg.AsyncConnection[Any]]:
        """Соединение с собственным TGT, если авторизация kerberos."""
        if not isinstance(cfg.auth, KerberosAuthBase):
            return psycopg.AsyncConnection

        return KerberosConnection.bound_to(ClientCredentials.of(cfg.auth))

    async def open(self) -> None:
        """Открыть пул (установить фоновые соединения)."""
        self._loop_id = id(asyncio.get_running_loop())
        logger.info(
            "AsyncPostgresPool open db=%s auth=%s loop=%#x",
            self._cfg.dbname,
            self._cfg.auth.method,
            self._loop_id,
        )
        await self._pool.open()

    def _check_loop(self, op: str) -> None:
        """Свериться с loop'ом, в котором пул открыт.

        Внутренние asyncio-локи psycopg_pool привязываются к тому loop'у, где их
        впервые дождались; из другого loop они бросают невнятное
        `RuntimeError: ... is bound to a different event loop`, и пул остаётся
        нерабочим до конца жизни процесса. Ловим это на своей границе, чтобы в
        логе были оба loop'а и операция, а не только стек psycopg.
        """
        if self._loop_id is None:
            return

        current = id(asyncio.get_running_loop())
        if current == self._loop_id:
            return

        # наверху вызов повторяется бесконечно — полный стек пишем один раз
        if not self._loop_reported:
            self._loop_reported = True
            logger.error(
                "AsyncPostgresPool loop mismatch db=%s op=%s opened_loop=%#x "
                "current_loop=%#x — пул открыт в другом event loop; ищите второй "
                "loop (asyncio.run в потоке, свой loop у фоновой задачи, "
                "пересоздание loop'а сервером)",
                self._cfg.dbname,
                op,
                self._loop_id,
                current,
                stack_info=True,
            )
        else:
            logger.debug(
                "AsyncPostgresPool loop mismatch db=%s op=%s opened_loop=%#x "
                "current_loop=%#x",
                self._cfg.dbname,
                op,
                self._loop_id,
                current,
            )

        raise PostgresPoolLoopError(
            f"pool for {self._cfg.dbname} is bound to event loop {self._loop_id:#x}, "
            f"called from {current:#x} ({op})"
        )

    @classmethod
    async def get(
        cls,
        cfg: PostgresConfig,
        *,
        override_options: dict[str, str] | None = None,
        configure: Configure | None = None,
    ) -> AsyncPostgresPool:
        """Открытый пул-singleton по cfg + override_options; закрытый пересоздаётся.

        configure применяется при создании, при повторном get игнорируется.
        """
        key = cls._cache_key(cfg, override_options)

        async with cls._CACHE_LOCK:
            pool = cls._CACHE.get(key)
            if pool is not None and not pool._closed:
                # кэш процессный и переживает любой loop: тут видно момент, когда
                # пул уезжает в чужой loop — дальше он уже нерабочий навсегда
                current = id(asyncio.get_running_loop())
                if pool._loop_id is not None and pool._loop_id != current:
                    logger.error(
                        "AsyncPostgresPool cache hit from another loop db=%s "
                        "opened_loop=%#x current_loop=%#x",
                        cfg.dbname,
                        pool._loop_id,
                        current,
                        stack_info=True,
                    )
                return pool

            pool = cls(cfg, override_options=override_options, configure=configure)
            await pool.open()
            cls._CACHE[key] = pool
            return pool

    @classmethod
    async def close_all(cls) -> None:
        """Закрывает и забывает все singleton-пулы процесса."""
        async with cls._CACHE_LOCK:
            pools = list(cls._CACHE.values())
            cls._CACHE.clear()

        for pool in pools:
            await pool.close()

    @staticmethod
    def _cache_key(
        cfg: PostgresConfig,
        override_options: dict[str, str] | None,
    ) -> _CacheKey:
        settings = json.dumps(
            {**cfg.conn_settings(), **cfg.pool_settings()},
            sort_keys=True,
            default=str,
        )
        return settings, tuple(sorted((override_options or {}).items()))

    @classmethod
    async def dedicated(cls, cfg: PostgresConfig) -> psycopg.AsyncConnection[Any]:
        """Отдельное соединение вне пула в autocommit: для LISTEN нужно соединение
        без транзакций, которое никто не забирает под запросы."""
        connection_class = cls._connection_class(cfg)
        settings = cfg.conn_settings(None)
        settings["autocommit"] = True
        return await connection_class.connect(**settings)

    @property
    def raw(self) -> Any:
        """Внутренний psycopg_pool.AsyncConnectionPool (для langgraph-саверов)."""
        return self._pool

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
        """Взять AsyncConnection из пула."""
        if self._closed:
            raise PostgresPoolClosedError("PostgresPool is closed")

        self._check_loop("connection")

        async with self._pool.connection() as conn:
            yield conn

    @asynccontextmanager
    async def cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[Any], None]:
        """AsyncConnection + tuple-cursor — одиночные запросы без row_factory."""
        async with self.connection() as conn, conn.cursor() as cur:
            yield cur

    @asynccontextmanager
    async def client_cursor(
        self,
    ) -> AsyncGenerator[psycopg.AsyncClientCursor[Any], None]:
        """AsyncConnection + AsyncClientCursor (client-side parameter binding)."""
        async with (
            self.connection() as conn,
            psycopg.AsyncClientCursor(conn) as cur,
        ):
            yield cur

    @asynccontextmanager
    async def dict_cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[DictRow], None]:
        """AsyncConnection + dict-cursor (row_factory=dict_row)."""
        async with (
            self.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            yield cur

    async def close(self) -> None:
        """Закрыть пул. Идемпотентно."""
        if self._closed:
            return
        self._closed = True
        await self._pool.close()
        logger.info("AsyncPostgresPool closed")


class CancellablePool:
    """Делегат AsyncPostgresPool: регистрирует conn.cancel как прерыватель.

    cancel() у AsyncConnection синхронный и зовётся из чужого потока — это и нужно
    остановке хода: отмена asyncio сама по себе не прерывает запрос, идущий в базе.
    """

    def __init__(self, inner: AsyncPostgresPool) -> None:
        self._inner = inner

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
        async with self._inner.connection() as conn:
            with self._abort(conn):
                yield conn

    @asynccontextmanager
    async def cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[Any], None]:
        async with self._inner.cursor() as cur:
            with self._abort(cur.connection):
                yield cur

    @asynccontextmanager
    async def dict_cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[DictRow], None]:
        async with self._inner.dict_cursor() as cur:
            with self._abort(cur.connection):
                yield cur

    async def close(self) -> None:
        await self._inner.close()

    @staticmethod
    @contextmanager
    def _abort(conn: psycopg.AsyncConnection[Any]) -> Generator[None, None, None]:
        with current_cancellation().abort_with(conn.cancel):
            yield
