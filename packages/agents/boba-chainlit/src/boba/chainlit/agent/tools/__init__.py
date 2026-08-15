"""Инструменты агента: по пакету на секцию конфига [tool.<name>]."""

from boba.tool.shell.tools import BashToolConfig, build_bash_tool

__all__ = [
    "BashToolConfig",
    "build_bash_tool",
]
