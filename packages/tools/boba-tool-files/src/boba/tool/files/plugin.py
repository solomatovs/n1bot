"""FilesPlugin: единая точка регистрации файловых tools."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from boba.patterns import StrId
from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion.types import ParseCsvList
from boba.tool.files.append import AppendTool, AppendToolConfig
from boba.tool.files.cat import CatTool, CatToolConfig
from boba.tool.files.cd import CdTool, CdToolConfig
from boba.tool.files.cp import CpTool, CpToolConfig
from boba.tool.files.edit import EditTool, EditToolConfig
from boba.tool.files.grep import GrepTool, GrepToolConfig
from boba.tool.files.ls import LsTool, LsToolConfig
from boba.tool.files.mkdir import MkdirTool, MkdirToolConfig
from boba.tool.files.mv import MvTool, MvToolConfig
from boba.tool.files.pwd import PwdTool, PwdToolConfig
from boba.tool.files.rm import RmTool, RmToolConfig
from boba.tool.files.stat import StatTool, StatToolConfig
from boba.tool.files.touch import TouchTool, TouchToolConfig
from boba.tool.files.tree import TreeTool, TreeToolConfig
from boba.tool.files.write import WriteTool, WriteToolConfig
from boba.tools.domain import Tool, ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["FilesPlugin", "FilesPluginConfig"]


@dataclass(frozen=True)
class FilesPluginConfig:
    """
    Builtin file-system tools:
    cat/ls/grep/edit/write/append/cp/mv/rm/mkdir/touch/cd/pwd/stat/tree
    """

    cat: CatToolConfig = field(default_factory=CatToolConfig)
    append: PromptOverlay = field(default_factory=PromptOverlay)
    cd: PromptOverlay = field(default_factory=PromptOverlay)
    cp: PromptOverlay = field(default_factory=PromptOverlay)
    edit: PromptOverlay = field(default_factory=PromptOverlay)
    grep: PromptOverlay = field(default_factory=PromptOverlay)
    ls: PromptOverlay = field(default_factory=PromptOverlay)
    mkdir: PromptOverlay = field(default_factory=PromptOverlay)
    mv: PromptOverlay = field(default_factory=PromptOverlay)
    pwd: PromptOverlay = field(default_factory=PromptOverlay)
    rm: PromptOverlay = field(default_factory=PromptOverlay)
    stat: PromptOverlay = field(default_factory=PromptOverlay)
    touch: PromptOverlay = field(default_factory=PromptOverlay)
    tree: PromptOverlay = field(default_factory=PromptOverlay)
    write: PromptOverlay = field(default_factory=PromptOverlay)
    tools: Annotated[
        list[str] | None,
        "Allowlist tool-имён внутри плагина: None (default) — все включены; "
        "иначе создаются и регистрируются только перечисленные. "
        "Имена соответствуют t.name().to_wire() (например 'cat', 'grep').",
        ParseCsvList(),
    ] = None


class FilesPlugin(Plugin[FilesPluginConfig, ToolSource]):
    """Plugin файловых tools: 15 tools, без connection."""

    NAME: ClassVar[StrId] = StrId("files")
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin.files")

    @classmethod
    def build(
        cls,
        cfg: FilesPluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        sid = cls.SOURCE_ID
        factories: dict[str, Callable[[], Tool]] = {
            "append": lambda: AppendTool(AppendToolConfig(prompt=cfg.append), ctx, sid),
            "cat": lambda: CatTool(cfg.cat, ctx, sid),
            "cd": lambda: CdTool(CdToolConfig(prompt=cfg.cd), ctx, sid),
            "cp": lambda: CpTool(CpToolConfig(prompt=cfg.cp), ctx, sid),
            "edit": lambda: EditTool(EditToolConfig(prompt=cfg.edit), ctx, sid),
            "grep": lambda: GrepTool(GrepToolConfig(prompt=cfg.grep), ctx, sid),
            "ls": lambda: LsTool(LsToolConfig(prompt=cfg.ls), ctx, sid),
            "mkdir": lambda: MkdirTool(MkdirToolConfig(prompt=cfg.mkdir), ctx, sid),
            "mv": lambda: MvTool(MvToolConfig(prompt=cfg.mv), ctx, sid),
            "pwd": lambda: PwdTool(PwdToolConfig(prompt=cfg.pwd), ctx, sid),
            "rm": lambda: RmTool(RmToolConfig(prompt=cfg.rm), ctx, sid),
            "stat": lambda: StatTool(StatToolConfig(prompt=cfg.stat), ctx, sid),
            "touch": lambda: TouchTool(TouchToolConfig(prompt=cfg.touch), ctx, sid),
            "tree": lambda: TreeTool(TreeToolConfig(prompt=cfg.tree), ctx, sid),
            "write": lambda: WriteTool(WriteToolConfig(prompt=cfg.write), ctx, sid),
        }
        names = cls._select(cfg.tools, factories.keys())
        yield StaticToolSource(
            source_id=sid,
            tools=[factories[n]() for n in names],
        )

    @staticmethod
    def _select(
        allowlist: list[str] | None,
        all_names: Iterable[str],
    ) -> list[str]:
        """Применить allowlist к набору имён; None/пустой allowlist = все."""
        available = list(all_names)
        if not allowlist:
            return available
        unknown = [n for n in allowlist if n not in available]
        if unknown:
            msg = (
                f"FilesPlugin.tools: unknown names {unknown!r}, "
                f"available: {available!r}"
            )
            raise ValueError(msg)
        return [n for n in available if n in allowlist]
