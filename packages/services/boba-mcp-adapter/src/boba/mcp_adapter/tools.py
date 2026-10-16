"""Карточки инструментов порта ToolServer как инструменты сервера fastmcp.

Один адаптер для инструментов boba по MCP: сервис исполнения boba-mcp
собирает из этих частей свой FastMCP, ретранслятор чата отдаёт ими
собственные инструменты чата агенту. McpTool — инструмент fastmcp над
карточкой порта: схема карточки для клиента, вызов портом узлом DagNode,
итог клиенту ответом McpReplies стандартными полями результата MCP
(ResultWire). RoleTools — McpTool порта на пару «роли, профиль», собранные
один раз. McpToolProvider — провайдер fastmcp: на каждый запрос берёт
инструменты вызывающего у CallerServers (кто вызывает, решает приложение по
своему токену). FeatureExtension — возможность сервера, объявляемая клиенту
при согласовании протокола. JournalUris — uri каналов журнала вызова
ресурсами MCP и их разбор.

Ошибки:
наружу уходит только итог вызова — сбой любого вида возвращается
    результатом с isError и текстом отказа в content.
исключения CallerServers.current() — реализация приложения не узнала
    вызывающего; список инструментов и вызов для него отказывают.
исключения RoleTools._server() — наследник не собрал порт пары.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import mcp_types as mt
from fastmcp.resources.template import expand_uri_template, match_uri_template
from fastmcp.server.extensions import ServerExtension
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.components import FastMCPComponent
from fastmcp.utilities.versions import VersionSpec
from pydantic import BaseModel, PrivateAttr, ValidationError

from boba.toolkit.calls import CallIdPrefix, CallViews
from boba.toolkit.dag import DagNode, NodeOutcome, NodeOutcomes, ToolCard, ToolServer
from boba.toolkit.failure import FailurePacker
from boba.toolkit.mcp import McpToolStubs
from boba.toolkit.result import FailureResult
from boba.toolkit.wire import JournalRead, ResultWire

__all__ = [
    "CallerServers",
    "FeatureExtension",
    "JournalUris",
    "McpReplies",
    "McpTool",
    "McpToolProvider",
    "RoleTools",
]

logger = logging.getLogger(__name__)


class McpReplies:
    """Ответы сервера клиенту в формате fastmcp — единственное место, где
    итог вызова становится ToolResult.

    Создаётся каждым, кто отвечает на tools/call: инструментом порта
    (McpTool), операциями приложения и его middleware. Итог вызова
    (NodeOutcome) и отказ сервера едут клиенту полями ResultWire: блоки
    результата в content, данные по outputSchema в structuredContent.
    Ответ операции — её модель в structuredContent.
    """

    def __init__(self) -> None:
        self._wire = ResultWire()
        self._outcomes = NodeOutcomes()

    def of(self, outcome: NodeOutcome) -> ToolResult:
        """Итог вызова инструмента."""
        packed = self._wire.packed(outcome)

        structured: dict[str, Any] | None = None
        if packed.structured is not None:
            structured = dict(packed.structured)

        return ToolResult(
            content=list(packed.content),
            structured_content=structured,
            meta=dict(packed.meta),
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

    def journaled(
        self, result: ToolResult, links: Sequence[mt.ContentBlock]
    ) -> ToolResult:
        """Тот же ответ со ссылками на каналы журнала вызова в конце content:
        по ним клиент читает журнал (JournalUris)."""
        if not links:
            return result

        return ToolResult(
            content=[*result.content, *links],
            structured_content=result.structured_content,
            meta=result.meta,
            is_error=result.is_error,
        )


class JournalUris:
    """Адреса каналов журнала вызова ресурсами MCP по шаблону uri расширения
    журнала (JournalFeature.resource).

    Сервер (CallContextMiddleware) по uri() пишет в конец итога ссылки на
    каналы журнала вызова, клиент (лента чата) по address() узнаёт в такой
    ссылке адрес журнала. Шаблон раскрывают и сопоставляют функции шаблонов
    fastmcp — те же, по которым сервер отвечает на resources/read.
    """

    def __init__(self, template: str) -> None:
        self._template = template

    def uri(self, address: JournalRead) -> str:
        """Uri ресурса канала журнала по его адресу."""
        params = address.model_dump(mode="json", exclude_none=True)

        return expand_uri_template(self._template, params)

    def address(self, uri: str) -> JournalRead | None:
        """Адрес канала журнала по ссылке; None — ссылка не на журнал."""
        params = match_uri_template(uri, self._template)
        if params is None:
            return None

        try:
            return JournalRead.model_validate(params)
        except ValidationError as exc:
            logger.info("journal link %s names no journal channel: %s", uri, exc)
            return None


class McpTool(Tool):
    """Инструмент порта как инструмент fastmcp.

    Создаёт его RoleTools из карточки инструмента порта: схема для
    клиента — готовая схема вызова после всех обвязок, где аргументы с кодом
    названы типом содержимого (CallViews), и схема итога карточки; тело —
    вызов порта ToolServer узлом DagNode под идентификатором, который
    выдаёт сервер; итог порта (NodeOutcome) уходит клиенту ответом
    McpReplies. fastmcp аргументы не проверяет: проверка одна, по схеме
    инструмента внутри исполнителя.
    """

    _server: ToolServer = PrivateAttr()
    _replies: McpReplies = PrivateAttr()
    _outcomes: NodeOutcomes = PrivateAttr()
    _failures: FailurePacker = PrivateAttr()

    def __init__(self, card: ToolCard, server: ToolServer) -> None:
        annotations = McpToolStubs().annotations_of(card.hints)
        output: dict[str, Any] | None = None
        if card.output is not None:
            output = dict(card.output)

        super().__init__(
            name=card.name,
            description=card.description,
            parameters=CallViews().typed(card.parameters, card.views),
            output_schema=output,
            annotations=annotations,
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


class RoleTools(ABC):
    """Базовый класс инструментов порта на пару «роли, профиль»: McpTool по
    карточкам порта строятся один раз на пару и дальше отдаются готовыми.

    Наследуют RoleToolServers сервиса boba-mcp и ChatTools ретранслятора
    чата: наследник решает, какой порт ToolServer видит пара (_server).
    """

    def __init__(self) -> None:
        self._tools: dict[tuple[frozenset[str], str], dict[str, McpTool]] = {}

    def tools(self, roles: frozenset[str], profile: str) -> Mapping[str, McpTool]:
        """Инструменты пары по именам."""
        key = (roles, profile)
        offered = self._tools.get(key)
        if offered is not None:
            return offered

        server = self._server(roles, profile)
        offered = {}
        for card in server.tools():
            offered[card.name] = McpTool(card, server)

        self._tools[key] = offered

        return offered

    @abstractmethod
    def _server(self, roles: frozenset[str], profile: str) -> ToolServer:
        """Порт инструментов, доступных паре."""


class CallerServers(Protocol):
    """Инструменты вызывающего текущего запроса.

    Реализует приложение: по токену запроса узнаёт вызывающего и отдаёт
    инструменты, которые ему доступны. Зовёт McpToolProvider на каждый
    список и вызов инструментов.
    """

    @abstractmethod
    async def current(self) -> Mapping[str, McpTool]:
        """Ошибки — свои у приложения: вызывающего по запросу не узнать."""


class McpToolProvider(Provider):
    """Провайдер fastmcp: инструменты вызывающего и операции приложения.

    Создаётся сборкой сервера. На каждый запрос берёт инструменты
    вызывающего у CallerServers и добавляет к ним операции приложения
    (operations), которые видит каждый вызывающий.
    """

    def __init__(self, servers: CallerServers, operations: Sequence[Tool]) -> None:
        super().__init__()
        self._servers = servers
        self._operations = tuple(operations)

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
        tools = await self._servers.current()

        offered: dict[str, Tool] = dict(tools)
        for operation in self._operations:
            offered[operation.name] = operation

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
