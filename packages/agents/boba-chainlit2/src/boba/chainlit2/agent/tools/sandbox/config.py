"""Конфиг tool bash ([tool.sandbox]): реестр профилей и выбранный профиль."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.chainlit2.sandbox import SandboxConfig

__all__ = ["BashSandboxConfig"]


class BashSandboxConfig(BaseModel):
    """Профиль выбирает администратор конфигом; LLM о песочнице не знает."""

    model_config = ConfigDict(extra="ignore")

    sandbox: SandboxConfig = Field(
        description='Реестр профилей ссылкой: sandbox = "${sandbox}".',
    )
    profile: str = Field(
        min_length=1,
        description="Имя профиля из [sandbox.profiles], в котором идёт запуск.",
    )

    @model_validator(mode="after")
    def _validate_profile(self) -> Self:
        # pydantic превращает в ValidationError только ValueError
        try:
            self.sandbox.profile(self.profile)
        except KeyError as e:
            raise ValueError(str(e.args[0])) from e
        return self
