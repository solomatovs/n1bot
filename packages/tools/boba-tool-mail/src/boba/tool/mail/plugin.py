"""Объявление плагина mail: entry point группы boba.tools."""

from typing import Final

from boba.tool.mail.tools import TOOLS
from boba.toolkit.manifest import ToolPlugin

MANIFEST: Final = ToolPlugin(
    id="mail",
    tools=tuple(TOOLS),
    network=True,
)
