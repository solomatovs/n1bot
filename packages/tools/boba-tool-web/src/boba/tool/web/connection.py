"""WebConnection: whitelist соединений web-инструментов по имени.

Соединение выбирается tool-arg'ом connection_name, URL запроса обязан попасть
под хост base_url соединения (точный или шаблон `*.domain`); профиль уезжает
в запрос уже с конкретным хостом.

Ошибки:
UnknownConnectionError — имя соединения вне whitelist'а; текст готов
    для пользователя.
UnknownHostError — хост URL не покрыт соединением; текст готов для пользователя.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.result import TableResult
from boba.toolkit.sql import UnknownConnectionError
from boba.transport.http import HostPattern, HttpProfile

__all__ = ["UnknownHostError", "WebConnection"]


class UnknownHostError(Exception):
    """Хост URL не покрыт выбранным соединением; текст готов для пользователя."""


class WebConnection(BaseModel):
    """Whitelist web-соединений: connection_name -> HttpProfile."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str]
    """Секция конфига инструмента; подкласс обязан задать."""

    profiles: dict[str, HttpProfile] = Field(
        default_factory=dict,
        description=(
            "dict[connection_name, web-профиль]. Ключ — значение tool-arg "
            "`connection_name`. Приложение собирает whitelist из соединений "
            "пользователя на каждый вызов."
        ),
    )
    names: list[str] = Field(
        default_factory=list,
        description=(
            "Имена соединений, доступных пользователю, без профилей: видны в "
            "connection_list, а профиль приезжает только у выбранного соединения."
        ),
    )
    hosts: dict[str, str] = Field(
        default_factory=dict,
        description="dict[connection_name, хост base_url]: что покрывает соединение.",
    )

    def targets(self) -> list[str]:
        known = set(self.names)
        known.update(self.profiles)
        return sorted(known)

    def targets_table(self) -> TableResult:
        """Выдача connection_list: имя соединения и хост (или шаблон) под ним."""
        rows: list[dict[str, Any]] = []
        for target in self.targets():
            rows.append({"connection_name": target, "host": self._host_of(target)})

        return TableResult(rows=rows)

    def _host_of(self, target: str) -> str:
        profile = self.profiles.get(target)
        if profile is not None:
            return profile.host()

        return self.hosts.get(target, "")

    def resolve(self, connection_name: str) -> HttpProfile:
        profile = self.profiles.get(connection_name)
        if profile is None:
            msg = (
                f"{type(self).SECTION}: connection_name {connection_name!r} is not "
                f"in the whitelist (allowed={self.targets()})"
            )
            raise UnknownConnectionError(msg)

        return profile

    def resolve_for(self, connection_name: str, url: str) -> HttpProfile:
        """Профиль соединения, привязанный к хосту URL; чужой хост — отказ."""
        profile = self.resolve(connection_name)
        host = HostPattern.host_of(url)

        if not profile.covers(host):
            msg = (
                f"web: host {host!r} is outside connection {connection_name!r} "
                f"(covers {profile.host()!r}). URL={url!r}"
            )
            raise UnknownHostError(msg)

        return profile.bound_to(host)
