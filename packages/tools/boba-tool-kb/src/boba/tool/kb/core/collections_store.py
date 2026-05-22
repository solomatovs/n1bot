"""
PostgresCollectionsStore — реализация `boba.indexing.CollectionsStore`
поверх postgres + pgvector.

Коллекция-уровневый CRUD:
- read:  list_collections / collection_info
- write: ensure_collection / delete_collection

Document-уровневые операции с чанками (`upsert`, `find`, `diff_by_hash`
и т.п.) живут отдельно в [`PostgresChunkStore`](chunk_store.py) — это
другая ось ABC.

`ensure_collection`/`delete_collection` идемпотентны: `ON CONFLICT DO NOTHING`
на insert и DELETE существующих/несуществующих имён без ошибки.
`delete_collection` сносит и связанные чанки, и запись в каталоге в одной
транзакции, чтобы не оставлять висящих чанков с FK-стороны.

`VectorStoreSchemaConfig` — единый источник правды по именам таблиц,
который получает и bootstrap-CLI, и этот store. Любое расхождение
приведёт к `relation does not exist` в рантайме — лучше так, чем тихое
расхождение схемы.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from psycopg import sql
from psycopg.rows import dict_row

from boba.db.postgres import PostgresPool
from boba.indexing.chunk_store import CollectionInfo, CollectionsStore
from boba.indexing.context import CollectionId
from boba.tool.kb.core.vector_store_config import VectorStoreSchemaConfig

logger = logging.getLogger(__name__)

__all__ = ["PostgresCollectionsStore"]


class PostgresCollectionsStore(CollectionsStore):
    """Postgres-реализация `CollectionsStore` (только collection-уровень)."""

    def __init__(
        self,
        pool: PostgresPool,
        *,
        schema_cfg: VectorStoreSchemaConfig,
    ) -> None:
        self._pool = pool
        self._schema_cfg = schema_cfg
        self._chunks_table = schema_cfg.chunks_ident()
        self._collections_table = schema_cfg.collections_ident()

    def list_collections(self) -> Iterable[CollectionInfo]:
        query = sql.SQL(
            """
            SELECT c.name,
                   c.description,
                   COALESCE(cnt.count, 0) AS count
            FROM {collections_table} c
            LEFT JOIN (
                SELECT collection, count(*)::int AS count
                FROM {chunks_table}
                GROUP BY collection
            ) cnt ON cnt.collection = c.name
            ORDER BY c.name
            """,
        ).format(
            collections_table=self._collections_table,
            chunks_table=self._chunks_table,
        )
        with (
            self._pool.connection() as conn,
            conn.cursor(
                row_factory=dict_row,
            ) as cur,
        ):
            cur.execute(query)
            for row in cur:
                yield CollectionInfo(
                    name=CollectionId(row["name"]),
                    description=row["description"] or "",
                    count=int(row["count"]),
                )

    def collection_info(self, name: CollectionId) -> CollectionInfo:
        query = sql.SQL(
            """
            SELECT c.name, c.description,
                   (SELECT count(*)::int FROM {chunks_table}
                    WHERE collection = c.name) AS count
            FROM {collections_table} c
            WHERE c.name = %s
            """,
        ).format(
            chunks_table=self._chunks_table,
            collections_table=self._collections_table,
        )
        with (
            self._pool.connection() as conn,
            conn.cursor(
                row_factory=dict_row,
            ) as cur,
        ):
            cur.execute(query, (str(name),))
            row = cur.fetchone()
            if row is None:
                return CollectionInfo(
                    name=name,
                    description="",
                    count=0,
                )
            return CollectionInfo(
                name=CollectionId(row["name"]),
                description=row["description"] or "",
                count=int(row["count"]),
            )

    def ensure_collection(
        self,
        name: CollectionId,
        *,
        description: str | None,
    ) -> None:
        # Семантика «idempotent ensure»: если коллекция есть — НЕ
        # перетираем description (тестируется в chromadb-аналоге, держим
        # совместимым поведение). Description ставится только при первом
        # создании.
        query = sql.SQL(
            """
            INSERT INTO {collections_table} (name, description)
            VALUES (%s, %s)
            ON CONFLICT (name) DO NOTHING
            """,
        ).format(collections_table=self._collections_table)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(query, (str(name), description or ""))

    def delete_collection(self, name: CollectionId) -> None:
        delete_chunks = sql.SQL(
            "DELETE FROM {chunks_table} WHERE collection = %s",
        ).format(chunks_table=self._chunks_table)
        delete_catalog = sql.SQL(
            "DELETE FROM {collections_table} WHERE name = %s",
        ).format(collections_table=self._collections_table)
        with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            cur.execute(delete_chunks, (str(name),))
            cur.execute(delete_catalog, (str(name),))
