"""Postgres для payload'ов; пула нет — каждый вызов свой процесс и соединение.
Учётные данные приходят каналом tool_args: не видны ни в argv, ни в /proc, ни в логах.

Ошибки: PostgresError — до базы не достучаться (сеть, отказ libpq, kerberos)."""

from __future__ import annotations

from typing import Any

import psycopg

from boba.db.postgres.async_pool import PostgresError
from boba.db.postgres.config import PostgresConfig
from boba.krb import KerberosError, KeytabCredentials

__all__ = ["PayloadPostgres"]


class PayloadPostgres:
    """Подключение по профилю запроса; профиль разобран моделью на границе."""

    @staticmethod
    async def connect(
        connection: PostgresConfig,
    ) -> psycopg.AsyncConnection[Any]:
        if connection.kerberos is None:
            return await PayloadPostgres._connect(connection)

        credentials = KeytabCredentials(connection.kerberos)

        try:
            async with credentials.applied_async():
                return await PayloadPostgres._connect(connection)
        except KerberosError as e:
            msg = f"kerberos failed: {type(e).__name__}: {e}"
            raise PostgresError(msg) from e

    @staticmethod
    async def _connect(connection: PostgresConfig) -> psycopg.AsyncConnection[Any]:
        try:
            return await psycopg.AsyncConnection.connect(**connection.conn_settings())
        except psycopg.Error as e:
            msg = f"connect failed: {type(e).__name__}: {e}"
            raise PostgresError(msg) from e
