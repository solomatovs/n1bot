"""Проверка figure-спеки в песочнице."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.launcher import LauncherFactory, NoChunks

__all__ = ["ChartCaller", "ValidateFigureAnswer", "ValidateFigureRequest"]


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


class ChartCaller:
    """Один вызов payload'а на график; спека едет в запросе."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.chart.payload")

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._caller = launchers(tool)

    def validate(self, spec: str) -> str:
        """Заголовок графика; спека, не прошедшая схему, роняет payload."""
        request = ValidateFigureRequest.of(spec)
        answer = self._caller.call_stream(
            self.ENTRY, request, NoChunks(), ValidateFigureAnswer
        )
        return answer.title
