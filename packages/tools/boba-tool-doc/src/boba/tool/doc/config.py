"""Конфиг плагина doc.

[tool.doc] / BOBA_TOOL__DOC__*. Используется в read_document через
FromConfig. Поля enable / tools читает framework через
AgentBuilder.discover_plugins; extra="ignore" позволяет им жить в
той же TOML-секции без ValidationError.

Настройки самого парсера (ocr_enabled/ocr_language/max_pages) наследуются
из LiteParseParams — общего миксина движка liteparse; здесь добавляются
только doc-tool-специфичные presentation-лимиты.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field

from boba.liteparse import LiteParseParams

__all__ = ["DocPluginConfig"]


class DocPluginConfig(LiteParseParams):
    """Парсинг документов в текст (liteparse).

    Config-секция: [tool.doc].
    """

    model_config = ConfigDict(extra="ignore")

    max_text_chars: int = Field(
        default=200_000,
        ge=1,
        description="Лимит длины возвращаемого текста; излишек обрезается.",
    )
    search_context_chars: int = Field(
        default=80,
        ge=0,
        description=(
            "Сколько символов контекста показывать вокруг совпадения в search_document."
        ),
    )
    search_max_matches: int = Field(
        default=50,
        ge=1,
        description="Максимум совпадений в ответе search_document.",
    )
