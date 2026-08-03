"""Конфиг tool chart ([tool.chart]): песочница валидации спеки."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.sandbox import SandboxEntryConfig

__all__ = ["ChartToolsConfig"]


class ChartToolsConfig(BaseModel):
    """Схему графика проверяет payload в песочнице, а не процесс приложения."""

    model_config = ConfigDict(extra="ignore")

    sandbox: SandboxEntryConfig = Field(
        description="Окружение и точка входа payload'а: [tool.chart.sandbox].",
    )
