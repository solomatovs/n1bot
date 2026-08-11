"""Tool confluence_ingest_pages: индексация явного списка страниц по page_id."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.kb.confluence.ingest_caller import (
    ConfluenceIngestCaller,
)
from boba.tool.kb.confluence.ingest_protocol import IngestMode
from boba.toolkit.result import TableResult
from boba.toolkit.types import LLMStringList

__all__ = ["confluence_ingest_pages"]


def confluence_ingest_pages(  # noqa: PLR0913 — настройки прогона независимы
    caller: ConfluenceIngestCaller,
    page_ids: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description=(
                "Список page_id страниц Confluence для индексации. "
                'Передавай JSON-массив строк: `["950276", "950278"]`. '
                "Каждый id — строка из URL `viewpage.action?pageId=<id>`. "
                "Используй, когда уже знаешь конкретные страницы (например, "
                "из результатов `confluence_search_cql`)."
            ),
        ),
    ],
    prune_missing: Annotated[
        bool,
        Field(
            description=(
                "Если true, удалить из коллекции чанки, чьих source_id нет "
                "среди страниц, попавших в discovery текущего run'а."
            ),
        ),
    ] = False,
    force_update: Annotated[
        bool,
        Field(
            description=(
                "Если true, переиндексировать указанные страницы целиком: "
                "переэмбеддить все чанки (минуя skip-by-hash) и удалить "
                "устаревшие чанки этих страниц, включая снятые с них вложения."
            ),
        ),
    ] = False,
    *,
    ocr_enabled: Annotated[bool, Field(description="Распознавать текст по картинкам.")],
    num_workers: Annotated[int, Field(ge=1, description="Параллелизм OCR.")],
    ocr_language: Annotated[
        str,
        Field(min_length=1, description="Язык OCR в формате Tesseract."),
    ],
) -> TableResult:
    """Индексирует явный список страниц Confluence по page_id в KB."""
    result = caller.ingest(
        mode=IngestMode.PAGES,
        page_ids=page_ids,
        prune_missing=prune_missing,
        force_update=force_update,
        ocr_enabled=ocr_enabled,
        num_workers=num_workers,
        ocr_language=ocr_language,
    )
    return TableResult(
        rows=[result],
        note=f"page_ids ({len(page_ids)}): {', '.join(page_ids)}",
    )
