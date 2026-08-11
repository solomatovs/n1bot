"""Контракт postgres-узлов: имена операций, args вызывающего, запросы payload'а.

Args узла несут только пользовательские поля; профиль соединения с секретами и
лимиты добавляет обогатитель, и в спецификацию графа они не попадают.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from boba.db.postgres import PostgresConfig
from boba.toolkit.channels import StreamFormat
from boba.toolkit.sql import SqlCall, SqlQueryRequest

__all__ = [
    "PgCopyArgs",
    "PgCopyFormat",
    "PgCopyRequest",
    "PgCopyTrailer",
    "PgQueryArgs",
    "PgQueryRequest",
    "PgStage",
]


class PgStage(StrEnum):
    """Имена узлов postgres в реестре стадий; они же — операции payload'а."""

    QUERY = "pg_query"
    COPY = "pg_copy"


class PgCopyFormat(StrEnum):
    """Формат выгрузки COPY: он же формат продукта узла в tool_payload."""

    TEXT = "text"
    CSV = "csv"

    @property
    def stream(self) -> StreamFormat:
        """Формат канала данных, объявляемый контрактом узла."""
        if self is PgCopyFormat.TEXT:
            return StreamFormat.TEXT

        return StreamFormat.CSV

    def statement(self, sql: str) -> str:
        """COPY ... TO STDOUT нужного формата; заголовок несут оба формата."""
        if self is PgCopyFormat.TEXT:
            return f"COPY ({sql}) TO STDOUT WITH (FORMAT TEXT, HEADER)"

        return f"COPY ({sql}) TO STDOUT WITH (FORMAT CSV, HEADER)"


class PgQueryArgs(BaseModel):
    """Args узла pg_query: что задаёт вызывающий, потолки даёт конфиг."""

    model_config = ConfigDict(extra="forbid")

    connection_name: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    params: Sequence[JsonValue] = ()


class PgCopyArgs(BaseModel):
    """Args узла pg_copy: запрос и формат выгрузки."""

    model_config = ConfigDict(extra="forbid")

    connection_name: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    copy_format: PgCopyFormat = PgCopyFormat.TEXT


class PgQueryRequest(SqlQueryRequest[PostgresConfig, tuple[Any, ...]]):
    """Запрос строк с лимитом; параметры позиционные, под %s psycopg."""

    OP: ClassVar[str] = PgStage.QUERY


class PgCopyRequest(SqlCall[PostgresConfig]):
    """Выгрузка COPY ... TO STDOUT; потолок объёма держит читатель потока."""

    OP: ClassVar[str] = PgStage.COPY

    copy_format: PgCopyFormat


class PgCopyTrailer(BaseModel):
    """Итог выгрузки: байты ушли каналом данных, здесь — число строк."""

    model_config = ConfigDict(extra="forbid")

    rows: int = Field(ge=0)
