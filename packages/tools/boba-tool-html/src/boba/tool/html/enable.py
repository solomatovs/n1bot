"""Predicate factory для `@tool(enable_if=...)` в html-плагине."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from boba.tool.html.config import HtmlPluginConfig
from boba.tools import FromConfig

__all__ = ["html_enable_if"]


def html_enable_if(tool_name: str) -> Callable[..., bool]:
    """Predicate: tool регистрируется, если плагин включён И входит в allowlist."""

    def _predicate(
        cfg: Annotated[HtmlPluginConfig, FromConfig()],
    ) -> bool:
        if not cfg.enable:
            return False
        if cfg.tools is None:
            return True
        return tool_name in cfg.tools

    _predicate.__name__ = f"_html_{tool_name}_enabled"
    return _predicate
