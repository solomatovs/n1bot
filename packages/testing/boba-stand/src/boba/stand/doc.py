"""Стенд чтения документов: каталог моделей OCR и шрифт с кириллицей для
образцов картинок и сканов берутся из секции [doc_stand] в stand.toml.

Ошибки:
DocStandError — секция [doc_stand] отсутствует или неполна.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Self

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from boba.config import bind
from boba.runtime.config import ConfigLocator
from boba.stand.site import StandLayers

__all__ = ["DocStand", "DocStandError"]


class DocStandError(Exception):
    """Конфиг стенда документов недоступен или неполон."""


class DocStand(BaseModel):
    """Секция [doc_stand]: модели OCR и шрифт для рисования образцов."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    SECTION: ClassVar[str] = "doc_stand"

    ocr_models_dir: Path
    cyrillic_font: Path

    @classmethod
    def load(cls) -> Self:
        path = ConfigLocator.path()
        stand_path = path.parent / StandLayers.FILE
        if not stand_path.is_file():
            raise DocStandError(f"doc stand: {stand_path} not found")

        raw = StandLayers.compose(path)

        try:
            return bind(raw, path=cls.SECTION, model=cls)
        except ValidationError as exc:
            raise DocStandError(
                f"doc stand: [{cls.SECTION}] in {stand_path}: {exc}"
            ) from exc

    @classmethod
    def required(cls) -> Self:
        try:
            return cls.load()
        except DocStandError as exc:
            pytest.skip(str(exc), allow_module_level=True)
