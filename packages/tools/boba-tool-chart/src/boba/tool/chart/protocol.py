"""Контракт chart-payload'а: спека на вход, заголовок на выход."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ValidateFigureAnswer", "ValidateFigureRequest"]


class ValidateFigureRequest(BaseModel):
    """Проверить Plotly figure-спеку схемой plotly."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str] = "validate_figure"

    op: str = Field(min_length=1, description="Операция payload'а.")
    spec: str = Field(min_length=1, description="Figure-спека как JSON-текст.")

    @classmethod
    def of(cls, spec: str) -> ValidateFigureRequest:
        return cls(op=cls.OP, spec=spec)


class ValidateFigureAnswer(BaseModel):
    """Заголовок графика; пустая строка — заголовка нет."""

    model_config = ConfigDict(extra="forbid")

    title: str
