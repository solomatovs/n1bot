"""Tool confluence_ingest_cql: индексация страниц, отобранных CQL-запросом."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.kb.confluence.ingest_caller import (
    ConfluenceIngestCaller,
)
from boba.tool.kb.confluence.ingest_protocol import IngestMode

__all__ = ["confluence_ingest_cql"]


def confluence_ingest_cql(  # noqa: PLR0913 — настройки прогона независимы
    caller: ConfluenceIngestCaller,
    cql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "CQL-запрос для discovery страниц. Пример: "
                '`space = DOCS AND lastModified > "2024-01-01"`. '
                "Используй для тонких фильтров (по дате, метке, автору) — "
                "когда не нужен весь space, но и конкретных page_id нет."
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
    *,
    ocr_enabled: Annotated[bool, Field(description="Распознавать текст по картинкам.")],
    num_workers: Annotated[int, Field(ge=1, description="Параллелизм OCR.")],
    ocr_language: Annotated[
        str,
        Field(min_length=1, description="Язык OCR в формате Tesseract."),
    ],
) -> dict[str, Any]:
    """Индексирует страницы Confluence, отобранные CQL-запросом, в KB."""
    result = caller.ingest(
        mode=IngestMode.CQL,
        cql=cql,
        prune_missing=prune_missing,
        force_update=False,
        ocr_enabled=ocr_enabled,
        num_workers=num_workers,
        ocr_language=ocr_language,
    )
    return {"cql": cql, **result}
