"""
CLI-runner: индексация Confluence-space'ов в KB.

Не tool-функция (`@tool` нет), а отдельный скрипт-обёртка над
`confluence_space_ingest`. Лежит рядом с tools для удобства поиска.

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \
        .venv/bin/python -m boba.tool.kb.confluence.tools.ingest_all_spaces

Опции:
    --type {global|personal|any}  фильтр по типу space (default: global)
    --only KEY1,KEY2,...          ингестить только эти space-ключи (skip discovery)
    --skip KEY1,KEY2,...          пропустить эти space-ключи
    --dry-run                     только discover и распечатать список — без ingest
    --prune                       FullCleanup в каждой коллекции (по умолчанию off)

"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from dishka.entities.component import Component
from pydantic import Field

from boba.agent import AgentBuilder
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList
from boba.text import StructuralChunker
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.request_sources._common import (
    confluence_discover_spaces,
)
from boba.tool.kb.confluence.tools.space_ingest import confluence_space_ingest
from boba.tool.kb.core import providers as kb_providers
from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.vector_store import PostgresVectorStore

__all__ = ["IngestAllSpacesConfig", "main"]

logger = logging.getLogger("boba.tool.kb.confluence.tools.ingest_all_spaces")


class IngestAllSpacesConfig(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        config_path="tool.kb.ingest_all_spaces",
        use_cli=True,
    )

    space_type: Annotated[
        Literal["global", "personal", "any"],
        Field(description="Фильтр по типу space при discovery."),
    ] = "global"

    only: Annotated[
        StringList,
        Field(
            description=(
                "Список space-keys: ингестить ТОЛЬКО их (skip discovery). "
                'CSV в env (`A,B`), TOML-array (`["A", "B"]`).'
            ),
        ),
    ] = []  # noqa: RUF012 — pydantic-side default, не shared mutable state

    skip: Annotated[
        StringList,
        Field(
            description="Список space-keys: исключить из discovery.",
        ),
    ] = []  # noqa: RUF012

    prune: Annotated[
        bool,
        Field(description="prune_missing=True для каждого space."),
    ] = False


_KB_COMPONENT = Component(kb_providers.__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = IngestAllSpacesConfig()

    builder = AgentBuilder().use_plugin(kb_providers)
    container = builder.di.build_container()
    conn_cfg = ConfluenceConnectionConfig()

    try:
        with container() as req:
            if args.only:
                logger.info("using --only override: %d space-keys", len(args.only))
                keys: Iterator[str] = iter(args.only)
            else:
                logger.info("discovering spaces (type=%s)…", args.space_type)
                keys = iter(confluence_discover_spaces(conn_cfg, args.space_type))

            skip_set = set(args.skip)
            if skip_set:
                logger.info(
                    "will lazily skip %d keys: %s",
                    len(skip_set),
                    sorted(skip_set),
                )
                keys = (k for k in keys if k not in skip_set)

            # Resolve heavy KB-deps из DI
            kb_cfg = req.get(KbConfig, component=_KB_COMPONENT)
            store = req.get(PostgresVectorStore, component=_KB_COMPONENT)
            chunker = req.get(StructuralChunker, component=_KB_COMPONENT)

            # Per-space loop (streaming, без материализации списка)
            totals = {
                "indexed": 0,
                "skipped_unchanged": 0,
                "pruned": 0,
                "failed": 0,
            }
            start = time.monotonic()
            processed = 0
            for i, key in enumerate(keys, start=1):
                processed = i
                space_start = time.monotonic()
                try:
                    result: dict[str, Any] = confluence_space_ingest(
                        store=store,
                        chunker=chunker,
                        kb_cfg=kb_cfg,
                        conn_cfg=conn_cfg,
                        space_keys=[key],
                        prune_missing=args.prune,
                    )
                except Exception:
                    logger.exception(
                        "[%d] space=%s — FAILED (continuing)",
                        i,
                        key,
                    )
                    totals["failed"] += 1
                    continue

                for k in ("indexed", "skipped_unchanged", "pruned", "failed"):
                    totals[k] += int(result.get(k, 0))
                logger.info(
                    "[%d] space=%s indexed=%d skipped=%d pruned=%d failed=%d "
                    "(%.1fs; cum indexed=%d skipped=%d failed=%d)",
                    i,
                    key,
                    result.get("indexed", 0),
                    result.get("skipped_unchanged", 0),
                    result.get("pruned", 0),
                    result.get("failed", 0),
                    time.monotonic() - space_start,
                    totals["indexed"],
                    totals["skipped_unchanged"],
                    totals["failed"],
                )

            if processed == 0:
                logger.warning("nothing to ingest — iterator was empty")
                return 0

            elapsed = time.monotonic() - start
            logger.info(
                "DONE: %d spaces in %.1fs — total indexed=%d skipped_unchanged=%d "
                "pruned=%d failed=%d",
                processed,
                elapsed,
                totals["indexed"],
                totals["skipped_unchanged"],
                totals["pruned"],
                totals["failed"],
            )
            return 0 if totals["failed"] == 0 else 1
    finally:
        container.close()


if __name__ == "__main__":
    raise SystemExit(main())
