"""Описание endpoint'ов и единая политика их инструментов без доступа к БД.

Ошибки:
ValidationError — строка endpoint'а не соответствует формату каталога.
EndpointConfigError — пути или инструменты каталога противоречат друг другу.
"""

from __future__ import annotations

import re
import string
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.access import ToolAvailability, ToolPolicy
from boba.identity.signin import ProfileCatalog


class EndpointConfigError(Exception):
    """Описание endpoint'а нельзя опубликовать."""


class EndpointBuildError(EndpointConfigError):
    """Ошибка сборки с определением endpoint'а для сохранения прежней версии."""

    def __init__(self, endpoint: McpEndpoint, reason: str) -> None:
        super().__init__(reason)
        self.endpoint = endpoint


class PromptArgumentFile(BaseModel):
    """Секция [arguments.<имя>] файла промпта."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    required: bool


class PromptMessageFile(BaseModel):
    """Сообщение [[messages]] файла промпта: роль и текст с подстановкой
    аргументов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1)


class PromptFile(BaseModel):
    """Файл промпта целиком; аргументы в тексте сообщений — только
    объявленные."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    arguments: dict[str, PromptArgumentFile] = Field(default_factory=dict)
    messages: list[PromptMessageFile] = Field(min_length=1)

    @model_validator(mode="after")
    def _declared(self) -> PromptFile:
        formatter = string.Formatter()
        for message in self.messages:
            for _, field, spec, conversion in formatter.parse(message.text):
                if field is None:
                    continue

                if field not in self.arguments:
                    known = sorted(self.arguments)
                    msg = (
                        f"a message text names {{{field}}}, expected only the "
                        f"declared arguments {known}; write a brace as text doubled"
                    )
                    raise ValueError(msg)

                if spec or conversion:
                    msg = (
                        f"a message text formats {{{field}}} with "
                        f"{conversion or ''}{spec or ''}, expected a bare {{{field}}}"
                    )
                    raise ValueError(msg)

        return self


class McpEndpoint(BaseModel):
    """Строка каталога и её grants; репозиторий передаёт модель сборке MCP."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    prefix_url: str
    title: str = Field(min_length=1)
    description: str = ""
    instructions: str = ""
    enabled: bool = True
    tools: tuple[str, ...] = ()
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    prompts: dict[str, PromptFile] = Field(default_factory=dict)
    roles: frozenset[str] = frozenset()

    @field_validator("prefix_url")
    @classmethod
    def _path(cls, value: str) -> str:
        if value != "/" and re.fullmatch(r"(?:/[A-Za-z0-9_~.-]+)+", value) is None:
            raise ValueError("prefix_url expects / or a path such as /all or /dq/all")

        for segment in value.split("/")[1:]:
            if segment in (".", ".."):
                raise ValueError("prefix_url cannot contain . or .. segments")

        return value

    @field_validator("tools")
    @classmethod
    def _names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("tools must contain unique exported names")

        for name in value:
            if not name.strip():
                raise ValueError("tools cannot contain an empty name")

            if name == "*":
                raise ValueError("tools must name exact exported tools, not *")

        return value

    @model_validator(mode="after")
    def _overrides(self) -> McpEndpoint:
        unknown = self.overrides.keys() - set(self.tools)
        if unknown:
            raise ValueError(f"overrides names unselected tools: {sorted(unknown)}")

        return self

    def path_under(self, prefix: str) -> str:
        return prefix.rstrip("/") + self.prefix_url

    def available_to(self, roles: Iterable[str]) -> bool:
        if not self.enabled:
            return False

        return not self.roles.isdisjoint(roles)


class EndpointCatalog(ProfileCatalog, ToolPolicy):
    """Кеш endpoint'ов для входа OAuth и проверки вызовов без запросов к БД.

    Сборка MCP заменяет весь проверенный снимок при старте и reload. Ссылки
    потребителей на этот объект сохраняются. Роли пользователя подписаны
    в токене; reload обновляет grants endpoint'ов, а не членство пользователя.
    """

    def __init__(self, endpoints: Sequence[McpEndpoint]) -> None:
        self._endpoints: dict[str, McpEndpoint] = {}
        for endpoint in endpoints:
            path = endpoint.prefix_url
            if path in self._endpoints:
                raise EndpointConfigError(f"MCP catalog has duplicate route {path!r}")

            if endpoint.enabled:
                self._endpoints[path] = endpoint

    def replace(self, snapshot: EndpointCatalog) -> None:
        """Публикует подготовленный снимок одним присваиванием в event loop."""
        self._endpoints = dict(snapshot._endpoints)

    def endpoints(self) -> Sequence[McpEndpoint]:
        return tuple(self._endpoints.values())

    def resources(self, public_url: str) -> Mapping[str, str]:
        return {
            endpoint.prefix_url: endpoint.path_under(public_url)
            for endpoint in self._endpoints.values()
        }

    def granted_by_roles(self, user_roles: frozenset[str]) -> frozenset[str]:
        return frozenset(self._granted(user_roles))

    def _granted(self, roles: frozenset[str]) -> Iterator[str]:
        for endpoint in self._endpoints.values():
            if endpoint.available_to(roles):
                yield endpoint.prefix_url

    def known(self, name: str) -> bool:
        return name in self._endpoints

    def decide(
        self, tool_name: str, user_roles: Iterable[str], profile: str
    ) -> ToolAvailability:
        endpoint = self._endpoints.get(profile)
        if endpoint is None:
            return ToolAvailability.DENIED

        if not endpoint.available_to(user_roles):
            return ToolAvailability.DENIED

        if tool_name not in endpoint.tools:
            return ToolAvailability.DENIED

        return ToolAvailability.AVAILABLE
