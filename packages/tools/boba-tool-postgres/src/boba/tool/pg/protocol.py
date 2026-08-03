"""Контракт postgres-payload'а: SQL в песочнице, секреты едут через stdin."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PgCopyAnswer",
    "PgCopyRequest",
    "PgQueryAnswer",
    "PgQueryRequest",
]


class PgCall(BaseModel):
    """Общая часть: к чему подключаться и что выполнять."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)
    connection: dict[str, Any] = Field(
        description="libpq-параметры connect(): host/dbname/user/options/...",
    )
    sql: str = Field(min_length=1)


class PgQueryRequest(PgCall):
    """Запрос строк с лимитом."""

    OP: ClassVar[str] = "pg_query"

    params: tuple[Any, ...] = Field(
        description="Позиционные параметры запроса; пустой кортеж — без них.",
    )
    row_limit: int = Field(ge=1)


class PgQueryAnswer(BaseModel):
    """Строки результата и признак того, что их было больше лимита."""

    model_config = ConfigDict(extra="forbid")

    rows: tuple[dict[str, Any], ...]
    truncated: bool


class PgCopyRequest(PgCall):
    """Выгрузка COPY ... TO STDOUT с потолком по байтам."""

    OP: ClassVar[str] = "pg_copy"

    max_bytes: int = Field(ge=1)


class PgCopyAnswer(BaseModel):
    """Текст выгрузки и признак обрезки по max_bytes."""

    model_config = ConfigDict(extra="forbid")

    text: str
    truncated: bool
