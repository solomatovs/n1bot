"""Контракт liteparse-payload'а: настройки парсера и разбор байт из запроса (base64)."""

from __future__ import annotations

import base64
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ParseBytesAnswer",
    "ParseBytesRequest",
    "ParseParams",
    "ParsedPage",
]


class ParseParams(BaseModel):
    """Настройки парсера; одинаковы у всех операций payload'а."""

    model_config = ConfigDict(extra="forbid")

    ocr_enabled: bool = Field(description="Включать OCR для сканов и изображений.")
    ocr_language: str = Field(min_length=1, description="Язык OCR: 'rus+eng'.")
    max_pages: int = Field(ge=0, description="Лимит страниц парсера; 0 = без лимита.")
    tessdata_path: str = Field(min_length=1, description="Каталог моделей OCR.")


class ParseBytesRequest(BaseModel):
    """Разобрать документ, приехавший содержимым в запросе."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str] = "parse_bytes"

    op: str = Field(min_length=1, description="Операция payload'а.")
    filename: str = Field(
        min_length=1,
        description="Имя файла с расширением: по нему liteparse узнаёт формат.",
    )
    content_b64: str = Field(
        min_length=1,
        description="Содержимое документа в base64.",
    )
    params: ParseParams = Field(description="Настройки парсера.")

    @classmethod
    def of(cls, data: bytes, filename: str, params: ParseParams) -> ParseBytesRequest:
        content = base64.b64encode(data).decode("ascii")
        return cls(op=cls.OP, filename=filename, content_b64=content, params=params)


class ParsedPage(BaseModel):
    """Одна страница (или лист) документа."""

    model_config = ConfigDict(extra="forbid")

    page_num: int
    text: str


class ParseBytesAnswer(BaseModel):
    """Разобранный документ постранично."""

    model_config = ConfigDict(extra="forbid")

    num_pages: int = Field(ge=0)
    pages: tuple[ParsedPage, ...]

    @property
    def text(self) -> str:
        """Весь текст документа: страницы через перевод строки."""
        parts: list[str] = []
        for page in self.pages:
            parts.append(page.text)
        return "\n".join(parts)
