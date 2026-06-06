"""Конфиг плагина doc.

`[tool.doc]` / `BOBA_TOOL__DOC__*`. Используется в `read_document` через
FromConfig. Поля `enable` / `tools` читает framework через
`AgentBuilder.discover_plugins`; `extra="ignore"` позволяет им жить в
той же TOML-секции без ValidationError.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["DocPluginConfig"]


class DocPluginConfig(BaseModel):
    """Парсинг документов в текст (liteparse).

    Config-секция: `[tool.doc]`.
    """

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
    max_text_chars: int = Field(
        default=200_000,
        ge=1,
        description="Лимит длины возвращаемого текста; излишек обрезается.",
    )
    search_context_chars: int = Field(
        default=80,
        ge=0,
        description=(
            "Сколько символов контекста показывать вокруг совпадения "
            "в search_document."
        ),
    )
    search_max_matches: int = Field(
        default=50,
        ge=1,
        description="Максимум совпадений в ответе search_document.",
    )
