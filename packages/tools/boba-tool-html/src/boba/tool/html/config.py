"""Конфиг плагина html (v2): `[tool.html]` / `BOBA_TOOL__HTML__*`."""

from __future__ import annotations

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList

__all__ = ["HtmlPluginConfig"]


class HtmlPluginConfig(BobaFlatSettings):
    """HTML multi-tool plugin: outline + section.

    Работает с workspace (через `ProjectWorkspaceShell`), без сетевого
    connection.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        config_path="tool.html",
    )

    enable: bool = Field(
        default=False,
        description="Регистрировать ли html-tools в DI/каталоге LLM.",
    )
    tools: StringList | None = Field(
        default=None,
        description=(
            "Allowlist tool-имён: None — оба включены; иначе только "
            "перечисленные ('html_outline', 'html_section')."
        ),
    )
