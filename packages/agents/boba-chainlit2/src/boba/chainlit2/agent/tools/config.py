"""Конфиги shell-инструментов: [tool.shell] и [tool.sandbox]."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.chainlit2.agent.tools.profile_local import DEFAULT_PASSTHROUGH
from boba.chainlit2.agent.tools.sandbox_profile import SandboxProfile

__all__ = ["BashLocalConfig", "BashSandboxConfig"]


class BashLocalConfig(BaseModel):
    """Конфиг bash_local ([tool.shell]); LLM задаёт только command/stdin."""

    model_config = ConfigDict(extra="ignore")

    workspace_root: Path = Field(
        default=Path(),
        description=(
            "Host-путь к корню проекта. Резолвится до абсолютного. "
            "Используется как cwd по умолчанию для subprocess'а."
        ),
    )
    cwd: str = Field(
        default="",
        description=(
            "Рабочая директория. Пустая строка = workspace_root. Не "
            "валидируется на существование — Popen вернёт ошибку."
        ),
    )
    env_passthrough: tuple[str, ...] = Field(
        default=DEFAULT_PASSTHROUGH,
        description=(
            "Host-env переменные, наследуемые внутрь процесса. По умолчанию — "
            "безопасный минимум. Пустой tuple = ничего не наследовать."
        ),
    )
    env_set: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Явные env-переменные. Перекрывают env_passthrough при совпадении имён."
        ),
    )
    timeout_sec: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="Жёсткий таймаут выполнения (1..3600 сек).",
    )
    max_output_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        description=(
            "Лимит stdout И stderr по отдельности (мин. 1024). Превышение -> "
            "обрезка и `truncated=True` в результате."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """workspace_root резолвится до абсолютного пути и проверяется."""
        resolved = self.workspace_root.expanduser().resolve(strict=False)
        if not resolved.exists():
            msg = f"bash_local.workspace_root не существует: {resolved}"
            raise ValueError(msg)
        if not resolved.is_dir():
            msg = f"bash_local.workspace_root не директория: {resolved}"
            raise ValueError(msg)
        object.__setattr__(self, "workspace_root", resolved)
        return self


class BashSandboxConfig(BaseModel):
    """Конфиг bash ([tool.sandbox]): реестр профилей песочницы."""

    model_config = ConfigDict(extra="ignore")

    workspace_root: Path = Field(
        default=Path(),
        description=(
            "Host-путь к корню проекта. RW-bind в песочнице + cwd для "
            "запуска bwrap."
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
        """default_profile должен быть среди profiles; пустой реестр валиден."""
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
