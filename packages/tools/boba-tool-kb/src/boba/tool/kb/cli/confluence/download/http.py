"""CLI-runner: unified Confluence HTTP-download на ФС.

Покрывает все варианты скачивания одной командой:

- `page_ids=[]` + `only=[]` + `skip=[]` → bulk discovery (`space_type`),
  per-space loop с агрегированными метриками.
- `page_ids=[]` + `only=[A, B]` → скачать только указанные space-ключи
  (skip discovery); `skip` всё ещё применяется как blacklist.
- `page_ids=[ID1, ID2]` → скачать явные страницы через
  `ConfluencePagesRequestSource`. `only`/`skip`/`space_type` игнорируются
  (warn в лог если заданы).

Применение:
    BOBA_CONFIG_PATH=./local/config.toml \\
        .venv/bin/python -m boba.tool.kb.cli.confluence.download.http \\
        [--page-ids 123,456 | --only KEY1,KEY2 | --type global]

Все параметры (confluence/dest_dir + runner-флаги) лежат в секции
`[cli.kb.confluence.download]`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from pydantic import Field

from boba.indexing import PipelineId
from boba.settings import BobaSettingsConfigDict, StringList
from boba.tool.kb.confluence._download_common import download_pages
from boba.tool.kb.confluence.request_sources._common import (
    confluence_discover_spaces,
)
from boba.tool.kb.confluence.request_sources.pages import (
    ConfluencePagesRequestSource,
)
from boba.tool.kb.confluence.request_sources.space import (
    ConfluenceSpaceRequestSource,
)
from boba.tool.kb.confluence.tools.download import (
    ConfluenceDownloadConfig,
)

__all__ = ["ConfluenceDownloadCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.confluence.download.http")

_PIPELINE_ID: PipelineId = PipelineId("cli.confluence.download")


class ConfluenceDownloadCliConfig(ConfluenceDownloadConfig):
    """Self-contained CLI-конфиг unified HTTP-download runner'а.

    Наследует поля `ConfluenceDownloadConfig` (`confluence`, `dest_dir`)
    — runner делает то же тело через `download_pages`, переключая
    `RequestSource` по входным фильтрам.

    Config-секция: `[cli.kb.confluence.download]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="cli.kb.confluence.download",
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
                "Список space-keys: качать ТОЛЬКО их (skip discovery). "
                'CSV в env (`A,B`), TOML-array (`["A", "B"]`).'
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
                "Список page_id: качать ТОЛЬКО эти страницы. Перекрывает "
                "`only`/`skip`/`space_type` (warn в лог если заданы вместе). "
                'CSV в env (`123,456`), TOML-array (`["123", "456"]`).'
            ),
        ),
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


def _run_page_ids_mode(cfg: ConfluenceDownloadCliConfig) -> int:
    if cfg.only or cfg.skip:
        logger.warning(
            "page_ids задан — игнорирую only=%s, skip=%s, space_type=%s",
            list(cfg.only),
            list(cfg.skip),
            cfg.space_type,
        )
    logger.info(
        "downloading %d page(s) → %s (as_markdown=%s)",
        len(cfg.page_ids),
        cfg.dest_dir,
        cfg.as_markdown,
    )

    source = ConfluencePagesRequestSource(
        base_url=cfg.confluence.base_url,
        auth=cfg.confluence.make_auth(),
        page_ids=list(cfg.page_ids),
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
        logger.exception("confluence.download page_ids-mode FAILED")
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


def _run_spaces_mode(cfg: ConfluenceDownloadCliConfig) -> int:
    if cfg.only:
        logger.info("using only=%d space-keys (skip discovery)", len(cfg.only))
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
            result: dict[str, Any] = download_pages(
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = ConfluenceDownloadCliConfig()  # pyright: ignore[reportCallIssue]

    if cfg.page_ids:
        return _run_page_ids_mode(cfg)
    return _run_spaces_mode(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
