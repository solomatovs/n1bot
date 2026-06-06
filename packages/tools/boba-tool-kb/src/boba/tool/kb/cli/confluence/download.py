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
        .venv/bin/python -m boba.tool.kb.cli.confluence.download \\
        [--page-ids 123,456 | --only KEY1,KEY2 | --type global]
        [--attachment-media-types application/pdf,image/*]
        [--attachment-titles *.pdf,report-*]

Allowlist-фильтры вложений (`--attachment-media-types`, `--attachment-titles`)
работают по fnmatch-globs; OR-семантика между списками; пустые → старое
поведение (качаются ВСЕ вложения). Отсеянные не запрашиваются по HTTP.

Все параметры (confluence/dest_dir + runner-флаги) лежат в секции
`[cli.kb.confluence.download]`.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import ConfigDict, Field

from boba.agent.workspace_fs import FsWorkspaceShell
from boba.indexing import PipelineId
from boba.settings import StringList, bind, build_app_config
from boba.tool.kb.confluence.download import (
    ConfluenceDownloadConfig,
    ConfluenceDownloader,
)
from boba.tool.kb.confluence.models import AttachmentFilter
from boba.tool.kb.confluence.request_sources import (
    ConfluencePagesRequestSource,
    ConfluencePaginator,
    ConfluenceSpaceRequestSource,
)
from boba.workspace.contract import WorkspaceId, WorkspaceShell

__all__ = ["ConfluenceDownloadCliConfig", "main"]

logger = logging.getLogger("boba.tool.kb.cli.confluence.download")


class ConfluenceDownloadCliConfig(ConfluenceDownloadConfig):
    """Self-contained CLI-конфиг unified HTTP-download runner'а.

    Наследует поля `ConfluenceDownloadConfig` (`confluence`, `dest_dir`)
    — runner делает то же тело через `ConfluenceDownloader.run`, переключая
    `RequestSource` по входным фильтрам.

    Config-секция: `[cli.kb.confluence.download]`.
    """

    model_config = ConfigDict(extra="ignore")

    space_type: Annotated[
        Literal["global", "personal", "any"],
        Field(description="Фильтр по типу space при discovery"),
    ] = "global"

    only: Annotated[
        StringList,
        Field(
            description=(
                "Список space-keys: качать ТОЛЬКО их (skip discovery)"
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

    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Если true — конвертирует HTML в Markdown (`markdownify`, "
                "ATX-заголовки) и пишет `.md` с YAML-frontmatter."
            ),
        ),
    ] = False

    dest_dir: str = Field(
        default="./local/downloads",
        min_length=1,
        description=(
            "FS-путь куда писать (создаётся, если не существует)"
        ),
    )


class ConfluenceDownloadCli:
    """Operator-логика discovery + per-space loop поверх `ConfluenceDownloader`."""

    PIPELINE_ID: ClassVar[PipelineId] = PipelineId("cli.confluence.download")

    @staticmethod
    def build_attachment_filter(cfg: ConfluenceDownloadCliConfig) -> AttachmentFilter:
        flt = AttachmentFilter.from_lists(
            media_types=cfg.attachment_media_types,
            titles=cfg.attachment_titles,
        )
        if not flt.is_passthrough():
            logger.info(
                "attachment filter: media_types=%s titles=%s",
                list(flt.media_type_patterns),
                list(flt.title_patterns),
            )
        return flt

    @staticmethod
    def run_page_ids_mode(
        cfg: ConfluenceDownloadCliConfig, shell: WorkspaceShell,
    ) -> int:
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
            auth=cfg.confluence.profile.auth.httpx_auth(),
            page_ids=list(cfg.page_ids),
            body_format=cfg.confluence.body_format,
        )

        att_filter = ConfluenceDownloadCli.build_attachment_filter(cfg)
        start = time.monotonic()
        try:
            result = ConfluenceDownloader.run(
                shell=shell,
                request_source=source,
                conn=cfg.confluence,
                dest_dir=cfg.dest_dir,
                as_markdown=cfg.as_markdown,
                pipeline_id=ConfluenceDownloadCli.PIPELINE_ID,
                attachment_filter=att_filter,
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

    @staticmethod
    def run_spaces_mode(
        cfg: ConfluenceDownloadCliConfig, shell: WorkspaceShell,
    ) -> int:
        if cfg.only:
            logger.info("using only=%d space-keys (skip discovery)", len(cfg.only))
            keys: Iterator[str] = iter(cfg.only)
        else:
            logger.info("discovering spaces (type=%s)…", cfg.space_type)
            keys = iter(
                ConfluencePaginator.discover_spaces(cfg.confluence, cfg.space_type),
            )

        skip_set = set(cfg.skip)
        if skip_set:
            logger.info(
                "will lazily skip %d keys: %s",
                len(skip_set),
                sorted(skip_set),
            )
            keys = (k for k in keys if k not in skip_set)

        att_filter = ConfluenceDownloadCli.build_attachment_filter(cfg)
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
                result: dict[str, Any] = ConfluenceDownloader.run(
                    shell=shell,
                    request_source=source,
                    conn=cfg.confluence,
                    dest_dir=cfg.dest_dir,
                    as_markdown=cfg.as_markdown,
                    pipeline_id=ConfluenceDownloadCli.PIPELINE_ID,
                    attachment_filter=att_filter,
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

    config = build_app_config(sys.argv[1:])
    cfg = bind(config, "cli.kb.confluence.download", ConfluenceDownloadCliConfig)

    # CWD как корень — `dest_dir` интерпретируется shell'ом как обычный
    # FS-путь (без обёртки workspace_root).
    shell: WorkspaceShell = FsWorkspaceShell(
        workspace_id=WorkspaceId("cli-confluence-download"),
        root=Path("."),
    )
    logger.info("writing to dest_dir=%s (relative to CWD)", cfg.dest_dir)

    if cfg.page_ids:
        return ConfluenceDownloadCli.run_page_ids_mode(cfg, shell)

    return ConfluenceDownloadCli.run_spaces_mode(cfg, shell)


if __name__ == "__main__":
    raise SystemExit(main())
