"""Общая запись Confluence-страниц на ФС для `confluence_*_download`-тулов.

`confluence_page_download` (явный список page_ids) и
`confluence_space_download` (весь space через discovery) делают одно и
то же тело: для каждого `HttpRequest` из переданного `RequestSource`
выполняется HTTP, JSON декодируется в HTML, при `as_markdown=True`
дополнительно конвертируется в Markdown через markdownify, и пишется
в `{dest_dir}/{space_key}/{ancestor_title}/.../{page_id}.{html|md}`
с YAML/HTML frontmatter'ом — поддиректории повторяют структуру
Confluence-URL: `{space_key}` сверху, затем дерево предков страницы
(root → direct parent). Если `space_key` отсутствует в metadata,
этот уровень пропускается.

Caller (tool-обёртка) собирает `RequestSource` (Pages|Space|Cql) и
зовёт `download_pages`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import markdownify

from boba.indexing import PipelineContext, PipelineId, ReaderKeys, RequestSource
from boba.tool.kb.confluence._pipeline_common import iter_confluence_documents
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.transport.http import HttpRequest

__all__ = ["download_pages"]

_FS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FS_COMPONENT_MAX = 120


def _sanitize_component(name: str) -> str:
    """Filesystem-safe имя компонента пути из произвольного title.

    Заменяет запрещённые символы на `_`, схлопывает пробелы, обрезает
    точки/пробелы по краям (Windows-совместимость) и длину до 120 символов.
    Возвращает `"_"` если после очистки строка пустая.
    """
    cleaned = _FS_FORBIDDEN.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > _FS_COMPONENT_MAX:
        cleaned = cleaned[:_FS_COMPONENT_MAX].rstrip(" .")
    return cleaned or "_"


def download_pages(
    *,
    request_source: RequestSource[HttpRequest],
    conn: ConfluenceConnection,
    dest_dir: str,
    as_markdown: bool,
    pipeline_id: PipelineId,
) -> dict[str, Any]:
    """
    Скачивает страницы в директорию `dest_dir` на ФС;
    формат — HTML (default) или Markdown.

    Возвращает `{dest_dir, saved: [{page_id,title,url,space_key,path,bytes}], total}`.
    """
    dest_path = Path(dest_dir.rstrip("/"))
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"Не удалось создать директорию {str(dest_path)!r}: {e}",
        ) from e

    pctx = PipelineContext(pipeline_id=pipeline_id)

    saved: list[dict[str, str]] = []
    try:
        for decoded in iter_confluence_documents(
            request_source=request_source, conn=conn, pctx=pctx,
        ):
            page_id = decoded.metadata.get(ConfluenceKeys.PAGE_ID) or ""
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
            ancestors = (
                decoded.metadata.get(ConfluenceKeys.ANCESTORS_TITLES) or ()
            )
            page_dir = dest_path
            if space_key:
                page_dir = page_dir / _sanitize_component(space_key)
            for a_title in ancestors:
                page_dir = page_dir / _sanitize_component(a_title)

            try:
                page_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RuntimeError(
                    f"Не удалось создать директорию {str(page_dir)!r}: {e}",
                ) from e
            file_path = page_dir / f"{page_id}.{ext}"
            try:
                file_path.write_bytes(payload)
            except OSError as e:
                raise RuntimeError(
                    f"Ошибка записи файла {str(file_path)!r}: {e}",
                ) from e
            saved.append({
                "page_id": page_id,
                "title": title,
                "url": url,
                "space_key": space_key,
                "path": str(file_path),
                "bytes": str(len(payload)),
            })
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence download failed: {type(e).__name__}: {e}",
        ) from e

    return {
        "dest_dir": str(dest_path),
        "saved": saved,
        "total": len(saved),
    }


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
