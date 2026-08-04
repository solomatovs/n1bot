"""Контракт liteparse-payload'а: настройки парсера и разбор байт из запроса (base64).

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
    "ParseBytesRequest",
    "ParseParams",
    "ParsedPage",
]


class ParseParams(LiteParseParams):
    """Настройки парсера в запросе payload'а; поля — из LiteParseParams."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def of(cls, base: LiteParseParams) -> ParseParams:
        """Единственное место копирования настроек конфига в запрос."""
        return cls(
            ocr_enabled=base.ocr_enabled,
            ocr_language=base.ocr_language,
            max_pages=base.max_pages,
            tessdata_path=base.tessdata_path,
            num_workers=base.num_workers,
        )


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

    def content(self) -> bytes:
        """Байты документа из base64-поля запроса."""
        return base64.b64decode(self.content_b64)


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
