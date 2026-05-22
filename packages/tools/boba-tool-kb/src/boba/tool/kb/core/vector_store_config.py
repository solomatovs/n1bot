"""`VectorStoreSchemaConfig` — схема + имена таблиц KB-хранилища.

Секция `[tool.kb.vector_store]`. Один и тот же конфиг используется
**и** bootstrap-CLI (`apply_bootstrap` + `ensure_vector_index` — создают
таблицы / индексы), **и** runtime-стороной (`PostgresVectorStore`,
`PostgresKnowledgeBase` — пишут/читают эти же таблицы). Это инвариант:
если разъедутся — bootstrap создаст одни таблицы, store будет ходить в
другие, и пайплайн тихо упадёт на `relation does not exist`.

Все идентификаторы (schema/chunks_table/collections_table) — валидные
postgres-идентификаторы без кавычек/точек/пробелов; psycopg.sql.Identifier
квотирует их при подстановке.
"""

from __future__ import annotations

import re
from typing import Self

from psycopg import sql
from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["VectorStoreSchemaConfig"]

# PG-идентификатор: первая буква/_, далее буквы/цифры/_. Длина ≤ 63
# (NAMEDATALEN-1 в стандартной сборке Postgres). Намеренно консервативно:
# квотированные «странные» имена (пробелы, верхний регистр) разрешать не
# хотим — упрощает дебаг и сравнение в information_schema.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_IDENT_LEN = 63


def _validate_ident(value: str, *, field: str) -> str:
    if len(value) > _MAX_IDENT_LEN:
        msg = (
            f"{field}: длина {len(value)} > {_MAX_IDENT_LEN} "
            f"(NAMEDATALEN-1 в Postgres)"
        )
        raise ValueError(msg)
    if not _IDENT_RE.match(value):
        msg = (
            f"{field}: {value!r} — не валидный postgres-идентификатор "
            "([A-Za-z_][A-Za-z0-9_]*)"
        )
        raise ValueError(msg)
    return value


class VectorStoreSchemaConfig(BobaFlatSettings):
    """Schema + имена таблиц KB-хранилища ([tool.kb.vector_store])."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.vector_store",
    )

    schema: str = Field(
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
        _validate_ident(self.schema, field="tool.kb.vector_store.schema")
        _validate_ident(
            self.chunks_table,
            field="tool.kb.vector_store.chunks_table",
        )
        _validate_ident(
            self.collections_table,
            field="tool.kb.vector_store.collections_table",
        )
        if self.chunks_table == self.collections_table:
            msg = (
                "tool.kb.vector_store.chunks_table == collections_table "
                f"({self.chunks_table!r}) — должны различаться"
            )
            raise ValueError(msg)
        return self

    # ----- готовые Composable'ы (для use-site) ----------------------------- #

    def chunks_ident(self) -> sql.Identifier:
        """Schema-qualified Identifier для таблицы чанков."""
        return sql.Identifier(self.schema, self.chunks_table)

    def collections_ident(self) -> sql.Identifier:
        """Schema-qualified Identifier для таблицы коллекций."""
        return sql.Identifier(self.schema, self.collections_table)

    def schema_ident(self) -> sql.Identifier:
        """Schema-only Identifier — для квалифицированных функций."""
        return sql.Identifier(self.schema)

    def chunks_name_literal(self) -> sql.Literal:
        """Unqualified имя таблицы как SQL-литерал — для information_schema."""
        return sql.Literal(self.chunks_table)

    def schema_name_literal(self) -> sql.Literal:
        """Имя схемы как SQL-литерал — для information_schema.table_schema."""
        return sql.Literal(self.schema)
