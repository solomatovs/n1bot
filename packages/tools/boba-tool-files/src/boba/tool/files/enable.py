"""Predicate factory для `@tool(enable_if=...)` в files-плагине.

Каждый tool вызывает `files_enabled("cat")`, чтобы получить замыкание,
интроспектируемое framework'ом (один `FromConfig`-параметр). Замыкание
возвращает True, если плагин включён И tool либо явно в allowlist'е,
либо allowlist=None (все включены).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from boba.tool.files.config import FilesPluginConfig
from boba.tools import FromConfig

__all__ = ["files_enable_if"]


def files_enable_if(tool_name: str) -> Callable[..., bool]:
    """Создать enable_if-predicate для tool'а с указанным wire-именем."""

    def _predicate(
        cfg: Annotated[FilesPluginConfig, FromConfig()],
    ) -> bool:
        if not cfg.enable:
            return False

        if cfg.tools is None:
            return True

        return tool_name in cfg.tools

    _predicate.__name__ = f"_files_{tool_name}_enabled"

    return _predicate
