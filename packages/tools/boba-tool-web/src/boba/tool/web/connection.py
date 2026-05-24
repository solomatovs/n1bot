"""WebConnection: shared TOML-секция с whitelist'ом хостов + HTTP-настройками.

`BaseModel` (не settings), без `config_path` — встраивается как nested-поле
в tool-конфиги (`WebFetchConfig`, `WebDownloadConfig`). `BobaFlatSettings`
рекурсивно поднимает её листовые поля на уровень корневой TOML-секции
tool'а, поэтому оператор пишет всё одной плоской секцией `[web]`.

`hosts` — это **одновременно** ACL и реестр auth-профилей: host, которого
нет в этом словаре, запрещён к запросу полностью. Пустой `hosts` валидируется
как ошибка — иначе оператор случайно может оставить tool с пустым whitelist'ом.

`make_transport()` — фабрика `HttpTransport`. httpx.Client'ы Transport
открывает сам, здесь только параметры.
"""

from __future__ import annotations

from typing import Self
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator

from boba.tool.web.host_profile import WebHostProfile
from boba.transport.http import HttpTransport

__all__ = ["WebConnection"]


class WebConnection(BaseModel):
    """Shared конфиг web-tools: hosts-ACL + HTTP-параметры + cache_dir."""

    timeout_sec: float = Field(
        default=30.0,
        gt=0,
        description="HTTP-таймаут (сек).",
    )
    ssl_verify: bool = Field(
        default=True,
        description="Проверять ли TLS-сертификат.",
    )
    hosts: dict[str, WebHostProfile] = Field(
        description=(
            "ACL+auth: ключ — точный hostname (без схемы/порта), значение — "
            "профиль с обязательным полем `auth`. Hostname не в этом словаре "
            "→ запрос запрещён."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.hosts:
            msg = (
                "web.hosts: список пустой. Tool бесполезен без хотя бы одного "
                "разрешённого host'а. Добавьте секцию "
                "[web.hosts.\"<hostname>\".auth] с method=..."
            )
            raise ValueError(msg)
        return self

    def resolve(self, url: str) -> WebHostProfile:
        """Hostname URL → профиль; ValueError если не в whitelist."""
        host = (urlparse(url).hostname or "").lower()
        profile = self.hosts.get(host)
        if profile is None:
            allowed = sorted(self.hosts)
            msg = (
                f"web: host {host!r} не в whitelist'е (allowed={allowed}). "
                f"URL={url!r}"
            )
            raise ValueError(msg)
        return profile

    def make_transport(self) -> HttpTransport:
        return HttpTransport(
            timeout_sec=self.timeout_sec,
            verify=self.ssl_verify,
        )
