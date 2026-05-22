"""Tool `confluence_space_download` + `ConfluenceSpaceDownloadConfig`.

Скачивает все страницы Confluence space в workspace. Discovery через
`/rest/api/space/{key}/content` с пагинацией; каждая страница пишется
как HTML (default) или Markdown.

LLM передаёт `space_key` + опц. `as_markdown`; connection и `dest_dir` —
из TOML-секции `[tool.kb.confluence.download.space]`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence._download_common import download_pages
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources.space import (
    ConfluenceSpaceRequestSource,
)
from boba.tools import FromConfig, tool

__all__ = ["ConfluenceSpaceDownloadConfig", "confluence_space_download"]

_PIPELINE_ID: PipelineId = PipelineId("confluence.space_download")


class ConfluenceSpaceDownloadConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_space_download`.

    Config-секция: `[tool.kb.confluence.download.space]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.download.space",
        defaults_from=("confluence",),
    )

    confluence: ConfluenceConnection
    dest_dir: str = Field(
        min_length=1,
        description="Директория внутри workspace для сохранения.",
    )


@tool
def confluence_space_download(
    cfg: Annotated[ConfluenceSpaceDownloadConfig, FromConfig()],
    space_key: Annotated[
        str,
        Field(
            min_length=1,
            description=("Confluence space key"),
        ),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=("конвертирует HTML в Markdown если true"),
        ),
    ] = False,
) -> dict[str, Any]:
    """
    Скачивает все страницы указанного Confluence space на ФС.

    Целевая директория — `cfg.dest_dir`. Файлы: `{dest_dir}/{page_id}.html`
    (HTML) или `{dest_dir}/{page_id}.md`. Возвращает
    `{dest_dir, space_key, saved, total}`.
    """
    source = ConfluenceSpaceRequestSource(
        conn=cfg.confluence,
        space_key=space_key,
        body_format=cfg.confluence.body_format,
    )
    result = download_pages(
        request_source=source,
        conn=cfg.confluence,
        dest_dir=cfg.dest_dir,
        as_markdown=as_markdown,
        pipeline_id=_PIPELINE_ID,
    )
    return {"space_key": space_key, **result}
