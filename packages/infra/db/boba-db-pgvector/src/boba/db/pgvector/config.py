"""Конфиг KB-store: схема, имена таблиц и подключение.

Модели живут отдельно от store-адаптеров: read-side инструментам нужен только
конфиг, а импорт store потянул бы pgvector с numpy.

Ошибки: своих не выпускает; несогласованные имена таблиц роняют валидацию
модели ValueError.
"""

from __future__ import annotations

from typing import Self

from psycopg import sql
from pydantic import BaseModel, Field, model_validator

from boba.connections.postgres import PostgresConfig

__all__ = [
    "PostgresStoreConfig",
    "PostgresStoreSchema",
]


class PostgresStoreSchema(BaseModel):
    """Schema и имена таблиц KB; один конфиг для bootstrap-CLI и ingest/search-tools."""

    batch_size: int = Field(
        default=100,
        description="batch_size",
    )
    pg_schema: str = Field(
        default="public",
        description=(
            "Postgres schema, в которой живут таблицы KB (`chunks_table` и "
            "`collections_table`) + функция `immutable_unaccent`. Должна "
            "существовать к моменту запуска bootstrap-CLI (или быть `public`)."
        ),
    )
    chunks_table: str = Field(
        default="kb_chunks",
        description=(
            "Имя таблицы чанков (хранит embedding + metadata + tsvector). "
            "По дефолту `kb_chunks`; bootstrap-CLI создаёт её именно с этим "
            "именем в указанной `schema`."
        ),
    )
    collections_table: str = Field(
        default="kb_collections",
        description=(
            "Имя таблицы-каталога коллекций (one row per collection). "
            "По дефолту `kb_collections`."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.chunks_table == self.collections_table:
            msg = (
                "PostgresStoreSchema.chunks_table == collections_table "
                f"({self.chunks_table!r}), they must differ"
            )
            raise ValueError(msg)
        return self

    def chunks_ident(self) -> sql.Identifier:
        return sql.Identifier(self.pg_schema, self.chunks_table)

    def collections_ident(self) -> sql.Identifier:
        return sql.Identifier(self.pg_schema, self.collections_table)

    def schema_ident(self) -> sql.Identifier:
        return sql.Identifier(self.pg_schema)

    def chunks_name_literal(self) -> sql.Literal:
        return sql.Literal(self.chunks_table)

    def schema_name_literal(self) -> sql.Literal:
        return sql.Literal(self.pg_schema)


class PostgresStoreConfig(BaseModel):
    """Composite-конфиг для KB-store-сервисов: connection + tables."""

    connection: PostgresConfig
    tables: PostgresStoreSchema
