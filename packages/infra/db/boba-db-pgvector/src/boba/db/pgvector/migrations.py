"""Bootstrap-миграции KB-схемы для CLI: DDL-only, каждый SQL идемпотентен."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar, LiteralString, cast

from psycopg import sql

from boba.db.pgvector.store import PostgresStoreSchema

logger = logging.getLogger(__name__)

__all__ = ["Migrations"]


class Migrations:
    """DDL-bootstrap KB-схемы: применение migrations/*.sql + HNSW-индекс."""

    _MIGRATIONS_DIR: ClassVar[Path] = Path(__file__).parent / "migrations"

    @staticmethod
    def _render_migration(
        text: str,
        *,
        schema_cfg: PostgresStoreSchema,
    ) -> sql.Composable:
        """Подставляет идентификаторы/литералы schema_cfg в SQL-шаблон; cast до
        LiteralString безопасен: источник — файл миграции пакета, не user-input."""
        chunks_name = schema_cfg.chunks_table
        schema_name = schema_cfg.pg_schema
        return sql.SQL(cast(LiteralString, text)).format(
            schema=schema_cfg.schema_ident(),
            chunks_table=schema_cfg.chunks_ident(),
            collections_table=schema_cfg.collections_ident(),
            schema_name_lit=schema_cfg.schema_name_literal(),
            chunks_name_lit=schema_cfg.chunks_name_literal(),
            chunks_tsv_gin_name=sql.Identifier(f"{chunks_name}_tsv_gin"),
            chunks_collection_idx_name=sql.Identifier(f"{chunks_name}_collection"),
            chunks_collection_source_idx_name=sql.Identifier(
                f"{chunks_name}_collection_source",
            ),
            chunks_collection_tsv_gin_name=sql.Identifier(
                f"{chunks_name}_collection_tsv_gin",
            ),
            chunks_collection_source_chunk_idx_name=sql.Identifier(
                f"{chunks_name}_collection_source_chunk",
            ),
            # drop index требует схему в имени: search_path соединения миграций
            # до схемы KB не расширяется
            chunks_tsv_gin_qualified=sql.Identifier(
                schema_name, f"{chunks_name}_tsv_gin"
            ),
            chunks_collection_idx_qualified=sql.Identifier(
                schema_name, f"{chunks_name}_collection"
            ),
            chunks_collection_source_idx_qualified=sql.Identifier(
                schema_name, f"{chunks_name}_collection_source"
            ),
        )

    @staticmethod
    async def apply_bootstrap(
        conn: Any,
        *,
        schema_cfg: PostgresStoreSchema,
    ) -> None:
        """Применяет все миграции из migrations/*.sql в лексикографическом порядке."""
        migrations_dir = Migrations._MIGRATIONS_DIR
        if not migrations_dir.is_dir():
            msg = f"pgvector migrations: {migrations_dir} is not an existing directory"
            raise RuntimeError(msg)
        files = sorted(migrations_dir.glob("*.sql"))
        if not files:
            logger.warning("no migrations found in %s", migrations_dir)
            return
        for f in files:
            text = f.read_text(encoding="utf-8")
            rendered = Migrations._render_migration(text, schema_cfg=schema_cfg)
            logger.info(
                "applying migration %s (schema=%s, chunks=%s, collections=%s)",
                f.name,
                schema_cfg.pg_schema,
                schema_cfg.chunks_table,
                schema_cfg.collections_table,
            )
            await conn.execute(rendered, prepare=False)

    @staticmethod
    async def ensure_vector_index(
        conn: Any,
        *,
        dim: int,
        schema_cfg: PostgresStoreSchema,
    ) -> None:
        """HNSW-индекс на выражение embedding::vector(dim): pgvector требует
        фиксированной размерности, поэтому один индекс = одна dim (dim в имени)."""
        if dim <= 0:
            msg = f"ensure_vector_index: dim must be positive, got {dim}"
            raise ValueError(msg)
        index_name = f"{schema_cfg.chunks_table}_embedding_hnsw_{dim}"
        # vector_cosine_ops = cosine (<=>); для L2 сменить opclass и пересоздать индекс
        stmt = sql.SQL(
            """
            create index if not exists {index_name}
                on {chunks_table} using hnsw
                ((embedding::vector({dim})) vector_cosine_ops)
            """,
        ).format(
            index_name=sql.Identifier(index_name),
            chunks_table=schema_cfg.chunks_ident(),
            dim=sql.Literal(dim),
        )
        await conn.execute(stmt, prepare=False)
