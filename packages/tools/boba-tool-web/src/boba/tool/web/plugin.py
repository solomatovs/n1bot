"""Объявление плагина web: entry point группы boba.tools."""

from typing import Final

from boba.tool.web.tools import TOOLS
from boba.toolkit.manifest import DataImage, ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="web",
    tools=tuple(TOOLS),
    network=True,
    data=(DataImage(name="rapidocr", target="/var/cache/rapidocr"),),
)
