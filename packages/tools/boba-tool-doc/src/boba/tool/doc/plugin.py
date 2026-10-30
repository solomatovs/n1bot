"""Объявление плагина doc: entry point группы boba.tools."""

from typing import Final

from boba.tool.doc.tools import TOOLS
from boba.toolkit.manifest import ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="doc",
    tools=tuple(TOOLS),
)
