"""FilesPlugin: единая точка регистрации файловых tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, MinValue, ParseInt
from boba.declaration import FieldSpec, NestedField, ObjectSchema
from boba.ext.files_tools.append import AppendTool, AppendToolConfig
from boba.ext.files_tools.cat import CatTool, CatToolConfig
from boba.ext.files_tools.cd import CdTool, CdToolConfig
from boba.ext.files_tools.cp import CpTool, CpToolConfig
from boba.ext.files_tools.edit import EditTool, EditToolConfig
from boba.ext.files_tools.grep import GrepTool, GrepToolConfig
from boba.ext.files_tools.ls import LsTool, LsToolConfig
from boba.ext.files_tools.mkdir import MkdirTool, MkdirToolConfig
from boba.ext.files_tools.mv import MvTool, MvToolConfig
from boba.ext.files_tools.pwd import PwdTool, PwdToolConfig
from boba.ext.files_tools.rm import RmTool, RmToolConfig
from boba.ext.files_tools.stat import StatTool, StatToolConfig
from boba.ext.files_tools.touch import TouchTool, TouchToolConfig
from boba.ext.files_tools.tree import TreeTool, TreeToolConfig
from boba.ext.files_tools.write import WriteTool, WriteToolConfig
from boba.patterns import StrId
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay, prompt_field
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


class FilesPlugin:
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
        cls, cfg: FilesPluginConfig, ctx: ExtensionContext,
    ) -> ToolSource:
        return StaticToolSource(
            source_id=cls.SOURCE_ID,
            priority=0,
            tools=[
                AppendTool(AppendToolConfig(prompt=cfg.append), ctx),
                CatTool(cfg.cat, ctx),
                CdTool(CdToolConfig(prompt=cfg.cd), ctx),
                CpTool(CpToolConfig(prompt=cfg.cp), ctx),
                EditTool(EditToolConfig(prompt=cfg.edit), ctx),
                GrepTool(GrepToolConfig(prompt=cfg.grep), ctx),
                LsTool(LsToolConfig(prompt=cfg.ls), ctx),
                MkdirTool(MkdirToolConfig(prompt=cfg.mkdir), ctx),
                MvTool(MvToolConfig(prompt=cfg.mv), ctx),
                PwdTool(PwdToolConfig(prompt=cfg.pwd), ctx),
                RmTool(RmToolConfig(prompt=cfg.rm), ctx),
                StatTool(StatToolConfig(prompt=cfg.stat), ctx),
                TouchTool(TouchToolConfig(prompt=cfg.touch), ctx),
                TreeTool(TreeToolConfig(prompt=cfg.tree), ctx),
                WriteTool(WriteToolConfig(prompt=cfg.write), ctx),
            ],
        )
