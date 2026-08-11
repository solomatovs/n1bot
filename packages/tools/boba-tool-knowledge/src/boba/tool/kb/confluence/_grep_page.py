"""Tool confluence_grep_page: поиск по контенту страницы через узел в песочнице."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.toolkit.result import TableResult

__all__ = ["confluence_grep_page"]


def confluence_grep_page(  # noqa: PLR0913 — независимые флаги grep'а
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
    matches = caller.grep(
        page_id=page_id,
        pattern=pattern,
        as_markdown=as_markdown,
        case_insensitive=case_insensitive,
        context=context,
        limit=limit,
        fixed_string=fixed_string,
    )

    rows: list[dict[str, Any]] = []
    for row in matches:
        rows.append(row.model_dump())

    note = f"page_id={page_id}: no matches found"
    if rows:
        note = f"page_id={page_id}: {len(rows)} match(es)"

    return TableResult(rows=rows, note=note, metadata={"page_id": page_id})
