"""HtmlPlugin: единая точка регистрации HTML-tools."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import ClassVar

from pydantic import Field

from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList
from boba.tool.html.outline import HtmlOutlineTool, HtmlOutlineToolConfig
from boba.tool.html.section import HtmlSectionTool, HtmlSectionToolConfig
from boba.tools.domain import Tool, ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["HtmlPlugin", "HtmlPluginConfig"]


class HtmlPluginConfig(BobaFlatSettings):
    """
    HTML multi-tool plugin: outline + section.
    Без connection-полей — работает по workspace.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_TOOL__HTML__",
        boba_toml_section="tool.html",
    )

    enable: bool = Field(
        default=False,
        description="Подключить плагин в discovery.",
    )
    html_outline: PromptOverlay = Field(default_factory=PromptOverlay)
    html_section: PromptOverlay = Field(default_factory=PromptOverlay)
    tools: StringList | None = Field(
        default=None,
        description=(
            "Allowlist tool-имён внутри плагина: None/пустой = все, иначе "
            "только перечисленные ('html_outline', 'html_section')."
        ),
    )


class HtmlPlugin(Plugin[HtmlPluginConfig, ToolSource]):
    """Plugin HTML-tools: outline + section."""

    NAME: ClassVar[str] = "html"
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin.html")

    @classmethod
    def build(
        cls,
        cfg: HtmlPluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        sid = cls.SOURCE_ID
        factories: dict[str, Callable[[], Tool]] = {
            "html_outline": lambda: HtmlOutlineTool(
                HtmlOutlineToolConfig(prompt=cfg.html_outline), ctx, sid,
            ),
            "html_section": lambda: HtmlSectionTool(
                HtmlSectionToolConfig(prompt=cfg.html_section), ctx, sid,
            ),
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
                f"HtmlPlugin.tools: unknown names {unknown!r}, "
                f"available: {available!r}"
            )
            raise ValueError(msg)
        return [n for n in available if n in allowlist]
