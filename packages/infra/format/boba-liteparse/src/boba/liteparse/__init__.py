"""boba-liteparse — единый toolchain поверх liteparse.

App-safe граница пакета: здесь только контрактные модели и ошибка,
без импорта нативного liteparse. Движок и ридер живут в
`boba.liteparse.engine` и импортируются точечно теми, кто исполняется
в песочнице с установленным liteparse.

Ошибки: pydantic.ValidationError — при разборе моделей контракта.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LiteParseError",
    "LiteParseParams",
    "ParsedPage",
]


class LiteParseError(RuntimeError):
    """Парсинг документа движком liteparse не удался.

    Несёт сырое сообщение парсера; потребитель сам решает, как его
    обернуть (doc-tool -> RuntimeError, indexer -> IncompatibleContentError).
    Наследуется от RuntimeError, чтобы существующие `except RuntimeError`
    продолжали ловить.
    """


class LiteParseParams(BaseModel):
    """Настройки парсера liteparse: OCR, лимит страниц, параллелизм."""

    model_config = ConfigDict(extra="ignore")

    ocr_enabled: bool = Field(
        default=False,
        description=(
            "Включить OCR (Tesseract) для сканов и изображений. "
            "Для текстовых PDF не нужен."
        ),
    )
    ocr_language: str = Field(
        default="rus+eng",
        min_length=1,
        description="Язык OCR в формате Tesseract: 'rus+eng', 'eng', 'rus'.",
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
    num_workers: int = Field(
        default=1,
        ge=1,
        description="Параллелизм OCR; ~50-100 MiB памяти на воркер.",
    )


class ParsedPage(BaseModel):
    """Одна страница (или лист) документа."""

    model_config = ConfigDict(extra="forbid")

    page_num: int = Field(ge=1)
    text: str
