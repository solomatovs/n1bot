"""Boba extension: builtin file-system tools."""

from __future__ import annotations

from collections.abc import Iterable

from boba.tools import ExtensionContext, StaticToolSource, ToolSource, ToolSourceId

from boba.ext.files.append import AppendTool
from boba.ext.files.cat import CatTool
from boba.ext.files.cd import CdTool
from boba.ext.files.config import FilesSection
from boba.ext.files.cp import CpTool
from boba.ext.files.edit import EditTool
from boba.ext.files.grep import GrepTool
from boba.ext.files.ls import LsTool
from boba.ext.files.mkdir import MkdirTool
from boba.ext.files.mv import MvTool
from boba.ext.files.pwd import PwdTool
from boba.ext.files.rm import RmTool
from boba.ext.files.stat import StatTool
from boba.ext.files.touch import TouchTool
from boba.ext.files.tree import TreeTool
from boba.ext.files.write import WriteTool

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: файловые tools, гейт по [ext.files] enable."""
    cfg = ctx.config.section(FilesSection)
    if not cfg.enable:
        return
    tools = [
        AppendTool(),
        CatTool(),
        CdTool(),
        CpTool(),
        EditTool(),
        GrepTool(),
        LsTool(),
        MkdirTool(),
        MvTool(),
        PwdTool(),
        RmTool(),
        StatTool(),
        TouchTool(),
        TreeTool(),
        WriteTool(),
    ]
    if cfg.tools_allow:
        allow = set(cfg.tools_allow)
        tools = [t for t in tools if t.tool_id().to_wire() in allow]
    yield StaticToolSource(
        ToolSourceId("builtin.files"),
        priority=0,
        tools=tools,
    )
