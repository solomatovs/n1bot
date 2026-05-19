"""Конфиг плагина files (v2).

Один `BobaFlatSettings` на все 15 tools: `[tool.files]` /
`BOBA_TOOL__FILES__*`. Используется в каждом `@tool` через FromConfig
(в основном — для проверки `enable_if`, а в `CatTool` ещё и для
`cat_max_lines`).
"""

from __future__ import annotations

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList

__all__ = ["FilesPluginConfig"]


class FilesPluginConfig(BobaFlatSettings):
    """Builtin file-system tools.

    cat/ls/grep/edit/write/append/cp/mv/rm/mkdir/touch/cd/pwd/stat/tree.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_TOOL__FILES__",
        boba_toml_section="tool.files",
    )

    enable: bool = Field(
        default=False,
        description="Регистрировать ли files-tools в DI/каталоге LLM.",
    )
    tools: StringList | None = Field(
        default=None,
        description=(
            "Allowlist tool-имён внутри плагина: None (default) — все "
            "включены; иначе создаются только перечисленные. Имена — "
            "snake_case без суффикса Tool: 'cat', 'grep', 'ls', ..."
        ),
    )
    cat_max_lines: int = Field(
        default=2000,
        ge=1,
        description="Максимум строк в одном вызове cat.",
    )
