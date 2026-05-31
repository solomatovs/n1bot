"""Tool `confluence_fetch_page` + `ConfluenceFetchPageConfig`.

Возвращает контент одной Confluence-страницы прямо вызывающему LLM,
без записи на диск и без индексации. Вход — `page_id`, выход —
строка с контентом страницы (HTML или Markdown, выбор за LLM через
`as_markdown`).

В отличие от `confluence_download`, attachment'ы НЕ скачиваются и НЕ
запрашиваются по HTTP вообще: pipeline идёт мимо `ConfluenceContentTransport`'а
с его fan-out'ом — `request_source → http_transport → ConfluenceJsonDecoder`
напрямую.

Config-секция: `[tool.kb.confluence.fetch]`.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

import httpx
import markdownify
from pydantic import Field

from boba.indexing import PipelineContext, PipelineId
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.parsing import ConfluenceJsonDecoder
from boba.tool.kb.confluence.request_sources import ConfluencePagesRequestSource
from boba.tools import FromConfig, tool

__all__ = ["ConfluenceFetchPageConfig", "confluence_fetch_page"]


class ConfluenceFetchPageConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_fetch_page`."""

    PIPELINE_ID: ClassVar[PipelineId] = PipelineId("confluence.fetch_page")

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.fetch",
        defaults_from=("confluence",),
    )

    confluence: ConfluenceConnection


@tool
def confluence_fetch_page(
    cfg: Annotated[ConfluenceFetchPageConfig, FromConfig()],
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
    request_source = ConfluencePagesRequestSource(
        base_url=cfg.confluence.base_url,
        auth=cfg.confluence.make_auth(),
        page_ids=[page_id],
        body_format=cfg.confluence.body_format,
    )
    transport = cfg.confluence.make_transport()
    decoder = ConfluenceJsonDecoder(body_format=cfg.confluence.body_format)
    pctx = PipelineContext(pipeline_id=ConfluenceFetchPageConfig.PIPELINE_ID)

    try:
        for http_req in request_source.stream(pctx):
            for raw in transport.stream(pctx, [http_req]):
                decoded = decoder.convert(raw)
                html = decoded.handle.read().decode("utf-8", errors="replace")
                if as_markdown:
                    return markdownify.markdownify(html, heading_style="ATX")
                return html
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence fetch failed: {type(e).__name__}: {e}",
        ) from e

    raise RuntimeError(f"Confluence fetch returned no page for page_id={page_id!r}")
