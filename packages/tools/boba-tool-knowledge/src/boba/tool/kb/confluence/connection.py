"""ConfluenceConnection — endpoint Confluence поверх web-профиля."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from boba.connections.http import HttpProfile

__all__ = ["ConfluenceConnection"]


class ConfluenceConnection(BaseModel):
    """Confluence endpoint: body_format + транспортный профиль (base_url внутри)."""

    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description=(
            "`view` — clean HTML (рекомендуется); `export_view` — с макросами; "
            "`storage` — raw storage XML."
        ),
    )
    profile: HttpProfile = Field(
        description=(
            "Транспортный web-профиль (base_url/timeout/ssl/auth) ссылкой "
            '`profile = "${web.<name>}"`. base_url Confluence задаётся в профиле; '
            "auth (PAT/Basic) — там же `auth = { method = 'bearer', token = '...' }`."
        ),
    )

    @property
    def base_url(self) -> str:
        if self.profile.base_url is None:
            msg = "confluence base_url is not set: define base_url in the web profile"
            raise ValueError(msg)

        return self.profile.base_url
