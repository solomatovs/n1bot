"""
WebHostProfile: host профиль
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.tool.web.auth import WebAuth

__all__ = ["WebHostProfile"]


class WebHostProfile(BaseModel):
    """Per-host профиль: hostname + discriminated `auth`."""

    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(
        min_length=1,
        description=(
            "Hostname (без схемы/порта), которому принадлежит профиль. "
            "Нормализуется в lower-case. Tool ищет первый профиль "
            "в `[tool.web].profiles` с матчащим hostname URL'а."
        ),
    )
    auth: WebAuth = Field(
        description=(
            "Auth-метод: {method='none'|'basic'|'bearer'|'digest'}. "
            "Обязателен — даже anonymous должен быть указан явно как "
            "method='none'."
        ),
    )

    @field_validator("hostname")
    @classmethod
    def _lowercase(cls, v: str) -> str:
        return v.lower()
