"""Старый kb-поиск по чанкам kb_chunks (коллекция Confluence): живёт на период
миграции на схему ix рядом с инструментами tools.py и удаляется вместе с ней.
Модуль — обычная программа: зигота запускает его по имени и ждёт TOOLS.

Эмбеддинг (fastembed/ONNX) и SQL исполняются в теле — потому оно живёт в
песочнице: инференс над недоверенным текстом не идёт в процессе приложения.

Ошибки:
PostgresError — до базы знаний не достучаться (сеть, libpq, kerberos).
psycopg.Error — СУБД отклонила поисковый запрос.
LlmError — эмбеддер недоступен, не загрузился или ответил мусором.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Final

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import Field

from boba.db.postgres import PayloadPostgres, PgQuery, PgQueryBuilder, PostgresError
from boba.llm.chat import LlmError
from boba.tool.kb.kb import LLM, KbChunksConfig
from boba.tool.kb.models import SearchHit
from boba.tool.kb.search import (
    CollectionSearch,
    ConfluenceCollection,
    KbSearch,
)
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import TableResult
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)


class KbChunksErrorKind(StrEnum):
    """Ожидаемые отказы поиска по чанкам."""

    DATABASE_UNAVAILABLE = "database_unavailable"
    QUERY_FAILED = "kb_query_failed"
    EMBEDDING_FAILED = "embedding_failed"


class KbRows:
    """Строка выдачи SQL -> SearchHit -> строка таблицы коллекции."""

    def hit(self, row: Mapping[str, Any], *, vector: bool) -> SearchHit:
        if vector:
            distance = float(row["distance"])
        else:
            distance = -float(row["rank"])

        return SearchHit(
            distance=distance,
            metadata=self._metadata(row),
            format_content=row["format_content"] or "",
            tags=self._tags(row),
        )

    def _tags(self, row: Mapping[str, Any]) -> tuple[str, ...]:
        raw = row.get("tags") or ()
        tags: list[str] = []
        for tag in raw:
            tags.append(str(tag))

        return tuple(tags)

    def _metadata(self, row: Mapping[str, Any]) -> dict[str, str]:
        raw = row.get("metadata") or {}
        if not isinstance(raw, dict):
            return {}

        out: dict[str, str] = {}
        for key, value in raw.items():
            if value is None:
                continue

            out[str(key)] = str(value)

        return out


class KbChunkSearch:
    """Поиск по чанкам коллекции: векторный через эмбеддер запроса и
    полнотекстовый; запрос исполняется своим подключением к базе знаний."""

    EMPTY_NOTE: Final = "nothing found"

    def __init__(self, cfg: KbChunksConfig) -> None:
        self._cfg = cfg
        self._rows = KbRows()

    async def vector(
        self, collection: type[CollectionSearch], query: str, top_k: int
    ) -> TableResult:
        embedding, dim = await self._embed(query)
        statement = (
            self._query(dim)
            .add(
                KbSearch.VECTOR_SQL,
                collections=[self._cfg.collection],
                embedding=embedding,
                top_k=top_k,
            )
            .build()
        )
        found = await self._select(statement, iterative=True)

        return self._result(collection, found, vector=True)

    async def fts(
        self, collection: type[CollectionSearch], query: str, top_k: int
    ) -> TableResult:
        statement = (
            self._query(0)
            .add(
                KbSearch.FTS_SQL,
                collections=[self._cfg.collection],
                query=query,
                top_k=top_k,
            )
            .build()
        )
        found = await self._select(statement, iterative=False)

        return self._result(collection, found, vector=False)

    def _query(self, dim: int) -> PgQueryBuilder:
        """Сборщик с именами схемы, таблицы чанков и размерности вектора."""
        tables = self._cfg.tables

        return PgQueryBuilder(
            schema=sql.Identifier(tables.pg_schema),
            chunks_table=sql.Identifier(tables.pg_schema, tables.chunks_table),
            dim=sql.Literal(dim),
        )

    async def _embed(self, query: str) -> tuple[list[float], int]:
        """Вектор запроса и его размерность — их ждёт SQL-шаблон."""
        build = Elapsed()
        embedder = LLM.embedding(self._cfg.embedding)
        logger.info(
            "embedder ready in %dms (%s)", build.ms(), self._cfg.embedding.provider.kind
        )

        embed = Elapsed()
        vector = await embedder.embed_query(query)

        values: list[float] = []
        for item in vector:
            values.append(float(item))

        logger.info("query embedded in %dms (dim=%d)", embed.ms(), len(values))

        return values, len(values)

    async def _select(
        self, statement: PgQuery, *, iterative: bool
    ) -> list[dict[str, Any]]:
        connect = Elapsed()
        conn = await PayloadPostgres.connect_config(self._cfg.connection)
        logger.info("kb connected in %dms", connect.ms())

        async with conn, conn.cursor(row_factory=dict_row) as cur:
            query = Elapsed()
            if iterative:
                scan = PgQueryBuilder().add(KbSearch.ITERATIVE_SCAN).build()
                await cur.execute(scan.text, scan.params)

            await cur.execute(statement.text, statement.params)
            rows = await cur.fetchall()
            logger.info("kb query finished in %dms (%d rows)", query.ms(), len(rows))

            return rows

    def _result(
        self,
        collection: type[CollectionSearch],
        found: list[dict[str, Any]],
        *,
        vector: bool,
    ) -> TableResult:
        rows: list[dict[str, Any]] = []
        for raw in found:
            rows.append(collection.row(self._rows.hit(raw, vector=vector)))

        note = None
        if not rows:
            note = self.EMPTY_NOTE

        return TableResult(rows=rows, note=note)


@tool
async def kb_vector_search(
    query: Annotated[
        str,
        Field(min_length=1, description=KbSearch.QUERY_DESC_VECTOR),
    ],
    top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
    *,
    cfg: Annotated[KbChunksConfig, Injected],
) -> TableResult:
    """Семантический (vector) поиск по коллекции Confluence-страниц.

    Возвращает таблицу hits: distance, format_content и метаданные страницы,
    по релевантности.
    """
    return await KbChunkSearch(cfg).vector(ConfluenceCollection, query, top_k)


@tool
async def kb_fts_search(
    query: Annotated[
        str,
        Field(min_length=1, description=KbSearch.QUERY_DESC_FTS),
    ],
    top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
    *,
    cfg: Annotated[KbChunksConfig, Injected],
) -> TableResult:
    """Полнотекстовый (fts) поиск по коллекции Confluence-страниц.

    Возвращает таблицу hits: rank-расстояние, format_content и метаданные
    страницы, по релевантности.
    """
    return await KbChunkSearch(cfg).fts(ConfluenceCollection, query, top_k)


EXPECTED: Mapping[type[Exception], KbChunksErrorKind] = {
    PostgresError: KbChunksErrorKind.DATABASE_UNAVAILABLE,
    psycopg.Error: KbChunksErrorKind.QUERY_FAILED,
    LlmError: KbChunksErrorKind.EMBEDDING_FAILED,
}

TOOLS: Final = ToolMain.toolset(kb_vector_search, kb_fts_search)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
