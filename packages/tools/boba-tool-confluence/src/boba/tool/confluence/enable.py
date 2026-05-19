"""Predicate factory для `@tool(enable_if=...)` в confluence-плагине."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from boba.tool.confluence.config import ConfluencePluginConfig
from boba.tools import FromConfig

__all__ = ["confluence_enable_if"]


def confluence_enable_if(tool_name: str) -> Callable[..., bool]:
    """Predicate: tool регистрируется при enabled плагине + в allowlist."""

    def _predicate(
        cfg: Annotated[ConfluencePluginConfig, FromConfig()],
    ) -> bool:
        if not cfg.enable:
            return False

        if cfg.tools is None:
            return True

        return tool_name in cfg.tools

    _predicate.__name__ = f"_confluence_{tool_name}_enabled"

    return _predicate
