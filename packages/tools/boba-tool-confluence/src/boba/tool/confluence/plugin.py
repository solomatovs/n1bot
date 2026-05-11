"""ConfluencePlugin: единая точка регистрации Confluence-tools.

Конфиг плагина — плоский DTO с общими connection-полями на корне
(`base_url`, `auth_method`, `auth_user`, `auth_token`, `timeout_sec`,
`body_format`) и `PromptOverlay`-блоками — по одному на tool.

Регистрируемые tools:
- `confluence_search`        — CQL-поиск по тексту.
- `confluence_page_outline`  — структура заголовков страницы по page_id.
- `confluence_page_section`  — текст одной секции по page_id + anchor.
- `confluence_page_download` — скачать страницы как REST-JSON в workspace.

`build(cfg, ctx)` собирает Tool-DTO inline из общих полей и
соответствующего overlay'я, инстанцирует Tools, упаковывает в
`StaticToolSource`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Literal

from boba.patterns import StrId
from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay
from boba.schema import schema
from boba.schema.coercion import ParseFloat, ParseString
from boba.schema.coercion.types import ParseCsvList
from boba.tool.confluence.connection import ConfluenceConnection
from boba.tool.confluence.page_download import (
    ConfluencePageDownloadTool,
    ConfluencePageDownloadToolConfig,
)
from boba.tool.confluence.page_outline import (
    ConfluencePageOutlineTool,
    ConfluencePageOutlineToolConfig,
)
from boba.tool.confluence.page_section import (
    ConfluencePageSectionTool,
    ConfluencePageSectionToolConfig,
)
from boba.tool.confluence.search import (
    ConfluenceSearchTool,
    ConfluenceSearchToolConfig,
)
from boba.tools.domain import Tool, ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["ConfluencePlugin", "ConfluencePluginConfig"]


@schema(invariants=ConfluenceConnection.invariant())
@dataclass(frozen=True)
class ConfluencePluginConfig:
    """
    Confluence multi-tool plugin: search + page_outline + page_section.
    Connection — на корне, описания — в overlay'ях
    """

    base_url: Annotated[str, "URL Confluence (env: ...__BASE_URL).", ParseString()]
    auth_token: Annotated[
        str,
        "PAT или пароль (env: ...__AUTH_TOKEN).",
        ParseString(),
    ]
    auth_method: Annotated[
        Literal["pat", "basic"],
        "`pat` — Bearer-токен; `basic` — login+password.",
    ] = "pat"
    auth_user: Annotated[
        str,
        "Логин для basic-auth; для PAT — пусто.",
        ParseString(),
    ] = ""
    timeout_sec: Annotated[
        float,
        "HTTP-таймаут (сек).",
        ParseFloat(),
    ] = 30.0
    body_format: Annotated[
        Literal["view", "export_view", "storage"],
        "`view` — clean HTML (рекомендуется); `export_view` — с макросами;"
        "`storage` — raw storage XML.",
    ] = "view"
    confluence_search: PromptOverlay = field(default_factory=PromptOverlay)
    confluence_page_outline: PromptOverlay = field(default_factory=PromptOverlay)
    confluence_page_section: PromptOverlay = field(default_factory=PromptOverlay)
    confluence_page_download: PromptOverlay = field(default_factory=PromptOverlay)
    tools: Annotated[
        list[str] | None,
        "Allowlist tool-имён внутри плагина: None/пустой = все, иначе только "
        "перечисленные ('confluence_search', 'confluence_page_outline', "
        "'confluence_page_section', 'confluence_page_download').",
        ParseCsvList(),
    ] = None


class ConfluencePlugin(Plugin[ConfluencePluginConfig, ToolSource]):
    """Plugin Confluence-tools: search + page_outline + page_section."""

    NAME: ClassVar[StrId] = StrId("confluence")
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin.confluence")

    @classmethod
    def build(
        cls,
        cfg: ConfluencePluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        sid = cls.SOURCE_ID
        factories: dict[str, Callable[[], Tool]] = {
            "confluence_search": lambda: ConfluenceSearchTool(
                ConfluenceSearchToolConfig(
                    base_url=cfg.base_url,
                    auth_method=cfg.auth_method,
                    auth_user=cfg.auth_user,
                    auth_token=cfg.auth_token,
                    timeout_sec=cfg.timeout_sec,
                    prompt=cfg.confluence_search,
                ),
                ctx,
                sid,
            ),
            "confluence_page_outline": lambda: ConfluencePageOutlineTool(
                ConfluencePageOutlineToolConfig(
                    base_url=cfg.base_url,
                    auth_method=cfg.auth_method,
                    auth_user=cfg.auth_user,
                    auth_token=cfg.auth_token,
                    timeout_sec=cfg.timeout_sec,
                    body_format=cfg.body_format,
                    prompt=cfg.confluence_page_outline,
                ),
                ctx,
                sid,
            ),
            "confluence_page_section": lambda: ConfluencePageSectionTool(
                ConfluencePageSectionToolConfig(
                    base_url=cfg.base_url,
                    auth_method=cfg.auth_method,
                    auth_user=cfg.auth_user,
                    auth_token=cfg.auth_token,
                    timeout_sec=cfg.timeout_sec,
                    body_format=cfg.body_format,
                    prompt=cfg.confluence_page_section,
                ),
                ctx,
                sid,
            ),
            "confluence_page_download": lambda: ConfluencePageDownloadTool(
                ConfluencePageDownloadToolConfig(
                    base_url=cfg.base_url,
                    auth_method=cfg.auth_method,
                    auth_user=cfg.auth_user,
                    auth_token=cfg.auth_token,
                    timeout_sec=cfg.timeout_sec,
                    body_format=cfg.body_format,
                    prompt=cfg.confluence_page_download,
                ),
                ctx,
                sid,
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
                f"ConfluencePlugin.tools: unknown names {unknown!r}, "
                f"available: {available!r}"
            )
            raise ValueError(msg)
        return [n for n in available if n in allowlist]
