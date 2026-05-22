"""
PostgresChunkStore — реализация `boba.indexing.ChunkStore[str]` поверх
postgres + pgvector.

Чисто chunk-уровневые операции (документы внутри коллекции):
- read:  get_by_ids / peek / find / diff_by_hash
- write: upsert / delete / update_metadata

Коллекция-уровневый CRUD (`ensure_collection` / `delete_collection` /
`list_collections` / `collection_info`) живёт отдельно в
[`PostgresCollectionsStore`](collections_store.py) — это другая ось ABC.

Хранение:
- Все коллекции в одной таблице (по дефолту `kb_chunks`, имя/схема —
  через `VectorStoreSchemaConfig` [tool.kb.vector_store]). Разделение по
  колонке `collection`.
- Системные поля (source_id, chunk_index, content_hash) — отдельные колонки
  таблицы. Тэги — `text[]`. Остальная metadata — `jsonb`.
- Embedding — `vector` (без фиксированной dim в столбце); HNSW-индекс
  per-dim создаётся оператором заранее через CLI
  `boba.tool.kb.core.cli.bootstrap`. Store предполагает, что схема и
  индекс уже на месте — runtime DDL не делает.

Embedder в Store не инжектится: store принимает `EmbeddedChunk[str]` на
write. Embedder живёт в pipeline-orchestrator'е выше
(`CollectionScopedView`, `PostgresKnowledgeBase`).

Vector-search (cosine `<=>`) и гибридный search (vector + FTS, RRF) живут
в `PostgresKnowledgeBase` — application-уровень, не часть ABC Store.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from itertools import islice
from typing import Any, ClassVar, TypeVar

from psycopg import sql
from psycopg.rows import dict_row

from boba.db.postgres import PostgresPool
from boba.indexing.chunk_store import ChunkStore, HashDiff
from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary, EmbeddedChunk
from boba.indexing.content_hash import ContentHash, StringContentHash
from boba.indexing.context import CollectionId
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
from boba.tool.kb.core.vector_store_config import VectorStoreSchemaConfig

logger = logging.getLogger(__name__)

__all__ = ["PostgresChunkStore"]

_E = TypeVar("_E")


class PostgresChunkStore(ChunkStore[str]):
    """Postgres-реализация `ChunkStore[str]` (только document-уровень)."""

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
        *,
        embedding_dim: int,
        schema_cfg: VectorStoreSchemaConfig,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._pool = pool
        self._embedding_dim = embedding_dim
        self._schema_cfg = schema_cfg
        self._chunks_table = schema_cfg.chunks_ident()
        self._batch_size = batch_size

    # ------------------------------------------------------------------ #
    # ChunkStore[str] — read-side                                         #
    # ------------------------------------------------------------------ #

    def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Iterable[Chunk[str]]:
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return

        query = sql.SQL(
            """
            SELECT chunk_id, source_id, chunk_index, content_hash,
                   raw_content, format_content, metadata, tags
            FROM {chunks_table}
            WHERE collection = %s AND chunk_id = ANY(%s)
            """,
        ).format(chunks_table=self._chunks_table)
        with (
            self._pool.connection() as conn,
            conn.cursor(
                row_factory=dict_row,
            ) as cur,
        ):
            cur.execute(query, (str(collection), ids))
            for row in cur:
                yield self._row_to_chunk(row)

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
                query = sql.SQL(
                    """
                    SELECT chunk_id, source_id, chunk_index,
                           format_content AS snippet, metadata, tags
                    FROM {chunks_table}
                    WHERE collection = %s
                    ORDER BY source_id, chunk_index
                    LIMIT %s
                    """,
                ).format(chunks_table=self._chunks_table)
                cur.execute(query, (str(collection), limit))
            else:
                query = sql.SQL(
                    """
                    SELECT chunk_id, source_id, chunk_index,
                           format_content AS snippet, metadata, tags
                    FROM {chunks_table}
                    WHERE collection = %s AND source_id = %s
                    ORDER BY chunk_index
                    LIMIT %s
                    """,
                ).format(chunks_table=self._chunks_table)
                cur.execute(query, (str(collection), str(source_id), limit))
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
            FROM {chunks_table}
            WHERE {where}
            ORDER BY source_id, chunk_index
            """,
        ).format(chunks_table=self._chunks_table, where=where_clause)
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

    def diff_by_hash(
        self,
        collection: CollectionId,
        candidates: Iterable[tuple[ChunkId, ContentHash]],
    ) -> HashDiff:
        # Сохраняем порядок кандидатов; SELECT возвращает только
        # (chunk_id, content_hash) — без payload и embedding'а.
        items: list[tuple[ChunkId, ContentHash]] = list(candidates)
        if not items:
            return HashDiff(to_upsert=[], unchanged=[])

        ids = [str(cid) for cid, _ in items]
        query = sql.SQL(
            """
            SELECT chunk_id, content_hash
            FROM {chunks_table}
            WHERE collection = %s AND chunk_id = ANY(%s)
            """,
        ).format(chunks_table=self._chunks_table)
        with (
            self._pool.connection() as conn,
            conn.cursor() as cur,
        ):
            cur.execute(query, (str(collection), ids))
            stored: dict[str, str] = {row[0]: row[1] for row in cur}

        to_upsert: list[ChunkId] = []
        unchanged: list[ChunkId] = []
        for chunk_id, candidate_hash in items:
            stored_wire = stored.get(str(chunk_id))
            if stored_wire is None:
                to_upsert.append(chunk_id)
            elif stored_wire == candidate_hash.to_wire():
                unchanged.append(chunk_id)
            else:
                to_upsert.append(chunk_id)

        return HashDiff(
            to_upsert=to_upsert,
            unchanged=unchanged,
        )

    # ------------------------------------------------------------------ #
    # ChunkStore[str] — write-side                                        #
    # ------------------------------------------------------------------ #

    def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[EmbeddedChunk[str]],
    ) -> None:
        upsert_sql = sql.SQL(
            """
            INSERT INTO {chunks_table} (
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
        ).format(chunks_table=self._chunks_table)
        for batch in self._batched(chunks):
            rows: list[tuple[Any, ...]] = []
            for ec in batch:
                rows.append(
                    (
                        str(ec.chunk_id),
                        str(collection),
                        str(ec.source_id),
                        ec.chunk_index,
                        ec.content_hash.to_wire(),
                        ec.raw_content,
                        ec.format_content,
                        list(ec.embedding),
                        json.dumps(dict(ec.metadata.to_wire())),
                        sorted(ec.tags),
                    ),
                )
            with (
                self._pool.connection() as conn,
                conn.transaction(),
                conn.cursor() as cur,
            ):
                cur.executemany(upsert_sql, rows)

    def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return
        query = sql.SQL(
            "DELETE FROM {chunks_table} WHERE collection = %s AND chunk_id = ANY(%s)",
        ).format(chunks_table=self._chunks_table)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(query, (str(collection), ids))

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
        query = sql.SQL(
            """
            UPDATE {chunks_table}
            SET metadata = metadata || %s::jsonb,
                updated_at = now()
            WHERE collection = %s AND chunk_id = ANY(%s)
            """,
        ).format(chunks_table=self._chunks_table)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(query, (json.dumps(wire_patch), str(collection), ids))

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _row_to_chunk(self, row: Mapping[str, Any]) -> Chunk[str]:
        return Chunk(
            chunk_id=ChunkId(row["chunk_id"]),
            source_id=SourceId(row["source_id"]),
            format_content=row["format_content"] or "",
            raw_content=row["raw_content"] or "",
            chunk_index=int(row["chunk_index"]),
            content_hash=StringContentHash(text=row["content_hash"]),
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
        items: Iterable[_E],
    ) -> Iterable[list[_E]]:
        it = iter(items)
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
