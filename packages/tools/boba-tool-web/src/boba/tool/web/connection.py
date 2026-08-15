"""WebConnection: whitelist хостов — dict[hostname -> HttpConnection-профиль]."""

from __future__ import annotations

from typing import Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.transport.http import HttpProfile

__all__ = ["UnknownHostError", "WebConnection"]


class UnknownHostError(Exception):
    """Хост URL не значится в whitelist'е; текст готов для пользователя."""


class WebConnection(BaseModel):
    """Whitelist хостов: hostname -> HttpProfile; хост вне списка — запрос запрещён."""

    model_config = ConfigDict(extra="ignore")

    profiles: dict[str, HttpProfile] = Field(
        default_factory=dict,
        description=(
            "dict[hostname, web-профиль ссылкой]. Ключ — hostname (по нему "
            "resolve выбирает профиль для входящего URL)."
        ),
    )

    @field_validator("profiles", mode="before")
    @classmethod
    def _lower_keys(cls, v: object) -> object:
        if isinstance(v, dict):
            return {str(k).lower(): val for k, val in v.items()}
        return v

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.profiles:
            msg = (
                "tool.web: empty whitelist, set profiles = "
                '{ "<hostname>" = "${web.<name>}" }.'
            )
            raise ValueError(msg)
        return self

    def resolve_profile(self, url: str) -> HttpProfile:
        host = (urlparse(url).hostname or "").lower()
        profile = self.profiles.get(host)
        if profile is None:
            allowed = sorted(self.profiles)
            msg = (
                f"web: host {host!r} is not in the whitelist "
                f"(allowed={allowed}). URL={url!r}"
            )
            raise UnknownHostError(msg)
        return profile
