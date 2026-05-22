"""
CLI-runner: bulk-скачать Confluence-space'ы на ФС.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import Field

from boba.indexing import PipelineId
from boba.settings import BobaSettingsConfigDict, StringList
from boba.tool.kb.confluence._download_common import download_pages
from boba.tool.kb.confluence.request_sources._common import (
    confluence_discover_spaces,
)
from boba.tool.kb.confluence.request_sources.space import (
    ConfluenceSpaceRequestSource,
)
from boba.tool.kb.confluence.tools.download.space import (
    ConfluenceDownloadSpaceConfig,
)

__all__ = ["ConfluenceDownloadSpacesCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.confluence.download.spaces")

_PIPELINE_ID: PipelineId = PipelineId("cli.confluence.download.spaces")


class ConfluenceDownloadSpacesCliConfig(ConfluenceDownloadSpaceConfig):
    """Self-contained CLI-конфиг bulk-download runner'а.

    Наследует поля `ConfluenceDownloadSpaceConfig` (`confluence`, `dest_dir`)
    — runner делает то же тело через `download_pages` для каждого
    discovered space-ключа. Сверху — runner-флаги через CLI (`use_cli=True`).

    Config-секция: `[cli.kb.confluence.download.spaces]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="cli.kb.confluence.download.spaces",
        defaults_from=("confluence",),
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
                "Список space-keys: скачать ТОЛЬКО их (skip discovery). "
                'CSV в env (`A,B`), TOML-array (`["A", "B"]`).'
            ),
        ),
    ] = []  # noqa: RUF012 — pydantic-side default, не shared mutable state

    skip: Annotated[
        StringList,
        Field(description="Список space-keys: исключить из discovery."),
    ] = []  # noqa: RUF012

    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Если true — конвертирует HTML в Markdown (`markdownify`, "
                "ATX-заголовки) и пишет `.md` с YAML-frontmatter."
            ),
        ),
    ] = False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = ConfluenceDownloadSpacesCliConfig()  # pyright: ignore[reportCallIssue]

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

    totals = {"saved": 0, "failed": 0}
    start = time.monotonic()
    processed = 0
    for i, key in enumerate(keys, start=1):
        processed = i
        space_start = time.monotonic()
        source = ConfluenceSpaceRequestSource(
            conn=cfg.confluence,
            space_key=key,
            body_format=cfg.confluence.body_format,
        )
        try:
            result = download_pages(
                request_source=source,
                conn=cfg.confluence,
                dest_dir=cfg.dest_dir,
                as_markdown=cfg.as_markdown,
                pipeline_id=_PIPELINE_ID,
            )
        except Exception:
            logger.exception(
                "[%d] space=%s — FAILED (continuing)",
                i,
                key,
            )
            totals["failed"] += 1
            continue

        space_saved = int(result.get("total", 0))
        totals["saved"] += space_saved
        logger.info(
            "[%d] space=%s saved=%d (%.1fs; cum saved=%d failed=%d) → %s",
            i,
            key,
            space_saved,
            time.monotonic() - space_start,
            totals["saved"],
            totals["failed"],
            result.get("dest_dir"),
        )

    if processed == 0:
        logger.warning("nothing to download — iterator was empty")
        return 0

    elapsed = time.monotonic() - start
    logger.info(
        "DONE: %d spaces in %.1fs — total saved=%d failed=%d → %s",
        processed,
        elapsed,
        totals["saved"],
        totals["failed"],
        cfg.dest_dir,
    )
    return 0 if totals["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
