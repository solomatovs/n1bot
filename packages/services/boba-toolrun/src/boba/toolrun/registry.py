"""Цепочка обвязок вызова, реестр собранных инструментов и решение о
доступности под роли и профиль.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool

from boba.access import ToolAccess
from boba.identity.context import CallContexts
from boba.toolkit.chain import CallAmbient
from boba.toolkit.ports import StreamSpecs
from boba.toolrun.access import ToolAccessGuard
from boba.toolrun.call_id import CallFields
from boba.toolrun.cancellation import CancellableTools
from boba.toolrun.dag_run import DagRunner
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.stream_calls import (
    LocalDagService,
    NodeArgs,
    StreamChannelFields,
    StreamGroupsConfig,
    ToolServer,
    ToolServers,
)
from boba.toolrun.streams import CallJournals
from boba.toolrun.wrapping import CallHooks, ToolAsyncBody, ToolBody

__all__ = ["ToolChain", "ToolRegistry"]

logger = logging.getLogger(__name__)


class ToolChain:
    """Обвязки вызова инструмента в порядке их постановки — единственный
    владелец этого порядка.

    Создаёт его загрузчик инструментов (ToolLoader) из секции
    [stream_groups], журналов вызовов и держателя контекста процесса; стенды
    тестов создают такой же. seal() ставит обвязки на уже собранные
    инструменты, изнутри наружу: обвязки поверхности процесса, поля каналов
    и служебные поля вызова в схеме, журнал, отмена, права, упаковка ошибок;
    последним sync-телу даётся корутина.
    """

    def __init__(
        self,
        stream_config: StreamGroupsConfig,
        journals: CallJournals,
        contexts: CallContexts,
        ambient: CallAmbient,
        surface_hooks: Sequence[CallHooks[Any]] = (),
    ) -> None:
        self._surface_hooks = tuple(surface_hooks)
        """Обвязки поверхности процесса (чат монтирует элементы результата):
        ставятся сразу после тела, до журнала и разбора ошибок."""
        self._channels = StreamChannelFields(stream_config)
        self._fields = CallFields()
        self._run_log = ToolRunLogger(journals, contexts, ambient)
        self._cancellable = CancellableTools()
        self._access = ToolAccessGuard(contexts.subject)
        self._errors = ToolErrorGuard()
        self._async_body = ToolAsyncBody()

    def seal(
        self, tools: Sequence[BaseTool], access: ToolAccess, specs: StreamSpecs
    ) -> None:
        """Ставит обвязки на инструменты; права access проверяются на вызове,
        specs — потоковые декларации этих инструментов."""
        for hooks in self._surface_hooks:
            ToolBody.hook_all(tools, hooks)

        self._channels.attach_all(tools, specs)
        self._fields.attach_all(tools)
        self._run_log.guard_all(tools)
        self._cancellable.guard_all(tools)
        self._access.guard_all(tools, access)
        self._errors.guard_all(tools)
        self._async_body.ensure_all(tools)


@dataclass(frozen=True)
class ToolRegistry:
    """Собранные инструменты, права доступа к ним и секция [stream_groups]:
    по ней сессия исполняет вызовы инструментов через DAG.

    own — имена собственных инструментов процесса (чат — инструменты
    каталога соединений): они образуют отдельный сервер инструментов, и
    только реестр знает, какой инструмент какому серверу принадлежит.
    node_args — правила аргументов узлов, которые исполнитель применяет до
    старта DAG. specs — потоковые декларации инструментов: по ним
    планировщик стыкует каналы. ambient — обстановка вызова процесса: в
    неё исполнитель DAG ставит ручку узла."""

    tools: list[BaseTool]
    access: ToolAccess
    stream_config: StreamGroupsConfig
    own: frozenset[str]
    node_args: Sequence[NodeArgs]
    specs: StreamSpecs
    ambient: CallAmbient

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
                LocalDagService(
                    hosted,
                    self.stream_config,
                    self.node_args,
                    self.specs,
                    self.ambient,
                ),
                LocalDagService(own, self.stream_config, (), self.specs, self.ambient),
            ]
        )

    def runner(self, tools: Mapping[str, BaseTool]) -> DagRunner:
        """Исполнитель DAG над инструментами tools с декларациями реестра."""
        config = self.stream_config

        return DagRunner(
            tools, self.specs, self.ambient, config.timings(), config.pipe_bytes
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
