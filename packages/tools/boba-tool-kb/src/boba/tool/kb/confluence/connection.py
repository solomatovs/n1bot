"""ConfluenceConnection — endpoint Confluence поверх web-профиля.

Composition: body_format (Confluence-специфика) + profile (HttpProfile —
общий транспортный профиль с base_url/timeout/ssl/auth, тот же, что у
web-tool'ов). base_url теперь живёт внутри профиля (web.<name>.base_url) —
отдельной [confluence]-секции нет; конфиги ссылаются на профиль напрямую
`profile = "${web.<name>}"` и дублируют body_format у себя.

Чистая модель данных: Confluence-код берёт profile напрямую —
HttpTransport(conn.profile) единая точка исполнения HTTP (auth/timeout/ssl
берутся из профиля), base_url — из conn.base_url (свойство над profile).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from boba.transport.http import HttpProfile

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
        """base_url из web-профиля; ошибка, если в профиле он не задан."""
        if self.profile.base_url is None:
            msg = "confluence base_url не задан: укажи base_url в web-профиле"
            raise ValueError(msg)

        return self.profile.base_url
