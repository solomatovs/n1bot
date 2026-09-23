"""KB-store поверх postgres+pgvector; схему создаёт KbSchema при старте,
runtime DDL не делает.

Ошибки:
PostgresError — база или пул отказали чанкам и коллекциям.
LedgerError — база отказала реестру источников.
UnsupportedFilterError — фильтр поиска не переводится в SQL.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from itertools import islice
from typing import Any, ClassVar, TypeVar

from psycopg import sql

from boba.db.pgvector.config import PostgresStoreConfig, PostgresStoreSchema
from boba.db.postgres import (
    AsyncPostgresPool,
    CancellablePool,
    PgQueryBuilder,
    PostgresPool,
    PostgresTable,
)
from boba.db.postgres.connection import PostgresConfig
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
from boba.indexing.ledger import LedgerError, RunScope, SourceLedger, SourceRecord
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
    "KbTable",
    "PostgresChunkStore",
    "PostgresCollectionsStore",
    "PostgresSourceLedger",
    "PostgresStoreConfig",
    "PostgresStoreSchema",
]

_E = TypeVar("_E")


class KbPool:
    """Пул-singleton по подключению с register_vector: без него INSERT vector
    падает; соединения отдаются с прерыванием запроса по отмене хода."""

    def __init__(self, connection: PostgresConfig) -> None:
        self._connection = connection

    async def open(self) -> CancellablePool:
        pool = await AsyncPostgresPool.get(
            self._connection,
            configure=register_vector_async,
        )
        return CancellablePool(pool)


class KbTable(PostgresTable):
    """База хранилищ KB: пул с адаптером vector, имена таблиц конфига стоящими
    именами {chunks}, {collections}, {sources}; наследуют PostgresChunkStore,
    PostgresSourceLedger и PostgresCollectionsStore."""

    LABEL: ClassVar[str] = "kb"

    def __init__(self, cfg: PostgresStoreConfig) -> None:
        super().__init__(cfg.connection, cfg.tables.pg_schema)
        self._cfg = cfg
        self._tables = cfg.tables

    async def _open_pool(self) -> PostgresPool:
        return await KbPool(self._cfg.connection).open()

    def _query(self) -> PgQueryBuilder:
        schema = self._tables.pg_schema

        return PgQueryBuilder(
            schema=sql.Identifier(schema),
            chunks=sql.Identifier(schema, self._tables.chunks_table),
            collections=sql.Identifier(schema, self._tables.collections_table),
            sources=sql.Identifier(schema, self._tables.sources_table),
        )


class FilterSql:
    """Перевод дерева Filter в условие where: поля системных колонок — сами
    колонки, остальные — ключи jsonb metadata; значения уезжают параметрами
    f0, f1, … и собираются в params()."""

    SYSTEM_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"chunk_id", "collection", "source_id", "chunk_index", "content_hash"},
    )

    def __init__(self) -> None:
        self._params: dict[str, Any] = {}

    def params(self) -> dict[str, Any]:
        return dict(self._params)

    def _bind(self, value: Any) -> sql.Placeholder:
        name = f"f{len(self._params)}"
        self._params[name] = value

        return sql.Placeholder(name)

    def compile(self, f: Filter) -> sql.Composable:  # noqa: C901, PLR0911, PLR0912
        """Ошибки:
        UnsupportedFilterError — пустой список тегов или фильтров, неизвестный вид.
        """
        if isinstance(f, Eq):
            return self._compare(f.field, sql.SQL("="), f.value)

        if isinstance(f, Ne):
            return self._compare(f.field, sql.SQL("<>"), f.value)

        if isinstance(f, Lt):
            return self._compare_numeric(f.field, sql.SQL("<"), f.value)

        if isinstance(f, Lte):
            return self._compare_numeric(f.field, sql.SQL("<="), f.value)

        if isinstance(f, Gt):
            return self._compare_numeric(f.field, sql.SQL(">"), f.value)

        if isinstance(f, Gte):
            return self._compare_numeric(f.field, sql.SQL(">="), f.value)

        if isinstance(f, In):
            return self._contained(f.field, list(f.values), invert=False)

        if isinstance(f, NotIn):
            return self._contained(f.field, list(f.values), invert=True)

        if isinstance(f, HasTag):
            return sql.SQL("({tag} = any(tags))").format(tag=self._bind(f.tag))

        if isinstance(f, HasAnyTag):
            if not f.tags:
                raise UnsupportedFilterError(
                    f, "postgres", "empty tag list in HasAnyTag"
                )

            return sql.SQL("(tags && {tags})").format(tags=self._bind(list(f.tags)))

        if isinstance(f, HasAllTags):
            if not f.tags:
                raise UnsupportedFilterError(
                    f, "postgres", "empty tag list in HasAllTags"
                )

            return sql.SQL("(tags @> {tags})").format(tags=self._bind(list(f.tags)))

        if isinstance(f, And):
            return self._joined(f, f.filters, sql.SQL(" and "))

        if isinstance(f, Or):
            return self._joined(f, f.filters, sql.SQL(" or "))

        if isinstance(f, Not):
            return sql.SQL("(not {inner})").format(inner=self.compile(f.filter))

        raise UnsupportedFilterError(
            f,
            "postgres",
            f"unknown filter type {type(f).__name__}",
        )

    def _joined(
        self, f: Filter, filters: Sequence[Filter], glue: sql.SQL
    ) -> sql.Composable:
        if not filters:
            raise UnsupportedFilterError(f, "postgres", f"empty {type(f).__name__}")

        if len(filters) == 1:
            return self.compile(filters[0])

        parts: list[sql.Composable] = []
        for inner in filters:
            parts.append(self.compile(inner))

        return sql.SQL("({parts})").format(parts=glue.join(parts))

    def _field(self, field: str) -> sql.Composable:
        if field in self.SYSTEM_FIELDS:
            return sql.Identifier(field)

        return sql.SQL("metadata->>{key}").format(key=sql.Literal(field))

    def _compare(self, field: str, op: sql.SQL, value: Any) -> sql.Composable:
        return sql.SQL("({field} {op} {value})").format(
            field=self._field(field), op=op, value=self._bind(value)
        )

    def _compare_numeric(self, field: str, op: sql.SQL, value: Any) -> sql.Composable:
        return sql.SQL("(({field})::numeric {op} {value}::numeric)").format(
            field=self._field(field), op=op, value=self._bind(value)
        )

    def _contained(
        self, field: str, values: list[Any], *, invert: bool
    ) -> sql.Composable:
        if not values and invert:
            return sql.SQL("true")

        if not values:
            return sql.SQL("false")

        op = sql.SQL("= any")
        if invert:
            op = sql.SQL("<> all")

        return sql.SQL("({field} {op}({values}))").format(
            field=self._field(field), op=op, values=self._bind(values)
        )


class PostgresChunkStore(KbTable, ChunkStore[str]):
    """Реализация ChunkStore[str] на таблице чанков postgres."""

    def __init__(self, *, cfg: PostgresStoreConfig) -> None:
        super().__init__(cfg)

    async def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Sequence[Chunk[str]]:
        ids = self._ids(chunk_ids)
        if not ids:
            return []

        query = (
            self._query()
            .add(
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
                    {chunks}
                where
                    collection = %(collection)s
                    and chunk_id = any(%(ids)s)
                """,
                collection=str(collection),
                ids=ids,
            )
            .build()
        )
        rows = await self._rows(query, f"reading {len(ids)} chunks of {collection}")

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
        query = (
            self._query()
            .add(
                """
                select
                    chunk_id,
                    source_id,
                    chunk_index,
                    format_content as snippet,
                    metadata,
                    tags
                from
                    {chunks}
                where
                    collection = %(collection)s
                """,
                collection=str(collection),
            )
            .when(
                source_id is not None,
                "and source_id = %(source_id)s",
                source_id=str(source_id),
            )
            .add(
                """
                order by
                    source_id,
                    chunk_index
                limit
                    %(limit)s
                """,
                limit=limit,
            )
            .build()
        )
        rows = await self._rows(query, f"peeking {collection}")

        return self._to_summaries(rows)

    async def find(
        self,
        collection: CollectionId,
        *,
        where: Filter | None,
        limit: int | None = None,
    ) -> Sequence[ChunkSummary[str]]:
        compiler = FilterSql()
        condition: sql.Composable = sql.SQL("")
        if where is not None:
            condition = sql.SQL("and {where}").format(where=compiler.compile(where))

        query = (
            self._query()
            .add(
                """
                select
                    chunk_id,
                    source_id,
                    chunk_index,
                    format_content as snippet,
                    metadata,
                    tags
                from
                    {chunks}
                where
                    collection = %(collection)s
                    {condition}
                order by
                    source_id,
                    chunk_index
                """,
                collection=str(collection),
                condition=condition,
                **compiler.params(),
            )
            .when(limit is not None, "limit %(limit)s", limit=limit)
            .build()
        )
        rows = await self._rows(query, f"searching {collection}")

        return self._to_summaries(rows)

    async def diff_by_hash(
        self,
        collection: CollectionId,
        candidates: Iterable[tuple[ChunkId, ContentHash]],
    ) -> HashDiff:
        items: list[tuple[ChunkId, ContentHash]] = list(candidates)
        if not items:
            return HashDiff(to_upsert=[], unchanged=[])

        ids: list[str] = []
        for chunk_id, _hash in items:
            ids.append(str(chunk_id))

        query = (
            self._query()
            .add(
                """
                select
                    chunk_id,
                    content_hash
                from
                    {chunks}
                where 1=1
                    and collection = %(collection)s
                    and chunk_id = any(%(ids)s)
                """,
                collection=str(collection),
                ids=ids,
            )
            .build()
        )
        rows = await self._rows(query, f"comparing {len(ids)} hashes of {collection}")

        stored: dict[str, str] = {}
        for row in rows:
            stored[row["chunk_id"]] = row["content_hash"]

        to_upsert: list[ChunkId] = []
        unchanged: list[ChunkId] = []
        for chunk_id, candidate_hash in items:
            stored_wire = stored.get(str(chunk_id))
            if stored_wire is None:
                to_upsert.append(chunk_id)
                continue

            if stored_wire == candidate_hash.to_wire():
                unchanged.append(chunk_id)
                continue

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
        upsert = (
            self._query()
            .add(
                """
                insert into {chunks} (
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
                    %(chunk_id)s,
                    %(collection)s,
                    %(source_id)s,
                    %(chunk_index)s,
                    %(content_hash)s,
                    %(raw_content)s,
                    %(format_content)s,
                    %(embedding)s::vector,
                    %(metadata)s::jsonb,
                    %(tags)s,
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
                """
            )
            .build()
        )

        for batch in self._batched(chunks):
            rows: list[dict[str, Any]] = []
            for chunk in batch:
                rows.append(self._chunk_row(collection, chunk))

            async with self._transaction(
                f"upserting {len(rows)} chunks into {collection}"
            ) as cur:
                await cur.executemany(upsert.text, rows)

    def _chunk_row(
        self, collection: CollectionId, chunk: EmbeddedChunk[str]
    ) -> dict[str, Any]:
        return {
            "chunk_id": str(chunk.chunk_id),
            "collection": str(collection),
            "source_id": str(chunk.source_id),
            "chunk_index": chunk.chunk_index,
            "content_hash": chunk.content_hash.to_wire(),
            "raw_content": chunk.raw_content,
            "format_content": chunk.format_content,
            "embedding": list(chunk.embedding),
            "metadata": json.dumps(dict(chunk.metadata.to_wire())),
            "tags": sorted(chunk.tags),
        }

    async def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        ids = self._ids(chunk_ids)
        if not ids:
            return

        query = (
            self._query()
            .add(
                """
                delete from
                    {chunks}
                where 1=1
                    and collection = %(collection)s
                    and chunk_id = any(%(ids)s)
                """,
                collection=str(collection),
                ids=ids,
            )
            .build()
        )

        await self._execute(query, f"deleting {len(ids)} chunks of {collection}")

    async def delete_by_source(
        self,
        collection: CollectionId,
        source_id: SourceId,
        *,
        from_index: int,
    ) -> int:
        query = (
            self._query()
            .add(
                """
                delete from
                    {chunks}
                where 1=1
                    and collection = %(collection)s
                    and source_id = %(source_id)s
                    and chunk_index >= %(from_index)s
                """,
                collection=str(collection),
                source_id=str(source_id),
                from_index=from_index,
            )
            .build()
        )

        return await self._execute(
            query, f"deleting chunks of {source_id} in {collection} from {from_index}"
        )

    async def update_metadata(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
        patch: Mapping[str, str | int | float | bool],
    ) -> None:
        ids = self._ids(chunk_ids)
        if not ids:
            return

        wire_patch: dict[str, str] = {}
        for key, value in patch.items():
            wire_patch[key] = str(value)

        query = (
            self._query()
            .add(
                """
                update {chunks} set
                    metadata = metadata || %(patch)s::jsonb,
                    updated_at = now()
                where 1=1
                    and collection = %(collection)s
                    and chunk_id = any(%(ids)s)
                """,
                patch=json.dumps(wire_patch),
                collection=str(collection),
                ids=ids,
            )
            .build()
        )

        await self._execute(
            query, f"patching metadata of {len(ids)} chunks in {collection}"
        )

    def _ids(self, chunk_ids: Iterable[ChunkId]) -> list[str]:
        ids: list[str] = []
        for chunk_id in chunk_ids:
            ids.append(str(chunk_id))

        return ids

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

    def _row_to_metadata(self, row: Mapping[str, Any]) -> Metadata:
        raw = row.get("metadata") or {}
        if not isinstance(raw, dict):
            return Metadata.empty()

        wire: dict[str, str] = {}
        for key, value in raw.items():
            if value is None:
                continue

            wire[str(key)] = str(value)

        return Metadata.from_wire(wire)

    def _batched(
        self,
        items: Iterable[_E],
    ) -> Iterator[list[_E]]:
        it = iter(items)
        while True:
            batch = list(islice(it, self._tables.batch_size))
            if not batch:
                return

            yield batch


class PostgresSourceLedger(KbTable, SourceLedger):
    """Реестр источников одной коллекции в таблице sources_table.

    Время хранится timestamptz, наружу и внутрь ходит epoch-float домена.
    Обход невиденных идёт keyset-пагинацией по source_id: удаление уже
    отданных строк не сдвигает окно.
    """

    LABEL: ClassVar[str] = "ledger"

    PAGE: ClassVar[int] = 200
    """Сколько записей реестра берётся одним запросом при обходе."""

    def __init__(self, *, cfg: PostgresStoreConfig, collection: CollectionId) -> None:
        super().__init__(cfg)
        self._collection = str(collection)

    def _failure(self, action: str, exc: Exception) -> Exception:
        return LedgerError(self._detail(action, exc))

    def _record_query(self) -> PgQueryBuilder:
        """Колонки записи реестра; условие вызывающий добавляет следующим куском."""
        return self._query().add(
            """
            select
                source_id,
                parent_id,
                fingerprint,
                content_hash,
                grade,
                stamp,
                extract(epoch from seen_at) as seen_at,
                extract(epoch from indexed_at) as indexed_at,
                seen_run,
                scope
            from
                {sources}
            """
        )

    async def lookup(self, source_id: SourceId) -> SourceRecord | None:
        query = (
            self._record_query()
            .add(
                """
                where 1=1
                    and collection = %(collection)s
                    and source_id = %(source_id)s
                """,
                collection=self._collection,
                source_id=str(source_id),
            )
            .build()
        )
        row = await self._row(query, f"looking up {source_id}")
        if row is None:
            return None

        return self._row_to_record(row)

    async def touch(
        self, source_ids: Sequence[SourceId], *, at: float, scope: RunScope
    ) -> None:
        ids: list[str] = []
        for source_id in source_ids:
            ids.append(str(source_id))

        if not ids:
            return

        query = (
            self._query()
            .add(
                """
                update {sources} set
                    seen_at  = to_timestamp(%(at)s),
                    seen_run = %(run)s,
                    scope    = case when %(scope)s <> '' then %(scope)s else scope end
                where 1=1
                    and collection = %(collection)s
                    and source_id = any(%(ids)s)
                """,
                at=at,
                run=scope.run,
                scope=scope.scope,
                collection=self._collection,
                ids=ids,
            )
            .build()
        )

        await self._execute(query, f"touching {len(ids)} sources")

    async def record(self, record: SourceRecord) -> None:
        parent = ""
        if record.parent is not None:
            parent = str(record.parent)

        query = (
            self._query()
            .add(
                """
                insert into {sources} (
                    collection,
                    source_id,
                    parent_id,
                    fingerprint,
                    content_hash,
                    grade,
                    stamp,
                    seen_at,
                    indexed_at,
                    seen_run,
                    scope
                )
                values (
                    %(collection)s,
                    %(source_id)s,
                    %(parent_id)s,
                    %(fingerprint)s,
                    %(content_hash)s,
                    %(grade)s,
                    %(stamp)s,
                    to_timestamp(%(seen_at)s),
                    to_timestamp(%(indexed_at)s),
                    %(seen_run)s,
                    %(scope)s
                )
                on conflict (collection, source_id) do update set
                    parent_id    = excluded.parent_id,
                    fingerprint  = excluded.fingerprint,
                    content_hash = excluded.content_hash,
                    grade        = excluded.grade,
                    stamp        = excluded.stamp,
                    seen_at      = excluded.seen_at,
                    indexed_at   = excluded.indexed_at,
                    seen_run     = excluded.seen_run,
                    scope        = case
                        when excluded.scope <> '' then excluded.scope
                        else {sources}.scope
                    end
                """,
                collection=self._collection,
                source_id=str(record.source_id),
                parent_id=parent,
                fingerprint=record.fingerprint,
                content_hash=record.content_hash,
                grade=record.grade,
                stamp=record.stamp,
                seen_at=record.seen_at,
                indexed_at=record.indexed_at,
                seen_run=record.seen_run,
                scope=record.scope,
            )
            .build()
        )

        await self._execute(query, f"recording {record.source_id}")

    async def orphans(self, run: str) -> AsyncIterator[SourceRecord]:
        after = ""
        while True:
            query = (
                self._query()
                .add(
                    """
                    select
                        child.source_id,
                        child.parent_id,
                        child.fingerprint,
                        child.content_hash,
                        child.grade,
                        child.stamp,
                        extract(epoch from child.seen_at) as seen_at,
                        extract(epoch from child.indexed_at) as indexed_at,
                        child.seen_run,
                        child.scope
                    from
                        {sources} as child
                        join {sources} as parent
                            on parent.collection = child.collection
                            and parent.source_id = child.parent_id
                    where 1=1
                        and child.collection = %(collection)s
                        and child.parent_id <> ''
                        and child.seen_run <> %(run)s
                        and parent.seen_run = %(run)s
                        and child.source_id > %(after)s
                    order by
                        child.source_id
                    limit %(page)s
                    """,
                    collection=self._collection,
                    run=run,
                    after=after,
                    page=self.PAGE,
                )
                .build()
            )
            rows = await self._rows(query, "scanning orphans")
            if not rows:
                return

            for row in rows:
                yield self._row_to_record(row)

            after = str(rows[-1]["source_id"])

    async def unseen_roots(self, scope: str, run: str) -> AsyncIterator[SourceRecord]:
        after = ""
        while True:
            query = (
                self._record_query()
                .add(
                    """
                    where 1=1
                        and collection = %(collection)s
                        and scope = %(scope)s
                        and parent_id = ''
                        and seen_run <> %(run)s
                        and source_id > %(after)s
                    order by
                        source_id
                    limit %(page)s
                    """,
                    collection=self._collection,
                    scope=scope,
                    run=run,
                    after=after,
                    page=self.PAGE,
                )
                .build()
            )
            rows = await self._rows(query, "scanning unseen roots")
            if not rows:
                return

            for row in rows:
                yield self._row_to_record(row)

            after = str(rows[-1]["source_id"])

    async def children(self, parent: SourceId) -> AsyncIterator[SourceRecord]:
        after = ""
        while True:
            query = (
                self._record_query()
                .add(
                    """
                    where 1=1
                        and collection = %(collection)s
                        and parent_id = %(parent_id)s
                        and source_id > %(after)s
                    order by
                        source_id
                    limit %(page)s
                    """,
                    collection=self._collection,
                    parent_id=str(parent),
                    after=after,
                    page=self.PAGE,
                )
                .build()
            )
            rows = await self._rows(query, f"scanning children of {parent}")
            if not rows:
                return

            for row in rows:
                yield self._row_to_record(row)

            after = str(rows[-1]["source_id"])

    async def forget(self, source_id: SourceId) -> None:
        query = (
            self._query()
            .add(
                """
                delete from
                    {sources}
                where 1=1
                    and collection = %(collection)s
                    and source_id = %(source_id)s
                """,
                collection=self._collection,
                source_id=str(source_id),
            )
            .build()
        )

        await self._execute(query, f"forgetting {source_id}")

    def _row_to_record(self, row: Mapping[str, Any]) -> SourceRecord:
        parent: SourceId | None = None
        if row["parent_id"]:
            parent = SourceId(str(row["parent_id"]))

        return SourceRecord(
            source_id=SourceId(str(row["source_id"])),
            parent=parent,
            fingerprint=str(row["fingerprint"]),
            content_hash=str(row["content_hash"]),
            grade=int(row["grade"]),
            stamp=str(row["stamp"]),
            seen_at=float(row["seen_at"]),
            indexed_at=float(row["indexed_at"]),
            seen_run=str(row["seen_run"]),
            scope=str(row["scope"]),
        )


class PostgresCollectionsStore(KbTable, CollectionsStore):
    """Реализация CollectionsStore на таблице коллекций postgres."""

    def __init__(self, *, cfg: PostgresStoreConfig) -> None:
        super().__init__(cfg)

    async def list_collections(self) -> Sequence[CollectionInfo]:
        query = (
            self._query()
            .add(
                """
                select
                    c.name,
                    c.description,
                    coalesce(cnt.count, 0) as count
                from
                    {collections} c
                    left join (
                        select
                            collection,
                            count(*)::int as count
                        from
                            {chunks}
                        group by
                            collection
                    ) cnt on cnt.collection = c.name
                order by
                    c.name
                """
            )
            .build()
        )
        rows = await self._rows(query, "listing collections")

        collections: list[CollectionInfo] = []
        for row in rows:
            collections.append(self._info(row))

        return collections

    async def collection_info(self, name: CollectionId) -> CollectionInfo:
        query = (
            self._query()
            .add(
                """
                select
                    c.name,
                    c.description,
                    (
                        select
                            count(*)::int
                        from
                            {chunks}
                        where
                            collection = c.name
                    ) as count
                from
                    {collections} c
                where
                    c.name = %(name)s
                """,
                name=str(name),
            )
            .build()
        )
        row = await self._row(query, f"reading collection {name}")
        if row is None:
            return CollectionInfo(name=name, description="", count=0)

        return self._info(row)

    def _info(self, row: Mapping[str, Any]) -> CollectionInfo:
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
        text = description
        if text is None:
            text = ""

        query = (
            self._query()
            .add(
                """
                insert into {collections} (
                    name,
                    description
                )
                values (
                    %(name)s,
                    %(description)s
                )
                on conflict (name) do nothing
                """,
                name=str(name),
                description=text,
            )
            .build()
        )

        await self._execute(query, f"ensuring collection {name}")

    async def delete_collection(self, name: CollectionId) -> None:
        chunks = (
            self._query()
            .add(
                "delete from {chunks} where collection = %(name)s",
                name=str(name),
            )
            .build()
        )
        collection = (
            self._query()
            .add(
                "delete from {collections} where name = %(name)s",
                name=str(name),
            )
            .build()
        )

        async with self._transaction(f"deleting collection {name}") as cur:
            await cur.execute(chunks.text, chunks.params)
            await cur.execute(collection.text, collection.params)
