"""Tool `confluence_ingest` + `ConfluenceIngestConfig` (unified HTTP-ingest).

Один tool, три режима — LLM заполняет РОВНО ОДИН из дискриминирующих
параметров (space_keys / page_ids / cql), остальное автоматически:

- `space_keys=[A, B]`        → все страницы перечисленных Confluence-spaces
                               (discovery `/rest/api/space/{key}/content`).
- `page_ids=[123, 456]`      → явный список страниц по ID.
- `cql='space = DOCS ...'`   → discovery страниц через CQL.

Конфигурация (store/embedding/chunker/confluence/collection) — из TOML-секции
`[tool.kb.confluence.ingest]`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence._ingest_common import run_confluence_ingest
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources import (
    ConfluenceCqlRequestSource,
    ConfluenceMultiSpaceRequestSource,
    ConfluencePagesRequestSource,
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

__all__ = ["ConfluenceIngestConfig", "confluence_ingest"]

_PIPELINE_ID: PipelineId = PipelineId("kb.confluence_ingest")


class ConfluenceIngestConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_ingest`.

    Config-секция: `[tool.kb.confluence.ingest]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.ingest",
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
def confluence_ingest(
    cfg: Annotated[ConfluenceIngestConfig, FromConfig()],
    space_keys: Annotated[
        list[str] | None,
        Field(
            description=(
                "Mode A: индексация всех страниц перечисленных Confluence-spaces "
                "(discovery `/rest/api/space/{key}/content`)"
            ),
        ),
    ] = None,
    page_ids: Annotated[
        list[str] | None,
        Field(
            description=(
                "Mode B: индексация списка page_id. Каждый id — строка из "
                "URL `viewpage.action?pageId=<id>`. Используй, если уже знаешь "
                "конкретные страницы. Взаимоисключающий с `space_keys` и `cql`."
            ),
        ),
    ] = None,
    cql: Annotated[
        str | None,
        Field(
            description=(
                "Mode C: индексация страниц, отобранных CQL-запросом (например, "
                '`space = DOCS AND lastModified > "2024-01-01"`). Используй для '
                "тонких фильтров (по дате, метке, автору). Взаимоисключающий с "
                "`space_keys` и `page_ids`."
            ),
        ),
    ] = None,
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id нет "
                "среди страниц, попавших в discovery текущего run'а."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Confluence-ingest: spaces / page_ids / CQL по выбору LLM.

    LLM заполняет ровно один из (`space_keys`, `page_ids`, `cql`)

    Возвращает JSON `{mode, <discriminator>, collection, indexed,
    skipped_unchanged, pruned, failed}`
    """
    modes = [
        ("space_keys", space_keys),
        ("page_ids", page_ids),
        ("cql", cql),
    ]
    chosen = [(name, val) for name, val in modes if val]
    if len(chosen) != 1:
        provided = [name for name, _ in chosen] or ["<none>"]
        msg = (
            "confluence_ingest: задай РОВНО ОДИН из (space_keys, page_ids, "
            f"cql); получено {len(chosen)}: {provided}"
        )
        raise ValueError(msg)

    mode_name, mode_value = chosen[0]
    if mode_name == "space_keys":
        request_source = ConfluenceMultiSpaceRequestSource(
            conn=cfg.confluence,
            space_keys=mode_value,
            body_format=cfg.confluence.body_format,
        )
    elif mode_name == "page_ids":
        request_source = ConfluencePagesRequestSource(
            base_url=cfg.confluence.base_url,
            auth=cfg.confluence.make_auth(),
            page_ids=mode_value,
            body_format=cfg.confluence.body_format,
        )
    else:  # cql
        request_source = ConfluenceCqlRequestSource(
            conn=cfg.confluence,
            cql=mode_value,
            body_format=cfg.confluence.body_format,
        )

    chunk_store = PostgresChunkStore(cfg=cfg.store)
    collections_store = PostgresCollectionsStore(cfg=cfg.store)
    embedder = build_embedder(cfg.embedding)
    chunker = build_chunker(cfg.chunker)
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
    return {"mode": mode_name, mode_name: mode_value, **result}
