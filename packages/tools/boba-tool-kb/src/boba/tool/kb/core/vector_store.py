"""
PostgresVectorStore — реализация boba.indexing.VectorStore + CollectionsAdmin
поверх postgres + pgvector.

Один класс реализует 4 ABC:
- VectorStoreReader[str]:   get_by_ids / similarity_search / peek / find
- VectorStoreWriter[str]:   upsert / delete / update_metadata
- CollectionsAdminReader:   list_collections / collection_info
- CollectionsAdminWriter:   ensure_collection / delete_collection

Хранение:
- Все коллекции в одной таблице `kb_chunks`, разделение по колонке
  `collection`.
- Системные поля (source_id, chunk_index, content_hash) — отдельные колонки
  таблицы. Тэги — `text[]`. Остальная metadata — `jsonb`.
- Embedding — `vector` (без фиксированной dim в столбце); HNSW-индекс
  per-dim создаётся оператором заранее через CLI
  `boba.tool.kb.core.cli.bootstrap`. Store предполагает, что схема и
  индекс уже на месте — runtime DDL не делает.

Similarity-search здесь — чистый vector (cosine, `<=>`). Гибридный
search (vector + FTS, RRF) живёт в `PostgresKnowledgeBase.search` и
не относится к ABC.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from itertools import islice
from typing import Any, ClassVar

from psycopg import sql
from psycopg.rows import dict_row

from boba.db.postgres import PostgresPool
from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary
from boba.indexing.content_hash import StringContentHash
from boba.indexing.context import CollectionId
from boba.indexing.embedder import Embedder
from boba.indexing.filter import (
    And,
    Eq,
    Filter,
    Gt,
    Gte,
    HasAllTags,
    HasAnyTag,
    HasTag,
    In,
    Lt,
    Lte,
    Ne,
    Not,
    NotIn,
    Or,
    UnsupportedFilterError,
)
from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId
from boba.indexing.vector_store import (
    CollectionInfo,
    CollectionsAdminReader,
    CollectionsAdminWriter,
    SearchHit,
    VectorStoreReader,
    VectorStoreWriter,
)
logger = logging.getLogger(__name__)

__all__ = ["PostgresVectorStore"]


class PostgresVectorStore(
    VectorStoreReader[str],
    VectorStoreWriter[str],
    CollectionsAdminReader,
    CollectionsAdminWriter,
):
    """Postgres-реализация VectorStore[str] и CollectionsAdmin."""

    DEFAULT_BATCH_SIZE: ClassVar[int] = 100
    BACKEND_NAME: ClassVar[str] = "postgres"

    # Колонки-системники: на них завязан admin и schema.sql. Если меняешь
    # имя — синхронизируй с 001_init.sql и `_columns_select`.
    _SYSTEM_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"chunk_id", "collection", "source_id", "chunk_index", "content_hash"},
    )

    def __init__(
        self,
        pool: PostgresPool,
        embedder: Embedder[str],
        *,
        embedding_dim: int,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._pool = pool
        self._embedder = embedder
        self._embedding_dim = embedding_dim
        self._batch_size = batch_size

    # ------------------------------------------------------------------ #
    # VectorStoreReader[str]                                             #
    # ------------------------------------------------------------------ #

    def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Iterable[Chunk[str]]:
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return

        with (
            self._pool.connection() as conn,
            conn.cursor(
                row_factory=dict_row,
            ) as cur,
        ):
            cur.execute(
                """
                SELECT chunk_id, source_id, chunk_index, content_hash,
                       raw_content, format_content, metadata, tags
                FROM kb_chunks
                WHERE collection = %s AND chunk_id = ANY(%s)
                """,
                (str(collection), ids),
            )
            for row in cur:
                yield self._row_to_chunk(row)

    def similarity_search(
        self,
        collection: CollectionId,
        *,
        query: str,
        k: int,
    ) -> Iterable[SearchHit[str]]:
        embedding = list(self._embedder.embed_query(query))
        # pgvector требует `vector(N)` с литералом N (type modifier);
        # параметризовать $1 нельзя — Postgres парсит type modifiers до
        # bind'а. Инлайним через sql.Literal (значение из cfg, не из юзера).
        query_sql = sql.SQL(
            """
            SELECT chunk_id, source_id, chunk_index,
                   format_content AS snippet,
                   metadata, tags,
                   (embedding::vector({dim})) <=> %s::vector AS distance
            FROM kb_chunks
            WHERE collection = %s AND embedding IS NOT NULL
            ORDER BY distance ASC
            LIMIT %s
            """,
        ).format(dim=sql.Literal(self._embedding_dim))
        with (
            self._pool.connection() as conn,
            conn.cursor(
                row_factory=dict_row,
            ) as cur,
        ):
            cur.execute(query_sql, (embedding, str(collection), k))
            for row in cur:
                yield SearchHit(
                    chunk_id=ChunkId(row["chunk_id"]),
                    distance=float(row["distance"]),
                    snippet=row["snippet"] or "",
                    metadata=self._row_to_metadata(row),
                )

    def peek(
        self,
        collection: CollectionId,
        *,
        source_id: SourceId | None,
        limit: int,
    ) -> Iterable[ChunkSummary[str]]:
        with (
            self._pool.connection() as conn,
            conn.cursor(
                row_factory=dict_row,
            ) as cur,
        ):
            if source_id is None:
                cur.execute(
                    """
                    SELECT chunk_id, source_id, chunk_index,
                           format_content AS snippet, metadata, tags
                    FROM kb_chunks
                    WHERE collection = %s
                    ORDER BY source_id, chunk_index
                    LIMIT %s
                    """,
                    (str(collection), limit),
                )
            else:
                cur.execute(
                    """
                    SELECT chunk_id, source_id, chunk_index,
                           format_content AS snippet, metadata, tags
                    FROM kb_chunks
                    WHERE collection = %s AND source_id = %s
                    ORDER BY chunk_index
                    LIMIT %s
                    """,
                    (str(collection), str(source_id), limit),
                )
            for row in cur:
                yield self._row_to_summary(row)

    def find(
        self,
        collection: CollectionId,
        *,
        where: Filter | None,
        limit: int | None = None,
    ) -> Iterable[ChunkSummary[str]]:
        where_sql, params = self._compile_filter(where)
        clauses: list[sql.Composable] = [sql.SQL("collection = %s")]
        bind_params: list[Any] = [str(collection)]
        if where_sql is not None:
            clauses.append(where_sql)
            bind_params.extend(params)
        where_clause = sql.SQL(" AND ").join(clauses)
        query = sql.SQL(
            """
            SELECT chunk_id, source_id, chunk_index,
                   format_content AS snippet, metadata, tags
            FROM kb_chunks
            WHERE {where}
            ORDER BY source_id, chunk_index
            """,
        ).format(where=where_clause)
        if limit is not None:
            query = sql.SQL("{q} LIMIT {lim}").format(
                q=query,
                lim=sql.Literal(limit),
            )
        with (
            self._pool.connection() as conn,
            conn.cursor(
                row_factory=dict_row,
            ) as cur,
        ):
            cur.execute(query, bind_params)
            for row in cur:
                yield self._row_to_summary(row)

    # ------------------------------------------------------------------ #
    # VectorStoreWriter[str]                                             #
    # ------------------------------------------------------------------ #

    def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[Chunk[str]],
    ) -> None:
        for batch in self._batched(chunks):
            documents = [c.format_content for c in batch]
            embeddings = list(self._embedder.embed_documents(documents))
            if len(embeddings) != len(batch):
                msg = (
                    f"embedder returned {len(embeddings)} vectors "
                    f"for {len(batch)} chunks"
                )
                raise RuntimeError(msg)
            rows: list[tuple[Any, ...]] = []
            for c, embedding in zip(batch, embeddings, strict=True):
                rows.append(
                    (
                        str(c.chunk_id),
                        str(collection),
                        str(c.source_id),
                        c.chunk_index,
                        (
                            c.content_hash.to_wire()
                            if c.content_hash is not None
                            else ""
                        ),
                        c.raw_content,
                        c.format_content,
                        list(embedding),
                        json.dumps(dict(c.metadata.to_wire())),
                        sorted(c.tags),
                    ),
                )
            with (
                self._pool.connection() as conn,
                conn.transaction(), conn.cursor() as cur,
            ):
                    cur.executemany(
                        """
                        INSERT INTO kb_chunks (
                            chunk_id, collection, source_id, chunk_index,
                            content_hash, raw_content, format_content,
                            embedding, metadata, tags, updated_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s::vector, %s::jsonb, %s, now()
                        )
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            collection     = EXCLUDED.collection,
                            source_id      = EXCLUDED.source_id,
                            chunk_index    = EXCLUDED.chunk_index,
                            content_hash   = EXCLUDED.content_hash,
                            raw_content    = EXCLUDED.raw_content,
                            format_content = EXCLUDED.format_content,
                            embedding      = EXCLUDED.embedding,
                            metadata       = EXCLUDED.metadata,
                            tags           = EXCLUDED.tags,
                            updated_at     = now()
                        """,
                        rows,
                    )

    def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM kb_chunks WHERE collection = %s AND chunk_id = ANY(%s)",
                (str(collection), ids),
            )

    def update_metadata(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
        patch: Mapping[str, str | int | float | bool],
    ) -> None:
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return
        # Patch-семантика: merge через jsonb `||`. Только ключи из patch
        # перезаписываются, остальные значения metadata остаются. Embedding
        # и content не трогаем (контракт ABC.update_metadata).
        wire_patch = {k: str(v) for k, v in patch.items()}
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kb_chunks
                SET metadata = metadata || %s::jsonb,
                    updated_at = now()
                WHERE collection = %s AND chunk_id = ANY(%s)
                """,
                (json.dumps(wire_patch), str(collection), ids),
            )

    # ------------------------------------------------------------------ #
    # CollectionsAdminReader                                             #
    # ------------------------------------------------------------------ #

    def list_collections(self) -> Iterable[CollectionInfo]:
        with (
            self._pool.connection() as conn,
            conn.cursor(
                row_factory=dict_row,
            ) as cur,
        ):
            cur.execute(
                """
                SELECT c.name,
                       c.description,
                       COALESCE(cnt.count, 0) AS count
                FROM kb_collections c
                LEFT JOIN (
                    SELECT collection, count(*)::int AS count
                    FROM kb_chunks
                    GROUP BY collection
                ) cnt ON cnt.collection = c.name
                ORDER BY c.name
                """,
            )
            for row in cur:
                yield CollectionInfo(
                    name=CollectionId(row["name"]),
                    description=row["description"] or "",
                    count=int(row["count"]),
                )

    def collection_info(self, name: CollectionId) -> CollectionInfo:
        with (
            self._pool.connection() as conn,
            conn.cursor(
                row_factory=dict_row,
            ) as cur,
        ):
            cur.execute(
                """
                SELECT c.name, c.description,
                       (SELECT count(*)::int FROM kb_chunks
                        WHERE collection = c.name) AS count
                FROM kb_collections c
                WHERE c.name = %s
                """,
                (str(name),),
            )
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

    # ------------------------------------------------------------------ #
    # CollectionsAdminWriter                                             #
    # ------------------------------------------------------------------ #

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
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kb_collections (name, description)
                VALUES (%s, %s)
                ON CONFLICT (name) DO NOTHING
                """,
                (str(name), description or ""),
            )

    def delete_collection(self, name: CollectionId) -> None:
        with (
            self._pool.connection() as conn,
            conn.transaction(), conn.cursor() as cur,
        ):
            cur.execute(
                "DELETE FROM kb_chunks WHERE collection = %s",
                (str(name),),
            )
            cur.execute(
                "DELETE FROM kb_collections WHERE name = %s",
                (str(name),),
            )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _row_to_chunk(self, row: Mapping[str, Any]) -> Chunk[str]:
        content_hash_wire = row.get("content_hash") or ""
        return Chunk(
            chunk_id=ChunkId(row["chunk_id"]),
            source_id=SourceId(row["source_id"]),
            format_content=row["format_content"] or "",
            raw_content=row["raw_content"] or "",
            chunk_index=int(row["chunk_index"]),
            content_hash=(
                StringContentHash(text=content_hash_wire) if content_hash_wire else None
            ),
            metadata=self._row_to_metadata(row),
            tags=frozenset(row.get("tags") or ()),
        )

    def _row_to_summary(self, row: Mapping[str, Any]) -> ChunkSummary[str]:
        return ChunkSummary(
            chunk_id=ChunkId(row["chunk_id"]),
            source_id=SourceId(row["source_id"]),
            snippet=row.get("snippet") or "",
            chunk_index=int(row["chunk_index"]),
            metadata=self._row_to_metadata(row),
            tags=frozenset(row.get("tags") or ()),
        )

    @staticmethod
    def _row_to_metadata(row: Mapping[str, Any]) -> Metadata:
        raw = row.get("metadata") or {}
        if not isinstance(raw, dict):
            return Metadata.empty()
        wire: dict[str, str] = {str(k): str(v) for k, v in raw.items() if v is not None}
        return Metadata.from_wire(wire)

    def _batched(
        self,
        chunks: Iterable[Chunk[str]],
    ) -> Iterable[list[Chunk[str]]]:
        it = iter(chunks)
        while True:
            batch = list(islice(it, self._batch_size))
            if not batch:
                return
            yield batch

    # ------------------------------------------------------------------ #
    # Filter → SQL                                                        #
    # ------------------------------------------------------------------ #

    @classmethod
    def _compile_filter(
        cls,
        f: Filter | None,
    ) -> tuple[sql.Composable | None, list[Any]]:
        if f is None:
            return None, []
        params: list[Any] = []
        composed = cls._filter_to_sql(f, params)
        return composed, params

    @classmethod
    def _filter_to_sql(  # noqa: C901, PLR0911, PLR0912
        cls,
        f: Filter,
        params: list[Any],
    ) -> sql.Composable:
        if isinstance(f, Eq):
            return cls._cmp_sql(f.field, "=", f.value, params)
        if isinstance(f, Ne):
            return cls._cmp_sql(f.field, "<>", f.value, params)
        if isinstance(f, Lt):
            return cls._cmp_sql(f.field, "<", f.value, params)
        if isinstance(f, Lte):
            return cls._cmp_sql(f.field, "<=", f.value, params)
        if isinstance(f, Gt):
            return cls._cmp_sql(f.field, ">", f.value, params)
        if isinstance(f, Gte):
            return cls._cmp_sql(f.field, ">=", f.value, params)
        if isinstance(f, In):
            return cls._cmp_in_sql(f.field, list(f.values), invert=False, params=params)
        if isinstance(f, NotIn):
            return cls._cmp_in_sql(f.field, list(f.values), invert=True, params=params)
        if isinstance(f, HasTag):
            params.append(f.tag)
            return sql.SQL("(%s = ANY(tags))")
        if isinstance(f, HasAnyTag):
            if not f.tags:
                raise UnsupportedFilterError(
                    f,
                    cls.BACKEND_NAME,
                    "пустой список тэгов в HasAnyTag",
                )
            params.append(list(f.tags))
            return sql.SQL("(tags && %s)")
        if isinstance(f, HasAllTags):
            if not f.tags:
                raise UnsupportedFilterError(
                    f,
                    cls.BACKEND_NAME,
                    "пустой список тэгов в HasAllTags",
                )
            params.append(list(f.tags))
            return sql.SQL("(tags @> %s)")
        if isinstance(f, And):
            if not f.filters:
                raise UnsupportedFilterError(f, cls.BACKEND_NAME, "empty And")
            if len(f.filters) == 1:
                return cls._filter_to_sql(f.filters[0], params)
            parts = [cls._filter_to_sql(s, params) for s in f.filters]
            return sql.SQL("(") + sql.SQL(" AND ").join(parts) + sql.SQL(")")
        if isinstance(f, Or):
            if not f.filters:
                raise UnsupportedFilterError(f, cls.BACKEND_NAME, "empty Or")
            if len(f.filters) == 1:
                return cls._filter_to_sql(f.filters[0], params)
            parts = [cls._filter_to_sql(s, params) for s in f.filters]
            return sql.SQL("(") + sql.SQL(" OR ").join(parts) + sql.SQL(")")
        if isinstance(f, Not):
            inner = cls._filter_to_sql(f.filter, params)
            return sql.SQL("(NOT ") + inner + sql.SQL(")")
        raise UnsupportedFilterError(
            f,
            cls.BACKEND_NAME,
            f"unknown filter type {type(f).__name__}",
        )

    @classmethod
    def _field_expr(cls, field: str) -> sql.Composable:
        """SQL-выражение для поля: системник → колонка, иначе jsonb-путь.

        Numeric/bool сравнения для metadata подразумевают cast (`::numeric`,
        `::boolean`) уже на стороне callee'а — здесь возвращаем строковое
        значение `metadata->>'key'`.
        """
        if field in cls._SYSTEM_FIELDS:
            return sql.Identifier(field)
        return sql.SQL("metadata->>{key}").format(key=sql.Literal(field))

    # Whitelist операторов сравнения, чтобы не собирать `sql.SQL(f"...")`
    # из не-literal-параметра (psycopg запрещает: prevents SQL injection).
    _NUMERIC_OPS: ClassVar[dict[str, sql.SQL]] = {
        "<": sql.SQL("<"),
        "<=": sql.SQL("<="),
        ">": sql.SQL(">"),
        ">=": sql.SQL(">="),
    }
    _EQUAL_OPS: ClassVar[dict[str, sql.SQL]] = {
        "=": sql.SQL("="),
        "<>": sql.SQL("<>"),
    }

    @classmethod
    def _cmp_sql(
        cls,
        field: str,
        op: str,
        value: Any,
        params: list[Any],
    ) -> sql.Composable:
        expr = cls._field_expr(field)
        params.append(value)
        # Для numeric сравнений (<, <=, >, >=) кастуем оба операнда в numeric
        # — это поддерживает фильтр над jsonb-числами, хранящимися как
        # строки в `metadata->>`. Системные колонки уже numeric/text — cast
        # для них либо no-op, либо корректный («10»::numeric → 10).
        if op in cls._NUMERIC_OPS:
            return (
                sql.SQL("((")
                + expr
                + sql.SQL(")::numeric ")
                + cls._NUMERIC_OPS[op]
                + sql.SQL(" %s::numeric)")
            )
        if op in cls._EQUAL_OPS:
            return (
                sql.SQL("(")
                + expr
                + sql.SQL(" ")
                + cls._EQUAL_OPS[op]
                + sql.SQL(" %s)")
            )
        msg = f"_cmp_sql: unknown op {op!r}"
        raise ValueError(msg)

    @classmethod
    def _cmp_in_sql(
        cls,
        field: str,
        values: list[Any],
        *,
        invert: bool,
        params: list[Any],
    ) -> sql.Composable:
        if not values:
            # Пустой IN — всегда false; пустой NOT IN — всегда true.
            return sql.SQL("FALSE") if not invert else sql.SQL("TRUE")
        expr = cls._field_expr(field)
        params.append(values)
        op = sql.SQL("<> ALL") if invert else sql.SQL("= ANY")
        return sql.SQL("(") + expr + sql.SQL(" ") + op + sql.SQL("(%s))")
