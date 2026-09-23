"""Конфиг инструментов doc ([tool.doc]): чтение документов boba-doc с OCR плюс
лимиты выдачи."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from boba.doc.config import DocSection

__all__ = ["DocToolsConfig"]


class DocToolsConfig(DocSection):
    """Чтение документов из workspace; файлы открывает payload в песочнице."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    max_text_chars: int = Field(
        default=200_000,
        ge=1,
        description="Лимит длины возвращаемого текста; излишек обрезается.",
    )
    search_context_chars: int = Field(
        default=80,
        ge=0,
        description="Сколько символов контекста показывать вокруг совпадения.",
    )
    max_result_chars: int = Field(
        default=10_000_000,
        ge=1,
        description="Потолок суммарного объёма потока строк выдачи (символов).",
    )
