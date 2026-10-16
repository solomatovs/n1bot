"""Имена KB-схемы для файлов schema/*.sql пакета и HNSW-индекс под
размерность модели.

Ошибки:
KbMigrationError — размерность вектора не положительна.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import sql

from boba.db.pgvector.config import PostgresStoreSchema
from boba.db.postgres import PgQuery, PgQueryBuilder

__all__ = ["KbMigrationError", "Migrations"]


class KbMigrationError(Exception):
    """Векторный индекс не собрать: негодная размерность."""


class Migrations:
    """Имена KB-схемы для файлов schema/*.sql пакета: таблицы и индексы
    конфига подставляются стоящими именами сборщика, схему {schema} ставит
    PostgresSchema.apply_files. KbSchema применяет файлы и векторный индекс
    при старте плагина."""

    def __init__(self, tables: PostgresStoreSchema) -> None:
        self._tables = tables

    def names(self) -> Mapping[str, sql.Composable]:
        """Плейсхолдеры файлов: таблицы, литералы имён и имена индексов."""
        schema = self._tables.pg_schema
        chunks = self._tables.chunks_table
        sources = self._tables.sources_table

        return {
            "chunks_table": sql.Identifier(schema, chunks),
            "collections_table": sql.Identifier(schema, self._tables.collections_table),
            "sources_table": sql.Identifier(schema, sources),
            "schema_name_lit": sql.Literal(schema),
            "chunks_name_lit": sql.Literal(chunks),
            "chunks_tsv_gin_name": sql.Identifier(f"{chunks}_tsv_gin"),
            "chunks_collection_idx_name": sql.Identifier(f"{chunks}_collection"),
            "chunks_collection_source_idx_name": sql.Identifier(
                f"{chunks}_collection_source"
            ),
            "chunks_collection_tsv_gin_name": sql.Identifier(
                f"{chunks}_collection_tsv_gin"
            ),
            "chunks_collection_source_chunk_idx_name": sql.Identifier(
                f"{chunks}_collection_source_chunk"
            ),
            "sources_collection_seen_idx_name": sql.Identifier(
                f"{sources}_collection_seen"
            ),
            "sources_collection_parent_idx_name": sql.Identifier(
                f"{sources}_collection_parent"
            ),
            "sources_collection_scope_idx_name": sql.Identifier(
                f"{sources}_collection_scope"
            ),
            "sources_collection_parent_run_idx_name": sql.Identifier(
                f"{sources}_collection_parent_run"
            ),
            # drop index требует схему в имени: search_path соединения миграций
            # до схемы KB не расширяется
            "chunks_tsv_gin_qualified": sql.Identifier(schema, f"{chunks}_tsv_gin"),
            "chunks_collection_idx_qualified": sql.Identifier(
                schema, f"{chunks}_collection"
            ),
            "chunks_collection_source_idx_qualified": sql.Identifier(
                schema, f"{chunks}_collection_source"
            ),
        }

    def vector_index(self, dim: int) -> PgQuery:
        """HNSW-индекс на выражение embedding::vector(dim): pgvector требует
        фиксированной размерности, поэтому один индекс = одна dim (dim в имени).

        Ошибки:
        KbMigrationError — dim не положителен.
        """
        if dim <= 0:
            msg = f"pgvector vector index: dim must be positive, got {dim}"
            raise KbMigrationError(msg)

        schema = self._tables.pg_schema
        chunks = self._tables.chunks_table

        # vector_cosine_ops = cosine (<=>); для L2 сменить opclass и пересоздать индекс
        return (
            PgQueryBuilder(
                index_name=sql.Identifier(f"{chunks}_embedding_hnsw_{dim}"),
                chunks_table=sql.Identifier(schema, chunks),
                dim=sql.Literal(dim),
            )
            .add(
                """
                create index if not exists {index_name}
                    on {chunks_table} using hnsw
                    ((embedding::vector({dim})) vector_cosine_ops)
                """
            )
            .build()
        )

    async def ensure_vector_index(self, conn: Any, dim: int) -> None:
        statement = self.vector_index(dim)
        await conn.execute(statement.text, statement.params, prepare=False)
