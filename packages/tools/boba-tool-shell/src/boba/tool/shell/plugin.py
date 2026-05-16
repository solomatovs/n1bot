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


class ShellPluginConfig(BobaFlatSettings):
    """Sandboxed bash tool (bubblewrap-based).

    Один tool `bash`. Профили песочницы задаются в `profiles`; LLM
    выбирает профиль по имени, но не может менять его параметры.

    При `enable=true` обязательны: `workspace_root`, непустой
    `profiles` и `default_profile`, ссылающийся на ключ из `profiles`.
    Дефолтных значений нет намеренно — host-путь и политика песочницы
    выбираются пользователем явно.
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
    workspace_root: Path | None = Field(
        default=None,
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
    default_profile: str | None = Field(
        default=None,
        description=(
            "Имя профиля, выбираемого если LLM не указал явно. "
            "Обязателен при enable=true, должен быть ключом `profiles`."
        ),
    )
    tools: StringList | None = Field(
        default=None,
        description=(
            "Allowlist tool-имён внутри плагина: None — все включены. "
            "Сейчас единственный tool — 'bash'."
        ),
    )
    bash: PromptOverlay = Field(default_factory=PromptOverlay)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.enable:
            return self
        if self.workspace_root is None:
            msg = "workspace_root обязателен при enable=true"
            raise ValueError(msg)
        if not self.profiles:
            msg = "profiles обязателен при enable=true (минимум один профиль)"
            raise ValueError(msg)
        if self.default_profile is None:
            msg = (
                f"default_profile обязателен при enable=true; "
                f"доступные профили: {sorted(self.profiles)}"
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
        # install_plugins зовёт build только при enable=true, после чего
        # ShellPluginConfig._validate гарантирует non-None для этих полей.
        workspace_root = cfg.workspace_root
        default_profile = cfg.default_profile
        if workspace_root is None or default_profile is None:
            msg = "ShellPluginConfig: build() вызван при незаполненных полях"
            raise RuntimeError(msg)
        sid = cls.SOURCE_ID
        factories: dict[str, Callable[[], Tool]] = {
            "bash": lambda: BashTool(
                BashToolConfig(prompt=cfg.bash),
                ctx,
                sid,
                workspace_root=str(workspace_root),
                profiles=dict(cfg.profiles),
                default_profile=default_profile,
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
