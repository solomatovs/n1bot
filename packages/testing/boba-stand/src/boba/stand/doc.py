"""Стенд чтения документов: каталог моделей OCR и шрифт с кириллицей для
образцов картинок и сканов берутся из секции [doc_stand] наложения стенда.

Ошибки:
DocStandError — секция [doc_stand] отсутствует или неполна.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict

from boba.config import ConfigError, bind
from boba.stand.site import StandConfigEnv, StandConfigPaths, StandError

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
        config = StandConfigPaths(StandConfigEnv.RUN)
        try:
            return bind(config.load(), path=cls.SECTION, model=cls)
        except (StandError, ConfigError) as exc:
            msg = (
                f"doc stand: [{cls.SECTION}] of the run config {config.paths()}: {exc}"
            )
            raise DocStandError(msg) from exc

    @classmethod
    def required(cls) -> Self:
        """Стенд для модуля тестов; без конфига сбор модуля падает с причиной."""
        return cls.load()
