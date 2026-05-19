"""Конфиги bash-tool'ов: `BashLocalConfig` и `BashSandboxConfig`.

Каждый — самостоятельный `BobaFlatSettings`, авто-загружаемый
framework'ом через FromConfig-маркер. Свой `boba_env_prefix` и свой
TOML-section: `[tool.bash_local]` и `[tool.bash_sandbox]`.

Поле `enable: bool` — флаг условной регистрации tool'а. Читается
predicate'ом, передаваемым в `@tool(enable_if=...)`. Если оба
tool'а имеют `enable=True` одновременно — оба зарегистрируются и
LLM увидит оба имени (`bash_local`, `bash_sandbox`); это не
запрещено, но обычно оператор включает один.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.shell._profile import SandboxProfile
from boba.tool.shell._profile_local import DEFAULT_PASSTHROUGH

__all__ = ["BashLocalConfig", "BashSandboxConfig"]


class BashLocalConfig(BobaFlatSettings):
    """Конфиг `bash_local`: subprocess без bwrap-изоляции.

    Все поля — operator-controlled. LLM выбирает только `command`/`stdin`,
    остальное задано здесь. `workspace_root` обязателен при `enable=True`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_TOOL__BASH_LOCAL__",
        boba_toml_section="tool.bash_local",
    )

    enable: bool = Field(
        default=False,
        description="Регистрировать tool в DI/каталоге LLM.",
    )
    workspace_root: Path = Field(
        default=Path(),
        description=(
            "Host-путь к корню проекта. Обязателен при enable=true. "
            "Резолвится до абсолютного/каноничного. Используется как "
            "cwd по умолчанию для subprocess'а."
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
            "Host-env переменные, наследуемые внутрь процесса. По "
            "умолчанию — безопасный минимум. Пустой tuple = ничего "
            "не наследовать."
        ),
    )
    env_set: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Явные env-переменные. Перекрывают env_passthrough при "
            "совпадении имён."
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
            "Лимит stdout И stderr по отдельности (мин. 1024). "
            "Превышение → обрезка и `truncated=True` в результате."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.enable:
            return self
        if "workspace_root" not in self.model_fields_set:
            msg = "bash_local.enable=true: workspace_root обязателен"
            raise ValueError(msg)
        resolved = self.workspace_root.expanduser().resolve(strict=False)
        if not resolved.exists():
            msg = f"bash_local.workspace_root не существует: {resolved}"
            raise ValueError(msg)
        if not resolved.is_dir():
            msg = f"bash_local.workspace_root не директория: {resolved}"
            raise ValueError(msg)
        object.__setattr__(self, "workspace_root", resolved)
        return self


class BashSandboxConfig(BobaFlatSettings):
    """Конфиг `bash_sandbox`: subprocess внутри bubblewrap.

    `profiles` — реестр именованных профилей песочницы (FS-маунты,
    network, env, timeouts). LLM выбирает профиль по имени из этого
    реестра, поля профиля менять не может. При `enable=True`
    `profiles` обязан быть непустым и содержать `default_profile`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_TOOL__BASH_SANDBOX__",
        boba_toml_section="tool.bash_sandbox",
    )

    enable: bool = Field(
        default=False,
        description="Регистрировать tool в DI/каталоге LLM.",
    )
    workspace_root: Path = Field(
        default=Path(),
        description=(
            "Host-путь к корню проекта. Обязателен при enable=true. "
            "Резолвится до абсолютного/каноничного. RW-bind в "
            "песочнице + cwd для запуска bwrap."
        ),
    )
    profiles: dict[str, SandboxProfile] = Field(
        default_factory=dict,
        description=(
            "Реестр sandbox-профилей по имени. Обязан быть непустым "
            "при enable=true."
        ),
    )
    default_profile: str = Field(
        default="",
        description=(
            "Профиль по умолчанию, если LLM не указал `profile` в "
            "args. Обязан быть среди ключей `profiles` при enable=true."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.enable:
            return self
        if "workspace_root" not in self.model_fields_set:
            msg = "bash_sandbox.enable=true: workspace_root обязателен"
            raise ValueError(msg)
        if not self.profiles:
            msg = "bash_sandbox.enable=true: profiles обязан быть непустым"
            raise ValueError(msg)
        if not self.default_profile:
            msg = "bash_sandbox.enable=true: default_profile обязателен"
            raise ValueError(msg)
        if self.default_profile not in self.profiles:
            msg = (
                f"bash_sandbox.default_profile={self.default_profile!r} "
                f"отсутствует в profiles; доступные: {sorted(self.profiles)}"
            )
            raise ValueError(msg)
        resolved = self.workspace_root.expanduser().resolve(strict=False)
        if not resolved.exists():
            msg = f"bash_sandbox.workspace_root не существует: {resolved}"
            raise ValueError(msg)
        if not resolved.is_dir():
            msg = f"bash_sandbox.workspace_root не директория: {resolved}"
            raise ValueError(msg)
        object.__setattr__(self, "workspace_root", resolved)
        return self
