"""Общая запись Confluence-страниц в workspace для `confluence_*_download`-тулов.

`confluence_page_download` (явный список page_ids) и
`confluence_space_download` (весь space через discovery) делают одно и
то же тело: для каждого `HttpRequest` из переданного `RequestSource`
выполняется HTTP, JSON декодируется в HTML, при `as_markdown=True`
дополнительно конвертируется в Markdown через markdownify, и пишется
в `{dest_dir}/{page_id}.{html|md}` с YAML/HTML frontmatter'ом.

Caller (tool-обёртка) собирает `RequestSource` (Pages|Space|Cql) и
зовёт `download_pages`.
"""

from __future__ import annotations

from typing import Any

import httpx
import markdownify

from boba.indexing import PipelineContext, PipelineId, ReaderKeys, RequestSource
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpRequest
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["download_pages"]


def download_pages(  # noqa: PLR0913 — keyword-only helper, явный набор deps
    *,
    request_source: RequestSource[HttpRequest],
    conn: ConfluenceConnection,
    shell: ProjectWorkspaceShell,
    dest_dir: str,
    as_markdown: bool,
    pipeline_id: PipelineId,
) -> dict[str, Any]:
    """
    Скачивает страницы в workspace;
    формат — HTML (default) или Markdown.

    Возвращает `{dest_dir, saved: [{page_id,title,url,space_key,path,bytes}], total}`.
    """
    dest_dir = dest_dir.rstrip("/")
    try:
        if not shell.exists(dest_dir):
            shell.mkdir(dest_dir)
    except WorkspaceError as e:
        raise RuntimeError(
            f"Не удалось создать директорию {dest_dir!r}: {e}",
        ) from e

    transport = conn.make_transport()
    decoder = ConfluenceJsonDecoder(body_format=conn.body_format)
    pctx = PipelineContext(pipeline_id=pipeline_id)

    saved: list[dict[str, str]] = []
    try:
        for http_req in request_source.stream(pctx):
            page_id = http_req.metadata.get(ConfluenceKeys.PAGE_ID) or ""
            for raw in transport.stream(pctx, [http_req]):
                decoded = decoder.convert(raw)
                title = decoded.metadata.get(ReaderKeys.PAGE_TITLE) or ""
                url = decoded.source_id
                space_key = decoded.metadata.get(ConfluenceKeys.SPACE_KEY) or ""
                html = decoded.handle.read().decode("utf-8", errors="replace")
                if as_markdown:
                    body = markdownify.markdownify(html, heading_style="ATX")
                    frontmatter = _md_frontmatter(
                        url=url, title=title, page_id=page_id, space_key=space_key,
                    )
                    payload = (frontmatter + body).encode("utf-8")
                    ext = "md"
                else:
                    header = _html_header(
                        url=url, title=title, page_id=page_id, space_key=space_key,
                    )
                    payload = header + html.encode("utf-8")
                    ext = "html"
                path = f"{dest_dir}/{page_id}.{ext}"
                _write(shell, path, payload)
                saved.append({
                    "page_id": page_id,
                    "title": title,
                    "url": url,
                    "space_key": space_key,
                    "path": path,
                    "bytes": str(len(payload)),
                })
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence download failed: {type(e).__name__}: {e}",
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


def _html_header(
    *, url: str, title: str, page_id: str, space_key: str,
) -> bytes:
    """HTML-комментарий с источником страницы — для цитирования LLM."""
    lines = ["<!--", f"source: {url}", f"title: {title}", f"page_id: {page_id}"]
    if space_key:
        lines.append(f"space: {space_key}")
    lines.append("-->")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _md_frontmatter(
    *, url: str, title: str, page_id: str, space_key: str,
) -> str:
    """YAML-frontmatter с источником страницы (для kb_ingest и навигации)."""
    lines = ["---", f"source: {url}", f"title: {title}", f"page_id: {page_id}"]
    if space_key:
        lines.append(f"space: {space_key}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)
