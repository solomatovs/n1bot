"""Postgres для payload'ов; пула нет — каждый вызов свой процесс и соединение.
Учётные данные приходят через stdin: не видны ни в argv, ни в /proc, ни в логах.

Ошибки:
PostgresError — до базы не достучаться (сеть, отказ libpq, kerberos)."""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.errors import PostgresError
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
        """Соединение по модели профиля; окружение авторизации на время
        connect поднимает PostgresAuthSession профиля."""
        elapsed = Elapsed()
        session = connection.auth_session()

        async with session.applied():
            conn = await PayloadPostgres._connect(connection)

        logger.info("postgres connected in %dms (%s)", elapsed.ms(), session.describe())
        return conn

    @staticmethod
    async def _connect(connection: PostgresConfig) -> psycopg.AsyncConnection[Any]:
        try:
            return await psycopg.AsyncConnection.connect(**connection.conn_settings())
        except psycopg.Error as e:
            msg = (
                f"connecting to postgres {connection.where()} "
                f"as {connection.trace()} failed: {type(e).__name__}: {e}"
            )
            raise PostgresError(msg) from e
