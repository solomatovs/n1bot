"""Объявление плагина ch: entry point группы boba.tools."""

from typing import Final

from boba.tool.ch.tools import TOOLS
from boba.toolkit.manifest import ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="ch",
    tools=tuple(TOOLS),
    network=True,
)
