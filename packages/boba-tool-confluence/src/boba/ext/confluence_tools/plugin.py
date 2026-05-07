"""ConfluencePlugin: единая точка регистрации Confluence-tools.

Конфиг плагина — плоская `ObjectSchema` с общими connection-полями на
корне (`base_url`, `auth_method`, `auth_user`, `auth_token`, `timeout_sec`,
`body_format`) и тремя `PromptOverlay`-блоками (по одному на tool).

`build(cfg, ctx)` собирает Tool-DTO inline из общих полей и
соответствующего overlay'я, инстанцирует Tools, упаковывает в
`StaticToolSource`. Tool-схемы для materializer'а как отдельные сущности
не нужны — Tool обходится своим dataclass-DTO.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, OneOf, ParseFloat, ParseString, Required
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.confluence_tools.connection import ConfluenceConnection
from boba.ext.confluence_tools.page_outline import (
    ConfluencePageOutlineTool,
    ConfluencePageOutlineToolConfig,
)
from boba.ext.confluence_tools.page_section import (
    ConfluencePageSectionTool,
    ConfluencePageSectionToolConfig,
)
from boba.ext.confluence_tools.search import (
    ConfluenceSearchTool,
    ConfluenceSearchToolConfig,
)
from boba.patterns import StrId
from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay, prompt_field
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["ConfluencePlugin", "ConfluencePluginConfig"]


@dataclass(frozen=True)
class ConfluencePluginConfig:
    """Плоский DTO плагина: connection-поля + per-tool PromptOverlay."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    body_format: str
    confluence_search: PromptOverlay
    confluence_page_outline: PromptOverlay
    confluence_page_section: PromptOverlay


class ConfluencePlugin(Plugin[ConfluencePluginConfig, ToolSource]):
    """Plugin Confluence-tools: search + page_outline + page_section."""

    NAME: ClassVar[StrId] = StrId("confluence")
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin.confluence")

    @classmethod
    def config(cls) -> ObjectSchema[ConfluencePluginConfig]:
        return ObjectSchema(
            description=(
                "Confluence multi-tool plugin: search + page_outline + "
                "page_section. Connection — на корне, описания — в overlay'ях."
            ),
            fields=[
                FieldSpec(
                    name="base_url",
                    coercer=ChainCoercer(Required(), ParseString()),
                    description="URL Confluence (env: ...__BASE_URL).",
                ),
                FieldSpec(
                    name="auth_method",
                    coercer=ChainCoercer(
                        Default("pat"),
                        ParseString(),
                        OneOf("pat", "basic"),
                    ),
                    description="`pat` — Bearer-токен; `basic` — login+password.",
                ),
                FieldSpec(
                    name="auth_user",
                    coercer=ChainCoercer(Default(""), ParseString()),
                    description="Логин для basic-auth; для PAT — пусто.",
                ),
                FieldSpec(
                    name="auth_token",
                    coercer=ChainCoercer(Required(), ParseString()),
                    description="PAT или пароль (env: ...__AUTH_TOKEN).",
                ),
                FieldSpec(
                    name="timeout_sec",
                    coercer=ChainCoercer(Default(30.0), ParseFloat()),
                    description="HTTP-таймаут (сек).",
                ),
                FieldSpec(
                    name="body_format",
                    coercer=ChainCoercer(
                        Default("view"),
                        ParseString(),
                        OneOf("view", "export_view", "storage"),
                    ),
                    description=(
                        "`view` — clean HTML (рекомендуется); "
                        "`export_view` — с макросами; "
                        "`storage` — raw storage XML."
                    ),
                ),
                prompt_field("confluence_search"),
                prompt_field("confluence_page_outline"),
                prompt_field("confluence_page_section"),
            ],
            invariants=ConfluenceConnection.invariant(),
            factory=ConfluencePluginConfig,
        )

    @classmethod
    def build(
        cls,
        cfg: ConfluencePluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        sid = cls.SOURCE_ID
        yield StaticToolSource(
            source_id=sid,
            tools=[
                ConfluenceSearchTool(
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
                ConfluencePageOutlineTool(
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
                ConfluencePageSectionTool(
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
            ],
        )
