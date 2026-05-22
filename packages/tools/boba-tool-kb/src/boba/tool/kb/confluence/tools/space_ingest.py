"""Tool `confluence_space_ingest`: индексация одного/нескольких space'ов в KB."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.indexing.embedder import Embedder
from boba.text import StructuralChunker
from boba.tool.kb.confluence._ingest_common import run_confluence_ingest
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.request_sources import (
    ConfluenceMultiSpaceRequestSource,
)
from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.vector_store import PostgresVectorStore
from boba.tools import FromConfig, FromDI, Scope, tool

__all__ = ["confluence_space_ingest"]

_PIPELINE_ID: PipelineId = PipelineId("kb.confluence_space_ingest")


@tool
def confluence_space_ingest(  # noqa: PLR0913
    store: Annotated[PostgresVectorStore, FromDI(Scope.APP)],
    embedder: Annotated[Embedder[str], FromDI(Scope.APP)],
    chunker: Annotated[StructuralChunker, FromDI(Scope.APP)],
    kb_cfg: Annotated[KbConfig, FromConfig()],
    conn_cfg: Annotated[ConfluenceConnectionConfig, FromConfig()],
    space_keys: Annotated[
        list[str],
        Field(
            min_length=1,
            description=(
                'Список Confluence space-keys (например, `["KAFKA"]` или '
                '`["KAFKA", "INFRA"]`). Все страницы каждого space '
                "(c пагинацией) объединяются в один pipeline-run и "
                "индексируются в `[tool.kb].collection`. Для одного space "
                "передавай список из одного элемента."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id "
                "нет среди скачанных страниц union'а всех `space_keys` "
                "(чистит удалённое в перечисленных spaces; страницы из "
                "других spaces в этой же коллекции не трогаются — у "
                "них не было source_id в текущем run'е)."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует все страницы перечисленных Confluence space'ов в KB-коллекцию.

    Target collection — `[tool.kb].collection` (pinned оператором). Возвращает
    JSON `{space_keys, collection, indexed, skipped_unchanged, pruned, failed}`.
    """
    request_source = ConfluenceMultiSpaceRequestSource(
        conn_cfg=conn_cfg,
        space_keys=space_keys,
        body_format=conn_cfg.body_format,
    )
    result = run_confluence_ingest(
        request_source=request_source,
        conn_cfg=conn_cfg,
        store=store,
        embedder=embedder,
        chunker=chunker,
        collection=kb_cfg.collection,
        collection_description=kb_cfg.collection_description,
        prune_missing=prune_missing,
        pipeline_id=_PIPELINE_ID,
    )
    return {"space_keys": list(space_keys), **result}
