"""
Tool `confluence_doc_ingest_paths` + `ConfluenceDocIngestConfig`.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

from pydantic import Field

from boba.indexing import (
    CollectionScopedView,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    RequestSource,
    StreamingIndexer,
    Transport,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.indexing.embedder import Embedder
from boba.kbdoc import KbDocReader
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, LLMStringList
from boba.text import StructuralChunker
from boba.tool.kb.confluence_doc.workspace_indexing import (
    WorkspaceTransport,
    WorkspaceWalkRequestSource,
)
from boba.tool.kb.core.chunking import ChunkerParams
from boba.tool.kb.core.embedding import EmbeddingModel
from boba.tool.kb.core.postgres import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.transport.fs import FsRequest
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["ConfluenceDocIngestConfig", "confluence_doc_ingest_paths"]


class ConfluenceDocIngestConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_doc_ingest_paths`.

    Config-секция: `[tool.kb.confluence_doc.ingest]`. Operator-controlled поля:
    store/embedding/chunker/collection. LLM выбирает только `paths` и
    `prune_missing` в tool-вызове.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence_doc.ingest",
        defaults_from=(
            "kb.storage",
            "postgres.{kb.storage:profile}",
            "embedding",
        ),
    )

    store: PostgresStoreConfig
    embedding: EmbeddingModel
    chunker: ChunkerParams
    collection: str = Field(
        default="kb_confluence_doc",
        min_length=1,
        max_length=255,
        description="Target-коллекция в `kb_chunks`.",
    )


class ConfluenceDocIngest:
    """Сборка KbDoc-ingest pipeline — общий хвост для CLI и tool'а.

    Источник и транспорт собирает caller: CLI — `FsWalkRequestSource` +
    `FsTransport` по абсолютному `folder`; tool — `WorkspaceWalkRequestSource`
    + `WorkspaceTransport` поверх `ProjectWorkspaceShell`.
    """

    PIPELINE_ID: ClassVar[PipelineId] = PipelineId("kb.confluence_doc_ingest_paths")

    @staticmethod
    def run(  # noqa: PLR0913 — keyword-only helper, явный набор deps
        *,
        request_source: RequestSource[FsRequest],
        transport: Transport[FsRequest],
        chunk_store: PostgresChunkStore,
        collections_store: PostgresCollectionsStore,
        embedder: Embedder[str],
        chunker: StructuralChunker,
        collection: str,
        prune_missing: bool,
        pipeline_id: PipelineId,
    ) -> dict[str, Any]:
        """Полный KbDoc → kb_chunks pipeline для уже-собранных source+transport.

        Возвращает JSON-stats `{collection, indexed, skipped_unchanged, pruned,
        failed}`. Caller добавляет свои поля (folder/paths/...).
        """
        collection_id = CollectionId(collection)
        collections_store.ensure_collection(collection_id, description=None)

        view: CollectionScopedView[str] = CollectionScopedView(
            store=chunk_store,
            embedder=embedder,
            collection=collection_id,
        )
        indexer: StreamingIndexer[FsRequest, str] = StreamingIndexer(
            request_source=request_source,
            transport=transport,
            reader=KbDocReader(),
            chunker=chunker,
            sink=view,
            query=view,
        )
        config: IndexerConfig[str] = IndexerConfig(
            cleanup=FullCleanup() if prune_missing else NoneCleanup(),
            force_update=False,
        )
        stats = indexer.invoke(PipelineContext(pipeline_id=pipeline_id), config)

        return {
            "collection": str(collection_id),
            "indexed": stats.chunks_upserted,
            "skipped_unchanged": stats.sources_skipped_unchanged,
            "pruned": stats.chunks_deleted,
            "failed": stats.sources_failed,
        }


@tool
def confluence_doc_ingest_paths(
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[ConfluenceDocIngestConfig, FromConfig()],
    paths: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description=(
                "Workspace-relative пути файлов или папок с KbDoc `.md`. "
                'Передавай JSON-массив строк: `["upload/a.md", "upload/b.md"]` '
                'или `["upload"]` (папка обходится рекурсивно). Файлы, '
                "загруженные пользователем через chainlit, лежат под "
                "`upload/<имя>` — эти пути ты получаешь из user-сообщения "
                "в блоке `[Прикреплённые файлы (workspace-relative): ...]`."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id нет "
                "среди файлов, попавших в discovery текущего run'а."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Индексирует KbDoc `.md`-файлы из workspace'а в KB.

    Возвращает JSON `{collection, indexed, skipped_unchanged, pruned, failed,
    paths}`. Не-`.md` файлы игнорируются. Несуществующие пути логируются
    warning'ом и пропускаются.
    """
    chunk_store = PostgresChunkStore(cfg=cfg.store)
    collections_store = PostgresCollectionsStore(cfg=cfg.store)
    embedder = cfg.embedding.build()
    chunker = cfg.chunker.build_chunker()

    result = ConfluenceDocIngest.run(
        request_source=WorkspaceWalkRequestSource(
            shell=shell,
            paths=list(paths),
            include=["*.md"],
        ),
        transport=WorkspaceTransport(shell=shell),
        chunk_store=chunk_store,
        collections_store=collections_store,
        embedder=embedder,
        chunker=chunker,
        collection=cfg.collection,
        prune_missing=prune_missing,
        pipeline_id=ConfluenceDocIngest.PIPELINE_ID,
    )
    return {"paths": list(paths), **result}
