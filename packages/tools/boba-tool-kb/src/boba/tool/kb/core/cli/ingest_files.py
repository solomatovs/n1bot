"""
CLI-runner: индексация локальной папки (`[tool.kb].files_folder`) в KB.

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
    [tool.kb]
    files_folder = "./local/docs"
    collection   = "knowledge_base"

CLI их не переопределяет — это by design (см. docstring `files_ingest`).
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from dishka.entities.component import Component
from pydantic import Field

from boba.agent import AgentBuilder
from boba.indexing import DispatchReader
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.text import StructuralChunker
from boba.tool.kb.core import providers as kb_providers
from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.tools.files_ingest import files_ingest
from boba.tool.kb.core.vector_store import PostgresVectorStore

__all__ = ["IngestFilesConfig", "main"]

logger = logging.getLogger("boba.tool.kb.core.cli.ingest_files")


class IngestFilesConfig(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        config_path="tool.kb.ingest_files",
        use_cli=True,
    )

    prune: Annotated[
        bool,
        Field(
            description=(
                "prune_missing=True: удалить из коллекции чанки, чьих "
                "source_id нет среди файлов в `files_folder`."
            ),
        ),
    ] = False


_KB_COMPONENT = Component(kb_providers.__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = IngestFilesConfig()

    builder = AgentBuilder().use_plugin(kb_providers)
    container = builder.di.build_container()

    try:
        with container() as req:
            kb_cfg = req.get(KbConfig, component=_KB_COMPONENT)
            store = req.get(PostgresVectorStore, component=_KB_COMPONENT)
            dispatch_reader = req.get(
                DispatchReader[str],
                component=_KB_COMPONENT,
            )
            chunker = req.get(StructuralChunker, component=_KB_COMPONENT)

            logger.info(
                "ingesting folder=%s → collection=%s (prune=%s)",
                kb_cfg.files_folder,
                kb_cfg.collection,
                args.prune,
            )

            start = time.monotonic()
            try:
                result = files_ingest(
                    store=store,
                    dispatch_reader=dispatch_reader,
                    chunker=chunker,
                    cfg=kb_cfg,
                    prune_missing=args.prune,
                )
            except Exception:
                logger.exception("files_ingest FAILED")
                return 1
            elapsed = time.monotonic() - start

            logger.info(
                "DONE in %.1fs — indexed=%d skipped_unchanged=%d "
                "pruned=%d failed=%d",
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
