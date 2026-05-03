"""Boba extension: Confluence-export navigation tools."""

from __future__ import annotations

from collections.abc import Iterable

from boba.tools import ExtensionContext, StaticToolSource, ToolSource, ToolSourceId

from boba.ext.confluence.outline import ConfluenceOutlineTool
from boba.ext.confluence.section import ConfluenceSectionTool

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: Confluence-tools одним источником."""
    del ctx
    yield StaticToolSource(
        ToolSourceId("builtin.confluence"),
        priority=0,
        tools=[
            ConfluenceOutlineTool(),
            ConfluenceSectionTool(),
        ],
    )
