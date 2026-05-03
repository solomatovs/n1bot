"""Boba extension: HTML navigation tools (outline + section)."""

from __future__ import annotations

from collections.abc import Iterable

from boba_next.tools import ExtensionContext, StaticToolSource, ToolSource, ToolSourceId

from boba.ext.html.outline import HtmlOutlineTool
from boba.ext.html.section import HtmlSectionTool

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
