"""Миграции KB-схемы: DDL из migrations/*.sql, каждый файл идемпотентен, и
HNSW-индекс под размерность модели.

Ошибки:
KbMigrationError — каталог миграций пуст или его нет, размерность вектора не
    положительна.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar

from psycopg import sql

from boba.db.pgvector.config import PostgresStoreSchema
from boba.db.postgres import PgQuery, PgQueryBuilder

logger = logging.getLogger(__name__)

__all__ = ["KbMigrationError", "Migrations"]


class KbMigrationError(Exception):
    """Миграции не собрать: нет файлов или негодная размерность."""


class Migrations:
    """DDL-bootstrap KB-схемы: имена таблиц и индексов конфига подставляются в
    файлы migrations/*.sql стоящими именами сборщика; KbSchema применяет
    их при старте, стенды — напрямую на соединении."""

    DIRECTORY: ClassVar[Path] = Path(__file__).parent / "migrations"

    def __init__(self, tables: PostgresStoreSchema) -> None:
        self._tables = tables

    def _names(self) -> dict[str, sql.Composable]:
        """Плейсхолдеры файлов: таблицы, литералы имён и имена индексов."""
        schema = self._tables.pg_schema
        chunks = self._tables.chunks_table
        sources = self._tables.sources_table

        return {
            "schema": sql.Identifier(schema),
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

    def files(self) -> list[Path]:
        """Файлы миграций в лексикографическом порядке.

        Ошибки:
        KbMigrationError — каталога нет или он пуст.
        """
        if not self.DIRECTORY.is_dir():
            msg = f"pgvector migrations: {self.DIRECTORY} is not an existing directory"
            raise KbMigrationError(msg)

        files = sorted(self.DIRECTORY.glob("*.sql"))
        if not files:
            msg = f"pgvector migrations: no *.sql files in {self.DIRECTORY}"
            raise KbMigrationError(msg)

        return files

    def statements(self) -> list[PgQuery]:
        """DDL каждого файла с подставленными именами, в порядке файлов."""
        names = self._names()
        statements: list[PgQuery] = []
        for path in self.files():
            statements.append(PgQueryBuilder(**names).from_file(path).build())

        return statements

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

    async def apply(self, conn: Any) -> None:
        """Все миграции на соединении; каждая логируется по имени файла."""
        names = self._names()
        for path in self.files():
            statement = PgQueryBuilder(**names).from_file(path).build()
            logger.info(
                "applying migration %s (schema=%s, chunks=%s, collections=%s)",
                path.name,
                self._tables.pg_schema,
                self._tables.chunks_table,
                self._tables.collections_table,
            )
            await conn.execute(statement.text, statement.params, prepare=False)

    async def ensure_vector_index(self, conn: Any, dim: int) -> None:
        statement = self.vector_index(dim)
        await conn.execute(statement.text, statement.params, prepare=False)
