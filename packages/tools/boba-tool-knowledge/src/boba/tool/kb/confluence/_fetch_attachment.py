"""Tool confluence_fetch_attachment: скачивание и парсинг вложения идут в песочнице."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.kb.confluence.caller import ConfluenceCaller

__all__ = ["confluence_fetch_attachment"]


def confluence_fetch_attachment(  # noqa: PLR0913 — настройки OCR независимы
    caller: ConfluenceCaller,
    page_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "ID страницы Confluence, на которой лежит вложение "
                "(колонка page_id в результатах kb_search)."
            ),
        ),
    ],
    filename: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Имя файла вложения с расширением (колонка page_title в "
                "результатах kb_search), напр. 'report.pdf'."
            ),
        ),
    ],
    *,
    ocr_enabled: Annotated[bool, Field(description="Распознавать текст по картинкам.")],
    num_workers: Annotated[int, Field(ge=1, description="Параллелизм OCR.")],
    ocr_language: Annotated[
        str,
        Field(min_length=1, description="Язык OCR в формате Tesseract."),
    ],
) -> str:
    """Скачивает вложение Confluence по page_id+filename и возвращает его текст."""
    return caller.attachment(
        page_id=page_id,
        filename=filename,
        ocr_enabled=ocr_enabled,
        num_workers=num_workers,
        ocr_language=ocr_language,
    )
