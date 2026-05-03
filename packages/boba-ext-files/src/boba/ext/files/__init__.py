"""Boba extension: builtin file-system tools."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.files.append import AppendTool, AppendToolSection
from boba.ext.files.cat import CatTool, CatToolSection
from boba.ext.files.cd import CdTool, CdToolSection
from boba.ext.files.config import FilesSection
from boba.ext.files.cp import CpTool, CpToolSection
from boba.ext.files.edit import EditTool, EditToolSection
from boba.ext.files.grep import GrepTool, GrepToolSection
from boba.ext.files.ls import LsTool, LsToolSection
from boba.ext.files.mkdir import MkdirTool, MkdirToolSection
from boba.ext.files.mv import MvTool, MvToolSection
from boba.ext.files.pwd import PwdTool, PwdToolSection
from boba.ext.files.rm import RmTool, RmToolSection
from boba.ext.files.stat import StatTool, StatToolSection
from boba.ext.files.touch import TouchTool, TouchToolSection
from boba.ext.files.tree import TreeTool, TreeToolSection
from boba.ext.files.write import WriteTool, WriteToolSection
from boba.tools import ExtensionContext, StaticToolSource, ToolSource, ToolSourceId

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point boba.tools: файловые tools, гейт по [ext.files] enable."""
    cfg = ctx.config.section(FilesSection)
    if not cfg.enable:
        return
    s = ctx.config.section
    tools = [
        AppendTool(s(AppendToolSection)),
        CatTool(s(CatToolSection)),
        CdTool(s(CdToolSection)),
        CpTool(s(CpToolSection)),
        EditTool(s(EditToolSection)),
        GrepTool(s(GrepToolSection)),
        LsTool(s(LsToolSection)),
        MkdirTool(s(MkdirToolSection)),
        MvTool(s(MvToolSection)),
        PwdTool(s(PwdToolSection)),
        RmTool(s(RmToolSection)),
        StatTool(s(StatToolSection)),
        TouchTool(s(TouchToolSection)),
        TreeTool(s(TreeToolSection)),
        WriteTool(s(WriteToolSection)),
    ]
    if cfg.tools_allow:
        allow = set(cfg.tools_allow)
        tools = [t for t in tools if t.tool_id().to_wire() in allow]
    yield StaticToolSource(
        ToolSourceId("builtin.files"),
        priority=0,
        tools=tools,
    )
