"""Карточки инструментов порта ToolServer как инструменты сервера fastmcp.

Один адаптер для всех MCP-серверов boba: сервис исполнения boba-mcp и
сервер инструментов чата собирают свой FastMCP из одних и тех же частей.
McpTool — инструмент fastmcp над карточкой порта: схема карточки для
клиента, вызов портом узлом DagNode, итог клиенту ответом McpReplies
стандартными полями результата MCP (ResultWire). McpToolProvider —
провайдер fastmcp: на каждый запрос берёт порт вызывающего у CallerServers
(кто вызывает, решает приложение по своему токену) и отдаёт инструменты
порта. FeatureExtension — возможность сервера, объявляемая клиенту при
согласовании протокола.

Ошибки:
наружу уходит только итог вызова — сбой любого вида возвращается
    результатом с isError и моделью отказа (FailureResult) в structuredContent.
исключения CallerServers.current() — реализация приложения не узнала
    вызывающего; список инструментов и вызов для него отказывают.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import mcp_types as mt
from fastmcp.server.extensions import ServerExtension
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.components import FastMCPComponent
from fastmcp.utilities.versions import VersionSpec
from pydantic import BaseModel, PrivateAttr

from boba.toolkit.calls import CallIdPrefix, CallViews
from boba.toolkit.dag import DagNode, NodeOutcome, NodeOutcomes, ToolCard, ToolServer
from boba.toolkit.failure import FailurePacker
from boba.toolkit.result import FailureResult
from boba.toolkit.wire import ResultWire

__all__ = [
    "CallerServers",
    "FeatureExtension",
    "McpReplies",
    "McpTool",
    "McpToolProvider",
]

logger = logging.getLogger(__name__)


class McpReplies:
    """Ответы сервера клиенту в формате fastmcp — единственное место, где
    итог вызова становится ToolResult.

    Создаётся каждым, кто отвечает на tools/call: инструментом порта
    (McpTool), операциями приложения и его middleware. Итог вызова
    (NodeOutcome) и отказ сервера едут клиенту полями ResultWire: текст для
    модели в content, сведения о вызове и результат семейства в
    structuredContent. Ответ операции — её модель в structuredContent.
    """

    def __init__(self) -> None:
        self._wire = ResultWire()
        self._outcomes = NodeOutcomes()

    def of(self, outcome: NodeOutcome) -> ToolResult:
        """Итог вызова инструмента."""
        packed = self._wire.packed(outcome)

        return ToolResult(
            content=[mt.TextContent(type="text", text=packed.content)],
            structured_content=dict(packed.structured or {}),
            is_error=packed.is_error,
        )

    def refused(self, tool: str, failure: FailureResult) -> ToolResult:
        """Отказ сервера вызову tool, который до исполнителя не дошёл:
        идентификатор вызова выдаётся здесь."""
        call_id = CallIdPrefix.API.new_id()

        return self.of(self._outcomes.unnamed(call_id, tool, failure))

    def answered(self, text: str, answer: BaseModel) -> ToolResult:
        """Ответ операции сервера: текст и её модель ответа."""
        return ToolResult(
            content=[mt.TextContent(type="text", text=text)],
            structured_content=answer.model_dump(mode="json"),
        )

    def stamped(self, result: ToolResult, run: str) -> ToolResult:
        """Тот же ответ с запуском run в сведениях о вызове: по запуску и
        идентификатору вызова клиент читает журнал."""
        structured = result.structured_content
        if structured is None:
            return result

        return ToolResult(
            content=result.content,
            structured_content=self._wire.stamped(structured, run),
            meta=result.meta,
            is_error=result.is_error,
        )


class McpTool(Tool):
    """Инструмент порта как инструмент fastmcp.

    Создаёт его McpToolProvider из карточки инструмента порта: схема для
    клиента — готовая схема вызова после всех обвязок, где аргументы с кодом
    названы типом содержимого (CallViews), тело — вызов порта ToolServer
    узлом DagNode под идентификатором, который выдаёт сервер; итог порта
    (NodeOutcome) уходит клиенту ответом McpReplies. fastmcp аргументы не
    проверяет: проверка одна, по схеме инструмента внутри исполнителя.
    """

    _server: ToolServer = PrivateAttr()
    _replies: McpReplies = PrivateAttr()
    _outcomes: NodeOutcomes = PrivateAttr()
    _failures: FailurePacker = PrivateAttr()

    def __init__(self, card: ToolCard, server: ToolServer) -> None:
        super().__init__(
            name=card.name,
            description=card.description,
            parameters=CallViews().typed(card.parameters, card.views),
            annotations=mt.ToolAnnotations(
                read_only_hint=card.hints.read_only,
                destructive_hint=card.hints.destructive,
            ),
        )
        self._server = server
        self._replies = McpReplies()
        self._outcomes = NodeOutcomes()
        self._failures = FailurePacker()

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        call_id = CallIdPrefix.API.new_id()
        try:
            call = DagNode.model_validate(
                {"key": call_id, "tool": self.name, "args": arguments}
            )
            outcome = await self._server.call(call)
        except Exception as exc:
            logger.exception("tool[%s]: the call crashed outside its body", self.name)
            failure = self._failures.pack(exc)
            outcome = self._outcomes.unnamed(call_id, self.name, failure)

        return self._replies.of(outcome)


class CallerServers(Protocol):
    """Порт инструментов вызывающего текущего запроса.

    Реализует приложение: по токену запроса узнаёт вызывающего и отдаёт
    порт с инструментами, которые ему доступны. Зовёт McpToolProvider на
    каждый список и вызов инструментов.
    """

    @abstractmethod
    async def current(self) -> ToolServer:
        """Ошибки — свои у приложения: вызывающего по запросу не узнать."""


class McpToolProvider(Provider):
    """Провайдер fastmcp: инструменты порта вызывающего.

    Создаётся сборкой сервера. На каждый запрос берёт порт вызывающего у
    CallerServers и отдаёт инструменты порта как McpTool; объекты McpTool
    строятся один раз на порт. Операции приложения (operations) видит
    каждый вызывающий.
    """

    def __init__(self, servers: CallerServers, operations: Sequence[Tool]) -> None:
        super().__init__()
        self._servers = servers
        self._operations = tuple(operations)
        self._tools: dict[ToolServer, dict[str, Tool]] = {}

    async def _list_tools(self) -> Sequence[Tool]:
        offered = await self._offered()

        return list(offered.values())

    async def _get_tool(
        self, name: str, version: VersionSpec | None = None
    ) -> Tool | None:
        offered = await self._offered()

        return offered.get(name)

    async def get_tasks(self) -> Sequence[FastMCPComponent]:
        """Фоновых задач у порта нет: вызов живёт, пока живо соединение."""
        return []

    async def _offered(self) -> dict[str, Tool]:
        server = await self._servers.current()
        offered = self._tools.get(server)
        if offered is not None:
            return offered

        offered = {}
        for card in server.tools():
            offered[card.name] = McpTool(card, server)

        for operation in self._operations:
            offered[operation.name] = operation

        self._tools[server] = offered

        return offered


class FeatureExtension(ServerExtension):
    """Возможность сервера как расширение MCP: идентификатор и настройки,
    которые клиент получает при согласовании протокола.

    Создаётся сборкой сервера на каждую возможность (журнал вызовов,
    запечатывание соединений) и регистрируется в сервере fastmcp.
    """

    def __init__(self, identifier: str, settings: Mapping[str, Any]) -> None:
        self.identifier = identifier
        self._settings = dict(settings)

    def settings(self) -> dict[str, Any]:
        return dict(self._settings)
