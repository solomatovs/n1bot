"""Параметры движка liteparse — единый набор настроек парсера.

Плоский pydantic-миксин: конфиги-потребители (DocPluginConfig у doc-tool,
ingest-конфиг у kb) наследуют его, чтобы поля ocr/max_pages жили в их
TOML-секции без дублирования. Сам движок (LiteParseEngine) принимает
именно LiteParseParams, не зная о конкретном конфиге.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["LiteParseParams"]


class LiteParseParams(BaseModel):
    """Настройки парсера liteparse (OCR + лимит страниц)."""

    model_config = ConfigDict(extra="ignore")

    ocr_enabled: bool = Field(
        default=False,
        description=(
            "Включить OCR (Tesseract) для сканов и изображений. "
            "Требует tesseract в образе; для текстовых PDF не нужен."
        ),
    )
    ocr_language: str = Field(
        default="eng",
        min_length=1,
        description="Язык OCR в формате Tesseract: 'eng', 'rus', 'rus+eng'.",
    )
    max_pages: int = Field(
        default=0,
        ge=0,
        description="Лимит числа парсируемых страниц. 0 = без лимита.",
    )
