"""Объявление bash-плагина: entry point группы boba.tools."""

from typing import Final

from boba.tool.shell.tools import TOOLS
from boba.toolkit.manifest import ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="bash",
    tools=tuple(TOOLS),
)
