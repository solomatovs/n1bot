"""Tool: скачать страницы Confluence как Markdown-файлы в workspace.

Симметричен `confluence_page_download` (HTML-варианту); разница только
в финальной трансформации: HTML → Markdown через `markdownify` (ATX-заголовки).
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
import markdownify
from pydantic import Field

from boba.indexing import PipelineContext, PipelineId, ReaderKeys
from boba.tool.confluence.config import ConfluencePluginConfig
from boba.tool.confluence.connection import ConfluenceConnection
from boba.tool.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.confluence.keys import ConfluenceKeys
from boba.tool.confluence.request_sources.pages import (
    ConfluencePagesRequestSource,
)
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["confluence_page_download_markdown"]


_PIPELINE_ID: PipelineId = PipelineId("confluence.page_download_markdown")


@tool
def confluence_page_download_markdown(
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
    cfg: Annotated[ConfluencePluginConfig, FromConfig()],
) -> dict[str, Any]:
    """Скачивает указанные страницы Confluence как Markdown-файлы в workspace.

    На каждую страницу создаётся файл `{dest_dir}/{page_id}.md` с Markdown-телом
    (HTML конвертируется через markdownify, ATX-заголовки). Дальше — file-tools.
    """
    dest_dir = dest_dir.rstrip("/")
    try:
        if not shell.exists(dest_dir):
            shell.mkdir(dest_dir)
    except WorkspaceError as e:
        raise RuntimeError(
            f"Не удалось создать директорию {dest_dir!r}: {e}",
        ) from e

    source = ConfluencePagesRequestSource(
        base_url=cfg.base_url,
        auth=ConfluenceConnection.make_auth(cfg),
        page_ids=page_ids,
        body_format=cfg.body_format,
    )
    transport = ConfluenceConnection.make_transport(cfg)
    decoder = ConfluenceJsonDecoder(body_format=cfg.body_format)
    pctx = PipelineContext(pipeline_id=_PIPELINE_ID)

    saved: list[dict[str, str]] = []
    try:
        for http_req in source.stream(pctx):
            page_id = http_req.metadata.get(ConfluenceKeys.PAGE_ID) or ""
            for raw in transport.stream(pctx, [http_req]):
                decoded = decoder.convert(raw)
                title = decoded.metadata.get(ReaderKeys.PAGE_TITLE) or ""
                url = decoded.source_id
                space_key = decoded.metadata.get(ConfluenceKeys.SPACE_KEY) or ""
                html = decoded.handle.read().decode("utf-8", errors="replace")
                md = markdownify.markdownify(html, heading_style="ATX")
                frontmatter = _md_frontmatter(
                    url=url, title=title, page_id=page_id, space_key=space_key,
                )
                md_bytes = (frontmatter + md).encode("utf-8")
                path = f"{dest_dir}/{page_id}.md"
                _write(shell, path, md_bytes)
                saved.append({
                    "page_id": page_id,
                    "title": title,
                    "url": url,
                    "space_key": space_key,
                    "path": path,
                    "bytes": str(len(md_bytes)),
                })
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence page download failed: {type(e).__name__}: {e}",
        ) from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка записи в workspace: {e}") from e

    return {
        "dest_dir": dest_dir,
        "saved": saved,
        "total": len(saved),
    }


def _write(shell: ProjectWorkspaceShell, path: str, payload: bytes) -> None:
    with shell.write_binary(path) as f:
        f.write(payload)


def _md_frontmatter(
    *, url: str, title: str, page_id: str, space_key: str,
) -> str:
    """YAML-frontmatter с источником страницы."""
    lines = [
        "---",
        f"source: {url}",
        f"title: {title}",
        f"page_id: {page_id}",
    ]
    if space_key:
        lines.append(f"space: {space_key}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)
