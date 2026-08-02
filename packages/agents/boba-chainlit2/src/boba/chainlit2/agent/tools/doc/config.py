"""Конфиг инструментов doc ([tool.doc]): парсер liteparse + лимиты выдачи."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from boba.chainlit2.infra.config import LocalStorageConfig
from boba.liteparse import LiteParseParams

__all__ = ["DocToolsConfig"]


class DocToolsConfig(LiteParseParams):
    """Чтение документов из workspace пользователя (PDF/docx/xlsx/изображения).

    Настройки самого парсера (ocr_enabled/ocr_language/max_pages) приходят
    из LiteParseParams.
    """

    model_config = ConfigDict(extra="ignore")

    storage: LocalStorageConfig = Field(
        description=(
            'Хранилище вложений, ссылкой: storage = "${storage}". Файлы '
            "берутся оттуда же, куда их кладёт загрузка в чат."
        ),
    )
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
