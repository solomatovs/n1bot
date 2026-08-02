"""Реестр профилей песочницы: секция [sandbox] верхнего уровня.

Профиль описывает окружение целиком (rootfs, монтирования, сеть, лимиты),
поэтому инструмент ссылается на готовый профиль, а не собирает его сам.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.sandbox.profile import SandboxProfile

__all__ = ["SandboxConfig"]


class SandboxConfig(BaseModel):
    """Профили песочницы, общие для всех инструментов."""

    model_config = ConfigDict(extra="ignore")

    profiles: dict[str, SandboxProfile] = Field(
        min_length=1,
        description="Реестр профилей по имени; инструмент называет имя явно.",
    )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.profiles))

    def profile(self, name: str) -> SandboxProfile:
        """Имя обязательно: профиля по умолчанию нет."""
        if not name:
            msg = f"sandbox profile name is required; available: {self.names()}"
            raise KeyError(msg)
        found = self.profiles.get(name)
        if found is None:
            msg = (
                f"sandbox profile {name!r} is not defined; "
                f"available: {self.names()}"
            )
            raise KeyError(msg)
        return found
