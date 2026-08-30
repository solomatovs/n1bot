"""Таблицы чата на общей базе postgres: отказ базы уходит наружу ошибкой слоя данных.

Ошибки:
DataUnavailableError — пул, соединение или запрос отказали.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from psycopg import sql

from boba.chat.threads import DataUnavailableError
from boba.db.postgres import PostgresError, PostgresTable

__all__ = ["PgTable"]


class PgTable(PostgresTable):
    """Таблица схемы чата: DDL и запросы с упаковкой отказа в DataUnavailableError."""

    async def _run(self, statements: Sequence[sql.Composed], operation: str) -> None:
        try:
            await self._apply_ddl(statements)
        except PostgresError as exc:
            raise DataUnavailableError(operation, str(exc)) from exc

    async def _execute_as(
        self, query: sql.Composed, params: Mapping[str, Any], operation: str
    ) -> None:
        try:
            await self._execute(query, params)
        except PostgresError as exc:
            raise DataUnavailableError(operation, str(exc)) from exc

    async def _fetch_as(
        self, query: sql.Composed, params: Mapping[str, Any], operation: str
    ) -> list[tuple[Any, ...]]:
        try:
            return await self._fetch(query, params)
        except PostgresError as exc:
            raise DataUnavailableError(operation, str(exc)) from exc
