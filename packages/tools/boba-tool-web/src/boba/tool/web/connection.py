"""WebConnection: tool-level HTTP + whitelist профилей."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator

from boba.settings.source import TomlEnvConfigSource
from boba.tool.web.host_profile import WebHostProfile
from boba.transport.http import HttpTransport

__all__ = ["WebConnection"]


class WebConnection(BaseModel):
    """HTTP-настройки + profiles → hosts (resolved)."""

    timeout_sec: float = Field(
        default=30.0,
        gt=0,
        description="HTTP-таймаут (сек).",
    )
    ssl_verify: bool = Field(
        default=True,
        description="Проверять ли TLS-сертификат.",
    )
    profiles: list[str] = Field(
        default_factory=list,
        description=(
            "Whitelist имён shared-секций `[web.<name>]` (hostname + auth). "
            "Tool по hostname URL'а ищет первый матчащий профиль в этом "
            "списке; не найден — запрос запрещён."
        ),
    )
    hosts: dict[str, WebHostProfile] = Field(
        default_factory=dict,
        description=(
            "Computed: profiles → dict[hostname, WebHostProfile]; "
            "заполняется `_resolve_profiles`. Оператор это поле не задаёт."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _resolve_profiles(cls, values: Any) -> Any:
        """profiles[name] → hosts[hostname] через [web.<name>]."""
        if not isinstance(values, Mapping):
            return values
        profiles = values.get("profiles") or []
        if not profiles:
            return values

        toml_source = TomlEnvConfigSource()
        hosts: dict[str, dict[str, Any]] = {}
        for name in profiles:
            name_s = str(name)
            shared = toml_source.for_path(("web", name_s))
            if not shared:
                msg = (
                    f"tool.web.profiles: профиль {name_s!r} — "
                    f"секция [web.{name_s}] не найдена или пуста"
                )
                raise ValueError(msg)
            hostname = str(shared.get("hostname") or "").lower()
            if not hostname:
                msg = (
                    f"tool.web.profiles: профиль {name_s!r} — "
                    f"в [web.{name_s}] отсутствует hostname"
                )
                raise ValueError(msg)
            if hostname in hosts:
                msg = (
                    f"tool.web.profiles: hostname {hostname!r} встречается "
                    f"в нескольких профилях — оставьте один на hostname"
                )
                raise ValueError(msg)
            hosts[hostname] = dict(shared)

        new_values = dict(values)
        new_values["hosts"] = hosts
        return new_values

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.hosts:
            msg = (
                "tool.web: ни одного профиля. Заведите [web.<name>] и "
                "пропишите имя в [tool.web].profiles = [...]."
            )
            raise ValueError(msg)
        return self

    def resolve(self, url: str) -> WebHostProfile:
        """hostname URL → профиль; ValueError если не в whitelist."""
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
