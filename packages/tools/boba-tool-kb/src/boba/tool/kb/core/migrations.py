"""Bootstrap-миграции схемы KB-БД: Migrations.apply_bootstrap + ensure_vector_index.

Обе операции — DDL-only, вызываются **одноразово** оператором через
bootstrap-CLI. Runtime'у схема считается данностью; bootstrap-CLI —
отдельный шаг оператора.

apply_bootstrap применяет все migrations/*.sql в лексикографическом
порядке. Каждый SQL — psycopg.sql.SQL(...) шаблон с плейсхолдерами
({schema}/{chunks_table}/{collections_table}/{*_lit}/{chunks_*_idx_name}),
значения подставляются из PostgresStoreSchema — тот же конфиг, который
получают PostgresChunkStore и PostgresKnowledgeBase. Каждый SQL ОБЯЗАН
быть идемпотентным.

ensure_vector_index создаёт HNSW-индекс на выражение embedding::vector(N)
(pgvector не индексирует vector-колонку без фиксированной размерности). Один
индекс = одна dim; имя индекса включает chunks_table и dim, чтобы
несколько dim'ов (и таблиц) сосуществовали в одной схеме.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar, LiteralString, cast

from psycopg import sql

from boba.tool.kb.core.postgres import PostgresStoreSchema

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
        """Подставить идентификаторы / литералы из schema_cfg в SQL-шаблон.

        sql.SQL тайпится LiteralString-only (защита от str-конкатенации
        user-input'а в SQL). Здесь источник — статический файл миграции из
        собственного пакета (migrations/*.sql), а не user-input -> cast
        безопасен. Параметры подстановки — Identifier/Literal из validated
        ChunkStoreSchemaConfig, sql-injection невозможен.
        """
        chunks_name = schema_cfg.chunks_table
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
        )

    @staticmethod
    def apply_bootstrap(
        conn: Any,
        *,
        schema_cfg: PostgresStoreSchema,
    ) -> None:
        """Применить все миграции из migrations/*.sql в alphabetical order.

        Args:
            conn: psycopg-совместимый Connection (sync). Будет вызван
                conn.execute(sql) для каждого файла.
            schema_cfg: схема + имена таблиц (тот же конфиг, который потом
                получит PostgresChunkStore).
        """
        migrations_dir = Migrations._MIGRATIONS_DIR
        if not migrations_dir.is_dir():
            msg = f"migrations dir not found: {migrations_dir}"
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
            conn.execute(rendered)

    @staticmethod
    def ensure_vector_index(
        conn: Any,
        *,
        dim: int,
        schema_cfg: PostgresStoreSchema,
    ) -> None:
        """Создать HNSW-индекс по (embedding::vector(dim)) если ещё нет.

        pgvector не индексирует колонку типа vector без фиксированной
        размерности — индекс ставится на expression embedding::vector(N).
        Поэтому один индекс = одна dim. Имя индекса —
        <chunks_table>_embedding_hnsw_<dim>; уникально и при смене dim, и при
        смене таблицы.

        Args:
            conn: psycopg sync Connection.
            dim: размерность вектора (= embedder.dim()).
            schema_cfg: schema + имена таблиц (для chunks_table и schema).
        """
        if dim <= 0:
            msg = f"ensure_vector_index: dim must be positive, got {dim}"
            raise ValueError(msg)
        index_name = f"{schema_cfg.chunks_table}_embedding_hnsw_{dim}"
        # opclass vector_cosine_ops — cosine distance (<=>). Если хочется
        # L2 (<->) — поменять на vector_l2_ops и пересоздать индекс.
        stmt = sql.SQL(
            "CREATE INDEX IF NOT EXISTS {index_name} "
            "ON {chunks_table} USING hnsw "
            "((embedding::vector({dim})) vector_cosine_ops)",
        ).format(
            index_name=sql.Identifier(index_name),
            chunks_table=schema_cfg.chunks_ident(),
            dim=sql.Literal(dim),
        )
        conn.execute(stmt)
