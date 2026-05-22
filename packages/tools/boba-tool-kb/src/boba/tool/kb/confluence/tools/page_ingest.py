"""Tool `confluence_page_ingest`: индексация явного списка Confluence-страниц в KB."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.text import StructuralChunker
from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.confluence._ingest_common import run_confluence_ingest
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources import ConfluencePagesRequestSource
from boba.tool.kb.core.vector_store import PostgresVectorStore
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["confluence_page_ingest"]

_PIPELINE_ID: PipelineId = PipelineId("kb.confluence_page_ingest")


@tool
def confluence_page_ingest(  # noqa: PLR0913 — tool с явным набором FromDI/FromConfig deps
    store: Annotated[PostgresVectorStore, FromDI(Scope.APP)],
    chunker: Annotated[StructuralChunker, FromDI(Scope.APP)],
    kb_cfg: Annotated[KbConfig, FromConfig()],
    conn_cfg: Annotated[ConfluenceConnectionConfig, FromConfig()],
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

    Target collection — `[tool.kb].collection` (pinned оператором). Возвращает
    JSON {page_ids, collection, indexed, skipped_unchanged, pruned, failed}.
    """
    request_source = ConfluencePagesRequestSource(
        base_url=conn_cfg.base_url,
        auth=ConfluenceConnection.make_auth(conn_cfg),
        page_ids=page_ids,
        body_format=conn_cfg.body_format,
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
    return {"page_ids": page_ids, **result}
