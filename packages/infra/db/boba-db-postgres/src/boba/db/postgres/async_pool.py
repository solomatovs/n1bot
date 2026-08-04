"""AsyncPostgresPool: async-обёртка над psycopg_pool.AsyncConnectionPool.

Ошибки: PostgresPoolClosedError — обращение к закрытому пулу;
KeytabError/KerberosError — соединению не выдан TGT из keytab.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import psycopg
from psycopg.rows import DictRow, dict_row

from boba.db.postgres.config import PostgresConfig
from boba.db.postgres.errors import PostgresPoolClosedError
from boba.krb import KerberosCredentials, KeytabCredentials

__all__ = ["AsyncPostgresPool", "KerberosConnection"]

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
    "async-обёртка над psycopg_pool.AsyncConnectionPool с явным open()/close()"

    def __init__(
        self,
        cfg: PostgresConfig,
        *,
        override_options: dict[str, str] | None = None,
    ) -> None:
        from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

        self._cfg = cfg
        self._pool = AsyncConnectionPool(
            connection_class=self._connection_class(cfg),
            kwargs=cfg.conn_settings(override_options),
            **cfg.pool_settings(),
            open=False,
        )
        self._closed = False
        logger.info(
            "AsyncPostgresPool created db=%s user=%s min_size=%d max_size=%s krb=%s",
            cfg.dbname,
            cfg.user,
            cfg.pool.min_size,
            cfg.pool.max_size,
            cfg.kerberos.principal if cfg.kerberos else "off",
        )

    @staticmethod
    def _connection_class(cfg: PostgresConfig) -> type[psycopg.AsyncConnection[Any]]:
        """Соединение с собственным TGT, если у конфига есть секция kerberos."""
        if cfg.kerberos is None:
            return psycopg.AsyncConnection

        return KerberosConnection.bound_to(KeytabCredentials(cfg.kerberos))

    async def open(self) -> None:
        """Открыть пул (установить фоновые соединения)."""
        await self._pool.open()

    @property
    def raw(self) -> Any:
        """Внутренний psycopg_pool.AsyncConnectionPool (для langgraph-саверов)."""
        return self._pool

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[psycopg.AsyncConnection[Any], None]:
        """Взять AsyncConnection из пула."""
        if self._closed:
            raise PostgresPoolClosedError("PostgresPool is closed")

        async with self._pool.connection() as conn:
            yield conn

    @asynccontextmanager
    async def cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[Any], None]:
        """AsyncConnection + tuple-cursor — одиночные запросы без row_factory."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            yield cur

    @asynccontextmanager
    async def client_cursor(
        self,
    ) -> AsyncGenerator[psycopg.AsyncClientCursor[Any], None]:
        """AsyncConnection + AsyncClientCursor (client-side parameter binding)."""
        async with (
            self._pool.connection() as conn,
            psycopg.AsyncClientCursor(conn) as cur,
        ):
            yield cur

    @asynccontextmanager
    async def dict_cursor(self) -> AsyncGenerator[psycopg.AsyncCursor[DictRow], None]:
        """AsyncConnection + dict-cursor (row_factory=dict_row)."""
        async with (
            self._pool.connection() as conn,
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
