"""KB-поиск: функции уровня модуля, модуль — обычная программа.

Эмбеддинг (fastembed/ONNX) и SQL исполняются в теле — потому оно живёт в
песочнице: инференс над недоверенным текстом не идёт в процессе приложения.

Ошибки:
PostgresError — до базы знаний не достучаться (сеть, libpq, kerberos).
psycopg.Error — СУБД отклонила поисковый запрос.
EmbeddingError — удалённый эмбеддер недоступен или ответил мусором.
Отсутствие весов локального эмбеддера ожидаемым не считается: это дефект
сборки rootfs, и трейсбек там по делу.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres import PayloadPostgres, PostgresError
from boba.llm.embedding import EmbeddingConfig, EmbeddingError
from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.models import SearchHit
from boba.tool.kb.search import (
    CollectionSearch,
    ConfluenceCollection,
    KbSearch,
)
from boba.tool.kb.warm import WarmEmbedder
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool, warmup
from boba.toolkit.result import Produces, TableResult, ToolResult, pack_result
from boba.toolkit.timing import Elapsed
from boba.toolkit.types import SecretRevealing

logger = logging.getLogger(__name__)


class KbWarmupConfig(BaseModel):
    """Конфиг прогрева зиготы: только эмбеддер, секреты подключения не нужны."""

    model_config = ConfigDict(extra="ignore")

    embedding: EmbeddingConfig


@warmup
async def warm_embedder(cfg: KbWarmupConfig) -> None:
    """Модель ONNX поднимается в зиготе: дети берут её через COW."""
    embedder = WarmEmbedder.load(cfg.embedding)
    await embedder.embed_query("warm-up")


class KbToolConfig(SecretRevealing, PostgresKnowledgeBaseConfig):
    """Конфиг kb-поиска: подключение, таблицы, эмбеддер; секция [tool.kb]."""

    SECTION: ClassVar[str] = "tool.kb"

    collection: str = Field(
        min_length=1,
        description="Имя коллекции чанков, по которой идёт поиск.",
    )


class KbErrorKind(StrEnum):
    """Ожидаемые отказы kb-поиска."""

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
        )

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


async def _embed(cfg: KbToolConfig, query: str) -> tuple[list[float], int]:
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
    cfg: KbToolConfig,
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


def _table_of(cfg: KbToolConfig) -> sql.Identifier:
    return sql.Identifier(cfg.tables.pg_schema, cfg.tables.chunks_table)


async def _search(
    cfg: KbToolConfig,
    collection: type[CollectionSearch],
    query: str,
    top_k: int,
    *,
    vector: bool,
) -> tuple[str, ToolResult]:
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

    table = TableResult(rows=rows, note=note)
    return pack_result(table)


@tool
async def kb_vector_search(
    query: Annotated[
        str,
        Field(min_length=1, description=KbSearch.QUERY_DESC_VECTOR),
    ],
    top_k: Annotated[int, Field(ge=1, description=KbSearch.TOPK_DESC)] = 5,
    *,
    cfg: Annotated[KbToolConfig, Injected],
) -> Annotated[tuple[str, ToolResult], Produces.of(TableResult)]:
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
    cfg: Annotated[KbToolConfig, Injected],
) -> Annotated[tuple[str, ToolResult], Produces.of(TableResult)]:
    """Полнотекстовый (fts) поиск по коллекции Confluence-страниц.

    Возвращает таблицу hits: rank-расстояние, format_content и метаданные
    страницы, по релевантности.
    """
    return await _search(cfg, ConfluenceCollection, query, top_k, vector=False)


EXPECTED: Mapping[type[Exception], KbErrorKind] = {
    PostgresError: KbErrorKind.DATABASE_UNAVAILABLE,
    psycopg.Error: KbErrorKind.QUERY_FAILED,
    EmbeddingError: KbErrorKind.EMBEDDING_FAILED,
}

TOOLS: Final = ToolMain.toolset(kb_vector_search, kb_fts_search)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
