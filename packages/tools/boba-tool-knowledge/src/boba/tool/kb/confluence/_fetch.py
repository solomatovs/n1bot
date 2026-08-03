"""Tool confluence_fetch_page: контент одной страницы.

Запрос к REST и конверсию в markdown делает payload в песочнице.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.protocol import ConfluencePageRequest
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceFetchPageConfig", "confluence_fetch_page"]


class ConfluenceFetchPageConfig(BaseModel):
    """Self-contained конфиг tool'а confluence_fetch_page."""

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )


def confluence_fetch_page(
    cfg: ConfluenceFetchPageConfig,
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
    request = ConfluencePageRequest(
        op=ConfluencePageRequest.OP,
        base_url=cfg.confluence.base_url or "",
        profile=ConfluenceCaller.transport_of(cfg.confluence),
        page_id=page_id,
        body_format=cfg.body_format,
        as_markdown=as_markdown,
    )
    return caller.page(request).text
