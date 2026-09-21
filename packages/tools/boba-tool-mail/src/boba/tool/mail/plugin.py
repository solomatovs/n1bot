"""Манифест плагина mail: entry point группы boba.tools."""

from typing import Final

from boba.tool.mail.tools import TOOLS
from boba.toolkit.manifest import ToolPluginManifest

MANIFEST: Final = ToolPluginManifest(
    section="mail",
    tools=tuple(TOOLS),
)
