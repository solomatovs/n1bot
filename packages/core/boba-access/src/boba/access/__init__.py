"""Доступ к инструментам: гранты ролей и профилей, одно решение о доступности.

Инструмент доступен субъекту, когда профиль виден его ролям, профиль
разрешает инструмент и хотя бы одна роль его разрешает. Deny by default.

Ошибки:
ToolAccessError — гранты ссылаются на инструменты, которых нет.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from boba.toolkit.types import StringList

__all__ = [
    "AccessSubject",
    "GrantCheck",
    "ProfileGrant",
    "RoleConfig",
    "RoleVisibility",
    "ToolAccess",
    "ToolAccessError",
    "ToolAvailability",
    "ToolGrant",
    "ToolGrants",
]


class ToolAvailability(StrEnum):
    """Решение о доступе субъекта к инструменту."""

    AVAILABLE = "available"
    DENIED = "denied"

    @property
    def allowed(self) -> bool:
        return self is ToolAvailability.AVAILABLE


class ToolGrant(BaseModel):
    """Набор инструментов, разрешённых субъекту доступа: роли или профилю.
    Тем же правилом имён и '*' профиль чата разрешает свои настройки агента
    (ChatProfileConfig.settings)."""

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


class RoleVisibility(BaseModel):
    """Роли, которым виден элемент конфига: хотя бы одна роль вызывающего
    совпала или среди ролей '*'.

    База секций с ролями: грант профиля (ProfileGrant), правило предложения
    соединений endpoint'а и заданное соединение сервера boba-mcp. Роли
    обязательны; грант профиля может их не называть — такой профиль не
    виден никому.
    """

    roles: StringList = Field(
        min_length=1,
        description="Роли, которым элемент виден; '*' — всем.",
    )

    def visible_for(self, user_roles: Iterable[str]) -> bool:
        roles = frozenset(user_roles)
        if not roles:
            return False

        if ToolGrant.WILDCARD in self.roles:
            return True

        return bool(frozenset(self.roles) & roles)


class ProfileGrant(ToolGrant, RoleVisibility):
    """Грант профиля: кроме инструментов — роли, которым профиль виден."""

    roles: StringList = Field(
        default=[],
        description="Роли, которым профиль виден; '*' — всем.",
    )


class AccessSubject(Protocol):
    """Что нужно знать о вызывающем: его роли и профиль."""

    @property
    def roles(self) -> Iterable[str]: ...

    @property
    def profile(self) -> str: ...


class ToolAccessError(Exception):
    """Гранты ссылаются на инструменты, которых среди собранных нет."""


class GrantCheck(StrEnum):
    """Как сверять гранты с собранными инструментами.

    STRICT — процесс держит всю таблицу: чужое имя в гранте — опечатка.
    HOSTED — часть инструментов живёт в другом процессе: проверяются только свои.
    """

    STRICT = "strict"
    HOSTED = "hosted"


@dataclass(frozen=True)
class ToolGrants:
    """Гранты процесса для ToolAccess: что разрешают роли и профили и как
    сверять имена инструментов. Собирает сборка приложения из своего конфига;
    роль с именем '*' (ToolGrant.WILDCARD) — грант любой роли вошедшего."""

    roles: Mapping[str, ToolGrant]
    profiles: Mapping[str, ProfileGrant]
    check: GrantCheck


class ToolAccess:
    """Права доступа: роли, профили и инструменты чата под одним решением."""

    def __init__(
        self,
        tool_names: Iterable[str],
        roles: Mapping[str, ToolGrant],
        profiles: Mapping[str, ProfileGrant],
        check: GrantCheck = GrantCheck.STRICT,
    ) -> None:
        self._tool_names = frozenset(tool_names)
        self._roles = dict(roles)
        self._profiles = dict(profiles)
        if check is GrantCheck.STRICT:
            self._check_grants()

    @property
    def tool_names(self) -> frozenset[str]:
        return self._tool_names

    def known(self, tool_name: str) -> bool:
        return tool_name in self._tool_names

    def profiles(self) -> frozenset[str]:
        """Имена профилей, для которых заданы гранты."""
        return frozenset(self._profiles)

    def profile_grant(self, profile: str) -> ProfileGrant | None:
        """Грант профиля: какие инструменты профиль оставляет доступными,
        откуда бы они ни были подключены; None — такого профиля нет."""
        return self._profiles.get(profile)

    def decide(
        self,
        tool_name: str,
        user_roles: Iterable[str],
        profile: str,
    ) -> ToolAvailability:
        """Единственное решение о доступе; DENIED по умолчанию."""
        if not self._granted(tool_name, frozenset(user_roles), profile):
            return ToolAvailability.DENIED

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

    def _role_covers(self, tool_name: str, user_roles: frozenset[str]) -> bool:
        any_role = self._roles.get(ToolGrant.WILDCARD)
        if any_role is not None and user_roles:
            if any_role.covers(tool_name):
                return True

        for role in user_roles:
            grant = self._roles.get(role)
            if grant is None:
                continue

            if grant.covers(tool_name):
                return True

        return False

    def _check_grants(self) -> None:
        known = sorted(self._tool_names)
        for role_name, grant in self._roles.items():
            missing = grant.unknown(self._tool_names)
            if missing:
                msg = (
                    f"tool access: section [roles.{role_name!r}] grants tools "
                    f"{missing} that are not among the built tools {known}"
                )
                raise ToolAccessError(msg)

        for profile_name, grant in self._profiles.items():
            missing = grant.unknown(self._tool_names)
            if missing:
                msg = (
                    f"tool access: section [profiles.{profile_name!r}] grants tools "
                    f"{missing} that are not among the built tools {known}"
                )
                raise ToolAccessError(msg)


class RoleConfig(ToolGrant):
    """Секция [roles.<ROLE>]: что роль разрешает своему обладателю."""
