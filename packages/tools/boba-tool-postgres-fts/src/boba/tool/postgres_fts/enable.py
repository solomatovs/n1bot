"""Predicate factory для `@tool(enable_if=...)` в postgres_fts-плагине."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from boba.tool.postgres_fts.config import PostgresFtsPluginConfig
from boba.tools import FromConfig

__all__ = ["pg_fts_enable_if"]


def pg_fts_enable_if(tool_name: str) -> Callable[..., bool]:
    """Predicate: tool регистрируется, если плагин включён И входит в allowlist."""

    def _predicate(
        cfg: Annotated[PostgresFtsPluginConfig, FromConfig()],
    ) -> bool:
        if not cfg.enable:
            return False
        if cfg.tools is None:
            return True
        return tool_name in cfg.tools

    _predicate.__name__ = f"_pg_fts_{tool_name}_enabled"
    return _predicate
