"""Реестр собранных инструментов и решение о доступности под роли и профиль.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from boba.access import ToolAccess
from boba.toolrun.stream_calls import (
    LocalDagService,
    NodeArgs,
    StreamGroupsConfig,
    ToolServer,
    ToolServers,
)

__all__ = ["ToolRegistry"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolRegistry:
    """Собранные инструменты, права доступа к ним и секция [stream_groups]:
    по ней сессия исполняет вызовы инструментов через DAG.

    own — имена собственных инструментов процесса (чат — инструменты
    каталога соединений): они образуют отдельный сервер инструментов, и
    только реестр знает, какой инструмент какому серверу принадлежит.
    node_args — правила аргументов узлов, которые исполнитель применяет до
    старта DAG."""

    tools: list[BaseTool]
    access: ToolAccess
    stream_config: StreamGroupsConfig
    own: frozenset[str]
    node_args: Sequence[NodeArgs]

    def server(self, tools: Iterable[BaseTool]) -> ToolServer:
        """Порт инструментов для клиента по инструментам tools.

        За портом два сервера: собственные инструменты процесса и остальные;
        вызов уходит по имени инструмента. Клиент про деление не знает.
        """
        own: list[BaseTool] = []
        hosted: list[BaseTool] = []
        for tool in tools:
            if tool.name in self.own:
                own.append(tool)
                continue

            hosted.append(tool)

        return ToolServers(
            [
                LocalDagService(hosted, self.stream_config, self.node_args),
                LocalDagService(own, self.stream_config, ()),
            ]
        )

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
        """Инструменты вне чата (REST, планировщик) по именам."""
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
