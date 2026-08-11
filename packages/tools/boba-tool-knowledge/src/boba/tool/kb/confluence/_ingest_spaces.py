"""Tool confluence_ingest_spaces: индексация всех страниц перечисленных spaces."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.kb.confluence.ingest_caller import (
    ConfluenceIngestCaller,
)
from boba.tool.kb.confluence.ingest_protocol import IngestMode
from boba.toolkit.result import TableResult
from boba.toolkit.types import LLMStringList

__all__ = ["confluence_ingest_spaces"]


def confluence_ingest_spaces(  # noqa: PLR0913 — настройки прогона независимы
    caller: ConfluenceIngestCaller,
    space_keys: Annotated[
        LLMStringList,
        Field(
            min_length=1,
            description=(
                "Список space-ключей Confluence для индексации. "
                'Передавай JSON-массив строк: `["DOCS", "ENG"]`. Discovery '
                "через `/rest/api/space/{key}/content` — индексируются все "
                "страницы каждого space. Используй, когда нужен полный "
                "охват space'а."
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
                "Если true, переиндексировать страницы space'ов целиком: "
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
    """Индексирует ВСЕ страницы перечисленных Confluence-spaces в KB."""
    result = caller.ingest(
        mode=IngestMode.SPACES,
        space_keys=space_keys,
        prune_missing=prune_missing,
        force_update=force_update,
        ocr_enabled=ocr_enabled,
        num_workers=num_workers,
        ocr_language=ocr_language,
    )
    return TableResult(rows=[{"space_keys": space_keys, **result}])
