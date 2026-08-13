"""Ролевой доступ к инструментам: карта прав и защита на вызове."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import partial, wraps
from typing import ClassVar

from langchain_core.tools import BaseTool

from boba.chainlit.agent.tools.wrapping import AsyncCall, SyncCall, ToolBody

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

    @staticmethod
    def guard_all(
        tools: Sequence[BaseTool],
        access: ToolAccess,
        roles_source: Callable[[], Iterable[str]],
    ) -> list[BaseTool]:
        guard = partial(
            ToolAccessGuard._guard, access=access, roles_source=roles_source
        )
        guard_async = partial(
            ToolAccessGuard._guard_async, access=access, roles_source=roles_source
        )

        return ToolBody.wrap_all(tools, guard, guard_async)

    @staticmethod
    def _check(
        name: str,
        access: ToolAccess,
        roles_source: Callable[[], Iterable[str]],
    ) -> None:
        roles = roles_source()
        if not access.allowed(name, roles):
            logger.warning(
                "access denied to tool %r (user roles: %s)",
                name,
                sorted(roles) or "нет",
            )
            msg = f"tool {name!r} is not available for your role"
            raise ToolAccessDeniedError(msg)

    @staticmethod
    def _guard(
        call: SyncCall,
        name: str,
        access: ToolAccess,
        roles_source: Callable[[], Iterable[str]],
    ) -> SyncCall:
        @wraps(call)
        def guarded(*args: object, **kwargs: object) -> object:
            ToolAccessGuard._check(name, access, roles_source)
            return call(*args, **kwargs)

        return guarded

    @staticmethod
    def _guard_async(
        call: AsyncCall,
        name: str,
        access: ToolAccess,
        roles_source: Callable[[], Iterable[str]],
    ) -> AsyncCall:
        @wraps(call)
        async def guarded(*args: object, **kwargs: object) -> object:
            ToolAccessGuard._check(name, access, roles_source)
            return await call(*args, **kwargs)

        return guarded
