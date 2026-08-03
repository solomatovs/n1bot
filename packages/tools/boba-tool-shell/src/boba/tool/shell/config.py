"""Конфиг tool bash ([tool.bash]): профиль запуска ссылкой на реестр."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.sandbox import SandboxToolConfig

__all__ = ["BashSandboxConfig"]


class BashSandboxConfig(BaseModel):
    """Окружение запуска выбирает администратор; LLM о песочнице не знает."""

    model_config = ConfigDict(extra="ignore")

    sandbox: SandboxToolConfig = Field(
        description="Профиль запуска: [tool.bash.sandbox].",
    )
