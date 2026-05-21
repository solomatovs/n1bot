"""Tool `confluence_page_download`: явный список Confluence-страниц → workspace.

HTML по умолчанию; `as_markdown=True` конвертирует HTML через `markdownify`
(ATX-заголовки) и пишет `.md` с YAML-frontmatter.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing import PipelineId
from boba.tool.kb.confluence._download_common import download_pages
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources.pages import ConfluencePagesRequestSource
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["confluence_page_download"]

_PIPELINE_ID: PipelineId = PipelineId("confluence.page_download")


@tool
def confluence_page_download(
    page_ids: Annotated[
        list[str],
        Field(
            min_length=1,
            description="ID страниц для скачивания. JSON-массив строк.",
        ),
    ],
    dest_dir: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Директория внутри workspace для сохранения "
                "(создаётся, если не существует)."
            ),
        ),
    ],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[ConfluenceConnectionConfig, FromConfig()],
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
    """Скачивает указанные страницы Confluence в workspace.

    Файлы: `{dest_dir}/{page_id}.html` (HTML) или `{dest_dir}/{page_id}.md`
    (Markdown). Дальше — html_outline/html_section/file-tools.
    """
    source = ConfluencePagesRequestSource(
        base_url=cfg.base_url,
        auth=ConfluenceConnection.make_auth(cfg),
        page_ids=page_ids,
        body_format=cfg.body_format,
    )
    return download_pages(
        request_source=source,
        cfg=cfg,
        shell=shell,
        dest_dir=dest_dir,
        as_markdown=as_markdown,
        pipeline_id=_PIPELINE_ID,
    )
