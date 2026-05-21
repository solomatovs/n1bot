"""Tool `confluence_space_download`: все страницы Confluence space → workspace.

Discovery через `/rest/api/space/{key}/content` с пагинацией; каждая
страница пишется в workspace как HTML (default) или Markdown.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.indexing import PipelineId
from boba.tool.kb.confluence._download_common import download_pages
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources.space import (
    ConfluenceSpaceRequestSource,
)
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["confluence_space_download"]

_PIPELINE_ID: PipelineId = PipelineId("confluence.space_download")


@tool
def confluence_space_download(
    space_key: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Confluence space key (например, `KAFKA`, `DOCS`). "
                "Скачиваются ВСЕ страницы space-а с пагинацией."
            ),
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
    """Скачивает все страницы указанного Confluence space в workspace.

    Файлы: `{dest_dir}/{page_id}.html` (HTML) или `{dest_dir}/{page_id}.md`.
    Возвращает `{dest_dir, space_key, saved, total}`.
    """
    source = ConfluenceSpaceRequestSource(
        base_url=cfg.base_url,
        auth=ConfluenceConnection.make_auth(cfg),
        space_key=space_key,
        body_format=cfg.body_format,
        timeout_sec=cfg.timeout_sec,
    )
    result = download_pages(
        request_source=source,
        cfg=cfg,
        shell=shell,
        dest_dir=dest_dir,
        as_markdown=as_markdown,
        pipeline_id=_PIPELINE_ID,
    )
    return {"space_key": space_key, **result}
