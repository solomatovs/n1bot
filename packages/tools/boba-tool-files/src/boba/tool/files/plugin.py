"""FilesPlugin: единая точка регистрации файловых tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from boba.patterns import StrId
from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay, prompt_field
from boba.schema.coercion import ChainCoercer, Default, MinValue, ParseInt
from boba.schema.declaration import FieldSpec, NestedField, ObjectSchema
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
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["FilesPlugin", "FilesPluginConfig"]


@dataclass(frozen=True)
class FilesPluginConfig:
    """Плоский DTO плагина: per-tool prompt overlay'и + nested cat (с max_lines)."""

    cat: CatToolConfig
    append: PromptOverlay
    cd: PromptOverlay
    cp: PromptOverlay
    edit: PromptOverlay
    grep: PromptOverlay
    ls: PromptOverlay
    mkdir: PromptOverlay
    mv: PromptOverlay
    pwd: PromptOverlay
    rm: PromptOverlay
    stat: PromptOverlay
    touch: PromptOverlay
    tree: PromptOverlay
    write: PromptOverlay


_CAT_SCHEMA: ObjectSchema[CatToolConfig] = ObjectSchema(
    description="Конфиг tool 'cat': лимит max_lines + prompt overlay.",
    fields=[
        FieldSpec(
            name="max_lines",
            coercer=ChainCoercer(Default(2000), ParseInt(), MinValue(1)),
            description="Максимум строк в одном вызове cat.",
        ),
        prompt_field("prompt"),
    ],
    factory=CatToolConfig,
)


class FilesPlugin(Plugin[FilesPluginConfig, ToolSource]):
    """Plugin файловых tools: 15 tools, без connection."""

    NAME: ClassVar[StrId] = StrId("files")
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin.files")

    @classmethod
    def config(cls) -> ObjectSchema[FilesPluginConfig]:
        return ObjectSchema(
            description=(
                "Builtin file-system tools: cat/ls/grep/edit/write/append/"
                "cp/mv/rm/mkdir/touch/cd/pwd/stat/tree."
            ),
            fields=[
                NestedField(name="cat", schema=_CAT_SCHEMA),
                prompt_field("append"),
                prompt_field("cd"),
                prompt_field("cp"),
                prompt_field("edit"),
                prompt_field("grep"),
                prompt_field("ls"),
                prompt_field("mkdir"),
                prompt_field("mv"),
                prompt_field("pwd"),
                prompt_field("rm"),
                prompt_field("stat"),
                prompt_field("touch"),
                prompt_field("tree"),
                prompt_field("write"),
            ],
            factory=FilesPluginConfig,
        )

    @classmethod
    def build(
        cls,
        cfg: FilesPluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        sid = cls.SOURCE_ID
        yield StaticToolSource(
            source_id=sid,
            tools=[
                AppendTool(AppendToolConfig(prompt=cfg.append), ctx, sid),
                CatTool(cfg.cat, ctx, sid),
                CdTool(CdToolConfig(prompt=cfg.cd), ctx, sid),
                CpTool(CpToolConfig(prompt=cfg.cp), ctx, sid),
                EditTool(EditToolConfig(prompt=cfg.edit), ctx, sid),
                GrepTool(GrepToolConfig(prompt=cfg.grep), ctx, sid),
                LsTool(LsToolConfig(prompt=cfg.ls), ctx, sid),
                MkdirTool(MkdirToolConfig(prompt=cfg.mkdir), ctx, sid),
                MvTool(MvToolConfig(prompt=cfg.mv), ctx, sid),
                PwdTool(PwdToolConfig(prompt=cfg.pwd), ctx, sid),
                RmTool(RmToolConfig(prompt=cfg.rm), ctx, sid),
                StatTool(StatToolConfig(prompt=cfg.stat), ctx, sid),
                TouchTool(TouchToolConfig(prompt=cfg.touch), ctx, sid),
                TreeTool(TreeToolConfig(prompt=cfg.tree), ctx, sid),
                WriteTool(WriteToolConfig(prompt=cfg.write), ctx, sid),
            ],
        )
