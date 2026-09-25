"""Postgres для payload'ов; пула нет — каждый вызов свой процесс и соединение.
Учётные данные приходят через stdin: не видны ни в argv, ни в /proc, ни в логах.
PgScript выполняет стейтменты before/after насоса на том же соединении.

Ошибки:
PostgresError — до базы не достучаться (сеть, отказ libpq, kerberos).
psycopg.Error — сервер отклонил стейтмент скрипта."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import psycopg

from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.errors import PostgresError
from boba.db.postgres.trace import PgScriptStep
from boba.toolkit.timing import Elapsed

__all__ = ["PayloadPostgres", "PgScript"]

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


class PgScript:
    """Стейтменты before/after насоса на его соединении: по одному, по порядку,
    в той же транзакции, что и команда насоса, поэтому temp-таблицы и `set
    local` из before видны команде и after, а ошибка любого шага откатывает всё.
    Строки выборок не собираются, шаг даёт только статус сервера."""

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn

    async def run(self, statements: Sequence[str]) -> tuple[PgScriptStep, ...]:
        steps: list[PgScriptStep] = []
        for statement in statements:
            step = await self._step(statement)
            steps.append(step)

        return tuple(steps)

    async def _step(self, statement: str) -> PgScriptStep:
        # bytes: тип Query psycopg требует LiteralString, а текст пишет LLM
        async with self._conn.cursor() as cur:
            await cur.execute(statement.encode(self._conn.info.encoding))
            statuses = self._statuses(cur)

        return PgScriptStep(statement=statement, status="; ".join(statuses))

    @staticmethod
    def _statuses(cur: psycopg.AsyncCursor[Any]) -> Sequence[str]:
        statuses: list[str] = []
        while True:
            status = cur.statusmessage
            if status is None:
                status = "done"

            statuses.append(status)
            if not cur.nextset():
                return statuses
