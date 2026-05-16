"""ShellPlugin: точка регистрации sandboxed bash tool."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, model_validator

from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList
from boba.tool.shell._profile import SandboxProfile
from boba.tool.shell.bash import BashTool, BashToolConfig
from boba.tools.domain import Tool, ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["ShellPlugin", "ShellPluginConfig"]


_REQUIRED_WHEN_ENABLED: frozenset[str] = frozenset(
    {"workspace_root", "profiles", "default_profile"},
)


class ShellPluginConfig(BobaFlatSettings):
    """Sandboxed bash tool (bubblewrap-based).

    Один tool `bash`. Профили песочницы задаются в `profiles`; LLM
    выбирает профиль по имени, но не может менять его параметры.

    При `enable=true` обязательны: `workspace_root`, непустой
    `profiles` и `default_profile`, ссылающийся на ключ из `profiles`.

    Типы полей объявлены строго (`Path`, `dict[...]`, `str`) без
    `Optional` — downstream-код после загрузки конфига работает с
    конкретными типами без рантайм-проверок. Обязательность при
    `enable=true` проверяется через `__pydantic_fields_set__`: поле
    считается заданным только если оно явно присутствует во входных
    данных (TOML/env). При `enable=false` все три поля можно опустить —
    их placeholder-значения не используются, потому что `build()` не
    вызывается.
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
            "Резолвится до абсолютного/каноничного. Монтируется RW в "
            "песочницу и используется как cwd по умолчанию."
        ),
    )
    profiles: dict[str, SandboxProfile] = Field(
        default_factory=dict,
        description=(
            "Реестр профилей песочницы по имени. При enable=true должен "
            "содержать хотя бы один профиль."
        ),
    )
    default_profile: str = Field(
        default="",
        description=(
            "Имя профиля, выбираемого если LLM не указал явно. "
            "Обязателен при enable=true, должен быть ключом `profiles`."
        ),
    )
    tools: StringList | None = Field(
        default=None,
        description=(
            "Allowlist tool-имён внутри плагина: None — все включены. "
        ),
    )
    bash: PromptOverlay = Field(default_factory=PromptOverlay)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.enable:
            return self
        missing = _REQUIRED_WHEN_ENABLED - self.model_fields_set
        if missing:
            msg = (
                f"при enable=true обязательны поля: {sorted(missing)}"
            )
            raise ValueError(msg)
        if self.default_profile not in self.profiles:
            msg = (
                f"default_profile={self.default_profile!r} отсутствует в "
                f"profiles; доступные: {sorted(self.profiles)}"
            )
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
    """Plugin sandboxed-bash; один tool, один source."""

    NAME: ClassVar[str] = "shell"
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin.shell")

    @classmethod
    def build(
        cls,
        cfg: ShellPluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        cls._check_bwrap_available()
        sid = cls.SOURCE_ID
        factories: dict[str, Callable[[], Tool]] = {
            "bash": lambda: BashTool(
                BashToolConfig(prompt=cfg.bash),
                ctx,
                sid,
                workspace_root=str(cfg.workspace_root),
                profiles=dict(cfg.profiles),
                default_profile=cfg.default_profile,
            ),
        }
        names = cls._select(cfg.tools, factories.keys())
        yield StaticToolSource(
            source_id=sid,
            tools=[factories[n]() for n in names],
        )

    @staticmethod
    def _check_bwrap_available() -> None:
        if shutil.which("bwrap") is None:
            msg = (
                "ShellPlugin: bubblewrap (`bwrap`) не найден в PATH. "
                "Установи пакет (apt: bubblewrap) или отключи плагин."
            )
            raise RuntimeError(msg)

    @staticmethod
    def _select(
        allowlist: list[str] | None,
        all_names: Iterable[str],
    ) -> list[str]:
        available = list(all_names)
        if not allowlist:
            return available
        unknown = [n for n in allowlist if n not in available]
        if unknown:
            msg = (
                f"ShellPlugin.tools: unknown names {unknown!r}, "
                f"available: {available!r}"
            )
            raise ValueError(msg)
        return [n for n in available if n in allowlist]
