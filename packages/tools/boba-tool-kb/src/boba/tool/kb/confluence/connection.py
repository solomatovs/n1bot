"""`ConfluenceConnection` — endpoint Confluence поверх web-профиля.

Composition: `base_url` + `body_format` (Confluence-специфика) + `profile`
(`HttpConnection` — общий транспортный профиль с timeout/ssl/auth, тот же,
что у web-tool'ов). Подключается ссылкой `profile = "${web.<name>}"`.

Чистая модель данных: Confluence-код берёт `profile` напрямую —
`HttpTransport(conn.profile)` для pipeline и `conn.profile.auth.httpx_auth()`
для авторизации REST-запросов к `base_url`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from boba.transport.http import HttpConnection

__all__ = ["ConfluenceConnection"]


class ConfluenceConnection(BaseModel):
    """Confluence endpoint: base_url + body_format + транспортный профиль."""

    base_url: str = Field(
        min_length=1,
        description="URL Confluence.",
    )
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description=(
            "`view` — clean HTML (рекомендуется); `export_view` — с макросами; "
            "`storage` — raw storage XML."
        ),
    )
    profile: HttpConnection = Field(
        default_factory=HttpConnection,
        description=(
            "Транспортный web-профиль (timeout/ssl/auth) ссылкой "
            '`profile = "${web.<name>}"`. Auth Confluence (PAT/Basic) задаётся '
            "в профиле как `auth = { method = 'bearer', token = '...' }`."
        ),
    )
