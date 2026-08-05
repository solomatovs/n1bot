"""Контракт postgres-payload'а: SQL в песочнице, секреты едут через stdin."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from boba.db.postgres import PostgresConfig

__all__ = [
    "PgCopyRequest",
    "PgCopyTrailer",
    "PgQueryRequest",
    "PgQueryTrailer",
]


class PgCall(BaseModel):
    """Общая часть: к чему подключаться и что выполнять."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)
    connection: PostgresConfig = Field(
        description=(
            "Профиль подключения целиком: libpq-параметры, опции сессии и "
            "креды kerberos, по которым payload сам получает TGT."
        ),
    )
    sql: str = Field(min_length=1)

    @field_serializer("connection", when_used="json")
    def _dump_connection(self, value: PostgresConfig) -> dict[str, Any]:
        """stdin песочницы — доверенный канал: только здесь пароль едет раскрытым."""
        return value.model_dump(
            mode="json",
            context={PostgresConfig.REVEAL_SECRETS: True},
        )


class PgQueryRequest(PgCall):
    """Запрос строк с лимитом."""

    OP: ClassVar[str] = "pg_query"

    params: tuple[Any, ...] = Field(
        description="Позиционные параметры запроса; пустой кортеж — без них.",
    )
    row_limit: int = Field(ge=1)


class PgQueryTrailer(BaseModel):
    """Итог запроса: строки ушли кадрами, здесь признак превышения лимита."""

    model_config = ConfigDict(extra="forbid")

    truncated: bool


class PgCopyRequest(PgCall):
    """Выгрузка COPY ... TO STDOUT с потолком по байтам."""

    OP: ClassVar[str] = "pg_copy"

    max_bytes: int = Field(ge=1)


class PgCopyTrailer(BaseModel):
    """Итог выгрузки: текст ушёл кадрами, здесь только признак обрезки."""

    model_config = ConfigDict(extra="forbid")

    truncated: bool
