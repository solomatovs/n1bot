"""HtmlPlugin: единая точка регистрации HTML-tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.declaration import ObjectSchema
from boba.ext.html_tools.outline import HtmlOutlineTool, HtmlOutlineToolConfig
from boba.ext.html_tools.section import HtmlSectionTool, HtmlSectionToolConfig
from boba.patterns import StrId
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay, prompt_field
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["HtmlPlugin", "HtmlPluginConfig"]


@dataclass(frozen=True)
class HtmlPluginConfig:
    """Плоский DTO плагина: только per-tool PromptOverlay (workspace-tools без connection)."""

    html_outline: PromptOverlay
    html_section: PromptOverlay


class HtmlPlugin:
    """Plugin HTML-tools: outline + section."""

    NAME: ClassVar[StrId] = StrId("html")
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin.html")

    @classmethod
    def config(cls) -> ObjectSchema[HtmlPluginConfig]:
        return ObjectSchema(
            description=(
                "HTML multi-tool plugin: outline + section. "
                "Без connection-полей — работает по workspace."
            ),
            fields=[
                prompt_field("html_outline"),
                prompt_field("html_section"),
            ],
            factory=HtmlPluginConfig,
        )

    @classmethod
    def build(cls, cfg: HtmlPluginConfig, ctx: ExtensionContext) -> ToolSource:
        return StaticToolSource(
            source_id=cls.SOURCE_ID,
            priority=0,
            tools=[
                HtmlOutlineTool(
                    HtmlOutlineToolConfig(prompt=cfg.html_outline), ctx,
                ),
                HtmlSectionTool(
                    HtmlSectionToolConfig(prompt=cfg.html_section), ctx,
                ),
            ],
        )
