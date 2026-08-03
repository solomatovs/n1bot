"""Ролевой доступ к инструментам: карта прав и защита на вызове."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from functools import wraps
from typing import ClassVar, cast

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

__all__ = ["ToolAccess", "ToolAccessDeniedError", "ToolAccessGuard"]


class ToolAccessDeniedError(Exception):
    """Инструмент вызван пользователем, которому он не разрешён."""


class ToolAccess:
    """Карта «имя инструмента -> роли, которым он доступен»."""

    WILDCARD: ClassVar[str] = "*"

    def __init__(self, roles_by_tool: Mapping[str, Iterable[str]]) -> None:
        self._roles = {
            name: frozenset(roles) for name, roles in roles_by_tool.items()
        }

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
        tools: list[BaseTool],
        access: ToolAccess,
        roles_source: Callable[[], Iterable[str]],
    ) -> list[BaseTool]:
        for tool in tools:
            func = getattr(tool, "func", None)
            if callable(func):
                tool.func = ToolAccessGuard._guard(
                    func, tool.name, access, roles_source,
                )
            coroutine = cast(
                "Callable[..., Awaitable[object]] | None",
                getattr(tool, "coroutine", None),
            )
            if callable(coroutine):
                tool.coroutine = ToolAccessGuard._guard_async(
                    coroutine, tool.name, access, roles_source,
                )
        return tools

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
        func: Callable[..., object],
        name: str,
        access: ToolAccess,
        roles_source: Callable[[], Iterable[str]],
    ) -> Callable[..., object]:
        @wraps(func)
        def guarded(*args: object, **kwargs: object) -> object:
            ToolAccessGuard._check(name, access, roles_source)
            return func(*args, **kwargs)

        return guarded

    @staticmethod
    def _guard_async(
        coroutine: Callable[..., Awaitable[object]],
        name: str,
        access: ToolAccess,
        roles_source: Callable[[], Iterable[str]],
    ) -> Callable[..., Awaitable[object]]:
        @wraps(coroutine)
        async def guarded(*args: object, **kwargs: object) -> object:
            ToolAccessGuard._check(name, access, roles_source)
            return await coroutine(*args, **kwargs)

        return guarded
