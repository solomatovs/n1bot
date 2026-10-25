"""Объявление плагина doc: entry point группы boba.tools."""

from typing import Final

from boba.tool.doc.tools import TOOLS
from boba.toolkit.manifest import DataImage, ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="doc",
    tools=tuple(TOOLS),
    data=(DataImage(name="rapidocr", target="/var/cache/rapidocr"),),
)
