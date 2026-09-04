"""Postgres для payload'ов; пула нет — каждый вызов свой процесс и соединение.
Учётные данные приходят через stdin: не видны ни в argv, ни в /proc, ни в логах.

Ошибки: PostgresError — до базы не достучаться (сеть, отказ libpq, kerberos)."""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from boba.db.postgres.async_pool import PostgresError
from boba.db.postgres.profile import PostgresConfig
from boba.kerberos import KerberosAuthBase, KerberosError
from boba.krb import ClientCredentials
from boba.toolkit.timing import Elapsed

__all__ = ["PayloadPostgres"]

logger = logging.getLogger(__name__)


class PayloadPostgres:
    """Подключение по libpq-параметрам запроса и JSON-совместимые строки."""

    @staticmethod
    async def connect(request: dict[str, Any]) -> psycopg.AsyncConnection[Any]:
        connection = PostgresConfig.model_validate(request["connection"])
        return await PayloadPostgres.connect_config(connection)

    @staticmethod
    async def connect_config(
        connection: PostgresConfig,
    ) -> psycopg.AsyncConnection[Any]:
        """Соединение по модели профиля; kerberos-профиль получает свой TGT."""
        elapsed = Elapsed()

        if not isinstance(connection.auth, KerberosAuthBase):
            conn = await PayloadPostgres._connect(connection)
            logger.info("postgres connected in %dms", elapsed.ms())
            return conn

        credentials = ClientCredentials.of(connection.auth)

        try:
            async with credentials.applied_async():
                conn = await PayloadPostgres._connect(connection)
        except KerberosError as e:
            msg = (
                f"postgres {PayloadPostgres._where(connection)}: kerberos "
                f"credentials of {credentials.principal} failed: "
                f"{type(e).__name__}: {e}"
            )
            raise PostgresError(msg) from e

        logger.info("postgres connected in %dms (kerberos)", elapsed.ms())
        return conn

    @staticmethod
    async def _connect(connection: PostgresConfig) -> psycopg.AsyncConnection[Any]:
        try:
            return await psycopg.AsyncConnection.connect(**connection.conn_settings())
        except psycopg.Error as e:
            msg = (
                f"connecting to postgres {PayloadPostgres._where(connection)} "
                f"as {connection.trace()} failed: {type(e).__name__}: {e}"
            )
            raise PostgresError(msg) from e

    @staticmethod
    def _where(connection: PostgresConfig) -> str:
        """host:port/dbname соединения для текста ошибки."""
        host = connection.host
        if not host:
            host = connection.hostaddr

        return f"{host}:{connection.port}/{connection.dbname}"
