"""Объявление плагина canvas: entry point группы boba.tools."""

from typing import Final

from boba.tool.canvas.tools import TOOLS
from boba.toolkit.manifest import ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="canvas",
    tools=tuple(TOOLS),
)
