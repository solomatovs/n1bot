"""Tool `confluence_space_ingest`: индексация целого Confluence space в KB."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.text import StructuralChunker
from boba.tool.kb.config import KbConfig
from boba.tool.kb.confluence._ingest_common import run_confluence_ingest
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources import ConfluenceSpaceRequestSource
from boba.tool.kb.vector_store import PostgresVectorStore
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["confluence_space_ingest"]

_PIPELINE_ID: PipelineId = PipelineId("kb.confluence_space_ingest")


@tool
def confluence_space_ingest(  # noqa: PLR0913 — tool с явным набором FromDI/FromConfig deps
    store: Annotated[PostgresVectorStore, FromDI(Scope.APP)],
    chunker: Annotated[StructuralChunker, FromDI(Scope.APP)],
    kb_cfg: Annotated[KbConfig, FromConfig()],
    conn_cfg: Annotated[ConfluenceConnectionConfig, FromConfig()],
    space_key: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Confluence space key (например, `KAFKA`, `DOCS`). "
                "Скачиваются ВСЕ страницы space-а (с пагинацией)."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id "
                "нет среди скачанных страниц (cleanup удалённых)."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует все страницы указанного Confluence space в KB-коллекцию.

    Target collection — `[tool.kb].collection` (pinned оператором). Возвращает
    JSON {space_key, collection, indexed, skipped_unchanged, pruned, failed}.
    """
    request_source = ConfluenceSpaceRequestSource(
        base_url=conn_cfg.base_url,
        auth=ConfluenceConnection.make_auth(conn_cfg),
        space_key=space_key,
        body_format=conn_cfg.body_format,
        timeout_sec=conn_cfg.timeout_sec,
    )
    result = run_confluence_ingest(
        request_source=request_source,
        conn_cfg=conn_cfg,
        store=store,
        chunker=chunker,
        collection=kb_cfg.collection,
        collection_description=kb_cfg.collection_description,
        prune_missing=prune_missing,
        pipeline_id=_PIPELINE_ID,
    )
    return {"space_key": space_key, **result}
