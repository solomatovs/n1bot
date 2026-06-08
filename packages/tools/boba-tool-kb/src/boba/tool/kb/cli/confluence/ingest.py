"""CLI-runner: Confluence HTTP-ingest в KB.

Покрывает все варианты HTTP-индексации одной командой:

- page_ids=[] + only=[] + skip=[] -> discovery всех spaces (space_type),
  per-space loop через confluence_ingest_spaces(space_keys=[key]) с
  агрегированными метриками (per-key для операторской видимости и
  continue-on-failure).
- page_ids=[] + only=[A, B] -> ингест только указанных space-ключей
  (skip discovery), skip всё ещё применяется как blacklist.
- page_ids=[ID1, ID2] -> ингест явных страниц через
  confluence_ingest_pages(page_ids=[...]). only/skip/space_type
  игнорируются (warn в лог если заданы).

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.confluence.ingest \\
        [--page-ids 123,456 | --only KEY1,KEY2 | --type global]

В kb_chunks сам не пишет — делегирует в confluence_ingest_pages /
confluence_ingest_spaces. Все параметры (store/embedding/chunker/confluence/
collection + runner-флаги) лежат в секции [cli.kb.confluence.ingest].
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field

from boba.settings import StringList, bind, build_app_config
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_pages import confluence_ingest_pages
from boba.tool.kb.confluence.ingest_spaces import confluence_ingest_spaces
from boba.tool.kb.confluence.request_sources import ConfluencePaginator

__all__ = ["ConfluenceIngestCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.confluence.ingest")


class ConfluenceIngestCliConfig(ConfluenceIngestConfig):
    """Self-contained CLI-конфиг HTTP-ingest runner'а.

    Наследует поля ConfluenceIngestConfig
    (connection/tables/embedding/chunker/confluence/collection). CLI добавляет
    своими полями only/skip/space_type/prune бизнес-логику discovery +
    per-space loop поверх tool-функций.

    Config-секция: [cli.kb.confluence.ingest].
    """

    model_config = ConfigDict(extra="ignore")

    space_type: Annotated[
        Literal["global", "personal", "any"],
        Field(description="Фильтр по типу space при discovery."),
    ] = "global"

    only: Annotated[
        StringList,
        Field(
            description=(
                "Список space-keys: индексировать ТОЛЬКО их (skip discovery). "
            ),
        ),
    ] = []  # noqa: RUF012 — pydantic-side default, не shared mutable state

    skip: Annotated[
        StringList,
        Field(description="Список space-keys: исключить из discovery/`only`."),
    ] = []  # noqa: RUF012

    page_ids: Annotated[
        StringList,
        Field(
            description=(
                "Список page_id: индексировать только эти страницы. "
                "Перекрывает `only`/`skip`/`space_type`"
            ),
        ),
    ] = []  # noqa: RUF012

    prune: Annotated[
        bool,
        Field(description="prune_missing=True для каждого space / page-batch."),
    ] = False

    force_update: Annotated[
        bool,
        Field(
            description=(
                "force_update=True для каждого space / page-batch: "
                "переэмбеддить все чанки затронутых страниц (минуя skip-by-hash) "
                "и снести их стейл, включая снятые вложения."
            ),
        ),
    ] = False


class ConfluenceIngestCli:
    """Operator-логика discovery + per-space loop поверх confluence_ingest_*."""

    @staticmethod
    def run_page_ids_mode(cfg: ConfluenceIngestCliConfig) -> int:
        if cfg.only or cfg.skip:
            logger.warning(
                "page_ids задан — игнорирую only=%s, skip=%s, space_type=%s",
                list(cfg.only),
                list(cfg.skip),
                cfg.space_type,
            )
        logger.info(
            "ingesting %d page(s) -> collection=%s (prune=%s force_update=%s)",
            len(cfg.page_ids),
            cfg.collection,
            cfg.prune,
            cfg.force_update,
        )

        start = time.monotonic()
        try:
            result: dict[str, Any] = dict(
                confluence_ingest_pages(
                    cfg=cfg,
                    page_ids=list(cfg.page_ids),
                    prune_missing=cfg.prune,
                    force_update=cfg.force_update,
                ).rows[0],
            )
        except Exception:
            logger.exception("confluence.ingest page_ids-mode FAILED")
            return 1
        elapsed = time.monotonic() - start

        logger.info(
            "DONE in %.1fs — indexed=%d skipped_unchanged=%d pruned=%d failed=%d",
            elapsed,
            result.get("indexed", 0),
            result.get("skipped_unchanged", 0),
            result.get("pruned", 0),
            result.get("failed", 0),
        )
        return 0 if result.get("failed", 0) == 0 else 1

    @staticmethod
    def run_spaces_mode(cfg: ConfluenceIngestCliConfig) -> int:
        if cfg.only:
            logger.info("using only=%d space-keys (skip discovery)", len(cfg.only))
            keys: Iterator[str] = iter(cfg.only)
        else:
            logger.info("discovering spaces (type=%s)…", cfg.space_type)
            conn = ConfluenceConnection(
                profile=cfg.confluence,
                body_format=cfg.body_format,
            )
            keys = iter(
                ConfluencePaginator.discover_spaces(conn, cfg.space_type),
            )

        skip_set = set(cfg.skip)
        if skip_set:
            logger.info(
                "will lazily skip %d keys: %s",
                len(skip_set),
                sorted(skip_set),
            )
            keys = (k for k in keys if k not in skip_set)

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
                row: dict[str, Any] = dict(
                    confluence_ingest_spaces(
                        cfg=cfg,
                        space_keys=[key],
                        prune_missing=cfg.prune,
                        force_update=cfg.force_update,
                    ).rows[0],
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
                totals[k] += int(row.get(k, 0))
            logger.info(
                "[%d] space=%s indexed=%d skipped=%d pruned=%d failed=%d "
                "(%.1fs; cum indexed=%d skipped=%d failed=%d)",
                i,
                key,
                row.get("indexed", 0),
                row.get("skipped_unchanged", 0),
                row.get("pruned", 0),
                row.get("failed", 0),
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = build_app_config(sys.argv[1:])
    cfg = bind(config, "cli.kb.confluence.ingest", ConfluenceIngestCliConfig)

    if cfg.page_ids:
        return ConfluenceIngestCli.run_page_ids_mode(cfg)
    return ConfluenceIngestCli.run_spaces_mode(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
