"""Ролевой доступ к инструментам: карта прав и защита на вызове."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import ClassVar

from langchain_core.tools import BaseTool

from boba.chainlit.agent.toolrun.wrapping import CallHooks, ToolBody

logger = logging.getLogger(__name__)

__all__ = ["ToolAccess", "ToolAccessDeniedError", "ToolAccessGuard"]


class ToolAccessDeniedError(Exception):
    """Инструмент вызван пользователем, которому он не разрешён."""


class ToolAccess:
    """Карта «имя инструмента -> роли, которым он доступен»."""

    WILDCARD: ClassVar[str] = "*"

    def __init__(self, roles_by_tool: Mapping[str, Iterable[str]]) -> None:
        self._roles = {name: frozenset(roles) for name, roles in roles_by_tool.items()}

    def known(self, tool_name: str) -> bool:
        return tool_name in self._roles

    def allowed(self, tool_name: str, user_roles: Iterable[str]) -> bool:
        granted = self._roles.get(tool_name)
        if not granted:
            return False
        if self.WILDCARD in granted:
            return True
        return bool(granted & frozenset(user_roles))

    def names_for(self, user_roles: Iterable[str]) -> set[str]:
        roles = frozenset(user_roles)
        return {name for name in self._roles if self.allowed(name, roles)}


class ToolAccessGuard:
    """Проверка прав в момент вызова инструмента."""

    class _Hooks(CallHooks[None]):
        def __init__(
            self,
            access: ToolAccess,
            roles_source: Callable[[], Iterable[str]],
        ) -> None:
            self._access = access
            self._roles_source = roles_source

        def before(
            self,
            name: str,
            args: tuple[object, ...],
            kwargs: dict[str, object],
        ) -> None:
            roles = self._roles_source()
            if self._access.allowed(name, roles):
                return

            logger.warning(
                "access denied to tool %r (user roles: %s)",
                name,
                sorted(roles) or "нет",
            )
            msg = f"tool {name!r} is not available for your role"
            raise ToolAccessDeniedError(msg)

    @classmethod
    def guard_all(
        cls,
        tools: Sequence[BaseTool],
        access: ToolAccess,
        roles_source: Callable[[], Iterable[str]],
    ) -> list[BaseTool]:
        return ToolBody.hook_all(tools, cls._Hooks(access, roles_source))
