"""Shell-инструмент: узел bash графа стадий и фасад одиночного вызова."""

from boba.tool.shell.protocol import BashArgs, BashFailure, BashStage
from boba.tool.shell.tools import BashRun, build_bash_tool

__all__ = [
    "BashArgs",
    "BashFailure",
    "BashRun",
    "BashStage",
    "build_bash_tool",
]
