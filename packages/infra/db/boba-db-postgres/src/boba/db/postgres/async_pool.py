"""AsyncPostgresPool: async-обёртка над psycopg_pool.AsyncConnectionPool.

Ошибки:
PostgresPoolClosedError — обращение к закрытому пулу или соединение пула без
    окружения авторизации.
PostgresPoolLoopError — обращение к пулу из чужого цикла событий.
PostgresError — соединению пула не выдан TGT.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import abstractmethod
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Generator,
    Sequence,
)
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
)
from typing import Any, ClassVar, Protocol

import psycopg
from psycopg.rows import DictRow, dict_row

from boba.cancellation import current_cancellation
from boba.db.postgres.connection import PostgresAuthSession, PostgresConfig
from boba.db.postgres.errors import PostgresError

__all__ = [
    "AsyncPostgresPool",
    "AuthConnection",
    "CancellablePool",
    "PostgresPool",
    "PostgresPoolClosedError",
    "PostgresPoolLoopError",
]


class PostgresPoolClosedError(PostgresError):
    """Попытка взять connection из уже закрытого pool'а."""


class PostgresPoolLoopError(PostgresError):
    """Обращение к пулу из event loop, отличного от того, в котором он открыт."""


logger = logging.getLogger(__name__)


class AuthConnection(psycopg.AsyncConnection[Any]):
    """Соединение пула, поднимающее окружение авторизации профиля на время
    connect: GSSAPI-обмен libpq идёт внутри connect, поэтому окружение
    должно стоять до него, а не после. Что именно поднимать, решает
    PostgresAuthSession профиля; у не-kerberos вариантов это ничего."""

    session: ClassVar[PostgresAuthSession | None] = None

    @classmethod
    def bound_to(cls, session: PostgresAuthSession) -> type[AuthConnection]:
        """Подтип, привязанный к окружению авторизации одного пула."""
        name = f"{cls.__name__}[{session.describe()}]"
        return type(name, (cls,), {"session": session})

    @classmethod
    async def connect(cls, conninfo: str = "", **kwargs: Any) -> AuthConnection:
        if cls.session is None:
            msg = (
                f"{cls.__name__}.connect called without an auth session: "
                "bind the class with bound_to(session) first"
            )
            raise PostgresPoolClosedError(msg)

        async with cls.session.applied():
            return await super().connect(conninfo, **kwargs)  # type: ignore[return-value]


class PostgresPool(Protocol):
    """Источник соединений хранилища: пул приложения либо его делегат с
    прерыванием запроса; PostgresTable берёт соединения только отсюда."""

    @abstractmethod
    def connection(
        self,
    ) -> AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]: ...

    @abstractmethod
    async def close(self) -> None: ...


class AsyncPostgresPool(PostgresPool):
    """
    Единственная точка работы с postgres: async-пул поверх AsyncConnectionPool
    """

    Configure = Callable[[psycopg.AsyncConnection[Any]], Awaitable[None]]
    """Hook на каждое новое соединение пула (регистрация типов pgvector/hstore)."""

    _CacheKey = tuple[str, str]
    _CACHE: ClassVar[dict[_CacheKey, AsyncPostgresPool]] = {}
    _CACHE_LOCK: ClassVar[asyncio.Lock] = asyncio.Lock()

    _LIVE: ClassVar[list[AsyncPostgresPool]] = []
    """Открытые и ещё не закрытые пулы процесса — и учёт, и цель close_all."""

    def __init__(
        self,
        cfg: PostgresConfig,
        *,
        configure: Configure | None = None,
    ) -> None:
        from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

        self._cfg = cfg
        self._pool = AsyncConnectionPool(
            connection_class=self._connection_class(cfg),
            kwargs=cfg.conn_settings(),
            **cfg.pool_settings(),
            configure=configure,
            open=False,
        )
        self._closed = False
        self._loop_id: int | None = None
        self._loop_reported = False

        logger.info(
            "AsyncPostgresPool created db=%s auth=%s search_path=%s "
            "min_size=%d max_size=%s",
            cfg.dbname,
            cfg.auth.method,
            self.search_path,
            cfg.pool.min_size,
            cfg.pool.max_size,
        )

    @staticmethod
    def _connection_class(cfg: PostgresConfig) -> type[psycopg.AsyncConnection[Any]]:
        """Соединение с окружением авторизации профиля на время connect."""
        return AuthConnection.bound_to(cfg.auth_session())

    @property
    def search_path(self) -> str:
        """Схема соединений пула; пустая — таблицы квалифицируются в запросах."""
        if self._cfg.options.search_path is None:
            return ""

        return self._cfg.options.search_path

    @classmethod
    def opened(cls) -> Sequence[AsyncPostgresPool]:
        """Живые пулы процесса: сколько их и на каких схемах."""
        return tuple(cls._LIVE)

    async def open(self) -> None:
        """Открыть пул (установить фоновые соединения)."""
        self._loop_id = id(asyncio.get_running_loop())
        logger.info(
            "AsyncPostgresPool open db=%s auth=%s search_path=%s loop=%#x",
            self._cfg.dbname,
            self._cfg.auth.method,
            self.search_path,
            self._loop_id,
        )
        await self._pool.open()
        self._LIVE.append(self)

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
                "current_loop=%#x: the pool was opened in another event loop; "
                "look for a second loop (asyncio.run in a thread, a background "
                "task with its own loop, the server recreating its loop)",
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
        configure: Configure | None = None,
    ) -> AsyncPostgresPool:
        """Открытый пул-singleton по cfg и configure; закрытый пересоздаётся.

        Пулы с разным configure — разные пулы: hook применяется к соединению
        при создании, на чужие соединения его уже не навесить.
        """
        key = cls._cache_key(cfg, configure)

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

            pool = cls(cfg, configure=configure)
            await pool.open()
            cls._CACHE[key] = pool
            return pool

    @classmethod
    async def close_all(cls) -> None:
        """Закрывает и забывает все живые пулы процесса, не только singleton'ы."""
        async with cls._CACHE_LOCK:
            cls._CACHE.clear()
            pools = list(cls._LIVE)

        for pool in pools:
            await pool.close()

    @staticmethod
    def _cache_key(
        cfg: PostgresConfig,
        configure: Configure | None,
    ) -> _CacheKey:
        settings = json.dumps(
            {**cfg.conn_settings(), **cfg.pool_settings()},
            sort_keys=True,
            default=str,
        )
        hook = ""
        if configure is not None:
            hook = f"{configure.__module__}.{configure.__qualname__}"

        return settings, hook

    @classmethod
    async def dedicated(cls, cfg: PostgresConfig) -> psycopg.AsyncConnection[Any]:
        """Отдельное соединение вне пула в autocommit: для LISTEN нужно соединение
        без транзакций, которое никто не забирает под запросы."""
        connection_class = cls._connection_class(cfg)
        settings = cfg.conn_settings()
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
            msg = f"pool for {self._cfg.dbname} is closed, no connection to give"
            raise PostgresPoolClosedError(msg)

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
        if self in self._LIVE:
            self._LIVE.remove(self)

        await self._pool.close()
        logger.info("AsyncPostgresPool closed")


class CancellablePool(PostgresPool):
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
