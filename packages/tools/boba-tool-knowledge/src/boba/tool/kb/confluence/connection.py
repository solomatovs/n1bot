"""ConfluenceConnection — endpoint Confluence поверх web-профиля."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from boba.transport.http.profile import HttpConnection

__all__ = ["ConfluenceConnection"]


class ConfluenceConnection(BaseModel):
    """Confluence endpoint: body_format + транспортный профиль (адрес внутри)."""

    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description=(
            "`view` — clean HTML (рекомендуется); `export_view` — с макросами; "
            "`storage` — raw storage XML."
        ),
    )
    profile: HttpConnection = Field(
        description=(
            "Транспортный web-профиль (host/port/path/timeout/ssl/auth) ссылкой "
            '`profile = "${web.<name>}"`. Адрес Confluence задаётся в профиле; '
            "auth (PAT/Basic) — там же `auth = { method = 'bearer', token = '...' }`."
        ),
    )

