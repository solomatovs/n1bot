"""Реестр собранных инструментов и решение о доступности под роли и профиль.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from boba.access import ToolAccess

__all__ = ["ToolRegistry"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolRegistry:
    """Собранные инструменты и права доступа к ним"""

    tools: list[BaseTool]
    access: ToolAccess

    def for_session(self, user_roles: Iterable[str], profile: str) -> list[BaseTool]:
        """Инструменты хода чата: всё, что решение допускает в чате."""
        roles = frozenset(user_roles)
        allowed = list(self._select(roles, profile, headless=False))

        logger.info(
            "tools available: %d of %d (roles: %s, chat profile: %s)",
            len(allowed),
            len(self.tools),
            sorted(roles) or "none",
            profile or "none",
        )
        return allowed

    def for_headless(
        self, user_roles: Iterable[str], profile: str
    ) -> dict[str, BaseTool]:
        """Инструменты вне чата (REST, workflow, планировщик) по именам."""
        roles = frozenset(user_roles)

        by_name: dict[str, BaseTool] = {}
        for tool in self._select(roles, profile, headless=True):
            by_name[tool.name] = tool

        return by_name

    def _select(
        self, roles: frozenset[str], profile: str, *, headless: bool
    ) -> Iterator[BaseTool]:
        for tool in self.tools:
            decision = self.access.decide(tool.name, roles, profile)
            if headless:
                admitted = decision.headless
            else:
                admitted = decision.in_chat

            if admitted:
                yield tool
