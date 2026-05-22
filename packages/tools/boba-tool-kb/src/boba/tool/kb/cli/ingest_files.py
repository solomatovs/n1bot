"""
CLI-runner: индексация локальной папки (`[tool.kb.files].folder`) в KB.

Не tool-функция (`@tool` нет), а операторский скрипт-обёртка над
`files_ingest`. Лежит в `core/cli/`, отдельно от tools, чтобы не
попадать в tool-allowlist'ы и не путаться при поиске.

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \
        .venv/bin/python -m boba.tool.kb.core.cli.ingest_files

Опции:
    --prune    FullCleanup: удалить из коллекции чанки, чьих source_id
               нет среди индексируемых файлов (по умолчанию off)

Папка и коллекция фиксируются оператором в config:
    [tool.kb.ingest_files]
    folder     = "./local/docs"
    collection = "kb_files"
    prune      = false
"""

from __future__ import annotations

import logging
import time

from dishka.entities.component import Component

from boba.agent import AgentBuilder
from boba.indexing import DispatchReader
from boba.indexing.embedder import Embedder
from boba.text import StructuralChunker
from boba.tool.kb.core import providers as kb_providers
from boba.tool.kb.core.chunk_store import PostgresChunkStore
from boba.tool.kb.core.collections_store import PostgresCollectionsStore
from boba.tool.kb.core.files_ingest_config import IngestFilesConfig
from boba.tool.kb.core.tools.ingest_files import ingest_files

__all__ = ["main"]

logger = logging.getLogger("boba.tool.kb.cli.ingest_files")


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
            cfg = req.get(IngestFilesConfig, component=_KB_COMPONENT)
            chunk_store = req.get(PostgresChunkStore, component=_KB_COMPONENT)
            collections_store = req.get(
                PostgresCollectionsStore,
                component=_KB_COMPONENT,
            )
            embedder = req.get(Embedder[str], component=_KB_COMPONENT)
            dispatch_reader = req.get(
                DispatchReader[str],
                component=_KB_COMPONENT,
            )
            chunker = req.get(StructuralChunker, component=_KB_COMPONENT)

            logger.info(
                "ingesting folder=%s → collection=%s (prune=%s)",
                cfg.folder,
                cfg.collection,
                cfg.prune,
            )

            start = time.monotonic()
            try:
                result = ingest_files(
                    chunk_store=chunk_store,
                    collections_store=collections_store,
                    embedder=embedder,
                    dispatch_reader=dispatch_reader,
                    chunker=chunker,
                    cfg=cfg,
                )
            except Exception:
                logger.exception("files_ingest FAILED")
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


if __name__ == "__main__":
    raise SystemExit(main())
