"""Конфиг песочницы: профили bubblewrap, рабочая директория, лимиты."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.chainlit2.agent.tools.sandbox.profile import SandboxProfile

__all__ = ["BashSandboxConfig"]


class BashSandboxConfig(BaseModel):
    """Конфиг bash ([tool.sandbox]): реестр профилей песочницы."""

    model_config = ConfigDict(extra="ignore")

    profiles: dict[str, SandboxProfile] = Field(
        default_factory=dict,
        description="Реестр sandbox-профилей по имени.",
    )
    default_profile: str = Field(
        default="",
        description=(
            "Профиль по умолчанию, если LLM не указал `profile` в args. "
            "Обязан быть среди ключей `profiles`."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.default_profile and self.default_profile not in self.profiles:
            msg = (
                f"sandbox.default_profile={self.default_profile!r} "
                f"is missing from profiles; available: {sorted(self.profiles)}"
            )
            raise ValueError(msg)
        return self
