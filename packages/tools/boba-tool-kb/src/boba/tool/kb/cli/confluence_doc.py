"""CLI-runner: индексация папки KbDoc-файлов в KB.

Оператор готовит md чанки и они по возможности индексируются как есть
если умещаются в максимальный размер чанка

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.confluence_doc
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field

from boba.settings import bind, build_app_config
from boba.tool.kb.confluence_doc.ingest import ConfluenceDocIngest
from boba.tool.kb.core.chunking import ChunkerParams, StructuralChunkerFactory
from boba.tool.kb.core.embedding import EmbeddingModel, LocalFastEmbedEmbedderFactory
from boba.tool.kb.core.postgres import (
    PostgresChunkStore,
    PostgresCollectionsStore,
    PostgresStoreConfig,
)
from boba.transport.fs import FsTransport, FsWalkRequestSource

__all__ = ["ConfluenceDocIngestCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.confluence_doc")


class ConfluenceDocIngestCliConfig(PostgresStoreConfig, ChunkerParams):
    """Self-contained CLI-конфиг индексатора папки KbDoc-файлов.

    Наследует PostgresStoreConfig (connection/tables — плоско) и ChunkerParams
    (chunk_size/chunk_overlap — плоско). Config-секция:
    [cli.kb.confluence_doc.ingest].
    """

    model_config = ConfigDict(extra="ignore")

    embedding: EmbeddingModel
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
    """Operator-сборка KbDoc-ingest по абсолютной папке (FsWalkRequestSource)."""

    @staticmethod
    def run_ingest(cfg: ConfluenceDocIngestCliConfig) -> dict[str, Any]:
        folder = Path(cfg.folder)
        if not folder.exists():
            msg = f"folder_not_found: {folder}"
            raise RuntimeError(msg)
        if not folder.is_dir():
            msg = f"folder_not_a_directory: {folder}"
            raise RuntimeError(msg)

        chunk_store = PostgresChunkStore(cfg=cfg)
        collections_store = PostgresCollectionsStore(cfg=cfg)
        embedder = LocalFastEmbedEmbedderFactory.build(cfg.embedding)
        chunker = StructuralChunkerFactory.build(cfg)

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
        )
        return {"folder": str(folder), **result}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # получаем настройки всего приложения
    if (config_path := os.environ.get("BOBA_CONFIG_PATH")) is None:
        raise ValueError("please pass env BOBA_CONFIG_PATH")

    config = build_app_config(config_path=Path(config_path))
    cfg = bind(config, "cli.kb.confluence_doc.ingest", ConfluenceDocIngestCliConfig)

    logger.info(
        "ingesting folder=%s -> collection=%s (prune=%s)",
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
