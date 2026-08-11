"""Конфиг секции [tool.confluence] и настройки парсера вложений."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceAttachmentSettings", "ConfluenceToolsConfig"]


class ConfluenceToolsConfig(BaseModel):
    """Общий конфиг секции [tool.confluence] для всех инструментов чтения."""

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile = Field(
        description='Web-профиль Confluence ссылкой `confluence = "${web.<name>}"`.',
    )
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )
    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match в grep.",
    )
    page_size: int = Field(
        default=500,
        ge=1,
        le=1000,
        description="Сколько спейсов запрашивать у REST за раз.",
    )

    def connection(self) -> ConfluenceConnection:
        """Endpoint Confluence: профиль плюс body-формат секции."""
        return ConfluenceConnection(
            profile=self.confluence,
            body_format=self.body_format,
        )


class ConfluenceAttachmentSettings(BaseModel):
    """Настройки парсера вложений, которые задаёт конфиг, а не вызов."""

    model_config = ConfigDict(extra="forbid")

    max_pages: int = Field(ge=0, description="Лимит страниц; 0 — без лимита.")
    tessdata_path: str = Field(
        min_length=1,
        description="Каталог моделей OCR внутри песочницы.",
    )
