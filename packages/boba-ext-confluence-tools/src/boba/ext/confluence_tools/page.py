"""Runtime-конфиг для page-tools (online Confluence).

Секция `[ext.confluence.page]`: connection (base_url + auth + timeout) +
`body_format`. Используется обоими tool'ами `confluence_page_outline` и
`confluence_page_section` через общий `ConfluencePageSection`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, OneOf, ParseString
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.confluence_tools.connection import ConfluenceConnection

__all__ = ["ConfluencePageConfig", "ConfluencePageSection"]


@dataclass(frozen=True)
class ConfluencePageConfig:
    """Runtime-конфиг подключения к Confluence для page-операций."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    body_format: str


class ConfluencePageSection(ConfigSection[ConfluencePageConfig]):
    """Секция [ext.confluence.page]: connection + body_format для page-tools."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "confluence", "page")

    schema: ClassVar[ObjectSchema[ConfluencePageConfig]] = ObjectSchema(
        description=(
            "чтение конкретной страницы Confluence (используется "
            "tool'ами confluence_page_outline / confluence_page_section)."
        ),
        fields=[
            *ConfluenceConnection.fields(),
            FieldSpec(
                name="body_format",
                coercer=ChainCoercer(
                    Default("view"),
                    ParseString(),
                    OneOf("view", "export_view", "storage"),
                ),
                description=(
                    "`view` — clean HTML (рекомендуется для tools); "
                    "`export_view` — с Confluence-макросами; "
                    "`storage` — raw storage XML."
                ),
            ),
        ],
        invariants=ConfluenceConnection.invariant(),
        factory=ConfluencePageConfig,
    )
