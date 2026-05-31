"""`PostgresStoreSchema` — переиспользуемая базовая модель schema/таблиц KB-store.

`BaseModel` (не settings), встраивается как nested-поле в tool-конфиги.
Содержит имена postgres-схемы и таблиц `kb_chunks` / `kb_collections`.
Один и тот же `PostgresStoreSchema` идёт и в bootstrap-CLI (создаёт
таблицы), и в ingest/search-tools (читают/пишут эти же таблицы).

Все идентификаторы (schema/chunks_table/collections_table) — валидные
postgres-идентификаторы без кавычек/точек/пробелов; psycopg.sql.Identifier
квотирует их при подстановке.
"""

from __future__ import annotations

from typing import Self

from psycopg import sql
from pydantic import BaseModel, Field, model_validator

__all__ = ["PostgresStoreSchema"]


class PostgresStoreSchema(BaseModel):
    """Schema + имена таблиц KB-хранилища."""

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
                f"({self.chunks_table!r}) — должны различаться"
            )
            raise ValueError(msg)
        return self

    def chunks_ident(self) -> sql.Identifier:
        """Schema-qualified Identifier для таблицы чанков."""
        return sql.Identifier(self.pg_schema, self.chunks_table)

    def collections_ident(self) -> sql.Identifier:
        """Schema-qualified Identifier для таблицы коллекций."""
        return sql.Identifier(self.pg_schema, self.collections_table)

    def schema_ident(self) -> sql.Identifier:
        """Schema-only Identifier — для квалифицированных функций."""
        return sql.Identifier(self.pg_schema)

    def chunks_name_literal(self) -> sql.Literal:
        """Unqualified имя таблицы как SQL-литерал — для information_schema."""
        return sql.Literal(self.chunks_table)

    def schema_name_literal(self) -> sql.Literal:
        """Имя схемы как SQL-литерал — для information_schema.table_schema."""
        return sql.Literal(self.pg_schema)
