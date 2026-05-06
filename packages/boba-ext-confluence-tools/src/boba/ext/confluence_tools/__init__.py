"""
Boba runtime-extension: онлайн-tools для Confluence
(search + page outline/section)
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.confluence_tools.config import ConfluenceSection
from boba.ext.confluence_tools.page import ConfluencePageSection
from boba.ext.confluence_tools.page_outline import (
    ConfluencePageOutlineTool,
    ConfluencePageOutlineToolSection,
)
from boba.ext.confluence_tools.page_section import (
    ConfluencePageSectionTool,
    ConfluencePageSectionToolSection,
)
from boba.ext.confluence_tools.search import (
    ConfluenceSearchSection,
    ConfluenceSearchTool,
    ConfluenceSearchToolSection,
)
from boba.tools.domain import ToolSourceId
from boba.tools.framework import (
    ExtensionContext,
    StaticToolSource,
    ToolSource,
)

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: Confluence-tools, гейт по [ext.confluence] enable."""
    cfg = ctx.config.section(ConfluenceSection)
    if not cfg.enable:
        return
    s = ctx.config.section
    tools = [
        ConfluenceSearchTool(
            s(ConfluenceSearchToolSection),
            s(ConfluenceSearchSection),
        ),
        ConfluencePageOutlineTool(
            s(ConfluencePageOutlineToolSection),
            s(ConfluencePageSection),
        ),
        ConfluencePageSectionTool(
            s(ConfluencePageSectionToolSection),
            s(ConfluencePageSection),
        ),
    ]
    if cfg.tools_allow:
        allow = set(cfg.tools_allow)
        tools = [t for t in tools if t.tool_id().to_wire() in allow]
    yield StaticToolSource(
        ToolSourceId("builtin.confluence"),
        priority=0,
        tools=tools,
    )
