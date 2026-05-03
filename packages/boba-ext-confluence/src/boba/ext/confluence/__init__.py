"""Boba extension: Confluence-export navigation tools."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.confluence.config import ConfluenceSection
from boba.ext.confluence.outline import (
    ConfluenceOutlineTool,
    ConfluenceOutlineToolSection,
)
from boba.ext.confluence.section import (
    ConfluenceSectionTool,
    ConfluenceSectionToolSection,
)
from boba.tools import ExtensionContext, StaticToolSource, ToolSource, ToolSourceId

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: Confluence-tools, гейт по [ext.confluence] enable."""
    cfg = ctx.config.section(ConfluenceSection)
    if not cfg.enable:
        return
    s = ctx.config.section
    tools = [
        ConfluenceOutlineTool(s(ConfluenceOutlineToolSection)),
        ConfluenceSectionTool(s(ConfluenceSectionToolSection)),
    ]
    if cfg.tools_allow:
        allow = set(cfg.tools_allow)
        tools = [t for t in tools if t.tool_id().to_wire() in allow]
    yield StaticToolSource(
        ToolSourceId("builtin.confluence"),
        priority=0,
        tools=tools,
    )
