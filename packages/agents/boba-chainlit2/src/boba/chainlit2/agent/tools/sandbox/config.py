"""Конфиг песочницы: профили bubblewrap, рабочая директория, лимиты."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.chainlit2.agent.tools.sandbox.profile import SandboxProfile

__all__ = ["BashSandboxConfig"]


class BashSandboxConfig(BaseModel):
    """Конфиг bash ([tool.sandbox]): реестр профилей песочницы."""

    model_config = ConfigDict(extra="ignore")

    workspace_root: Path = Field(
        default=Path(),
        description=(
            "База рабочих папок. На запись монтируется только "
            "<workspace_root>/<user_id>/<thread_id> — чат изолирован "
            "и от других пользователей, и от других чатов."
        ),
    )
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
                f"отсутствует в profiles; доступные: {sorted(self.profiles)}"
            )
            raise ValueError(msg)
        resolved = self.workspace_root.expanduser().resolve(strict=False)
        if not resolved.is_dir():
            msg = f"sandbox.workspace_root не директория: {resolved}"
            raise ValueError(msg)
        object.__setattr__(self, "workspace_root", resolved)
        return self
