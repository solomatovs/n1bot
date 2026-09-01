"""KB-store поверх postgres+pgvector; схему создаёт bootstrap-CLI, runtime DDL не
делает.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from itertools import islice
from typing import Any, ClassVar, TypeVar

from psycopg import sql

from boba.db.postgres.profile import PostgresConfig
from boba.db.pgvector.config import PostgresStoreConfig, PostgresStoreSchema
from boba.db.postgres import AsyncPostgresPool, CancellablePool
from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary, EmbeddedChunk
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
from boba.indexing.sections import SourceId
from boba.indexing.store import (
    ChunkStore,
    CollectionInfo,
    CollectionsStore,
    HashDiff,
)
from boba.indexing.values import CollectionId, ContentHash, Metadata, StringContentHash
from pgvector.psycopg import register_vector_async

logger = logging.getLogger(__name__)

__all__ = [
    "KbPool",
    "PostgresChunkStore",
    "PostgresCollectionsStore",
    "PostgresStoreConfig",
    "PostgresStoreSchema",
]

_E = TypeVar("_E")


class KbPool:
    """Пул-singleton по конфигу с register_vector: без него INSERT vector падает."""

    @staticmethod
    async def open(connection: PostgresConfig) -> CancellablePool:
        pool = await AsyncPostgresPool.get(
            connection,
            configure=register_vector_async,
        )
        return CancellablePool(pool)


class PostgresChunkStore(ChunkStore[str]):
    """Postgres-реализация ChunkStore[str]"""

    _SYSTEM_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"chunk_id", "collection", "source_id", "chunk_index", "content_hash"},
    )

    def __init__(
        self,
        *,
        cfg: PostgresStoreConfig,
    ) -> None:
        self._cfg = cfg
        self._tables = cfg.tables
        self._pool_ref: CancellablePool | None = None

    async def _pool(self) -> CancellablePool:
        """Пул берётся при первом обращении: __init__ не может await."""
        if self._pool_ref is None:
            self._pool_ref = await KbPool.open(self._cfg.connection)
        return self._pool_ref

    async def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Sequence[Chunk[str]]:
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return []

        query = sql.SQL(
            """
            select
                chunk_id,
                source_id,
                chunk_index,
                content_hash,
                raw_content,
                format_content,
                metadata,
                tags
            from
                {chunks_table}
            where
                collection = %s
                and chunk_id = ANY(%s)
            """,
        ).format(chunks_table=self._tables.chunks_ident())

        pool = await self._pool()
        async with pool.dict_cursor() as cur:
            await cur.execute(query, (str(collection), ids))
            rows = await cur.fetchall()

        chunks: list[Chunk[str]] = []
        for row in rows:
            chunks.append(self._row_to_chunk(row))
        return chunks

    async def peek(
        self,
        collection: CollectionId,
        *,
        source_id: SourceId | None,
        limit: int,
    ) -> Sequence[ChunkSummary[str]]:
        pool = await self._pool()
        async with pool.dict_cursor() as cur:
            if source_id is None:
                query = sql.SQL(
                    """
                    select
                        chunk_id,
                        source_id,
                        chunk_index,
                        format_content as snippet,
                        metadata,
                        tags
                    from
                        {chunks_table}
                    where
                        collection = %s
                    order by
                        source_id,
                        chunk_index
                    limit
                        %s
                    """,
                ).format(chunks_table=self._tables.chunks_ident())
                await cur.execute(query, (str(collection), limit))
            else:
                query = sql.SQL(
                    """
                    select
                        chunk_id,
                        source_id,
                        chunk_index,
                        format_content as snippet,
                        metadata,
                        tags
                    from
                        {chunks_table}
                    where
                        collection = %s
                        and source_id = %s
                    order by
                        chunk_index
                    limit
                        %s
                    """,
                ).format(chunks_table=self._tables.chunks_ident())
                await cur.execute(query, (str(collection), str(source_id), limit))

            rows = await cur.fetchall()

        return self._to_summaries(rows)

    async def find(
        self,
        collection: CollectionId,
        *,
        where: Filter | None,
        limit: int | None = None,
    ) -> Sequence[ChunkSummary[str]]:
        where_sql, params = self._compile_filter(where)
        clauses: list[sql.Composable] = [sql.SQL("collection = %s")]
        bind_params: list[Any] = [str(collection)]
        if where_sql is not None:
            clauses.append(where_sql)
            bind_params.extend(params)
        where_clause = sql.SQL(" and ").join(clauses)
        query = sql.SQL(
            """
            select
                chunk_id,
                source_id,
                chunk_index,
                format_content as snippet,
                metadata,
                tags
            from
                {chunks_table}
            where
                {where}
            order by
                source_id,
                chunk_index
            """,
        ).format(chunks_table=self._tables.chunks_ident(), where=where_clause)

        if limit is not None:
            query = sql.SQL("{q} limit {lim}").format(
                q=query,
                lim=sql.Literal(limit),
            )

        pool = await self._pool()
        async with pool.dict_cursor() as cur:
            await cur.execute(query, bind_params)
            rows = await cur.fetchall()

        return self._to_summaries(rows)

    async def diff_by_hash(
        self,
        collection: CollectionId,
        candidates: Iterable[tuple[ChunkId, ContentHash]],
    ) -> HashDiff:
        items: list[tuple[ChunkId, ContentHash]] = list(candidates)
        if not items:
            return HashDiff(to_upsert=[], unchanged=[])

        ids = [str(cid) for cid, _ in items]
        query = sql.SQL(
            """
            select
                chunk_id,
                content_hash
            from
                {chunks_table}
            where 1=1
                and collection = %s
                and chunk_id = ANY(%s)
            """,
        ).format(chunks_table=self._tables.chunks_ident())
        pool = await self._pool()
        async with pool.cursor() as cur:
            await cur.execute(query, (str(collection), ids))
            fetched = await cur.fetchall()

        stored: dict[str, str] = {}
        for row in fetched:
            stored[row[0]] = row[1]

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

    async def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[EmbeddedChunk[str]],
    ) -> None:
        upsert_sql = sql.SQL(
            """
            insert into {chunks_table} (
                chunk_id,
                collection,
                source_id,
                chunk_index,
                content_hash,
                raw_content,
                format_content,
                embedding,
                metadata,
                tags,
                updated_at
            )
            values (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s::vector,
                %s::jsonb,
                %s,
                now()
            )
            on conflict (chunk_id) do update set
                collection     = excluded.collection,
                source_id      = excluded.source_id,
                chunk_index    = excluded.chunk_index,
                content_hash   = excluded.content_hash,
                raw_content    = excluded.raw_content,
                format_content = excluded.format_content,
                embedding      = excluded.embedding,
                metadata       = excluded.metadata,
                tags           = excluded.tags,
                updated_at     = now()
            """,
        ).format(chunks_table=self._tables.chunks_ident())

        for batch in self._batched(chunks):
            rows = [
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
                )
                for ec in batch
            ]

            pool = await self._pool()
            async with pool.cursor() as cur:
                await cur.executemany(upsert_sql, rows)

    async def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return
        query = sql.SQL(
            """
            delete from
                {chunks_table}
            where 1=1
                and collection = %s
                and chunk_id = ANY(%s)
            """,
        ).format(chunks_table=self._tables.chunks_ident())
        pool = await self._pool()
        async with pool.cursor() as cur:
            await cur.execute(query, (str(collection), ids))

    async def update_metadata(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
        patch: Mapping[str, str | int | float | bool],
    ) -> None:
        ids = [str(c) for c in chunk_ids]
        if not ids:
            return
        wire_patch = {k: str(v) for k, v in patch.items()}
        query = sql.SQL(
            """
            update {chunks_table} set
                metadata = metadata || %s::jsonb,
                updated_at = now()
            where 1=1
                and collection = %s
                and chunk_id = ANY(%s)
            """,
        ).format(chunks_table=self._tables.chunks_ident())
        pool = await self._pool()
        async with pool.cursor() as cur:
            await cur.execute(query, (json.dumps(wire_patch), str(collection), ids))

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

    def _to_summaries(
        self,
        rows: Iterable[Mapping[str, Any]],
    ) -> Sequence[ChunkSummary[str]]:
        summaries: list[ChunkSummary[str]] = []
        for row in rows:
            summaries.append(self._row_to_summary(row))
        return summaries

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
            batch = list(islice(it, self._tables.batch_size))
            if not batch:
                return
            yield batch

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
                    "postgres",
                    "empty tag list in HasAnyTag",
                )
            params.append(list(f.tags))
            return sql.SQL("(tags && %s)")
        if isinstance(f, HasAllTags):
            if not f.tags:
                raise UnsupportedFilterError(
                    f,
                    "postgres",
                    "empty tag list in HasAllTags",
                )
            params.append(list(f.tags))
            return sql.SQL("(tags @> %s)")
        if isinstance(f, And):
            if not f.filters:
                raise UnsupportedFilterError(f, "postgres", "empty And")
            if len(f.filters) == 1:
                return cls._filter_to_sql(f.filters[0], params)
            parts = [cls._filter_to_sql(s, params) for s in f.filters]
            return sql.SQL("(") + sql.SQL(" and ").join(parts) + sql.SQL(")")
        if isinstance(f, Or):
            if not f.filters:
                raise UnsupportedFilterError(f, "postgres", "empty Or")
            if len(f.filters) == 1:
                return cls._filter_to_sql(f.filters[0], params)
            parts = [cls._filter_to_sql(s, params) for s in f.filters]
            return sql.SQL("(") + sql.SQL(" or ").join(parts) + sql.SQL(")")
        if isinstance(f, Not):
            inner = cls._filter_to_sql(f.filter, params)
            return sql.SQL("(not ") + inner + sql.SQL(")")
        raise UnsupportedFilterError(
            f,
            "postgres",
            f"unknown filter type {type(f).__name__}",
        )

    @classmethod
    def _field_expr(cls, field: str) -> sql.Composable:
        if field in cls._SYSTEM_FIELDS:
            return sql.Identifier(field)
        return sql.SQL("metadata->>{key}").format(key=sql.Literal(field))

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
        if not values and invert:
            return sql.SQL("true")

        if not values:
            return sql.SQL("false")

        expr = cls._field_expr(field)
        params.append(values)

        op = sql.SQL("= any")
        if invert:
            op = sql.SQL("<> all")

        return sql.SQL("(") + expr + sql.SQL(" ") + op + sql.SQL("(%s))")


class PostgresCollectionsStore(CollectionsStore):
    """Postgres-реализация CollectionsStore (только collection-уровень)."""

    def __init__(
        self,
        *,
        cfg: PostgresStoreConfig,
    ) -> None:
        self._cfg = cfg
        self._tables = cfg.tables
        self._pool_ref: CancellablePool | None = None

    async def _pool(self) -> CancellablePool:
        """Пул берётся при первом обращении: __init__ не может await."""
        if self._pool_ref is None:
            self._pool_ref = await KbPool.open(self._cfg.connection)
        return self._pool_ref

    async def list_collections(self) -> Sequence[CollectionInfo]:
        query = sql.SQL(
            """
            select
                c.name,
                c.description,
                COALESCE(cnt.count, 0) as count
            from
                {collections_table} c
                left join (
                    select
                        collection,
                        count(*)::int as count
                    from
                        {chunks_table}
                    group by
                        collection
                ) cnt on cnt.collection = c.name
            order by
                c.name
            """,
        ).format(
            collections_table=self._tables.collections_ident(),
            chunks_table=self._tables.chunks_ident(),
        )
        pool = await self._pool()
        async with pool.dict_cursor() as cur:
            await cur.execute(query)
            rows = await cur.fetchall()

        collections: list[CollectionInfo] = []
        for row in rows:
            collections.append(
                CollectionInfo(
                    name=CollectionId(row["name"]),
                    description=row["description"] or "",
                    count=int(row["count"]),
                )
            )
        return collections

    async def collection_info(self, name: CollectionId) -> CollectionInfo:
        query = sql.SQL(
            """
            select
                c.name,
                c.description,
                (
                    select
                        count(*)::int
                    from
                        {chunks_table}
                    where
                        collection = c.name
                ) as count
            from
                {collections_table} c
            where
                c.name = %s
            """,
        ).format(
            chunks_table=self._tables.chunks_ident(),
            collections_table=self._tables.collections_ident(),
        )
        pool = await self._pool()
        async with pool.dict_cursor() as cur:
            await cur.execute(query, (str(name),))
            row = await cur.fetchone()
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

    async def ensure_collection(
        self,
        name: CollectionId,
        *,
        description: str | None,
    ) -> None:
        query = sql.SQL(
            """
            insert into {collections_table} (
                name,
                description
            )
            values (
                %s,
                %s
            )
            on conflict (name) do nothing
            """,
        ).format(collections_table=self._tables.collections_ident())
        pool = await self._pool()
        async with pool.cursor() as cur:
            await cur.execute(query, (str(name), description or ""))

    async def delete_collection(self, name: CollectionId) -> None:
        chunks_query = sql.SQL(
            """
            delete from
                {chunks_table}
            where
                collection = %s
            """,
        ).format(chunks_table=self._tables.chunks_ident())
        collection_query = sql.SQL(
            """
            delete from
                {collections_table}
            where
                name = %s
            """,
        ).format(collections_table=self._tables.collections_ident())

        params = (str(name),)

        pool = await self._pool()
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(chunks_query, params)
            await conn.execute(collection_query, params)
