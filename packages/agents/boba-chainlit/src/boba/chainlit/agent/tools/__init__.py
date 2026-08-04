"""Инструменты агента: по пакету на секцию конфига [tool.<name>]."""

from boba.tool.chart import build_chart_tools
from boba.tool.shell import build_bash_tool

__all__ = [
    "build_bash_tool",
    "build_chart_tools",
]
