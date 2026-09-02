"""Реестр собранных инструментов и решение о доступности под роли и профиль.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from boba.access import ToolAccess
from boba.toolkit.ports import ToolStreamSpecs

__all__ = ["ToolRegistry"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolRegistry:
    """Собранные инструменты и права доступа к ним"""

    tools: list[BaseTool]
    access: ToolAccess

    def for_session(self, user_roles: Iterable[str], profile: str) -> list[BaseTool]:
        """Инструменты хода чата: всё, что решение допускает в чате.

        Потоковые насосы (инструменты с портами данных) сюда не попадают:
        их выход предназначен другому инструменту, а не модели — модель
        собирает их в цепочки через pipeline_catalog/pipeline_run.
        """
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
            if not headless and ToolStreamSpecs.of(tool.name).streaming():
                continue

            decision = self.access.decide(tool.name, roles, profile)
            if headless:
                admitted = decision.headless
            else:
                admitted = decision.in_chat

            if admitted:
                yield tool
