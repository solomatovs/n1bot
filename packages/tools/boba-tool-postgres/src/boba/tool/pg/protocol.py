"""Контракт postgres-payload'а: SQL в песочнице, секреты едут через stdin."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres import PostgresConfig
from boba.toolkit.sql import SqlCall, SqlQueryRequest

__all__ = [
    "PgCopyRequest",
    "PgCopyTrailer",
    "PgQueryRequest",
]


class PgQueryRequest(SqlQueryRequest[PostgresConfig, tuple[Any, ...]]):
    """Запрос строк с лимитом; параметры позиционные, под %s psycopg."""

    OP: ClassVar[str] = "pg_query"


class PgCopyRequest(SqlCall[PostgresConfig]):
    """Выгрузка COPY ... TO STDOUT с потолком по байтам."""

    OP: ClassVar[str] = "pg_copy"

    max_bytes: int = Field(ge=1)


class PgCopyTrailer(BaseModel):
    """Итог выгрузки: текст ушёл кадрами, здесь только признак обрезки."""

    model_config = ConfigDict(extra="forbid")

    truncated: bool
