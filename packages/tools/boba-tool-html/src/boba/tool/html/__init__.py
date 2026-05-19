"""boba-tool-html — v2 плагин: outline + section HTML-навигатор.

Entry-point модуль для `AgentBuilder.use_plugin(boba.tool.html)` или
discovery через `[project.entry-points."boba.plugins"]`.

Tools шарят `HtmlPluginConfig` (`[tool.html]`, `BOBA_TOOL__HTML__*`);
`enable=true` включает оба, allowlist `tools` сужает.
`ProjectWorkspaceShell` инжектится через `FromDI(Scope.APP)`.
"""

from __future__ import annotations

from boba.tool.html.config import HtmlPluginConfig
from boba.tool.html.outline import HtmlOutlineTool
from boba.tool.html.section import HtmlSectionTool

__all__ = [
    "HtmlOutlineTool",
    "HtmlPluginConfig",
    "HtmlSectionTool",
]
