"""Старый kb-поиск по чанкам kb_chunks (коллекция Confluence): живёт на период
миграции на схему ix рядом с инструментами tools.py и удаляется вместе с ней.
Модуль — обычная программа: зигота запускает его по имени и ждёт TOOLS.

Эмбеддинг (fastembed/ONNX) и SQL исполняются в теле — потому оно живёт в
песочнице: инференс над недоверенным текстом не идёт в процессе приложения.

Ошибки:
PostgresError — до базы знаний не достучаться (сеть, libpq, kerberos).
psycopg.Error — СУБД отклонила поисковый запрос.
EmbeddingError — удалённый эмбеддер недоступен или ответил мусором.
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

from boba.db.postgres import PayloadPostgres, PostgresError
from boba.llm.embedding import EmbeddingError
from boba.llm.warm import WarmEmbedder
from boba.tool.kb.kb import KbChunksConfig
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

    @classmethod
    def hit(cls, row: dict[str, Any], *, vector: bool) -> SearchHit:
        if vector:
            distance = float(row["distance"])
        else:
            distance = -float(row["rank"])

        return SearchHit(
            distance=distance,
            metadata=cls._metadata(row),
            format_content=row["format_content"] or "",
            tags=cls._tags(row),
        )

    @staticmethod
    def _tags(row: dict[str, Any]) -> tuple[str, ...]:
        raw = row.get("tags") or ()
        tags: list[str] = []
        for tag in raw:
            tags.append(str(tag))

        return tuple(tags)

    @staticmethod
    def _metadata(row: dict[str, Any]) -> dict[str, str]:
        raw = row.get("metadata") or {}
        if not isinstance(raw, dict):
            return {}

        out: dict[str, str] = {}
        for key, value in raw.items():
            if value is None:
                continue

            out[str(key)] = str(value)

        return out


async def _embed(cfg: KbChunksConfig, query: str) -> tuple[list[float], int]:
    """Вектор запроса и его размерность — их ждёт SQL-шаблон."""
    build = Elapsed()
    embedder = WarmEmbedder.of(cfg.embedding)
    logger.info("embedder ready in %dms (%s)", build.ms(), cfg.embedding.kind)

    embed = Elapsed()
    vector = await embedder.embed_query(query)

    values: list[float] = []
    for item in vector:
        values.append(float(item))

    logger.info("query embedded in %dms (dim=%d)", embed.ms(), len(values))

    return values, len(values)


async def _select(
    cfg: KbChunksConfig,
    statement: sql.Composed,
    params: dict[str, Any],
    *,
    iterative: bool = False,
) -> list[dict[str, Any]]:
    connect = Elapsed()
    conn = await PayloadPostgres.connect_config(cfg.connection)
    logger.info("kb connected in %dms", connect.ms())

    async with conn, conn.cursor(row_factory=dict_row) as cur:
        query = Elapsed()
        if iterative:
            await cur.execute(sql.SQL(KbSearch.ITERATIVE_SCAN))

        await cur.execute(statement, params)
        rows = await cur.fetchall()
        logger.info("kb query finished in %dms (%d rows)", query.ms(), len(rows))

        return rows


def _table_of(cfg: KbChunksConfig) -> sql.Identifier:
    return sql.Identifier(cfg.tables.pg_schema, cfg.tables.chunks_table)


async def _search(
    cfg: KbChunksConfig,
    collection: type[CollectionSearch],
    query: str,
    top_k: int,
    *,
    vector: bool,
) -> TableResult:
    if vector:
        embedding, dim = await _embed(cfg, query)
        statement = sql.SQL(KbSearch.VECTOR_SQL).format(
            dim=sql.Literal(dim),
            chunks_table=_table_of(cfg),
        )
        params: dict[str, Any] = {
            "collections": [cfg.collection],
            "embedding": embedding,
            "top_k": top_k,
        }
    else:
        statement = sql.SQL(KbSearch.FTS_SQL).format(
            chunks_table=_table_of(cfg),
            schema=sql.Identifier(cfg.tables.pg_schema),
        )
        params = {
            "collections": [cfg.collection],
            "query": query,
            "top_k": top_k,
        }

    raw_rows = await _select(cfg, statement, params, iterative=vector)

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        rows.append(collection.row(KbRows.hit(raw, vector=vector)))

    note = None
    if not rows:
        note = "nothing found"

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
    return await _search(cfg, ConfluenceCollection, query, top_k, vector=True)


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
    return await _search(cfg, ConfluenceCollection, query, top_k, vector=False)


EXPECTED: Mapping[type[Exception], KbChunksErrorKind] = {
    PostgresError: KbChunksErrorKind.DATABASE_UNAVAILABLE,
    psycopg.Error: KbChunksErrorKind.QUERY_FAILED,
    EmbeddingError: KbChunksErrorKind.EMBEDDING_FAILED,
}

TOOLS: Final = ToolMain.toolset(kb_vector_search, kb_fts_search)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
