"""Tool: онлайн-чтение конкретной секции страницы Confluence по anchor."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from pydantic import Field

from boba.indexing import (
    PipelineContext,
    PipelineId,
    ReaderKeys,
    RuntimePipeline,
    Section,
    SectionKeys,
)
from boba.tool.confluence.config import ConfluencePluginConfig
from boba.tool.confluence.connection import ConfluenceConnection
from boba.tool.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.confluence.enable import confluence_enable_if
from boba.tool.confluence.reader import ConfluenceReader
from boba.tool.confluence.request_sources.pages import (
    ConfluencePagesRequestSource,
)
from boba.tools import FromConfig, tool

__all__ = ["confluence_page_section"]


_PIPELINE_ID: PipelineId = PipelineId("confluence.page_section")


@tool(enable_if=confluence_enable_if("confluence_page_section"))
def confluence_page_section(
    page_id: Annotated[
        str,
        Field(min_length=1, description="ID страницы (как в confluence_page_outline)."),
    ],
    anchor: Annotated[
        str,
        Field(
            min_length=1,
            description="Anchor раздела из confluence_page_outline.",
        ),
    ],
    max_chars: Annotated[
        int,
        Field(ge=1, le=5_000_000, description="Максимум символов в text-поле."),
    ],
    cfg: Annotated[ConfluencePluginConfig, FromConfig()],
) -> dict[str, Any]:
    """Online-чтение одной секции страницы Confluence по anchor.

    page_id и anchor берутся из `confluence_page_outline`. Текст —
    от заголовка до следующего того же или большего уровня.
    """
    pipeline = RuntimePipeline(
        request_source=ConfluencePagesRequestSource(
            base_url=cfg.base_url,
            auth=ConfluenceConnection.make_auth(cfg),
            page_ids=[page_id],
            body_format=cfg.body_format,
        ),
        transport=ConfluenceConnection.make_transport(cfg),
        decoder=ConfluenceJsonDecoder(body_format=cfg.body_format),
        reader=ConfluenceReader(),
    )

    try:
        sections = list(
            pipeline.stream(PipelineContext(pipeline_id=_PIPELINE_ID)),
        )
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"Confluence page fetch failed: {type(e).__name__}: {e}",
        ) from e

    target = _find_section(sections, anchor)
    if target is None:
        raise RuntimeError(
            f"anchor={anchor!r} не найден на странице page_id={page_id!r}; "
            f"вызовите confluence_page_outline для актуального списка.",
        )

    text = target.content
    truncated = len(text) > max_chars
    if truncated:
        text = text[: max_chars - 1].rstrip() + "…"

    return {
        "page_id": page_id,
        "anchor": anchor,
        "title": target.metadata.get(ReaderKeys.PAGE_TITLE) or "",
        "url": str(target.source_id),
        "text": text,
        "truncated": truncated,
    }


def _find_section(
    sections: list[Section[str]],
    anchor: str,
) -> Section[str] | None:
    for s in sections:
        if s.metadata.get(SectionKeys.ANCHOR) == anchor:
            return s
    return None
