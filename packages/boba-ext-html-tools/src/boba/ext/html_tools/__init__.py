"""Boba extension: HTML navigation tools (outline + section)."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.html_tools.config import HtmlSection
from boba.ext.html_tools.outline import HtmlOutlineTool, HtmlOutlineToolSection
from boba.ext.html_tools.section import HtmlSectionTool, HtmlSectionToolSection
from boba.tools.domain import ToolSourceId
from boba.tools.framework import (
    ExtensionContext,
    StaticToolSource,
    ToolSource,
)

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: HTML-tools, гейт по [ext.html] enable."""
    cfg = ctx.config.section(HtmlSection)
    if not cfg.enable:
        return
    s = ctx.config.section
    tools = [
        HtmlOutlineTool(s(HtmlOutlineToolSection)),
        HtmlSectionTool(s(HtmlSectionToolSection)),
    ]
    if cfg.tools_allow:
        allow = set(cfg.tools_allow)
        tools = [t for t in tools if t.tool_id().to_wire() in allow]
    yield StaticToolSource(
        ToolSourceId("builtin.html"),
        priority=0,
        tools=tools,
    )
