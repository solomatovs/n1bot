"""CLI-runner: bulk-ingest Confluence-space'ов в KB через `confluence_space_ingest`.

Обёртка над одноимённым tool'ом: discovery всех spaces через
`confluence_discover_spaces` (или `--only`-override) и per-space loop с
агрегированными метриками.

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.ingest_confluence_spaces

Опции (CLI-флаги через `use_cli=True`):
    --type {global|personal|any}  фильтр по типу space (default: global)
    --only KEY1,KEY2,...          ингестить только эти space-ключи (skip discovery)
    --skip KEY1,KEY2,...          пропустить эти space-ключи
    --prune                       prune_missing=True для каждого space

Все параметры (store/embedding/chunker/confluence/collection + runner-флаги)
лежат в секции `[cli.kb.ingest_confluence_spaces]`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from pydantic import Field

from boba.settings import BobaSettingsConfigDict, StringList
from boba.tool.kb.confluence.request_sources._common import (
    confluence_discover_spaces,
)
from boba.tool.kb.confluence.tools.space_ingest import (
    ConfluenceSpaceIngestConfig,
    confluence_space_ingest,
)

__all__ = ["IngestAllSpacesConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.ingest_confluence_spaces")


class IngestAllSpacesConfig(ConfluenceSpaceIngestConfig):
    """Self-contained CLI-конфиг bulk-ingest runner'а.

    Наследует все поля `ConfluenceSpaceIngestConfig`
    (store/embedding/chunker/confluence/collection) — runner передаёт `self`
    в tool-функцию `confluence_space_ingest` (IS-A парент). Сверху —
    runner-флаги, доступные ещё и через CLI (`use_cli=True`).

    Config-секция: `[cli.kb.ingest_confluence_spaces]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="cli.kb.ingest_confluence_spaces",
        defaults_from=("postgres", "kb.storage", "embedding", "confluence"),
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = IngestAllSpacesConfig()  # pyright: ignore[reportCallIssue]

    if cfg.only:
        logger.info("using --only override: %d space-keys", len(cfg.only))
        keys: Iterator[str] = iter(cfg.only)
    else:
        logger.info("discovering spaces (type=%s)…", cfg.space_type)
        keys = iter(
            confluence_discover_spaces(cfg.confluence, cfg.space_type),
        )

    skip_set = set(cfg.skip)
    if skip_set:
        logger.info(
            "will lazily skip %d keys: %s",
            len(skip_set),
            sorted(skip_set),
        )
        keys = (k for k in keys if k not in skip_set)

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
                cfg=cfg,
                space_keys=[key],
                prune_missing=cfg.prune,
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


if __name__ == "__main__":
    raise SystemExit(main())
