"""Boba extension: HTML navigation tools (outline + section)."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.html.config import HtmlSection
from boba.ext.html.outline import HtmlOutlineTool
from boba.ext.html.section import HtmlSectionTool
from boba.tools import ExtensionContext, StaticToolSource, ToolSource, ToolSourceId

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: HTML-tools, гейт по [ext.html] enable."""
    cfg = ctx.config.section(HtmlSection)
    if not cfg.enable:
        return
    tools = [HtmlOutlineTool(), HtmlSectionTool()]
    if cfg.tools_allow:
        allow = set(cfg.tools_allow)
        tools = [t for t in tools if t.tool_id().to_wire() in allow]
    yield StaticToolSource(
        ToolSourceId("builtin.html"),
        priority=0,
        tools=tools,
    )
