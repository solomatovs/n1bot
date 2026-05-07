"""Boba extension: builtin file-system tools."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.files_tools.append import AppendTool, AppendToolSection
from boba.ext.files_tools.cat import CatTool, CatToolSection
from boba.ext.files_tools.cd import CdTool, CdToolSection
from boba.ext.files_tools.config import FilesSection
from boba.ext.files_tools.cp import CpTool, CpToolSection
from boba.ext.files_tools.edit import EditTool, EditToolSection
from boba.ext.files_tools.grep import GrepTool, GrepToolSection
from boba.ext.files_tools.ls import LsTool, LsToolSection
from boba.ext.files_tools.mkdir import MkdirTool, MkdirToolSection
from boba.ext.files_tools.mv import MvTool, MvToolSection
from boba.ext.files_tools.pwd import PwdTool, PwdToolSection
from boba.ext.files_tools.rm import RmTool, RmToolSection
from boba.ext.files_tools.stat import StatTool, StatToolSection
from boba.ext.files_tools.touch import TouchTool, TouchToolSection
from boba.ext.files_tools.tree import TreeTool, TreeToolSection
from boba.ext.files_tools.write import WriteTool, WriteToolSection
from boba.tools.domain import ToolSourceId
from boba.tools.framework import (
    ExtensionContext,
    StaticToolSource,
    ToolSource,
)

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
