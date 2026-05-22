"""CLI-runner: индексация локальной папки в KB через `files_ingest`-tool.

Не tool-функция (нет `@tool` декоратора), а операторский скрипт-обёртка
над одноимённой tool-функцией. Лежит в `cli/`, отдельно от `core/tools/`,
чтобы не попадать в tool-allowlist'ы.

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.files_ingest

Все параметры (folder, collection, prune, connection, tables, embedding,
chunker) фиксируются оператором в TOML-секции `[tool.kb.files_ingest]`.
"""

from __future__ import annotations

import logging
import time

from dishka.entities.component import Component

from boba.agent import AgentBuilder
from boba.indexing import DispatchReader
from boba.tool.kb.core import providers as kb_providers
from boba.tool.kb.core.tools.files_ingest import FilesIngestConfig, files_ingest

__all__ = ["main"]

logger = logging.getLogger("boba.tool.kb.cli.files_ingest")

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
            cfg = req.get(FilesIngestConfig, component=_KB_COMPONENT)
            dispatch_reader = req.get(
                DispatchReader[str],
                component=_KB_COMPONENT,
            )

            logger.info(
                "ingesting folder=%s → collection=%s (prune=%s)",
                cfg.folder,
                cfg.collection,
                cfg.prune,
            )

            start = time.monotonic()
            try:
                result = files_ingest(
                    cfg=cfg,
                    dispatch_reader=dispatch_reader,
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
