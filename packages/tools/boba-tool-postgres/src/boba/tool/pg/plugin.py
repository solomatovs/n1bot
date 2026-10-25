"""Объявление плагина pg: entry point группы boba.tools."""

from typing import Final

from boba.tool.pg.tools import TOOLS
from boba.toolkit.manifest import ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="pg",
    tools=tuple(TOOLS),
    network=True,
)
