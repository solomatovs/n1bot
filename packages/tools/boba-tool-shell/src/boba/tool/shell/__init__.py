"""Boba extension: два bash-tool'а — sandboxed (bwrap) и local."""

from __future__ import annotations

from boba.tool.shell.plugin import (
    LocalToolConfig,
    SandboxToolConfig,
    ShellPlugin,
    ShellPluginConfig,
)

__all__ = [
    "LocalToolConfig",
    "SandboxToolConfig",
    "ShellPlugin",
    "ShellPluginConfig",
]
