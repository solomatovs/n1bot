"""Tool confluence_grep_page: поиск по контенту страницы через payload в песочнице."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.protocol import ConfluenceGrepRequest
from boba.toolkit.result import TableResult
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceGrepPageConfig", "confluence_grep_page"]


class ConfluenceGrepPageConfig(BaseModel):
    """Self-contained конфиг tool'а confluence_grep_page."""

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )
    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match.",
    )


def confluence_grep_page(  # noqa: PLR0913
    cfg: ConfluenceGrepPageConfig,
    caller: ConfluenceCaller,
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
    pattern: Annotated[
        str,
        Field(min_length=1, description="Python-regex; литерал при fixed_string=true."),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Искать по Markdown-конверсии (`markdownify`, ATX-заголовки) "
                "вместо исходного Confluence-HTML. По умолчанию true."
            ),
        ),
    ] = True,
    case_insensitive: Annotated[
        bool,
        Field(description="Игнорировать регистр. По умолчанию false."),
    ] = False,
    context: Annotated[
        int,
        Field(ge=0, description="Строк контекста до и после каждого совпадения."),
    ] = 0,
    limit: Annotated[
        int,
        Field(ge=1, description="Максимум совпадений в ответе. По умолчанию 100."),
    ] = 100,
    fixed_string: Annotated[
        bool,
        Field(description="Литеральный поиск без regex. По умолчанию false."),
    ] = False,
) -> TableResult:
    """Скачивает Confluence-страницу и ищет в её контенте совпадения pattern."""
    request = ConfluenceGrepRequest(
        op=ConfluenceGrepRequest.OP,
        base_url=cfg.confluence.base_url or "",
        profile=ConfluenceCaller.transport_of(cfg.confluence),
        page_id=page_id,
        body_format=cfg.body_format,
        as_markdown=as_markdown,
        pattern=pattern,
        case_insensitive=case_insensitive,
        context=context,
        limit=limit,
        fixed_string=fixed_string,
        max_text_chars=cfg.max_text_chars,
    )
    rows: list[dict[str, Any]] = []
    for row in caller.grep(request):
        rows.append(row.model_dump())
    if rows:
        note = f"page_id={page_id}: совпадений {len(rows)}"
    else:
        note = f"page_id={page_id}: совпадений не найдено"
    return TableResult(rows=rows, note=note, metadata={"page_id": page_id})
