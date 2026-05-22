"""Tool `confluence_page_ingest` + `ConfluencePageIngestConfig`.

Индексирует явный список Confluence-страниц (page_ids) в KB-коллекцию.
LLM передаёт `page_ids` + опц. `prune_missing`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence._ingest_common import run_confluence_ingest
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources import ConfluencePagesRequestSource
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

__all__ = ["ConfluencePageIngestConfig", "confluence_page_ingest"]

_PIPELINE_ID: PipelineId = PipelineId("kb.confluence_page_ingest")


class ConfluencePageIngestConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_page_ingest`.

    Config-секция: `[tool.kb.confluence.ingest.page]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.ingest.page",
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
def confluence_page_ingest(
    cfg: Annotated[ConfluencePageIngestConfig, FromConfig()],
    page_ids: Annotated[
        list[str],
        Field(
            min_length=1,
            description=(
                "Список Confluence page_id для индексации. Каждый id — "
                "строка из URL `viewpage.action?pageId=<id>`."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id "
                "нет среди явно перечисленных страниц."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует явный список Confluence-страниц в KB-коллекцию.

    Возвращает JSON `{page_ids, collection, indexed, skipped_unchanged,
    pruned, failed}`.
    """
    chunk_store = PostgresChunkStore(cfg=cfg.store)
    collections_store = PostgresCollectionsStore(cfg=cfg.store)
    embedder = build_embedder(cfg.embedding)
    chunker = build_chunker(cfg.chunker)

    request_source = ConfluencePagesRequestSource(
        base_url=cfg.confluence.base_url,
        auth=cfg.confluence.make_auth(),
        page_ids=page_ids,
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
    return {"page_ids": page_ids, **result}
