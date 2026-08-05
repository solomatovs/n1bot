"""Tool web_fetch: окно строк веб-страницы."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.web._grep import WebGrepConfig
from boba.toolkit.launcher import TextCollector
from boba.web.caller import WebCaller

__all__ = ["web_fetch"]


def web_fetch(  # noqa: PLR0913
    cfg: WebGrepConfig,
    caller: WebCaller,
    url: Annotated[
        str,
        Field(min_length=1, description="URL для скачивания"),
    ],
    as_markdown: Annotated[
        bool,
        Field(description="true — конвертирует HTML->Markdown"),
    ],
    line_offset: Annotated[
        int,
        Field(ge=0, description="Вернуть контент начиная со строки `line_offset`"),
    ],
    line_count: Annotated[
        int,
        Field(ge=1, description="Сколько строк вернуть начиная с `line_offset`"),
    ],
) -> dict[str, Any]:
    """Скачать URL и вернуть окно строк его содержимого."""
    profile = cfg.resolve_profile(url)
    collector = TextCollector(
        max_chars=cfg.max_result_chars,
        limit_rows=None,
        header_lines=0,
    )
    trailer = caller.fetch(
        url=url,
        profile=profile,
        as_markdown=as_markdown,
        line_offset=line_offset,
        line_count=line_count,
        sink=collector,
    )
    return {
        "content": collector.text(),
        "source_url": trailer.source_url,
        "total_lines": trailer.total_lines,
        "returned_lines": trailer.returned_lines,
    }
