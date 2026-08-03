"""Общая часть конфигов инструментов, которые парсят документы liteparse."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.doc.liteparse.protocol import ParseParams
from boba.toolkit.sandbox import SandboxToolConfig

__all__ = ["SandboxParserConfig"]


class SandboxParserConfig(BaseModel):
    """Настройки парсера плюс песочница, в которой он исполняется.

    Поля парсера объявлены здесь, а не взяты из boba.liteparse: приложение
    документы не парсит и сам liteparse в его окружении не нужен — он есть
    только в rootfs песочницы, вместе с payload'ом.
    """

    model_config = ConfigDict(extra="ignore")

    ocr_enabled: bool = Field(
        default=False,
        description=(
            "Включить OCR (Tesseract) для сканов и изображений. "
            "Модели берутся из tessdata_path внутри песочницы."
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
    tessdata_path: str = Field(
        min_length=1,
        description=(
            "Каталог моделей OCR внутри песочницы. Без него liteparse качает "
            "их из интернета, а у профиля парсера сети нет."
        ),
    )
    sandbox: SandboxToolConfig = Field(
        description="Окружение и точка входа payload'а: [tool.<name>.sandbox].",
    )

    def parse_params(self) -> ParseParams:
        return ParseParams(
            ocr_enabled=self.ocr_enabled,
            ocr_language=self.ocr_language,
            max_pages=self.max_pages,
            tessdata_path=self.tessdata_path,
        )
