"""Tool confluence_fetch_page + ConfluenceFetchPageConfig.

Возвращает контент одной Confluence-страницы прямо вызывающему LLM,
без записи на диск и без индексации. Вход — page_id, выход —
строка с контентом страницы (HTML или Markdown, выбор за LLM через
as_markdown).

В отличие от confluence_download, attachment'ы НЕ скачиваются и НЕ
запрашиваются по HTTP вообще: pipeline идёт мимо ConfluenceContentTransport'а
с его fan-out'ом — request_source -> ConfluenceHttpTransport -> ConfluenceJsonDecoder
напрямую.

Config-секция: [tool.kb.confluence.fetch].
"""

from __future__ import annotations

from typing import Annotated, Literal

import httpx
import markdownify
from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.agent.tools.confluence.connection import ConfluenceConnection
from boba.chainlit2.agent.tools.confluence.parsing import ConfluenceJsonDecoder
from boba.chainlit2.agent.tools.confluence.pipeline import ConfluenceHttpTransport
from boba.chainlit2.agent.tools.confluence.request_sources import (
    ConfluencePagesRequestSource,
)
from boba.chainlit2.agent.tools.http import CancellableHttpTransport
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
    page_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "ID страницы Confluence (из URL `viewpage.action?pageId=<id>`). "
                "Attachment'ы не скачиваются."
            ),
        ),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Если true — конвертирует HTML в Markdown (`markdownify`, "
                "ATX-заголовки). Иначе возвращает исходный Confluence-HTML."
            ),
        ),
    ] = True,
) -> str:
    """Скачивает одну Confluence-страницу и возвращает её контент строкой."""
    conn = ConfluenceConnection(profile=cfg.confluence, body_format=cfg.body_format)
    request_source = ConfluencePagesRequestSource(
        base_url=conn.base_url,
        page_ids=[page_id],
        body_format=conn.body_format,
    )
    decoder = ConfluenceJsonDecoder(body_format=conn.body_format)

    try:
        with CancellableHttpTransport(conn.profile) as http:
            transport = ConfluenceHttpTransport(http)
            for req in request_source.requests():
                for raw in transport.fetch(req):
                    decoded = decoder.decode(raw)
                    html = decoded.handle.read().decode("utf-8", errors="replace")
                    if as_markdown:
                        return markdownify.markdownify(html, heading_style="ATX")
                    return html
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence fetch failed: {type(e).__name__}: {e}",
        ) from e

    raise RuntimeError(f"Confluence fetch returned no page for page_id={page_id!r}")
