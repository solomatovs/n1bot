"""WebHostProfile: per-host настройки (только auth в первой итерации).

Профиль хранится как значение в `WebConnection.hosts: dict[host, profile]`.
Ключ словаря — точный hostname (без схемы/порта). Без профиля host
запрещён к запросу полностью — это и whitelist, и контракт авторизации.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.web.auth import WebAuth

__all__ = ["WebHostProfile"]


class WebHostProfile(BaseModel):
    """Per-host профиль: discriminated `auth` обязателен и без default'а."""

    model_config = ConfigDict(extra="forbid")

    auth: WebAuth = Field(
        description=(
            "Auth-метод для этого host'а: {method='none'|'basic'|'bearer'|'digest'}. "
            "Обязателен — даже anonymous должен быть указан явно как method='none'."
        ),
    )
