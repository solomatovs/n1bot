"""ConfluencePlugin: единая точка регистрации Confluence-tools.

Конфиг плагина — плоский DTO с общими connection-полями на корне
(`base_url`, `auth_method`, `auth_user`, `auth_token`, `timeout_sec`,
`body_format`) и `PromptOverlay`-блоками — по одному на tool.

Регистрируемые tools:
- `confluence_search`                  — CQL-поиск по тексту.
- `confluence_page_outline`            — структура заголовков страницы.
- `confluence_page_section`            — текст одной секции по page_id+anchor.
- `confluence_page_download`           — скачать страницы как HTML.
- `confluence_page_download_markdown`  — скачать страницы как Markdown.

`build(cfg, ctx)` собирает Tool-DTO inline из общих полей и
соответствующего overlay'я, инстанцирует Tools, упаковывает в
`StaticToolSource`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList
from boba.tool.confluence.page_download import (
    ConfluencePageDownloadTool,
    ConfluencePageDownloadToolConfig,
)
from boba.tool.confluence.page_download_markdown import (
    ConfluencePageDownloadMarkdownTool,
    ConfluencePageDownloadMarkdownToolConfig,
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


class ConfluencePluginConfig(BobaFlatSettings):
    """Confluence multi-tool plugin: search + page_outline + page_section.

    Connection — на корне, описания — в overlay'ях.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_TOOL__CONFLUENCE__",
        boba_toml_section="tool.confluence",
    )

    enable: bool = Field(
        default=False,
        description="Подключить плагин в discovery.",
    )
    base_url: str = Field(
        default="",
        description="URL Confluence (обязателен при enable=True).",
    )
    auth_token: str = Field(
        default="",
        description="PAT или пароль (обязателен при enable=True).",
    )
    auth_method: Literal["pat", "basic"] = Field(
        default="pat",
        description="`pat` — Bearer-токен; `basic` — login+password.",
    )
    auth_user: str = Field(
        default="",
        description=(
            "Логин для basic-auth; обязателен при auth_method=basic. "
            "Для PAT — пусто."
        ),
    )
    timeout_sec: float = Field(
        default=30.0,
        description="HTTP-таймаут (сек).",
    )
    ssl_verify: bool = Field(
        default=False,
        description="Проверять ли TLS-сертификат.",
    )
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description=(
            "`view` — clean HTML (рекомендуется); `export_view` — с макросами; "
            "`storage` — raw storage XML."
        ),
    )
    confluence_search: PromptOverlay = Field(default_factory=PromptOverlay)
    confluence_page_outline: PromptOverlay = Field(default_factory=PromptOverlay)
    confluence_page_section: PromptOverlay = Field(default_factory=PromptOverlay)
    confluence_page_download: PromptOverlay = Field(default_factory=PromptOverlay)
    confluence_page_download_markdown: PromptOverlay = Field(
        default_factory=PromptOverlay,
    )
    tools: StringList | None = Field(
        default=None,
        description=(
            "Allowlist tool-имён внутри плагина: None/пустой = все, иначе "
            "только перечисленные ('confluence_search', "
            "'confluence_page_outline', 'confluence_page_section', "
            "'confluence_page_download', 'confluence_page_download_markdown')."
        ),
    )

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if not self.enable:
            return self
        if not self.base_url:
            msg = "base_url обязателен при enable=True"
            raise ValueError(msg)
        if not self.auth_token:
            msg = "auth_token обязателен при enable=True"
            raise ValueError(msg)
        if self.auth_method == "basic" and not self.auth_user:
            msg = "auth_user обязателен при auth_method='basic'"
            raise ValueError(msg)
        return self


class ConfluencePlugin(Plugin[ConfluencePluginConfig, ToolSource]):
    """Plugin Confluence-tools: search + page_outline + page_section."""

    NAME: ClassVar[str] = "confluence"
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin_confluence")

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
                    ssl_verify=cfg.ssl_verify,
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
                    ssl_verify=cfg.ssl_verify,
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
                    ssl_verify=cfg.ssl_verify,
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
                    ssl_verify=cfg.ssl_verify,
                    body_format=cfg.body_format,
                    prompt=cfg.confluence_page_download,
                ),
                ctx,
                sid,
            ),
            "confluence_page_download_markdown": (
                lambda: ConfluencePageDownloadMarkdownTool(
                    ConfluencePageDownloadMarkdownToolConfig(
                        base_url=cfg.base_url,
                        auth_method=cfg.auth_method,
                        auth_user=cfg.auth_user,
                        auth_token=cfg.auth_token,
                        timeout_sec=cfg.timeout_sec,
                        ssl_verify=cfg.ssl_verify,
                        body_format=cfg.body_format,
                        prompt=cfg.confluence_page_download_markdown,
                    ),
                    ctx,
                    sid,
                )
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
