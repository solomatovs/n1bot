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
from boba.toolkit.sql import SqlCall, SqlQueryRequest

__all__ = [
    "PgCopyArgs",
    "PgCopyDirection",
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


class PgCopyDirection(StrEnum):
    """Куда течёт COPY: оператор наполняет канал данных либо читает вход узла."""

    TO_STDOUT = "to_stdout"
    FROM_STDIN = "from_stdin"


class PgCopyFormat(StrEnum):
    """Формат данных COPY: он же формат продукта узла в tool_payload."""

    TEXT = "text"
    CSV = "csv"

    def statement(self, sql: str) -> str:
        """COPY ... TO STDOUT нужного формата; заголовок несут оба формата."""
        if self is PgCopyFormat.TEXT:
            return f"COPY ({sql}) TO STDOUT WITH (FORMAT TEXT, HEADER)"

        return f"COPY ({sql}) TO STDOUT WITH (FORMAT CSV, HEADER)"

    @classmethod
    def of(cls, name: str) -> PgCopyFormat:
        """Имя формата из опций; неизвестное и binary — ValueError."""
        wanted = name.lower()

        for member in cls:
            if member.value == wanted:
                return member

        names: list[str] = []
        for member in cls:
            names.append(member.value)

        supported = ", ".join(names)

        raise ValueError(f"unsupported COPY format: {wanted}; supported: {supported}")


class PgQueryArgs(BaseModel):
    """Args узла pg_query: что задаёт вызывающий, потолки даёт конфиг."""

    model_config = ConfigDict(extra="forbid")

    connection_name: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    params: Sequence[JsonValue] = ()


class PgCopyArgs(BaseModel):
    """Args узла pg_copy: оператор COPY и направление, объявленное вызывающим."""

    model_config = ConfigDict(extra="forbid")

    connection_name: str = Field(min_length=1)
    direction: PgCopyDirection = Field(
        description=(
            "to_stdout — the statement fills the data channel; "
            "from_stdin — the statement loads the node input into a table."
        ),
    )
    sql: str = Field(
        min_length=1,
        description=(
            "Whole COPY statement, e.g. `COPY (SELECT ...) TO STDOUT WITH "
            "(FORMAT CSV, HEADER)` or `COPY table (col, ...) FROM STDIN WITH "
            "(FORMAT CSV)`. It must match the declared direction."
        ),
    )


class PgQueryRequest(SqlQueryRequest[PostgresConfig, tuple[Any, ...]]):
    """Запрос строк с лимитом; параметры позиционные, под %s psycopg."""

    OP: ClassVar[str] = PgStage.QUERY


class PgCopyRequest(SqlCall[PostgresConfig]):
    """COPY каналами: to_stdout наполняет канал данных, from_stdin читает вход.

    Оператор уходит в СУБД как есть: расхождение с объявленным направлением —
    отказ postgres в рантайме, разбирать SQL на стороне payload'а незачем.
    """

    OP: ClassVar[str] = PgStage.COPY

    direction: PgCopyDirection


class PgCopyTrailer(BaseModel):
    """Итог COPY: байты прошли каналом, здесь — направление и число строк."""

    model_config = ConfigDict(extra="forbid")

    direction: PgCopyDirection
    rows: int = Field(ge=0)
