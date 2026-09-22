"""Секции конфига чтения документов: настройки роутера и union OCR по provider.
Модуль не тянет библиотек форматов и моделей, его импортирует и приложение,
которому нужна только модель секции.

Ошибки:
OcrUnavailableError — вызов просил OCR, а секция держит provider = off.
Неверная секция падает ValidationError pydantic у того, кто её собирает.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DisabledOcrConfig",
    "DocConfig",
    "DocSection",
    "OcrConfig",
    "OcrLanguage",
    "OcrModel",
    "OcrUnavailableError",
    "RapidOcrConfig",
]


class OcrUnavailableError(Exception):
    """Вызов просил OCR, а секция держит его выключенным."""


class DocConfig(BaseModel):
    """Настройки чтения: порог буфера в памяти для форматов с произвольным
    доступом и кодировки текстовых файлов по порядку перебора."""

    model_config = ConfigDict(frozen=True)

    spool_memory_limit: int = Field(gt=0)
    text_encodings: Sequence[str] = Field(min_length=1)


class OcrLanguage(StrEnum):
    """Языки распознавателя PP-OCRv5; eslav — русский, украинский, белорусский."""

    ESLAV = "eslav"
    CYRILLIC = "cyrillic"
    LATIN = "latin"
    EN = "en"
    CH = "ch"
    KOREAN = "korean"
    ARABIC = "arabic"
    DEVANAGARI = "devanagari"
    TH = "th"
    EL = "el"
    TA = "ta"
    TE = "te"


class OcrModel(StrEnum):
    """Имена файлов моделей в каталоге моделей; распознаватель зависит от языка."""

    DET = "ch_PP-OCRv5_det_mobile.onnx"
    CLS = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
    REC = "{language}_PP-OCRv5_rec_mobile.onnx"

    def path(self, models_dir: Path, language: OcrLanguage) -> Path:
        return models_dir / self.value.format(language=language.value)

    @classmethod
    def required(cls, models_dir: Path, language: OcrLanguage) -> Iterator[Path]:
        for model in cls:
            yield model.path(models_dir, language)


class DisabledOcrConfig(BaseModel):
    """Секция OCR с provider = off: картинки и сканы дают пустой текст."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["off"]

    @property
    def enabled(self) -> bool:
        return False

    def fingerprint(self) -> Mapping[str, object]:
        """Часть отпечатка индексатора: что влияет на распознанный текст."""
        return {"provider": self.provider}


class RapidOcrConfig(BaseModel):
    """Секция OCR с provider = rapidocr: каталог моделей, язык, порог
    уверенности и потоки onnxruntime."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["rapidocr"]
    models_dir: Path
    language: OcrLanguage
    text_score: float = Field(ge=0.0, le=1.0)
    threads: int = Field(ge=1)

    @property
    def enabled(self) -> bool:
        return True

    def fingerprint(self) -> Mapping[str, object]:
        return {
            "provider": self.provider,
            "language": self.language.value,
            "text_score": self.text_score,
        }


OcrConfig = Annotated[
    DisabledOcrConfig | RapidOcrConfig,
    Field(discriminator="provider"),
]
"""Discriminated union по provider — точная диагностика ошибок валидации."""


class DocSection(DocConfig):
    """Секция чтения документов с OCR: её наследуют секции индексатора и
    инструментов, которым нужен роутер целиком."""

    ocr: OcrConfig

    def without_ocr(self) -> Self:
        """Копия с выключенным OCR: вызов не просил распознавания."""
        return self.model_copy(update={"ocr": DisabledOcrConfig(provider="off")})

    def for_call(self, *, ocr: bool) -> Self:
        """Секция под вызов: без OCR, если вызов его не просил; просил при
        provider = off — отказ, чинить должен администратор."""
        if not ocr:
            return self.without_ocr()

        if not self.ocr.enabled:
            raise OcrUnavailableError(
                "ocr requested for the call, but the section holds ocr.provider = "
                "'off': ask the administrator to configure an OCR provider"
            )

        return self
