"""Tool confluence_fetch_page: контент одной страницы через узел в песочнице."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.kb.confluence.caller import ConfluenceCaller

__all__ = ["confluence_fetch_page"]


def confluence_fetch_page(
    caller: ConfluenceCaller,
    page_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "ID страницы Confluence (из URL `viewpage.action?pageId=<id>`)."
            ),
        ),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Если true — конвертирует HTML в Markdown (ATX-заголовки). "
                "Иначе возвращает исходный Confluence-HTML."
            ),
        ),
    ] = True,
) -> str:
    """Скачивает одну Confluence-страницу и возвращает её контент строкой."""
    return caller.page(page_id=page_id, as_markdown=as_markdown).text
