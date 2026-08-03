"""Конфиг tool chart ([tool.chart]): песочница валидации спеки."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.sandbox import SandboxToolConfig

__all__ = ["ChartToolsConfig"]


class ChartToolsConfig(BaseModel):
    """Схему графика проверяет payload в песочнице, а не процесс приложения."""

    model_config = ConfigDict(extra="ignore")

    sandbox: SandboxToolConfig = Field(
        description="Окружение и точка входа payload'а: [tool.chart.sandbox].",
    )
