"""ShellPlugin: регистрирует ровно один bash-tool в одном из двух режимов.

LLM-имя инструмента всегда `bash` — какой именно реализацией он подкреплён,
выбирает оператор через `variant`:
- `"sandbox"` — bwrap-изоляция, реестр именованных профилей с разными
  политиками FS/network;
- `"local"` — прямой `subprocess.Popen` без изоляции, плоский policy-конфиг.

Где живёт валидация:
- Range-checks числовых полей и path-canonicalize bind-путей —
  на DTO (`SandboxProfile`, `LocalToolConfig`) через pydantic
  `Field(ge=,le=)` / `@field_validator`. Срабатывают при парсинге.
- Структурные cross-field инварианты (registry профилей,
  существование `workspace_root`) — на `SandboxToolConfig` и
  `ShellPluginConfig` через `@model_validator(mode="after")`.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.shell._profile import SandboxProfile
from boba.tool.shell._profile_local import DEFAULT_PASSTHROUGH
from boba.tool.shell.bash_local import BashTool, BashToolConfig
from boba.tool.shell.bash_sandbox import BashSandboxTool, BashSandboxToolConfig
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = [
    "LocalToolConfig",
    "SandboxToolConfig",
    "ShellPlugin",
    "ShellPluginConfig",
]


_SANDBOX_VARIANT = "sandbox"
_LOCAL_VARIANT = "local"
Variant = Literal["sandbox", "local"]


class SandboxToolConfig(BaseModel):
    """Конфиг sandbox-варианта (`bash_sandbox`).

    Применяется, только если `bash_sandbox` входит в выбранные tool'ы
    плагина (см. `ShellPluginConfig.tools`).
    """

    model_config = ConfigDict(extra="forbid")

    profiles: dict[str, SandboxProfile] = Field(
        default_factory=dict,
        description=(
            "Реестр sandbox-профилей по имени. Требуется непустым, если "
            "tool `bash_sandbox` активен."
        ),
    )
    default_profile: str = Field(
        default="",
        description=(
            "Имя профиля, выбираемого по умолчанию для `bash_sandbox`."
        ),
    )
    prompt: PromptOverlay = Field(default_factory=PromptOverlay)

    @model_validator(mode="after")
    def _check_registry(self) -> Self:
        """Cross-field инвариант реестра профилей.

        Пустая секция (ни `profiles`, ни `default_profile` не заданы)
        валидна — это «sandbox не настроен, может быть неактивен».
        Но как только хоть что-то задано, требуется консистентная пара.
        """
        if not self.profiles and not self.default_profile:
            return self
        if not self.profiles:
            msg = "sandbox.profiles обязателен непустым, если задан default_profile"
            raise ValueError(msg)
        if not self.default_profile:
            msg = "sandbox.default_profile обязателен, если заданы profiles"
            raise ValueError(msg)
        if self.default_profile not in self.profiles:
            msg = (
                f"sandbox.default_profile={self.default_profile!r} отсутствует "
                f"в sandbox.profiles; доступные: {sorted(self.profiles)}"
            )
            raise ValueError(msg)
        return self


class LocalToolConfig(BaseModel):
    """Конфиг local-варианта (`bash`, без bwrap).

    Применяется, только если `bash` входит в выбранные tool'ы плагина.
    В отличие от sandbox здесь нет реестра именованных профилей: при
    отсутствии изоляции переключать нечего, поэтому policy-поля живут
    плоско.
    """

    model_config = ConfigDict(extra="forbid")

    cwd: str = Field(
        default="",
        description=(
            "Рабочая директория для процесса. Пустая строка = "
            "workspace_root плагина. Путь не валидируется на "
            "существование — subprocess.Popen вернёт ошибку, если "
            "что-то не так."
        ),
    )
    env_passthrough: tuple[str, ...] = Field(
        default=DEFAULT_PASSTHROUGH,
        description=(
            "Host-env переменные, видимые внутри процесса. По умолчанию "
            "— безопасный минимум (PATH/HOME/USER/LANG/LC_ALL/TERM). "
            "Пустой tuple = не наследовать ничего."
        ),
    )
    env_set: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Явно заданные env-переменные. Перекрывают значения из "
            "env_passthrough при совпадении имён."
        ),
    )
    timeout_sec: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="Жёсткий таймаут выполнения процесса (1..3600 сек).",
    )
    max_output_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        description=(
            "Лимит размера stdout И stderr по отдельности (мин. 1024). "
            "Превышение → обрезка и `truncated=True` в результате."
        ),
    )
    prompt: PromptOverlay = Field(default_factory=PromptOverlay)


class ShellPluginConfig(BobaFlatSettings):
    """Один bash-tool в одном из режимов; имя для LLM всегда `bash`.

    Какой вариант поднимать — определяет `variant`: `"sandbox"` или
    `"local"`. При `enable=true` `variant` и `workspace_root` обязательны.
    Для `sandbox` дополнительно нужен непустой `sandbox.profiles` и
    валидный `sandbox.default_profile`; для `local` достаточно
    дефолтов `LocalToolConfig`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_TOOL__SHELL__",
        boba_toml_section="tool.shell",
    )

    enable: bool = Field(
        default=False,
        description="Подключить плагин в discovery.",
    )
    workspace_root: Path = Field(
        default=Path(),
        description=(
            "Host-путь к корню проекта. Обязателен при enable=true. "
            "Резолвится до абсолютного/каноничного. Используется как "
            "RW-bind для bwrap и как cwd по умолчанию для обоих tool'ов."
        ),
    )
    variant: Variant | None = Field(
        default=None,
        description=(
            "Какой из двух bash-вариантов поднять: 'sandbox' (bwrap) или "
            "'local' (без изоляции). LLM-имя tool'а всегда `bash`, "
            "поэтому одновременно активен только один вариант. None при "
            "enable=true — ошибка."
        ),
    )
    sandbox: SandboxToolConfig = Field(default_factory=SandboxToolConfig)
    local: LocalToolConfig = Field(default_factory=LocalToolConfig)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.enable:
            return self

        if self.variant is None:
            msg = "при enable=true обязателен variant ('sandbox' или 'local')"
            raise ValueError(msg)

        if "workspace_root" not in self.model_fields_set:
            msg = "при enable=true обязателен workspace_root"
            raise ValueError(msg)

        resolved = self.workspace_root.expanduser().resolve(strict=False)
        if not resolved.exists():
            msg = f"workspace_root не существует: {resolved}"
            raise ValueError(msg)
        if not resolved.is_dir():
            msg = f"workspace_root не директория: {resolved}"
            raise ValueError(msg)
        object.__setattr__(self, "workspace_root", resolved)
        return self


class ShellPlugin(Plugin[ShellPluginConfig, ToolSource]):
    """Plugin одного bash-tool'а в одном из режимов — sandbox или local."""

    NAME: ClassVar[str] = "shell"
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin_shell")

    @classmethod
    def build(
        cls,
        cfg: ShellPluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        sid = cls.SOURCE_ID
        if not cfg.enable or cfg.variant is None:
            return

        workspace_root = str(cfg.workspace_root)

        if cfg.variant == _SANDBOX_VARIANT:
            sandbox_cfg = BashSandboxToolConfig(
                prompt=cfg.sandbox.prompt,
                workspace_root=workspace_root,
                profiles=dict(cfg.sandbox.profiles),
                default_profile=cfg.sandbox.default_profile,
            )
            tool = BashSandboxTool(sandbox_cfg, ctx, sid)
        else:
            local_cfg = BashToolConfig(
                prompt=cfg.local.prompt,
                workspace_root=workspace_root,
                cwd=cfg.local.cwd,
                env_passthrough=cfg.local.env_passthrough,
                env_set=dict(cfg.local.env_set),
                timeout_sec=cfg.local.timeout_sec,
                max_output_bytes=cfg.local.max_output_bytes,
            )
            tool = BashTool(local_cfg, ctx, sid)

        yield StaticToolSource(source_id=sid, tools=[tool])
