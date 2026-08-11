"""Операции базы знаний в песочнице: ONNX-инференс над недоверенным текстом
не идёт в процессе приложения, ценой перезагрузки модели на каждый вызов.

Ошибки: PostgresError — до базы знаний не достучаться; kb_query_failed —
СУБД отклонила поисковый запрос; ChannelError — в tool_args приехал запрос
чужой модели. Отсутствие весов эмбеддера ожидаемым не считается: это дефект
сборки rootfs, и трейсбек там по делу."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, LiteralString

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel

from boba.db.postgres import PayloadPostgres, PostgresError
from boba.tool.kb.protocol import KbNode, KbSearchRequest
from boba.toolkit.channels import ChannelError, StreamCodec
from boba.toolkit.payload import PayloadChannels, PayloadEntry, PayloadError
from boba.toolkit.sql import SqlRows
from boba.toolkit.workflow import EmptyTrailer


@dataclass(frozen=True)
class KbQuery:
    """Готовый запрос: идентификаторы подставлены, значения остались bind'ами."""

    statement: sql.Composed
    params: dict[str, Any]


class KbOps:
    """Векторный и полнотекстовый поиск по чанкам.

    SQL живёт здесь, а не в запросе: языки FTS зашиты и в шаблон, и в DDL
    tsv-колонки (migrations/002_multilang_tsv.sql) — оба места синхронны.
    """

    VECTOR_SQL: ClassVar[LiteralString] = """
select
    c.chunk_id,
    c.source_id,
    c.chunk_index,
    c.content_hash,
    c.metadata,
    c.tags,
    left(c.format_content, %(snippet_chars)s) AS snippet,
    (c.embedding::vector({dim})) <=> %(embedding)s::vector AS distance
from
    {chunks_table} c
where 1=1
    and c.collection = any(%(collections)s)
    and c.embedding is not null
order by
    distance asc
limit
    %(top_k)s
"""
    """Векторный поиск: format-плейсхолдеры {dim}/{chunks_table}, остальное — bind."""

    FTS_SQL: ClassVar[LiteralString] = """
with q as (
    select websearch_to_tsquery('russian', {schema}.immutable_unaccent(%(query)s))
        || websearch_to_tsquery('english', {schema}.immutable_unaccent(%(query)s))
        as tsq
)
select
    c.chunk_id,
    c.source_id,
    c.chunk_index,
    c.content_hash,
    c.metadata,
    c.tags,
    left(c.format_content, %(snippet_chars)s) as snippet,
    ts_rank_cd(c.tsv, q.tsq) as rank
from
    {chunks_table} c,
    q
where 1=1
    and c.collection = any(%(collections)s)
    and c.tsv @@ q.tsq
order by
    rank desc
limit
    %(top_k)s
"""
    """FTS-поиск: format-плейсхолдеры {chunks_table}/{schema}, остальное — bind."""

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        PostgresError: "database_unavailable",
    }

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {
        KbNode.VECTOR: KbSearchRequest,
        KbNode.FTS: KbSearchRequest,
    }

    @classmethod
    async def dispatch(
        cls,
        request: BaseModel,
        channels: PayloadChannels,
    ) -> BaseModel:
        if not isinstance(request, KbSearchRequest):
            msg = f"kb payload got an unexpected request: {type(request).__name__}"
            raise ChannelError(msg)

        query = cls.query_of(request)

        await cls.select(request, query, channels)

        return EmptyTrailer()

    @classmethod
    def query_of(cls, request: KbSearchRequest) -> KbQuery:
        if request.op is KbNode.VECTOR:
            return cls.vector_query(request)

        return cls.fts_query(request)

    @classmethod
    def vector_query(cls, request: KbSearchRequest) -> KbQuery:
        vector, dim = cls.embed(request)

        statement = sql.SQL(cls.VECTOR_SQL).format(
            dim=sql.Literal(dim),
            chunks_table=cls.table(request),
        )
        params: dict[str, Any] = {
            "collections": list(request.collections),
            "embedding": vector,
            "snippet_chars": request.snippet_chars,
            "top_k": request.top_k,
        }

        return KbQuery(statement=statement, params=params)

    @classmethod
    def fts_query(cls, request: KbSearchRequest) -> KbQuery:
        statement = sql.SQL(cls.FTS_SQL).format(
            chunks_table=cls.table(request),
            schema=sql.Identifier(request.schema_name),
        )
        params: dict[str, Any] = {
            "collections": list(request.collections),
            "query": request.query,
            "snippet_chars": request.snippet_chars,
            "top_k": request.top_k,
        }

        return KbQuery(statement=statement, params=params)

    @staticmethod
    def embed(request: KbSearchRequest) -> tuple[list[float], int]:
        """Вектор запроса и его размерность — их ждёт SQL-шаблон."""
        from fastembed import (  # noqa: PLC0415 # pyright: ignore[reportMissingImports]
            TextEmbedding,
        )

        encoder = TextEmbedding(
            model_name=request.embedding.model,
            cache_dir=request.embedding.cache_dir,
        )
        vector = next(iter(encoder.embed([request.query])))

        values: list[float] = []
        for item in vector:
            values.append(float(item))

        return values, len(values)

    @staticmethod
    def table(request: KbSearchRequest) -> sql.Identifier:
        return sql.Identifier(request.schema_name, request.chunks_table)

    @staticmethod
    async def select(
        request: KbSearchRequest,
        query: KbQuery,
        channels: PayloadChannels,
    ) -> None:
        """Каждая строка выдачи уходит в канал данных, не задерживаясь в памяти."""
        stream = channels.payload()

        conn = await PayloadPostgres.connect(request.connection)

        async with conn, conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(query.statement, query.params)
                async for row in cur:
                    stream.write(StreamCodec.encode_row(SqlRows.of_mapping(row)))
            except psycopg.Error as e:
                msg = f"kb search failed: {type(e).__name__}: {e}"
                raise PayloadError("kb_query_failed", msg) from e


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(KbOps))
