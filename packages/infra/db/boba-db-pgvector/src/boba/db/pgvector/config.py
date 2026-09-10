"""Конфиг KB-store: схема, имена таблиц и подключение.

Модели живут отдельно от store-адаптеров: read-side инструментам нужен только
конфиг, а импорт store потянул бы pgvector с numpy.

Ошибки: своих не выпускает; несогласованные имена таблиц роняют валидацию
модели ValueError.
"""

from __future__ import annotations

from typing import Self

from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.db.postgres.profile import PostgresConfig

__all__ = [
    "EmbeddingDimension",
    "KnowledgeBaseSchemaConfig",
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
            "Postgres schema, в которой живут таблицы KB (`chunks_table`, "
            "`collections_table`, `sources_table`) + функция `immutable_unaccent`. "
            "Должна существовать к моменту запуска bootstrap-CLI (или быть `public`)."
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
    sources_table: str = Field(
        description=(
            "Имя таблицы реестра источников: отпечаток версии, хэш тела и "
            "отметка последнего обхода на каждый проиндексированный источник."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        names = [self.chunks_table, self.collections_table, self.sources_table]
        if len(set(names)) != len(names):
            msg = (
                "PostgresStoreSchema: chunks_table, collections_table and "
                f"sources_table must differ, got {names!r}"
            )
            raise ValueError(msg)
        return self

    def chunks_ident(self) -> sql.Identifier:
        return sql.Identifier(self.pg_schema, self.chunks_table)

    def collections_ident(self) -> sql.Identifier:
        return sql.Identifier(self.pg_schema, self.collections_table)

    def sources_ident(self) -> sql.Identifier:
        return sql.Identifier(self.pg_schema, self.sources_table)

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


class EmbeddingDimension(BaseModel):
    """Из профиля embedding хранилищу нужна только размерность вектора: остальные
    ключи профиля читает инструмент, здесь они не разбираются."""

    model_config = ConfigDict(extra="ignore")

    dim: int = Field(gt=0, description="Размерность вектора колонки pgvector.")


class KnowledgeBaseSchemaConfig(PostgresStoreConfig):
    """Секция [tool.kb] глазами подготовки схемы: подключение, таблицы и
    размерность вектора. Полный конфиг инструмента живёт у самого плагина."""

    embedding: EmbeddingDimension
