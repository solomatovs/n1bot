"""HtmlPlugin: единая точка регистрации HTML-tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from boba.patterns import StrId
from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay, prompt_field
from boba.schema.declaration import ObjectSchema
from boba.tool.html.outline import HtmlOutlineTool, HtmlOutlineToolConfig
from boba.tool.html.section import HtmlSectionTool, HtmlSectionToolConfig
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["HtmlPlugin", "HtmlPluginConfig"]


@dataclass(frozen=True)
class HtmlPluginConfig:
    """Плоский DTO плагина: только per-tool PromptOverlay (workspace-tools без connection)."""

    html_outline: PromptOverlay
    html_section: PromptOverlay


class HtmlPlugin(Plugin[HtmlPluginConfig, ToolSource]):
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
    def build(
        cls,
        cfg: HtmlPluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        sid = cls.SOURCE_ID
        yield StaticToolSource(
            source_id=sid,
            tools=[
                HtmlOutlineTool(
                    HtmlOutlineToolConfig(prompt=cfg.html_outline), ctx, sid,
                ),
                HtmlSectionTool(
                    HtmlSectionToolConfig(prompt=cfg.html_section), ctx, sid,
                ),
            ],
        )
