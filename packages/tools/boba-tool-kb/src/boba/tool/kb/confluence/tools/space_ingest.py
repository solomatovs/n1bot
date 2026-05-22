"""Tool `confluence_space_ingest` + `ConfluenceSpaceIngestConfig`.

Индексирует все страницы перечисленных Confluence space'ов в KB-коллекцию.
LLM передаёт `space_keys` + опц. `prune_missing`; остальное (connection,
tables, embedding, chunker, target collection) — из TOML-секции
`[tool.kb.confluence.ingest.space]`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence._ingest_common import run_confluence_ingest
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources import (
    ConfluenceMultiSpaceRequestSource,
)
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

__all__ = ["ConfluenceSpaceIngestConfig", "confluence_space_ingest"]

_PIPELINE_ID: PipelineId = PipelineId("kb.confluence_space_ingest")


class ConfluenceSpaceIngestConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_space_ingest`.

    Config-секция: `[tool.kb.confluence.ingest.space]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.ingest.space",
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
def confluence_space_ingest(
    cfg: Annotated[ConfluenceSpaceIngestConfig, FromConfig()],
    space_keys: Annotated[
        list[str],
        Field(
            min_length=1,
            description=(
                'Список Confluence space-keys (например, `["KAFKA"]` или '
                '`["KAFKA", "INFRA"]`). Все страницы каждого space '
                "(с пагинацией) объединяются в один pipeline-run и "
                "индексируются в `cfg.collection`."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id "
                "нет среди скачанных страниц union'а всех `space_keys`."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует все страницы перечисленных Confluence space'ов в KB-коллекцию.

    Возвращает JSON `{space_keys, collection, indexed, skipped_unchanged,
    pruned, failed}`.
    """
    chunk_store = PostgresChunkStore(cfg=cfg.store)
    collections_store = PostgresCollectionsStore(cfg=cfg.store)
    embedder = build_embedder(cfg.embedding)
    chunker = build_chunker(cfg.chunker)

    request_source = ConfluenceMultiSpaceRequestSource(
        conn=cfg.confluence,
        space_keys=space_keys,
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
    return {"space_keys": list(space_keys), **result}
