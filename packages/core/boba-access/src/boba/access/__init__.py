"""Доступ к инструментам: гранты ролей и профилей, одно решение о доступности.

Инструмент доступен субъекту, когда профиль виден его ролям, профиль
разрешает инструмент и хотя бы одна роль его разрешает. Инструменты чата
(chat_only) доступны только внутри хода чата: решение отдаёт CHAT_ONLY,
а вызывающий сам знает, есть ли у него чат. Deny by default.

Ошибки:
ToolAccessError — гранты ссылаются на инструменты, которых нет.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.types import StringList

__all__ = [
    "AccessSubject",
    "ProfileGrant",
    "ToolAccess",
    "ToolAccessError",
    "ToolAvailability",
    "ToolGrant",
]


class ToolAvailability(StrEnum):
    """Решение о доступе субъекта к инструменту."""

    AVAILABLE = "available"
    DENIED = "denied"
    CHAT_ONLY = "chat_only"

    @property
    def headless(self) -> bool:
        """Можно звать вне чата: со страницы, по REST, из workflow."""
        return self is ToolAvailability.AVAILABLE

    @property
    def in_chat(self) -> bool:
        """Можно звать внутри хода чата."""
        return self is not ToolAvailability.DENIED


class ToolGrant(BaseModel):
    """Набор инструментов, разрешённых субъекту доступа: роли или профилю."""

    model_config = ConfigDict(extra="ignore")

    WILDCARD: ClassVar[str] = "*"

    tools: StringList = Field(
        default=[],
        description=(
            "Имена инструментов; '*' — все собранные. Пустой список — инструментов нет."
        ),
    )

    def covers(self, tool: str) -> bool:
        if self.WILDCARD in self.tools:
            return True

        return tool in self.tools

    def unknown(self, known: frozenset[str]) -> list[str]:
        """Имена, которых нет среди собранных инструментов: опечатки конфига."""
        missing: list[str] = []
        for name in self.tools:
            if name == self.WILDCARD:
                continue

            if name in known:
                continue

            missing.append(name)

        return missing


class ProfileGrant(ToolGrant):
    """Грант профиля: кроме инструментов — роли, которым профиль виден."""

    roles: StringList = Field(
        default=[],
        description="Роли, которым профиль виден; '*' — всем.",
    )

    def visible_for(self, user_roles: Iterable[str]) -> bool:
        roles = frozenset(user_roles)
        if not roles:
            return False

        if self.WILDCARD in self.roles:
            return True

        return bool(frozenset(self.roles) & roles)


class AccessSubject(Protocol):
    """Что нужно знать о вызывающем: его роли и профиль."""

    @property
    def roles(self) -> Iterable[str]: ...

    @property
    def profile(self) -> str: ...


class ToolAccessError(Exception):
    """Гранты ссылаются на инструменты, которых среди собранных нет."""


class ToolAccess:
    """Права доступа: роли, профили и инструменты чата под одним решением."""

    def __init__(
        self,
        tool_names: Iterable[str],
        roles: Mapping[str, ToolGrant],
        profiles: Mapping[str, ProfileGrant],
        chat_only: Iterable[str] = (),
    ) -> None:
        self._tool_names = frozenset(tool_names)
        self._roles = dict(roles)
        self._profiles = dict(profiles)
        self._chat_only = frozenset(chat_only)

        self._check_grants()

    @property
    def tool_names(self) -> frozenset[str]:
        return self._tool_names

    def known(self, tool_name: str) -> bool:
        return tool_name in self._tool_names

    def chat_only(self, tool_name: str) -> bool:
        return tool_name in self._chat_only

    def decide(
        self,
        tool_name: str,
        user_roles: Iterable[str],
        profile: str,
    ) -> ToolAvailability:
        """Единственное решение о доступе; DENIED по умолчанию."""
        if not self._granted(tool_name, frozenset(user_roles), profile):
            return ToolAvailability.DENIED

        if tool_name in self._chat_only:
            return ToolAvailability.CHAT_ONLY

        return ToolAvailability.AVAILABLE

    def _granted(self, tool_name: str, roles: frozenset[str], profile: str) -> bool:
        """Профиль виден ролям, профиль и хотя бы одна роль разрешают инструмент."""
        if tool_name not in self._tool_names:
            return False

        grant = self._profiles.get(profile)
        if grant is None:
            return False

        if not grant.visible_for(roles):
            return False

        if not grant.covers(tool_name):
            return False

        return self._role_covers(tool_name, roles)

    def allowed(
        self,
        tool_name: str,
        user_roles: Iterable[str],
        profile: str,
    ) -> bool:
        """Разрешён хоть где-то: в чате — да, вне чата — см. decide().headless."""
        return self.decide(tool_name, user_roles, profile).in_chat

    def _role_covers(self, tool_name: str, user_roles: frozenset[str]) -> bool:
        for role in user_roles:
            grant = self._roles.get(role)
            if grant is None:
                continue

            if grant.covers(tool_name):
                return True

        return False

    def _check_grants(self) -> None:
        for role_name, grant in self._roles.items():
            missing = grant.unknown(self._tool_names)
            if missing:
                raise ToolAccessError(f"role {role_name!r}: unknown tools {missing}")

        for profile_name, grant in self._profiles.items():
            missing = grant.unknown(self._tool_names)
            if missing:
                msg = f"profile {profile_name!r}: unknown tools {missing}"
                raise ToolAccessError(msg)

        stray = sorted(self._chat_only - self._tool_names)
        if stray:
            raise ToolAccessError(f"chat-only tools are not built: {stray}")
