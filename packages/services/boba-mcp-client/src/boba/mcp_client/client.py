"""Порт сервера инструментов над MCP-сервером.

Клиент работает с любым сервером стандарта MCP: берёт список инструментов
(tools/list), строит по их схемам инструменты для модели и исполняет вызовы
(tools/call). Транспорт — streamable HTTP, SSE или stdio. Итог вызова
сервера, который не знает о семействе результатов boba, — текст его content;
сервер boba-dag присылает ещё и модель результата, и она оживает как есть.

Ошибки:
McpClientError — сервер недоступен, не прошёл инициализацию, не отдал список
    инструментов либо порт позвали до open().
Сбой самого вызова наружу не выходит: он возвращается итогом-ошибкой вызова.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol

import httpx
import mcp.types as mt
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.message import SessionMessage
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from boba.toolkit.result import ErrorResult, MarkdownResult, ToolResultBase
from boba.toolkit.wire import ResultWire, WireMeta, WireResult
from boba.toolrun.stream_calls import CallReply, ToolServer

__all__ = [
    "BearerAuth",
    "HttpEndpoint",
    "McpClientError",
    "McpServerConfig",
    "McpToolServer",
    "NoAuth",
    "SseEndpoint",
    "StdioCommand",
]

logger = logging.getLogger(__name__)

Streams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
]
"""Пара потоков сообщений транспорта MCP: чтение и запись."""


class McpClientError(Exception):
    """С MCP-сервером не удалось договориться: соединение, инициализация,
    список инструментов."""


class McpFailure(StrEnum):
    """Виды отказа вызова, которые собирает сам клиент."""

    TOOL_ERROR = "mcp_tool_error"
    TRANSPORT = "mcp_transport_error"


class NoAuth(BaseModel):
    """Сервер без авторизации."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    auth: Literal["none"] = "none"

    def httpx_auth(self) -> httpx.Auth | None:
        return None


class BearerToken(httpx.Auth):
    """Готовый токен заголовком Authorization каждого запроса."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Any:
        request.headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        yield request


class BearerAuth(BaseModel):
    """Готовый токен доступа из конфига."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    auth: Literal["bearer"] = "bearer"
    token: SecretStr

    def httpx_auth(self) -> httpx.Auth | None:
        return BearerToken(self.token)


McpAuth = Annotated[NoAuth | BearerAuth, Field(discriminator="auth")]


class Transport(Protocol):
    """Транспорт MCP-сервера: открывает потоки сообщений сессии."""

    @abstractmethod
    def opened(self) -> AbstractAsyncContextManager[Streams]: ...

    @abstractmethod
    def label(self) -> str:
        """Адрес сервера для сообщений об ошибках и логов."""


class HttpAddress(BaseModel):
    """Адрес HTTP-сервера частями, как у HTTP-соединений."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: Literal["http", "https"]
    host: str = Field(min_length=1)
    port: int | None = Field(default=None, gt=0, lt=65536)
    path: str = Field(min_length=1)
    auth: McpAuth = NoAuth()

    def url(self) -> str:
        return str(
            httpx.URL(
                scheme=self.scheme, host=self.host, port=self.port, path=self.path
            )
        )


class HttpEndpoint(HttpAddress):
    """Сервер по streamable HTTP. Модель конфига; транспорт — она же
    (протокол Transport наследовать нельзя: метакласс pydantic)."""

    transport: Literal["streamable-http"] = "streamable-http"

    def label(self) -> str:
        return self.url()

    @asynccontextmanager
    async def opened(self) -> AsyncIterator[Streams]:
        client = streamablehttp_client(self.url(), auth=self.auth.httpx_auth())
        async with client as (read, write, _session_id):
            yield read, write


class SseEndpoint(HttpAddress):
    """Сервер по SSE (прежний HTTP-транспорт MCP). Модель конфига; транспорт
    — она же (протокол Transport)."""

    transport: Literal["sse"] = "sse"

    def label(self) -> str:
        return self.url()

    @asynccontextmanager
    async def opened(self) -> AsyncIterator[Streams]:
        async with sse_client(self.url(), auth=self.auth.httpx_auth()) as streams:
            yield streams


class StdioCommand(BaseModel):
    """Сервер-субпроцесс со стандартным вводом и выводом. Модель конфига;
    транспорт — она же (протокол Transport)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    def label(self) -> str:
        return " ".join((self.command, *self.args))

    @asynccontextmanager
    async def opened(self) -> AsyncIterator[Streams]:
        env: dict[str, str] | None = None
        if self.env:
            env = dict(self.env)

        params = StdioServerParameters(
            command=self.command, args=list(self.args), env=env, cwd=self.cwd
        )
        async with stdio_client(params) as streams:
            yield streams


McpEndpoint = Annotated[
    HttpEndpoint | SseEndpoint | StdioCommand, Field(discriminator="transport")
]


class McpServerConfig(BaseModel):
    """Один MCP-сервер в конфиге клиента.

    prefix добавляется к именам инструментов сервера: им разводят серверы с
    совпавшими именами. Сроки — на подключение с инициализацией и на один
    вызов инструмента.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: McpEndpoint
    prefix: str = ""
    connect_timeout_sec: float = Field(gt=0)
    call_timeout_sec: float = Field(gt=0)


class ErrorCauses:
    """Причины сбоя одной строкой: группа исключений anyio раскрывается до
    своих листьев, иначе в тексте остаётся «unhandled errors in a TaskGroup».
    Создаётся соединением и переводчиком результатов."""

    def text(self, error: BaseException) -> str:
        return "; ".join(self._leaves(error))

    def _leaves(self, error: BaseException) -> list[str]:
        if isinstance(error, BaseExceptionGroup):
            leaves: list[str] = []
            for inner in error.exceptions:
                leaves.extend(self._leaves(inner))

            return leaves

        return [f"{type(error).__name__}: {error}"]


class McpConnection:
    """Живая сессия MCP с одним сервером.

    Создаётся портом McpToolServer. Транспорт и сессия библиотеки mcp
    открываются и закрываются в одной собственной задаче — их области отмены
    привязаны к задаче, которая их открыла; запросы идут из задач вызовов.
    """

    CLIENT: ClassVar[mt.Implementation] = mt.Implementation(
        name="boba-chat", version="1"
    )

    def __init__(self, transport: Transport, connect_timeout_sec: float) -> None:
        self._transport = transport
        self._connect_timeout_sec = connect_timeout_sec
        self._causes = ErrorCauses()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[tuple[ClientSession, mt.InitializeResult]] | None
        self._ready = None

    async def open(self) -> tuple[ClientSession, mt.InitializeResult]:
        """Сессия и ответ сервера на инициализацию; повторный вызов отдаёт
        уже открытую."""
        if self._ready is None:
            self._stop = asyncio.Event()
            self._ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(
                self._serve(self._ready), name=f"mcp:{self._transport.label()}"
            )

        try:
            return await asyncio.wait_for(
                asyncio.shield(self._ready), self._connect_timeout_sec
            )
        except TimeoutError as exc:
            await self.close()
            msg = (
                f"mcp {self._transport.label()}: no answer to initialize within "
                f"{self._connect_timeout_sec}s"
            )
            raise McpClientError(msg) from exc
        except McpClientError:
            # неудачная попытка не запоминается: следующий open() подключается заново
            await self.close()
            raise

    async def close(self) -> None:
        task = self._task
        self._task = None
        self._ready = None
        self._stop.set()
        if task is None:
            return

        try:
            await task
        except Exception as exc:
            logger.info(
                "mcp %s: the session closed with %s: %s",
                self._transport.label(),
                type(exc).__name__,
                exc,
            )

    async def _serve(
        self, ready: asyncio.Future[tuple[ClientSession, mt.InitializeResult]]
    ) -> None:
        stop = self._stop
        try:
            async with (
                self._transport.opened() as (read, write),
                ClientSession(read, write, client_info=self.CLIENT) as session,
            ):
                hello = await session.initialize()
                ready.set_result((session, hello))
                await stop.wait()
        except Exception as exc:
            if not ready.done():
                msg = (
                    f"mcp {self._transport.label()}: connecting and initializing "
                    f"failed: {self._causes.text(exc)}"
                )
                ready.set_exception(McpClientError(msg))
                return

            raise


class McpToolStubs:
    """Инструменты модели по списку инструментов MCP-сервера.

    Создаётся портом McpToolServer. Инструмент — заглушка со схемой сервера:
    модель видит имя, описание и аргументы, а исполняет вызов порт, не тело.
    """

    def __init__(self, server: str, prefix: str) -> None:
        self._server = server
        self._prefix = prefix

    def name_of(self, tool: mt.Tool) -> str:
        return f"{self._prefix}{tool.name}"

    def stub(self, tool: mt.Tool) -> BaseTool:
        description = tool.description
        if not description:
            description = tool.name

        return StructuredTool(
            name=self.name_of(tool),
            description=description,
            args_schema=dict(tool.inputSchema),
            coroutine=self._never_called,
        )

    async def _never_called(self, **kwargs: object) -> str:
        msg = (
            f"a tool of mcp server {self._server!r} is executed by the tool "
            "server port; its body must not be called"
        )
        raise RuntimeError(msg)


class McpResults:
    """Итог вызова MCP как сообщение инструмента истории.

    Создаётся портом McpToolServer. Результат с полями boba в _meta —
    итог сервера boba-dag: его модель оживает по kind. Результат любого
    другого сервера — текст его content: текстовые блоки подряд, остальные
    блоки названы одной строкой, structuredContent без текста — JSON.
    """

    def __init__(self) -> None:
        self._wire = ResultWire()
        self._causes = ErrorCauses()

    def message(self, call: ToolCall, result: mt.CallToolResult) -> ToolMessage:
        text = self._text(result)
        failed = bool(result.isError)
        artifact = self._artifact(result, text, failed)

        return CallReply(call).message(artifact, failed)

    def failed(self, call: ToolCall, error: Exception) -> ToolMessage:
        """Итог вызова, который до сервера не дошёл или остался без ответа."""
        failure = ErrorResult(
            message=(
                f"tool {call['name']!r} got no result from its mcp server: "
                f"{self._causes.text(error)}"
            ),
            error_kind=McpFailure.TRANSPORT,
        )

        return CallReply(call).message(failure, True)

    def _artifact(
        self, result: mt.CallToolResult, text: str, failed: bool
    ) -> ToolResultBase:
        own = self._own(result, text, failed)
        if own is not None:
            return own

        if failed:
            message = text
            if not message:
                message = "the mcp server reported an error without a text"

            return ErrorResult(message=message, error_kind=McpFailure.TOOL_ERROR)

        return MarkdownResult(text=text)

    def _own(
        self, result: mt.CallToolResult, text: str, failed: bool
    ) -> ToolResultBase | None:
        """Модель результата сервера boba-dag; None — сервер её не прислал
        либо прислал вид, которого клиент не знает."""
        meta = result.meta
        if not meta:
            return None

        if WireMeta.NAMESPACE.value not in meta:
            return None

        wire = WireResult(
            content=text,
            structured=result.structuredContent,
            is_error=failed,
            meta=meta,
        )
        try:
            return self._wire.revived(wire).artifact
        except ValidationError as exc:
            logger.warning(
                "mcp result of a known kind does not match its model, shown as "
                "text: %s",
                exc,
            )
            return None

    def _text(self, result: mt.CallToolResult) -> str:
        parts: list[str] = []
        for block in result.content:
            parts.append(self._block_text(block))

        text = "\n".join(part for part in parts if part)
        if text:
            return text

        if result.structuredContent is not None:
            return json.dumps(result.structuredContent, ensure_ascii=False, indent=2)

        return ""

    def _block_text(self, block: mt.ContentBlock) -> str:
        if isinstance(block, mt.TextContent):
            return block.text

        if isinstance(block, mt.ImageContent | mt.AudioContent):
            kind = type(block).__name__.removesuffix("Content").lower()

            return f"[{kind} {block.mimeType}, {len(block.data)} base64 chars]"

        if isinstance(block, mt.EmbeddedResource):
            return self._resource_text(block.resource)

        if isinstance(block, mt.ResourceLink):
            return f"[resource link {block.uri}]"

        return f"[{type(block).__name__}]"

    @staticmethod
    def _resource_text(
        resource: mt.TextResourceContents | mt.BlobResourceContents,
    ) -> str:
        if isinstance(resource, mt.TextResourceContents):
            return resource.text

        return f"[resource {resource.uri}]"


class McpToolServer(ToolServer):
    """Реализация порта ToolServer над одним MCP-сервером.

    Создаётся сборкой клиента из конфига сервера. open() подключается,
    проходит инициализацию и один раз читает список инструментов; после
    него tools() отдаёт заглушки для модели, submit() шлёт вызовы серверу.
    Возможности boba (features) берутся из экспериментальных возможностей,
    объявленных сервером при инициализации: у стороннего сервера их нет.
    Оборванная сессия открывается заново следующим вызовом.
    """

    FEATURE_PREFIX: ClassVar[str] = "com.boba/"

    def __init__(self, name: str, config: McpServerConfig) -> None:
        self._name = name
        self._config = config
        self._connection = McpConnection(config.endpoint, config.connect_timeout_sec)
        self._stubs = McpToolStubs(name, config.prefix)
        self._results = McpResults()
        self._tools: list[BaseTool] | None = None
        self._remote: dict[str, str] = {}
        """Имя инструмента у модели -> имя на сервере."""
        self._features: dict[str, Mapping[str, object]] = {}

    async def open(self) -> None:
        """Ошибки:
        McpClientError — сервер недоступен или не отдал список инструментов.
        """
        session, hello = await self._connection.open()
        self._features = self._declared(hello)

        try:
            listed = await self._list(session)
        except Exception as exc:
            msg = (
                f"mcp server {self._name!r} ({self._config.endpoint.label()}): "
                f"tools/list failed: {type(exc).__name__}: {exc}"
            )
            raise McpClientError(msg) from exc

        tools: list[BaseTool] = []
        remote: dict[str, str] = {}
        for tool in listed:
            stub = self._stubs.stub(tool)
            tools.append(stub)
            remote[stub.name] = tool.name

        self._tools = tools
        self._remote = remote
        logger.info(
            "mcp server %s: %d tools, features %s",
            self._name,
            len(tools),
            sorted(self._features),
        )

    async def close(self) -> None:
        await self._connection.close()

    def tools(self) -> Sequence[BaseTool]:
        if self._tools is None:
            msg = (
                f"mcp server {self._name!r}: tools() is called before open(), "
                "the tool list is not read yet"
            )
            raise McpClientError(msg)

        return self._tools

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._features

    async def submit(
        self, calls: Sequence[ToolCall]
    ) -> Sequence[asyncio.Future[ToolMessage]]:
        pending: list[asyncio.Future[ToolMessage]] = []
        for call in calls:
            pending.append(asyncio.ensure_future(self._called(call)))

        return pending

    async def _called(self, call: ToolCall) -> ToolMessage:
        remote = self._remote.get(call["name"], call["name"])
        try:
            session, _hello = await self._connection.open()
            result = await session.call_tool(
                remote,
                dict(call["args"]),
                read_timeout_seconds=timedelta(seconds=self._config.call_timeout_sec),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "mcp server %s: tool %s got no result: %s: %s",
                self._name,
                remote,
                type(exc).__name__,
                exc,
            )
            await self._connection.close()

            return self._results.failed(call, exc)

        return self._results.message(call, result)

    @staticmethod
    async def _list(session: ClientSession) -> list[mt.Tool]:
        """Все страницы tools/list."""
        listed: list[mt.Tool] = []
        cursor: str | None = None
        while True:
            page = await session.list_tools(cursor=cursor)
            listed.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                return listed

    def _declared(self, hello: mt.InitializeResult) -> dict[str, Mapping[str, object]]:
        """Возможности boba среди экспериментальных возможностей сервера."""
        experimental = hello.capabilities.experimental
        if not experimental:
            return {}

        declared: dict[str, Mapping[str, object]] = {}
        for feature, settings in experimental.items():
            if not feature.startswith(self.FEATURE_PREFIX):
                continue

            declared[feature] = dict(settings)

        return declared
