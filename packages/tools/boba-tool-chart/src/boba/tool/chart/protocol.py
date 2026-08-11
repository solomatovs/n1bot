"""Контракт chart-узла: запрос проверки figure-спеки и квитанция с заголовком.

Ошибки: pydantic.ValidationError — при разборе моделей контракта.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ChartStage",
    "ValidateFigureAnswer",
    "ValidateFigureRequest",
    "ValidateFigureSettings",
]


class ChartStage:
    """Имя узла реестра стадий; оно же имя фасада для LLM."""

    VISUALIZE: ClassVar[str] = "visualize"


class ValidateFigureRequest(BaseModel):
    """Проверить Plotly figure-спеку схемой plotly."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str] = "validate_figure"

    op: str = Field(min_length=1, description="Операция payload'а.")
    spec: str = Field(min_length=1, description="Figure-спека как JSON-текст.")


class ValidateFigureSettings(BaseModel):
    """Часть запроса, которую задаёт узел, а не вызывающий: имя операции."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)


class ValidateFigureAnswer(BaseModel):
    """Данные квитанции: заголовок графика; пустая строка — заголовка нет."""

    model_config = ConfigDict(extra="forbid")

    title: str
