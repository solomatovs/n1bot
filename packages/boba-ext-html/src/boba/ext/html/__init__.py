"""Boba extension: HTML navigation tools (outline + section)."""

from __future__ import annotations

from collections.abc import Iterable

from boba.domain.core.tools import StaticToolSource, ToolSource, ToolSourceId
from boba.ext.html.outline import HtmlOutlineTool
from boba.ext.html.section import HtmlSectionTool
from boba.infra.tool_plugin_loader import ExtensionContext

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: HTML-tools одним источником."""
    del ctx
    yield StaticToolSource(
        ToolSourceId("builtin.html"),
        priority=0,
        tools=[
            HtmlOutlineTool(),
            HtmlSectionTool(),
        ],
    )
