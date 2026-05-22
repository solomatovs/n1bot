"""
CLI-runner: скачать явный список Confluence-страниц по их page_ids на ФС.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from pydantic import Field

from boba.indexing import PipelineId
from boba.settings import BobaSettingsConfigDict, StringList
from boba.tool.kb.confluence._download_common import download_pages
from boba.tool.kb.confluence.request_sources.pages import ConfluencePagesRequestSource
from boba.tool.kb.confluence.tools.download.page import (
    ConfluenceDownloadPageConfig,
)

__all__ = ["ConfluenceDownloadPageCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.confluence.download.page")

_PIPELINE_ID: PipelineId = PipelineId("cli.confluence.download.page")


class ConfluenceDownloadPageCliConfig(ConfluenceDownloadPageConfig):
    """Self-contained CLI-конфиг скачивания явного списка page_ids.

    Наследует поля tool-конфига (`confluence`, `dest_dir`) — runner делает
    то же тело через `download_pages` с `ConfluencePagesRequestSource`.
    Сверху — runner-флаги через CLI (`use_cli=True`).

    Config-секция: `[cli.kb.confluence.download.page]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="cli.kb.confluence.download.page",
        defaults_from=("confluence",),
        use_cli=True,
    )

    page_ids: Annotated[
        StringList,
        Field(
            description=(
                "ID страниц для скачивания. "
                'CSV в env (`123,456`), TOML-array (`["123", "456"]`), '
                "CLI `--page-ids 123,456`."
            ),
        ),
    ] = []  # noqa: RUF012 — pydantic-side default, не shared mutable state

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

    cfg = ConfluenceDownloadPageCliConfig()  # pyright: ignore[reportCallIssue]

    if not cfg.page_ids:
        logger.error(
            "page_ids пуст — нечего качать. Передай через TOML "
            "(`page_ids = [...]`), env (`BOBA_..._PAGE_IDS=a,b`) или "
            "CLI (`--page-ids a,b`).",
        )
        return 2

    logger.info(
        "downloading %d page(s) → %s (as_markdown=%s)",
        len(cfg.page_ids),
        cfg.dest_dir,
        cfg.as_markdown,
    )

    source = ConfluencePagesRequestSource(
        base_url=cfg.confluence.base_url,
        auth=cfg.confluence.make_auth(),
        page_ids=cfg.page_ids,
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
        logger.exception("confluence.download.page FAILED")
        return 1
    elapsed = time.monotonic() - start

    logger.info(
        "DONE in %.1fs — requested=%d saved=%d → %s",
        elapsed,
        len(cfg.page_ids),
        result["total"],
        result["dest_dir"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
