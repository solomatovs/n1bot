"""CLI-runner: индексация папки KbDoc-файлов в KB.

Оператор готовит md чанки и они по возможности индексируются как есть
если умещаются в максимальный размер чанка

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.confluence_doc.ingest
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field

from boba.indexing.context import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence_doc.ingest import ConfluenceDocIngest
from boba.tool.kb.core.chunking import ChunkerParams
from boba.tool.kb.core.embedding import EmbeddingModel
from boba.tool.kb.core.postgres import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.transport.fs import FsTransport, FsWalkRequestSource

__all__ = ["ConfluenceDocIngestCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.confluence_doc.ingest")


class ConfluenceDocIngestCliConfig(BobaFlatSettings):
    """Self-contained CLI-конфиг индексатора папки KbDoc-файлов.

    Config-секция: `[cli.kb.confluence_doc.ingest]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="cli.kb.confluence_doc.ingest",
        defaults_from=(
            "kb.storage",
            "postgres.{kb.storage:profile}",
            "embedding",
        ),
    )

    store: PostgresStoreConfig
    embedding: EmbeddingModel
    chunker: ChunkerParams
    folder: str = Field(
        description="Папка с KbDoc-файлами (`.md`) для индексации.",
    )
    collection: str = Field(
        default="kb_confluence_doc",
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


class ConfluenceDocIngestCli:
    """Operator-сборка KbDoc-ingest по абсолютной папке (`FsWalkRequestSource`)."""

    PIPELINE_ID: ClassVar[PipelineId] = PipelineId("kb.confluence_doc.ingest")

    @staticmethod
    def run_ingest(cfg: ConfluenceDocIngestCliConfig) -> dict[str, Any]:
        folder = Path(cfg.folder)
        if not folder.exists():
            msg = f"folder_not_found: {folder}"
            raise RuntimeError(msg)
        if not folder.is_dir():
            msg = f"folder_not_a_directory: {folder}"
            raise RuntimeError(msg)

        chunk_store = PostgresChunkStore(cfg=cfg.store)
        collections_store = PostgresCollectionsStore(cfg=cfg.store)
        embedder = cfg.embedding.build()
        chunker = cfg.chunker.build_chunker()

        result = ConfluenceDocIngest.run(
            request_source=FsWalkRequestSource(
                paths=[str(folder)],
                include=["*.md"],
            ),
            transport=FsTransport(),
            chunk_store=chunk_store,
            collections_store=collections_store,
            embedder=embedder,
            chunker=chunker,
            collection=cfg.collection,
            prune_missing=cfg.prune,
            pipeline_id=ConfluenceDocIngestCli.PIPELINE_ID,
        )
        return {"folder": str(folder), **result}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = ConfluenceDocIngestCliConfig()  # pyright: ignore[reportCallIssue]

    logger.info(
        "ingesting folder=%s → collection=%s (prune=%s)",
        cfg.folder,
        cfg.collection,
        cfg.prune,
    )

    start = time.monotonic()
    try:
        result = ConfluenceDocIngestCli.run_ingest(cfg)
    except Exception:
        logger.exception("confluence_doc.ingest FAILED")
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


if __name__ == "__main__":
    raise SystemExit(main())
