"""CLI-runner: индексация папки KbDoc-файлов в KB.

Оператор готовит md чанки и они по возможности индексируются как есть
если умещаются в максимальный размер чанка

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.kbdoc_ingest
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from dishka.entities.component import Component
from pydantic import Field

from boba.agent import AgentBuilder
from boba.indexing import (
    CollectionScopedView,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    StreamingIndexer,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.kbdoc import KbDocReader
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core import providers as kb_providers
from boba.tool.kb.core.chunker_factory import build_chunker
from boba.tool.kb.core.chunker_params import ChunkerParams
from boba.tool.kb.core.embedder_factory import build_embedder
from boba.tool.kb.core.embedding_model import EmbeddingModel
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.transport.fs import FsRequest, FsTransport, FsWalkRequestSource

__all__ = ["KbDocIngestCliConfig", "main"]


class KbDocIngestCliConfig(BobaFlatSettings):
    """Self-contained CLI-конфиг индексатора папки KbDoc-файлов.

    Config-секция: `[cli.kb.kbdoc_ingest]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="cli.kb.kbdoc_ingest",
        defaults_from=("postgres", "kb.storage", "embedding"),
    )

    store: PostgresStoreConfig
    embedding: EmbeddingModel
    chunker: ChunkerParams
    folder: str = Field(
        description="Папка с KbDoc-файлами (`.md`) для индексации.",
    )
    collection: str = Field(
        default="kb_kbdoc",
        min_length=1,
        max_length=255,
        description="Имя target-коллекции в `kb_chunks`.",
    )
    prune: bool = Field(
        default=False,
        description=(
            "Если true, удалить из коллекции чанки, чьих source_id нет "
            "среди файлов в `folder` (cleanup удалённых документов)."
        ),
    )


logger = logging.getLogger("boba.tool.kb.cli.kbdoc_ingest")

_KB_COMPONENT = Component(kb_providers.__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    builder = AgentBuilder().use_plugin(kb_providers)
    container = builder.di.build_container()

    try:
        with container() as req:
            cfg = req.get(KbDocIngestCliConfig, component=_KB_COMPONENT)
            kbdoc_reader = req.get(KbDocReader, component=_KB_COMPONENT)

            logger.info(
                "ingesting folder=%s → collection=%s (prune=%s)",
                cfg.folder,
                cfg.collection,
                cfg.prune,
            )

            start = time.monotonic()
            try:
                result = _run_ingest(cfg, kbdoc_reader)
            except Exception:
                logger.exception("kbdoc_ingest FAILED")
                return 1
            elapsed = time.monotonic() - start

            logger.info(
                "DONE in %.1fs — indexed=%d skipped_unchanged=%d pruned=%d failed=%d",
                elapsed,
                result["indexed"],
                result["skipped_unchanged"],
                result["pruned"],
                result["failed"],
            )
            return 0 if result["failed"] == 0 else 1
    finally:
        container.close()


def _run_ingest(
    cfg: KbDocIngestCliConfig,
    kbdoc_reader: KbDocReader,
) -> dict[str, Any]:
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
            include=["*.md"],
        ),
        transport=FsTransport(),
        reader=kbdoc_reader,
        chunker=chunker,
        sink=view,
        query=view,
    )
    indexer_config: IndexerConfig[str] = IndexerConfig(
        cleanup=FullCleanup() if cfg.prune else NoneCleanup(),
        force_update=False,
    )
    stats = indexer.invoke(
        PipelineContext(pipeline_id=PipelineId("kb.kbdoc_ingest")),
        indexer_config,
    )

    return {
        "folder": str(folder),
        "collection": str(collection),
        "indexed": stats.chunks_upserted,
        "skipped_unchanged": stats.sources_skipped_unchanged,
        "pruned": stats.chunks_deleted,
        "failed": stats.sources_failed,
    }


if __name__ == "__main__":
    raise SystemExit(main())
