"""Конфиг плагина files.

`[tool.files]` / `BOBA_TOOL__FILES__*`. Используется в каждом `@tool`
через FromConfig (в основном для `cat_max_lines` в `cat`).

Поля `enable` / `tools` — забота framework'а; они читаются из той же
TOML-секции, но через `AgentBuilder.discover_plugins`, не через
плагин-конфиг. `extra="ignore"` позволяет им сосуществовать в одной
TOML-секции без ValidationError.
"""

from __future__ import annotations

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["FilesPluginConfig"]


class FilesPluginConfig(BobaFlatSettings):
    """Builtin file-system tools (cat/ls/grep/edit/write/...).

    Config-секция: `[tool.files]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.files",
    )

    cat_max_lines: int = Field(
        default=2000,
        ge=1,
        description="Максимум строк в одном вызове cat.",
    )
