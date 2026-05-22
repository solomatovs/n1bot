"""Tool `files_ingest` + `FilesIngestConfig`: индексация FS-папки в KB.

Self-contained tool-конфиг: содержит всё необходимое для построения
сервисов inline (`PostgresChunkStore`, `PostgresCollectionsStore`,
embedder, chunker). LLM передаёт только trigger — пути/collection пинит
оператор в TOML-секции `[tool.kb.files_ingest]`.

Поддерживаемые форматы:
- `.md`         → `KbDocReader` (header + body как одна Section)
- `.html/.htm`  → `HtmlReader` (heading-aware)
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from boba.indexing import (
    CollectionScopedView,
    DispatchReader,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    StreamingIndexer,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core.chunker_factory import build_chunker
from boba.tool.kb.core.chunker_params import ChunkerParams
from boba.tool.kb.core.embedder_factory import build_embedder
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.transport.fs import FsRequest, FsTransport, FsWalkRequestSource

__all__ = ["FilesIngestConfig", "files_ingest"]


class FilesIngestConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `files_ingest`.

    Config-секция: `[tool.kb.files_ingest]`.
    Благодаря рекурсивному flatten'у в `BobaFlatSettings` все поля
    nested-под-моделей (host/port/.../schema/chunks_table/endpoint/.../
    chunk_size) лежат плоско в TOML на уровне `[tool.kb.files_ingest]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.files_ingest",
    )

    store: PostgresStoreConfig
    embedding: EmbeddingModel
    chunker: ChunkerParams
    folder: str = Field(
        description=(
            "Папка с файлами для индексации (`.md`/`.html`/`.htm`). "
            "LLM папку не выбирает — оператор пинит её здесь."
        ),
    )
    collection: str = Field(
        default="kb_files",
        min_length=1,
        max_length=255,
        description=(
            "Имя target-коллекции в `kb_chunks`. Отдельная от "
            "confluence-коллекции, чтобы FS-индексация не перемешивалась "
            "с автоматическим ingest'ом из Confluence."
        ),
    )
    prune: bool = Field(
        default=False,
        description=(
            "Если true, удалить из коллекции чанки, чьих source_id нет "
            "среди файлов в `folder` (cleanup удалённых документов)."
        ),
    )


@tool
def files_ingest(
    cfg: Annotated[FilesIngestConfig, FromConfig()],
    dispatch_reader: Annotated[DispatchReader[str], FromDI(Scope.APP)],
) -> dict[str, Any]:
    """Индексирует `cfg.folder` в `cfg.collection`.

    Возвращает JSON: `{folder, collection, indexed, skipped_unchanged,
    pruned, failed}`.
    """
    folder = Path(cfg.folder)
    if not folder.exists():
        msg = f"folder_not_found: {folder}"
        raise RuntimeError(msg)
    if not folder.is_dir():
        msg = f"folder_not_a_directory: {folder}"
        raise RuntimeError(msg)

    chunk_store = PostgresChunkStore(cfg=cfg.store)
    collections_store = PostgresCollectionsStore(cfg=cfg.store)
    embedder = build_embedder(cfg.embedding)
    chunker = build_chunker(cfg.chunker)

    collection = CollectionId(cfg.collection)
    collections_store.ensure_collection(collection, description=None)

    view: CollectionScopedView[str] = CollectionScopedView(
        store=chunk_store,
        embedder=embedder,
        collection=collection,
    )
    indexer: StreamingIndexer[FsRequest, str] = StreamingIndexer(
        request_source=FsWalkRequestSource(
            paths=[str(folder)],
            include=["*.md", "*.html", "*.htm"],
        ),
        transport=FsTransport(),
        reader=dispatch_reader,
        chunker=chunker,
        sink=view,
        query=view,
    )
    config: IndexerConfig[str] = IndexerConfig(
        cleanup=FullCleanup() if cfg.prune else NoneCleanup(),
        force_update=False,
    )
    stats = indexer.invoke(
        PipelineContext(pipeline_id=PipelineId("kb.files_ingest")),
        config,
    )

    return {
        "folder": str(folder),
        "collection": str(collection),
        "indexed": stats.chunks_upserted,
        "skipped_unchanged": stats.sources_skipped_unchanged,
        "pruned": stats.chunks_deleted,
        "failed": stats.sources_failed,
    }
