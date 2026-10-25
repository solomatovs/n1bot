"""Объявление плагина ora: entry point группы boba.tools."""

from typing import Final

from boba.tool.ora.tools import TOOLS
from boba.toolkit.manifest import ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="ora",
    tools=tuple(TOOLS),
    network=True,
)
