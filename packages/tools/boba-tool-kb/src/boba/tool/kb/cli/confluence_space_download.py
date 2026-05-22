"""
CLI-runner: скачать все страницы Confluence-space'а на ФС.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from pydantic import Field

from boba.indexing import PipelineId
from boba.settings import BobaSettingsConfigDict
from boba.tool.kb.confluence._download_common import download_pages
from boba.tool.kb.confluence.request_sources.space import (
    ConfluenceSpaceRequestSource,
)
from boba.tool.kb.confluence.tools.space_download import (
    ConfluenceSpaceDownloadConfig,
)

__all__ = ["ConfluenceSpaceDownloadCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.confluence_space_download")

_PIPELINE_ID: PipelineId = PipelineId("cli.confluence_space_download")


class ConfluenceSpaceDownloadCliConfig(ConfluenceSpaceDownloadConfig):
    """Self-contained CLI-конфиг скачивания Confluence-space'а.

    Наследует поля tool-конфига (`confluence`, `dest_dir`) — runner делает
    то же тело через `download_pages`. Сверху — runner-флаги через CLI
    (`use_cli=True`).

    Config-секция: `[cli.kb.confluence.download.space]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="cli.kb.confluence.download.space",
        defaults_from=("confluence",),
        use_cli=True,
    )

    space_key: Annotated[
        str,
        Field(
            min_length=1,
            description="Confluence space key (например, KAFKA).",
        ),
    ]

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

    cfg = ConfluenceSpaceDownloadCliConfig()  # pyright: ignore[reportCallIssue]

    logger.info(
        "downloading space=%s → %s (as_markdown=%s)",
        cfg.space_key,
        cfg.dest_dir,
        cfg.as_markdown,
    )

    source = ConfluenceSpaceRequestSource(
        conn=cfg.confluence,
        space_key=cfg.space_key,
        body_format=cfg.confluence.body_format,
    )

    start = time.monotonic()
    try:
        result = download_pages(
            request_source=source,
            conn=cfg.confluence,
            dest_dir=cfg.dest_dir,
            as_markdown=cfg.as_markdown,
            pipeline_id=_PIPELINE_ID,
        )
    except Exception:
        logger.exception("confluence_space_download FAILED")
        return 1
    elapsed = time.monotonic() - start

    logger.info(
        "DONE in %.1fs — space=%s saved=%d → %s",
        elapsed,
        cfg.space_key,
        result["total"],
        result["dest_dir"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
