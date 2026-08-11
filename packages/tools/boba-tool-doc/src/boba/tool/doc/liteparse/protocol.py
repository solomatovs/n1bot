"""Контракт liteparse-узла: настройки парсера, аргументы вызова и разбор base64.

Настройки едут плоскими полями запроса, а не вложенным объектом: обогатитель
узла кладёт значения конфига рядом с аргументами вызывающего, и всё проверяется
одной моделью запроса.

Ошибки: pydantic.ValidationError — при разборе моделей контракта;
binascii.Error — из ParseBytesRequest.content при битом base64.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.text.document import LiteParseParams, ParsedPage

__all__ = [
    "ParseBytesAnswer",
    "ParseBytesArgs",
    "ParseBytesRequest",
    "ParseBytesTrailer",
    "ParseParams",
    "ParseRequest",
    "ParsedPage",
]


class ParseParams(LiteParseParams):
    """Настройки движка liteparse; собираются из полей запроса."""

    model_config = ConfigDict(extra="forbid")


class ParseRequest(BaseModel):
    """Общая часть запроса парсера: операция и настройки liteparse."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1, description="Операция payload'а.")
    ocr_enabled: bool = Field(description="Распознавать текст по картинкам.")
    ocr_language: str = Field(
        min_length=1,
        description="Язык OCR в формате Tesseract: 'rus+eng', 'eng', 'rus'.",
    )
    max_pages: int = Field(ge=0, description="Лимит страниц; 0 — без лимита.")
    tessdata_path: str = Field(
        min_length=1,
        description="Каталог моделей OCR внутри песочницы.",
    )
    num_workers: int = Field(ge=1, description="Параллелизм OCR.")

    def parse_params(self) -> ParseParams:
        """Настройки движка из полей запроса."""
        return ParseParams(
            ocr_enabled=self.ocr_enabled,
            ocr_language=self.ocr_language,
            max_pages=self.max_pages,
            tessdata_path=self.tessdata_path,
            num_workers=self.num_workers,
        )

    @classmethod
    def settings_of(cls, op: str, base: LiteParseParams) -> ParseRequest:
        """Настроечная часть запроса узла: значения конфига плюс имя операции."""
        return cls(
            op=op,
            ocr_enabled=base.ocr_enabled,
            ocr_language=base.ocr_language,
            max_pages=base.max_pages,
            tessdata_path=base.tessdata_path,
            num_workers=base.num_workers,
        )


class ParseBytesArgs(BaseModel):
    """Аргументы вызывающего: имя файла и содержимое документа."""

    model_config = ConfigDict(extra="forbid")

    ENCODING: ClassVar[str] = "ascii"

    filename: str = Field(
        min_length=1,
        description="Имя файла с расширением: по нему liteparse узнаёт формат.",
    )
    content_b64: str = Field(min_length=1, description="Содержимое в base64.")

    @classmethod
    def of(cls, data: bytes, filename: str) -> ParseBytesArgs:
        content = base64.b64encode(data).decode(cls.ENCODING)

        return cls(filename=filename, content_b64=content)


class ParseBytesRequest(ParseRequest):
    """Разобрать документ, приехавший содержимым в запросе."""

    OP: ClassVar[str] = "parse_bytes"

    filename: str = Field(
        min_length=1,
        description="Имя файла с расширением: по нему liteparse узнаёт формат.",
    )
    content_b64: str = Field(min_length=1, description="Содержимое в base64.")

    def content(self) -> bytes:
        """Байты документа из base64-поля запроса."""
        return base64.b64decode(self.content_b64)


class ParseBytesTrailer(BaseModel):
    """Итог разбора: страницы ушли строками в канал данных."""

    model_config = ConfigDict(extra="forbid")

    num_pages: int = Field(ge=0)


class ParseBytesAnswer(BaseModel):
    """Разобранный документ постранично."""

    model_config = ConfigDict(extra="forbid")

    num_pages: int = Field(ge=0)
    pages: tuple[ParsedPage, ...]

    @property
    def text(self) -> str:
        """Весь текст документа: страницы через перевод строки."""
        return "\n".join(self._page_texts())

    def _page_texts(self) -> Iterator[str]:
        for page in self.pages:
            yield page.text
