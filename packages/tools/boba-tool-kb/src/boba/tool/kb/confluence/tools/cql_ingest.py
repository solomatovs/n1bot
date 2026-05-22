"""Tool `confluence_cql_ingest` + `ConfluenceCqlIngestConfig`.

Индексирует страницы Confluence, найденные по CQL-запросу (например,
`space = DOCS AND lastModified > '2024-01-01'`). Discovery — через
`/rest/api/content/search?cql=...` с пагинацией.

LLM передаёт CQL-запрос; остальное (connection, tables, embedding,
chunker, target collection) — из TOML-секции
`[tool.kb.confluence_ingest.cql]`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence._ingest_common import run_confluence_ingest
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources import ConfluenceCqlRequestSource
from boba.tool.kb.core.chunker_factory import build_chunker
from boba.tool.kb.core.chunker_params import ChunkerParams
from boba.tool.kb.core.embedder_factory import build_embedder
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.tools import FromConfig, tool

__all__ = ["ConfluenceCqlIngestConfig", "confluence_cql_ingest"]

_PIPELINE_ID: PipelineId = PipelineId("kb.confluence_cql_ingest")


class ConfluenceCqlIngestConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_cql_ingest`.

    Config-секция: `[tool.kb.confluence_ingest.cql]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence_ingest.cql",
        defaults_from=("postgres", "kb.storage", "embedding", "confluence"),
    )

    store: PostgresStoreConfig
    embedding: EmbeddingModel
    chunker: ChunkerParams
    confluence: ConfluenceConnection
    collection: str = Field(
        default="kb_confluence",
        min_length=1,
        max_length=255,
        description="Target-коллекция в `kb_chunks`.",
    )


@tool
def confluence_cql_ingest(
    cfg: Annotated[ConfluenceCqlIngestConfig, FromConfig()],
    cql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "CQL-запрос для отбора страниц "
                '(например, `space = DOCS AND lastModified > "2024-01-01"`). '
                "Discovery — через `/rest/api/content/search?cql=...` с пагинацией."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id "
                "нет среди страниц, найденных по CQL в текущем run'е."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует страницы Confluence, отобранные CQL-запросом, в KB-коллекцию.

    Возвращает JSON `{cql, collection, indexed, skipped_unchanged, pruned, failed}`.
    """
    chunk_store = PostgresChunkStore(cfg=cfg.store)
    collections_store = PostgresCollectionsStore(cfg=cfg.store)
    embedder = build_embedder(cfg.embedding)
    chunker = build_chunker(cfg.chunker)

    request_source = ConfluenceCqlRequestSource(
        conn=cfg.confluence,
        cql=cql,
        body_format=cfg.confluence.body_format,
    )
    result = run_confluence_ingest(
        request_source=request_source,
        conn=cfg.confluence,
        chunk_store=chunk_store,
        collections_store=collections_store,
        embedder=embedder,
        chunker=chunker,
        collection=cfg.collection,
        prune_missing=prune_missing,
        pipeline_id=_PIPELINE_ID,
    )
    return {"cql": cql, **result}
