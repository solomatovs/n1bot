"""Конфиг инструментов doc ([tool.doc]): песочница payload'а + лимиты выдачи."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from boba.tool.doc.liteparse import SandboxParserConfig

__all__ = ["DocToolsConfig"]


class DocToolsConfig(SandboxParserConfig):
    """Чтение документов из workspace пользователя; файлы открывает payload в песочнице."""

    model_config = ConfigDict(extra="ignore")

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
    search_max_matches: int = Field(
        default=50,
        ge=1,
        description="Максимум совпадений в ответе search_document.",
    )
