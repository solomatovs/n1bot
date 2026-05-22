"""Tool `confluence_page_download` + `ConfluencePageDownloadConfig`.

Скачивает явный список Confluence-страниц в workspace. HTML по умолчанию;
`as_markdown=True` конвертирует HTML через `markdownify` (ATX-заголовки)
и пишет `.md` с YAML-frontmatter.

LLM передаёт `page_ids` + опц. `as_markdown`; connection и `dest_dir` —
из TOML-секции `[tool.kb.confluence.download.page]`.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing import PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence._download_common import download_pages
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources.pages import ConfluencePagesRequestSource
from boba.tools import FromConfig, tool

__all__ = ["ConfluencePageDownloadConfig", "confluence_page_download"]

_PIPELINE_ID: PipelineId = PipelineId("confluence.page_download")


class ConfluencePageDownloadConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_page_download`.

    Config-секция: `[tool.kb.confluence.download.page]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.download.page",
        defaults_from=("confluence",),
    )

    confluence: ConfluenceConnection
    dest_dir: str = Field(
        min_length=1,
        description=(
            "Директория внутри workspace для сохранения "
            "(создаётся, если не существует)."
        ),
    )


@tool
def confluence_page_download(
    cfg: Annotated[ConfluencePageDownloadConfig, FromConfig()],
    page_ids: Annotated[
        list[str],
        Field(
            min_length=1,
            description="ID страниц для скачивания. JSON-массив строк.",
        ),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Если true — конвертирует HTML в Markdown (`markdownify`, "
                "ATX-заголовки) и пишет `.md` с YAML-frontmatter. По умолчанию "
                "сохраняется HTML с `<!--`-frontmatter."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Скачивает указанные страницы Confluence на ФС.

    Целевая директория — `cfg.dest_dir`. Файлы: `{dest_dir}/{page_id}.html`
    (HTML) или `{dest_dir}/{page_id}.md` (Markdown). Дальше —
    html_outline/html_section/file-tools.
    """
    source = ConfluencePagesRequestSource(
        base_url=cfg.confluence.base_url,
        auth=cfg.confluence.make_auth(),
        page_ids=page_ids,
        body_format=cfg.confluence.body_format,
    )
    return download_pages(
        request_source=source,
        conn=cfg.confluence,
        dest_dir=cfg.dest_dir,
        as_markdown=as_markdown,
        pipeline_id=_PIPELINE_ID,
    )
