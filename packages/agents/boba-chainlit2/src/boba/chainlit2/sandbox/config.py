"""Конфиги песочницы: реестр профилей и профиль запуска инструмента."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.sandbox.profile import SandboxProfile

__all__ = ["SandboxConfig", "SandboxEntryConfig", "SandboxToolConfig"]


class SandboxConfig(BaseModel):
    """Секция [sandbox]: профили, на которые ссылаются инструменты."""

    model_config = ConfigDict(extra="ignore")

    profiles: dict[str, SandboxProfile] = Field(
        min_length=1,
        description="Профили по имени; инструмент берёт нужный ссылкой.",
    )


class SandboxToolConfig(BaseModel):
    """Секция [tool.<name>.sandbox]: в каком окружении запускать инструмент."""

    model_config = ConfigDict(extra="ignore")

    profile: SandboxProfile = Field(
        description='Профиль ссылкой: profile = "${sandbox.profiles.<name>}".',
    )
    override: Mapping[str, Any] = Field(
        description=(
            "Поля профиля, заменяемые для этого инструмента; пустая таблица "
            "означает «без изменений». Названное поле заменяет базовое целиком."
        ),
    )

    def effective(self) -> SandboxProfile:
        """Профиль запуска: база плюс то, что переопределил администратор."""
        if not self.override:
            return self.profile
        merged = self.profile.model_dump()
        merged.update(self.override)
        return SandboxProfile.model_validate(merged)


class SandboxEntryConfig(SandboxToolConfig):
    """То же плюс точка входа: чем запускается payload инструмента."""

    entry: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "argv payload'а внутри песочницы, например "
            '["python3", "/opt/payload/main.py"].'
        ),
    )
