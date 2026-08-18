"""Доступ к инструментам: пересечение прав ролей и профиля чата, защита на вызове."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence

from langchain_core.tools import BaseTool

from boba.chainlit.agent.toolrun.wrapping import CallHooks, ToolBody
from boba.toolkit.types import ToolGrant

logger = logging.getLogger(__name__)

__all__ = ["ToolAccess", "ToolAccessDeniedError", "ToolAccessGuard"]


class ToolAccessDeniedError(Exception):
    """Инструмент вызван в сессии, которой он не разрешён."""


class ToolAccess:
    """Права доступа: инструмент разрешён, когда его покрывают и роль, и профиль."""

    def __init__(
        self,
        tool_names: Iterable[str],
        roles: Mapping[str, ToolGrant],
        profiles: Mapping[str, ToolGrant],
    ) -> None:
        self._tool_names = frozenset(tool_names)
        self._roles = dict(roles)
        self._profiles = dict(profiles)

    def known(self, tool_name: str) -> bool:
        return tool_name in self._tool_names

    def allowed(
        self,
        tool_name: str,
        user_roles: Iterable[str],
        profile: str | None,
    ) -> bool:
        if tool_name not in self._tool_names:
            return False

        if profile is None:
            return False

        grant = self._profiles.get(profile)
        if grant is None:
            return False

        if not grant.covers(tool_name):
            return False

        return self._role_covers(tool_name, user_roles)

    def names_for(
        self,
        user_roles: Iterable[str],
        profile: str | None,
    ) -> set[str]:
        roles = frozenset(user_roles)

        names: set[str] = set()
        for name in self._tool_names:
            if self.allowed(name, roles, profile):
                names.add(name)

        return names

    def _role_covers(self, tool_name: str, user_roles: Iterable[str]) -> bool:
        for role in user_roles:
            grant = self._roles.get(role)
            if grant is None:
                continue

            if grant.covers(tool_name):
                return True

        return False


class ToolAccessGuard:
    """Проверка прав в момент вызова инструмента."""

    class _Hooks(CallHooks[None]):
        def __init__(
            self,
            access: ToolAccess,
            roles_source: Callable[[], Iterable[str]],
            profile_source: Callable[[], str | None],
        ) -> None:
            self._access = access
            self._roles_source = roles_source
            self._profile_source = profile_source

        def before(
            self,
            name: str,
            args: tuple[object, ...],
            kwargs: dict[str, object],
        ) -> None:
            roles = self._roles_source()
            profile = self._profile_source()
            if self._access.allowed(name, roles, profile):
                return

            logger.warning(
                "access denied to tool %r (user roles: %s, chat profile: %s)",
                name,
                sorted(roles) or "none",
                profile or "none",
            )
            msg = f"tool {name!r} is not available for your role and chat profile"
            raise ToolAccessDeniedError(msg)

    @classmethod
    def guard_all(
        cls,
        tools: Sequence[BaseTool],
        access: ToolAccess,
        roles_source: Callable[[], Iterable[str]],
        profile_source: Callable[[], str | None],
    ) -> list[BaseTool]:
        hooks = cls._Hooks(access, roles_source, profile_source)
        return ToolBody.hook_all(tools, hooks)
