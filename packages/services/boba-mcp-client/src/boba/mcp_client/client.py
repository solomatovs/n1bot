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
import base64
import json
import logging
from abc import abstractmethod
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
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

from boba.access import ProfileGrant
from boba.canvas.journal import StreamSlice
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
    MarkdownResult,
    ToolResultBase,
)
from boba.toolkit.types import StringList
from boba.toolkit.wire import (
    JournalFeature,
    JournalRead,
    JournalSignal,
    ResultWire,
    WireMeta,
    WireResult,
)
from boba.toolrun.stream_calls import CallReply, ToolServer

__all__ = [
    "BearerAuth",
    "BlockFiles",
    "CallSignals",
    "DroppedSignals",
    "HttpEndpoint",
    "JournalAddress",
    "JournalAddresses",
    "JournalListener",
    "McpClientError",
    "McpServerConfig",
    "McpServers",
    "McpServersConfig",
    "McpToolServer",
    "NamedBlocks",
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


class McpServerConfig(ProfileGrant):
    """Один MCP-сервер в конфиге клиента: секция [mcp.servers.<имя>].

    Доступ задаётся на сервер целиком, грантом профиля: roles — роли,
    которым сервер виден ('*' — всем), profiles — профили чата, в которых
    он подключён ('*' — во всех), tools — какие инструменты сервера брать
    ('*' — все). Имена инструментов сервера до подключения неизвестны,
    поэтому в списки инструментов профилей они не вносятся. prefix
    добавляется к именам инструментов: им разводят серверы с совпавшими
    именами. Сроки — на подключение с инициализацией и на один вызов.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: McpEndpoint
    profiles: StringList
    prefix: str = ""
    connect_timeout_sec: float = Field(gt=0)
    call_timeout_sec: float = Field(gt=0)

    def granted(self, roles: Iterable[str], profile: str) -> bool:
        """Подключён ли сервер сессии с такими ролями и профилем."""
        if not self.visible_for(roles):
            return False

        if self.WILDCARD in self.profiles:
            return True

        return profile in self.profiles


class McpServersConfig(BaseModel):
    """Секция [mcp]: MCP-серверы клиента по именам."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    servers: Mapping[str, McpServerConfig]


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


ToolBody = Callable[..., Awaitable[tuple[str, ToolResultBase]]]
"""Тело инструмента MCP: аргументы вызова -> текст и модель результата."""

RemoteCall = Callable[
    [str, Mapping[str, object]], Awaitable[tuple[str, ToolResultBase]]
]
"""Вызов сервера: имя инструмента на сервере и аргументы -> итог."""


class McpToolBody:
    """Тело одного инструмента MCP-сервера для langchain.

    Создаётся портом McpToolServer на каждый инструмент списка. langchain
    зовёт called() с аргументами модели; тело передаёт их вызову сервера
    вместе с именем инструмента на сервере.
    """

    def __init__(self, remote: str, call: RemoteCall) -> None:
        self._remote = remote
        self._call = call

    async def called(self, **arguments: object) -> tuple[str, ToolResultBase]:
        return await self._call(self._remote, arguments)


class McpToolStubs:
    """Инструменты модели по списку инструментов MCP-сервера.

    Создаётся портом McpToolServer. Инструмент несёт имя, описание и схему
    аргументов сервера; его тело — вызов сервера, который даёт порт. Вызов
    идёт обычным путём инструмента langchain, поэтому лента клиента получает
    те же события шага, что и от своих инструментов.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def name_of(self, tool: mt.Tool) -> str:
        return f"{self._prefix}{tool.name}"

    def stub(self, tool: mt.Tool, body: ToolBody) -> BaseTool:
        description = tool.description
        if not description:
            description = tool.name

        return StructuredTool(
            name=self.name_of(tool),
            description=description,
            args_schema=dict(tool.inputSchema),
            coroutine=body,
            response_format="content_and_artifact",
        )


class BlockFiles(Protocol):
    """Куда клиент кладёт нетекстовые блоки результата: картинки, звук,
    вложенные файлы.

    Реализует хозяин клиента: чат сохраняет блок файлом в workspace треда и
    показывает вложением. Итог — строка для модели о том, где теперь файл.
    """

    @abstractmethod
    async def attached(self, call: ToolCall, index: int, mime: str, data: bytes) -> str:
        """Принять блок номер index результата вызова call."""


class NamedBlocks(BlockFiles):
    """Реализация BlockFiles без хранилища: блок только называется в тексте
    результата. Ей пользуется клиент, которому некуда класть файлы."""

    async def attached(self, call: ToolCall, index: int, mime: str, data: bytes) -> str:
        return f"[{mime}, {len(data)} bytes]"


class JournalAddress(BaseModel):
    """Где лежит журнал вызова: сервер клиента и запуск на нём.

    Клиент кладёт адрес в response_metadata сообщения инструмента: история
    хранит его вместе с итогом, и журнал читается после конца хода.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    KEY: ClassVar[str] = "boba_journal"

    server: str = Field(min_length=1)
    run: str = Field(min_length=1)

    def stamp(self, message: ToolMessage) -> None:
        message.response_metadata[self.KEY] = self.model_dump(mode="json")


class JournalAddresses:
    """Разбор адреса журнала из сообщения инструмента истории. Создаётся
    тем, кто читает журнал по истории (панель живого вывода чата)."""

    def of(self, message: ToolMessage) -> JournalAddress | None:
        """Адрес журнала вызова; None — вызов исполнял не сервер с журналом."""
        stamped = message.response_metadata.get(JournalAddress.KEY)
        if stamped is None:
            return None

        return JournalAddress.model_validate(stamped)


class LiveCall:
    """Вызов, чьё тело сейчас исполняется: сам вызов и запуск, которым его
    исполнил сервер (известен из итога). Создаётся портом на время вызова."""

    def __init__(self, call: ToolCall) -> None:
        self.call = call
        self.run = ""


class JournalListener(Protocol):
    """Приёмник сигналов роста журнала одного вызова."""

    @abstractmethod
    async def appended(self, signal: JournalSignal) -> None:
        """Канал журнала вызова дорос; зовётся из задачи сессии MCP."""


class CallSignals(Protocol):
    """Куда клиент отдаёт сигналы роста журнала вызовов сервера boba-dag.

    Реализует хозяин клиента: чат по сигналам ведёт панель живого вывода.
    listener() зовётся в контексте вызова до отправки запроса; сигналы
    приходят в задаче сессии MCP, где контекста вызова уже нет.
    """

    @abstractmethod
    def listener(self, server: str, call: ToolCall) -> JournalListener:
        """Приёмник сигналов вызова call сервера server."""


class DroppedSignals(CallSignals, JournalListener):
    """Реализация CallSignals без получателя: сигналы не нужны клиенту, у
    которого нет панели живого вывода; журнал при этом читается окнами."""

    def listener(self, server: str, call: ToolCall) -> JournalListener:
        return self

    async def appended(self, signal: JournalSignal) -> None:
        return


class CallProgress:
    """Уведомления notifications/progress одного вызова как сигналы журнала.

    Создаётся портом McpToolServer на вызов сервера, объявившего журнал.
    Сигнал — модель JournalSignal в поле message; уведомление без неё —
    обычный прогресс сервера, журнала оно не касается.
    """

    def __init__(self, listener: JournalListener) -> None:
        self._listener = listener

    async def __call__(
        self, progress: float, total: float | None, message: str | None
    ) -> None:
        if message is None:
            return

        try:
            signal = JournalSignal.model_validate_json(message)
        except ValidationError:
            return

        await self._listener.appended(signal)


class McpResults:
    """Итог вызова MCP как сообщение инструмента истории.

    Создаётся портом McpToolServer. Результат с полями boba в _meta —
    итог сервера boba-dag: его модель оживает по kind. Результат любого
    другого сервера — текст его content: текстовые блоки подряд, блоки-файлы
    (картинки, звук, вложенные ресурсы) уходят хозяину клиента (BlockFiles)
    и остаются в тексте его строкой, structuredContent без текста — JSON.
    """

    def __init__(self, files: BlockFiles) -> None:
        self._files = files
        self._wire = ResultWire()
        self._causes = ErrorCauses()

    async def parts(
        self, call: ToolCall, result: mt.CallToolResult
    ) -> tuple[str, ToolResultBase]:
        """Текст для модели и модель результата вызова call."""
        text = await self._text(call, result)
        artifact = self._artifact(result, text, bool(result.isError))

        return artifact.packed()

    def failed(self, call: ToolCall, error: Exception) -> tuple[str, ToolResultBase]:
        """Итог вызова, который до сервера не дошёл или остался без ответа."""
        failure = ErrorResult(
            message=(
                f"tool {call['name']!r} got no result from its mcp server: "
                f"{self._causes.text(error)}"
            ),
            error_kind=McpFailure.TRANSPORT,
        )

        return failure.packed()

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

    async def _text(self, call: ToolCall, result: mt.CallToolResult) -> str:
        parts: list[str] = []
        files = 0
        for block in result.content:
            payload = self._payload(block)
            if payload is None:
                parts.append(self._block_text(block))
                continue

            mime, data = payload
            parts.append(await self._files.attached(call, files, mime, data))
            files += 1

        text = "\n".join(part for part in parts if part)
        if text:
            return text

        if result.structuredContent is not None:
            return json.dumps(result.structuredContent, ensure_ascii=False, indent=2)

        return ""

    @staticmethod
    def _payload(block: mt.ContentBlock) -> tuple[str, bytes] | None:
        """Тип и байты блока-файла; None — блок текстовый или ссылка."""
        if isinstance(block, mt.ImageContent | mt.AudioContent):
            return block.mimeType, base64.b64decode(block.data)

        if not isinstance(block, mt.EmbeddedResource):
            return None

        resource = block.resource
        if not isinstance(resource, mt.BlobResourceContents):
            return None

        mime = resource.mimeType
        if not mime:
            mime = "application/octet-stream"

        return mime, base64.b64decode(resource.blob)

    @staticmethod
    def _block_text(block: mt.ContentBlock) -> str:
        if isinstance(block, mt.TextContent):
            return block.text

        if isinstance(block, mt.EmbeddedResource):
            resource = block.resource
            if isinstance(resource, mt.TextResourceContents):
                return resource.text

        if isinstance(block, mt.ResourceLink):
            return f"[resource link {block.uri}]"

        return f"[{type(block).__name__}]"


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

    META_CALL_ID: ClassVar[str] = "boba/tool_call_id"
    """Ключ _meta запроса с идентификатором вызова модели."""

    def __init__(
        self,
        name: str,
        config: McpServerConfig,
        files: BlockFiles,
        signals: CallSignals,
    ) -> None:
        self._name = name
        self._signals = signals
        self._config = config
        self._connection = McpConnection(config.endpoint, config.connect_timeout_sec)
        self._stubs = McpToolStubs(config.prefix)
        self._calls: ContextVar[LiveCall] = ContextVar(f"mcp_call_{name}")
        """Вызов, чьё тело сейчас исполняется: телу нужен его id."""
        self._wire = ResultWire()
        self._by_name: dict[str, BaseTool] = {}
        """Имя инструмента у модели -> инструмент."""
        self._results = McpResults(files)
        self._tools: list[BaseTool] | None = None
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
        remote: dict[str, BaseTool] = {}
        for tool in listed:
            if not self._config.covers(tool.name):
                continue

            stub = self._stubs.stub(tool, McpToolBody(tool.name, self._body).called)
            tools.append(stub)
            remote[stub.name] = stub

        self._tools = tools
        self._by_name = remote
        logger.info(
            "mcp server %s: %d tools, features %s",
            self._name,
            len(tools),
            sorted(self._features),
        )

    async def close(self) -> None:
        await self._connection.close()

    @property
    def opened(self) -> bool:
        """Список инструментов прочитан: порт можно отдавать модели."""
        return self._tools is not None

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
        """Вызов инструмента обычным путём langchain: его события получает
        лента клиента. Итог тела — модель результата; сбой — модель отказа."""
        tool = self._by_name.get(call["name"])
        if tool is None:
            failure = ErrorResult(
                message=f"mcp server {self._name!r} has no tool {call['name']!r}",
                error_kind=McpFailure.TOOL_ERROR,
            )

            return CallReply(call).message(failure, True)

        live = LiveCall(call)
        token = self._calls.set(live)
        try:
            message = await tool.ainvoke(call)
        finally:
            self._calls.reset(token)

        artifact = message.artifact
        if not isinstance(artifact, ToolResultBase):
            artifact = MarkdownResult(text=str(message.content))

        reply = CallReply(call).message(artifact, isinstance(artifact, FailureResult))
        if live.run:
            JournalAddress(server=self._name, run=live.run).stamp(reply)

        return reply

    async def _body(
        self, remote: str, arguments: Mapping[str, object]
    ) -> tuple[str, ToolResultBase]:
        """Тело инструмента: запрос tools/call серверу от имени текущего вызова."""
        live = self._calls.get()
        call = live.call
        try:
            session, _hello = await self._connection.open()
            result = await session.send_request(
                mt.ClientRequest(self._request(remote, call, arguments)),
                mt.CallToolResult,
                request_read_timeout_seconds=timedelta(
                    seconds=self._config.call_timeout_sec
                ),
                progress_callback=self._progress(call),
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

        if result.meta:
            live.run = self._wire.run_of(result.meta)

        return await self._results.parts(call, result)

    def _progress(self, call: ToolCall) -> CallProgress | None:
        """Приёмник прогресса вызова; None — сервер журнал не объявлял, и
        токен прогресса ему не шлётся."""
        if JournalFeature.ID.value not in self._features:
            return None

        return CallProgress(self._signals.listener(self._name, call))

    async def journal(self, request: JournalRead) -> StreamSlice | None:
        """Окно журнала вызова с сервера; None — сервер журнал не объявлял
        либо такого журнала у него нет.

        Ошибки:
        McpClientError — сервер недоступен или ответил не окном журнала.
        """
        feature = self._features.get(JournalFeature.ID.value)
        if feature is None:
            return None

        tool = str(feature.get(JournalFeature.READ.value))
        arguments = request.model_dump(mode="json", exclude_none=True)
        try:
            session, _hello = await self._connection.open()
            result = await session.call_tool(
                tool,
                arguments,
                read_timeout_seconds=timedelta(seconds=self._config.call_timeout_sec),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            msg = (
                f"mcp server {self._name!r}: reading the journal of call "
                f"{request.node!r} in run {request.run!r} with {tool!r} failed: "
                f"{type(exc).__name__}: {exc}"
            )
            raise McpClientError(msg) from exc

        if result.isError:
            return None

        try:
            return StreamSlice.model_validate(result.structuredContent)
        except ValidationError as exc:
            msg = (
                f"mcp server {self._name!r}: {tool!r} answered call "
                f"{request.node!r} of run {request.run!r} with something other "
                f"than a journal window: {exc}"
            )
            raise McpClientError(msg) from exc

    def _request(
        self, remote: str, call: ToolCall, sent: Mapping[str, object]
    ) -> mt.CallToolRequest:
        """Запрос tools/call. Серверу, объявившему возможности boba, в _meta
        едет идентификатор вызова модели: его журнал и итог несут тот же id."""
        arguments = dict(sent)
        call_id = call["id"]
        if not self._features or not call_id:
            params = mt.CallToolRequestParams(name=remote, arguments=arguments)

            return mt.CallToolRequest(method="tools/call", params=params)

        params = mt.CallToolRequestParams(
            name=remote,
            arguments=arguments,
            _meta=mt.RequestParams.Meta.model_validate({self.META_CALL_ID: call_id}),
        )

        return mt.CallToolRequest(method="tools/call", params=params)

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


class McpServers:
    """MCP-серверы клиента: подключение на старте и порты для сессии.

    Создаётся сборкой приложения из секции [mcp]. start() подключает все
    серверы; сервер, который не ответил, приложение не роняет — он
    подключается при первой сессии, которой он положен. for_session()
    отдаёт порты серверов, доступных ролям и профилю сессии.
    """

    def __init__(
        self, config: McpServersConfig, files: BlockFiles, signals: CallSignals
    ) -> None:
        self._configs = dict(config.servers)
        self._servers: dict[str, McpToolServer] = {}
        for name, server in config.servers.items():
            self._servers[name] = McpToolServer(name, server, files, signals)

    async def journal(self, server: str, request: JournalRead) -> StreamSlice | None:
        """Окно журнала вызова с сервера server; None — такого сервера или
        журнала нет.

        Ошибки:
        McpClientError — сервер недоступен или ответил не окном журнала.
        """
        port = self._servers.get(server)
        if port is None:
            return None

        return await port.journal(request)

    def journaled(self) -> frozenset[str]:
        """Имена инструментов подключённых серверов, которые ведут журнал
        вызовов: у их шагов есть живой вывод."""
        names: set[str] = set()
        for server in self._servers.values():
            if not server.opened:
                continue

            if JournalFeature.ID.value not in server.features():
                continue

            for tool in server.tools():
                names.add(tool.name)

        return frozenset(names)

    async def start(self) -> None:
        for name, config in self._configs.items():
            # сервер, не выданный ни одному профилю или роли, не подключается
            if not config.profiles:
                continue

            if not config.roles:
                continue

            await self._open(name)

    async def stop(self) -> None:
        for server in self._servers.values():
            await server.close()

    async def for_session(
        self, roles: Iterable[str], profile: str
    ) -> Sequence[ToolServer]:
        granted = frozenset(roles)
        ports: list[ToolServer] = []
        for name, server in self._servers.items():
            if not self._configs[name].granted(granted, profile):
                continue

            if not server.opened:
                await self._open(name)

            if server.opened:
                ports.append(server)

        return ports

    async def _open(self, name: str) -> None:
        """Подключение сервера; сбой журналируется и сервер остаётся без порта
        до следующей попытки."""
        try:
            await self._servers[name].open()
        except McpClientError as exc:
            logger.error("mcp server %s is unavailable: %s", name, exc)
