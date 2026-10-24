"""MCP-сервер сервиса: список инструментов по ролям и вызовы через порт.

Сервер поднят в процессе теста на настоящем порту; клиент — fastmcp Client
по streamable HTTP с токеном. Инструменты — фейки стенда под той же
цепочкой обвязок и исполнителем субпроцессом, что в приложении.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import re
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
import mcp_types as mt
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.client.extension import ClientExtension, NotificationBinding, advertise
from mcp.client.session import IncomingMessage
from mcp.client.subscriptions import listen
from mcp.server.apps import APP_MIME_TYPE
from mcp.server.auth.provider import IdentityAssertionParams, TokenError
from mcp.shared.auth import JWT_BEARER_GRANT_TYPE, OAuthClientInformationFull
from mcp.shared.exceptions import MCPError
from pydantic import AnyUrl
from service_stand import (
    CLIENT_ID,
    CLIENT_SECRET,
    LOCAL_LOGIN,
    LOCAL_PASSWORD,
    NARROW,
    PROFILE,
    PROXY_SECRET,
    ServiceStand,
)

from boba.auth import SignIns
from boba.auth.proxy import ProxyAssertions
from boba.canvas.journal import StreamSlice
from boba.connections.sealed import (
    ConnectionOffer,
    ConnectionSeal,
    SealedConnection,
    SealedParamSchema,
    SealFeature,
)
from boba.mcp_adapter import JournalUris
from boba.mcp_server.auth import ServiceMark
from boba.mcp_server.files import JournalRoutes
from boba.mcp_server.server import (
    FileUploadTool,
    RunLimits,
)
from boba.oauth.server import (
    AuthServer,
    RegisteredClients,
    SealedValues,
    ServiceTokens,
)
from boba.toolkit.calls import CallViews, SchemaMark
from boba.toolkit.channels import JournalChannels, ToolChannel
from boba.toolkit.dag import (
    CallDag,
    NestedCallsFeature,
    NestedCallsOutput,
    NodeStatus,
)
from boba.toolkit.mcp import McpResults
from boba.toolkit.wire import (
    CommandsFeature,
    CommandsVersion,
    FilesFeature,
    JournalFeature,
    JournalNotice,
    JournalRead,
    ResultWire,
    ScopeFeature,
)
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import ToolSchema
from boba.toolrun.stream_calls import WorkflowNodeCall, WorkflowTool

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def stand(tmp_path: Path, port: int) -> ServiceStand:
    return ServiceStand(tmp_path, RunLimits(max_runs=4, max_waiting=4), port, "")


@pytest.fixture
async def url(stand: ServiceStand) -> AsyncIterator[str]:
    async with stand.serving():
        yield stand.url(PROFILE)


@pytest.fixture
async def dev_token(stand: ServiceStand, url: str) -> str:
    """Токен пользователя alice с ролью dev для endpoint'а стенда."""
    return await stand.dev_token(PROFILE)


@pytest.fixture
async def weak_token(stand: ServiceStand, url: str) -> str:
    """Токен пользователя bob с ролью weak для endpoint'а стенда."""
    return await stand.weak_token(PROFILE)


def _client(url: str, token: str) -> Client[Any]:
    return Client(StreamableHttpTransport(url, auth=token))


def _shown(result: mt.CallToolResult) -> dict[str, Any] | None:
    """Модель ответа операции сервиса в structuredContent."""
    return result.structured_content


async def _window(client: Client[Any], read: JournalRead) -> StreamSlice:
    """Окно канала журнала resources/read по шаблону ресурса журнала: текст
    и координаты окна из _meta содержимого."""
    uri = JournalUris(JournalRoutes.TEMPLATE).uri(read)
    contents = await client.read_resource(uri)
    if len(contents) != 1:
        raise AssertionError(f"a window is one text content: {contents}")

    (content,) = contents
    if not isinstance(content, mt.TextResourceContents):
        raise AssertionError(f"a window is text: {content}")

    meta = content.meta
    if meta is None:
        raise AssertionError(f"the window names its place in _meta: {content}")

    return StreamSlice.model_validate({**meta[JournalFeature.ID], "text": content.text})


def _text(result: mt.CallToolResult) -> str:
    """Текст итога инструмента, каким его получает модель клиента."""
    return McpResults().result("probe", "call", result).llm_view()


def _journal(result: mt.CallToolResult) -> JournalRead:
    """Адрес канала stdout журнала вызова по ссылке в итоге."""
    uris = JournalUris(JournalRoutes.TEMPLATE)
    for block in result.content:
        if not isinstance(block, mt.ResourceLink):
            continue

        read = uris.address(str(block.uri))
        if read is None:
            continue

        if read.channel is ToolChannel.STDOUT:
            return read

    raise AssertionError(f"the result links the stdout of its journal: {result}")


class TestToolList:
    async def test_caller_sees_the_tools_of_the_port(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            listed = await client.list_tools()

        # клиенту без запечатывания endpoint соединений не предлагает
        names = sorted(tool.name for tool in listed)
        expected = [
            "fake_collect",
            "fake_echo",
            "fake_emit",
            "fake_progress",
            "fake_query",
            "fake_scope",
            "fake_sleep",
            "fake_whoami",
            FileUploadTool.NAME,
            WorkflowTool.NAME,
        ]
        if names != sorted(expected):
            raise AssertionError(f"the list is the port's tools: {names}")

    async def test_schema_is_the_call_schema_without_service_fields(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            listed = await client.list_tools()

        echo = next(tool for tool in listed if tool.name == "fake_echo")
        own = next(tool for tool in stand.registry.tools if tool.name == "fake_echo")
        card = ToolSchema().card_of(own)
        if echo.input_schema != CallViews().typed(card.parameters, card.views):
            raise AssertionError(f"the schema is published as is: {echo.input_schema}")

        properties = echo.input_schema["properties"]
        if "cfg" in properties:
            raise AssertionError("the injected config is not shown to the client")

    async def test_scope_parameter_is_marked_in_the_tools_of_a_workspace(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Инструменты, которые работают в workspace, и связка несут
        необязательный scope_id с пометкой x-boba-scope, которую сервер
        объявил расширением области вызова."""
        async with _client(url, dev_token) as client:
            listed = await client.list_tools()

        schemas = {tool.name: tool.input_schema for tool in listed}
        for name in ("fake_scope", "fake_echo", WorkflowTool.NAME, FileUploadTool.NAME):
            marked = SchemaMark.SCOPE.params(schemas[name])
            if marked != {CallFields.SCOPE: True}:
                raise AssertionError(f"{name} carries the scope: {schemas[name]}")

            if CallFields.SCOPE in schemas[name].get("required", []):
                raise AssertionError(f"the scope of {name} is optional")

    async def test_weaker_role_sees_fewer_tools(
        self, stand: ServiceStand, url: str, weak_token: str
    ) -> None:
        async with _client(url, weak_token) as client:
            listed = await client.list_tools()

        names = [tool.name for tool in listed]
        if names != ["fake_echo", FileUploadTool.NAME]:
            raise AssertionError(f"role weak is granted one tool: {names}")

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "no tool of the service declares an app: the stand has no fake_sales, "
            "ToolCard and ToolHints carry no _meta.ui, McpTool passes no meta to "
            "fastmcp and the service serves no ui:// resources (plan items 6-7)"
        ),
    )
    async def test_tool_declares_its_app_and_the_server_serves_the_renderer(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Инструмент стенда fake_sales объявлен с видом prefab: tools/list
        несёт _meta.ui.resourceUri, ресурс читается типом приложения и его
        CSP не называет внешних доменов."""
        async with _client(url, dev_token) as client:
            listed = await client.list_tools()
            drawn = [tool.name for tool in listed if self._resource_of(tool)]
            if "fake_sales" not in drawn:
                raise AssertionError(
                    f"fake_sales declares its app in _meta.ui: {drawn}"
                )

            sales = next(tool for tool in listed if tool.name == "fake_sales")
            read = await client.read_resource(self._resource_of(sales))

        page = read[0]
        if page.mime_type != APP_MIME_TYPE:
            raise AssertionError(f"the resource is an app page: {page.mime_type}")

        if not isinstance(page, mt.TextResourceContents):
            raise AssertionError(f"the resource is text: {type(page).__name__}")

        if "<html" not in page.text:
            raise AssertionError(f"the resource is the renderer: {page.text[:200]}")

        csp = (page.meta or {}).get("ui", {}).get("csp", {})
        for domains in csp.values():
            for domain in domains:
                host = str(urlsplit(domain).hostname)
                if host.endswith(("jsdelivr.net", "googleapis.com", "gstatic.com")):
                    raise AssertionError(f"the CSP names no CDN: {csp}")

    @staticmethod
    def _resource_of(tool: mt.Tool) -> str:
        """Адрес ресурса приложения инструмента из _meta.ui; пусто — нет."""
        meta = tool.meta or {}
        ui = meta.get("ui")
        if not isinstance(ui, dict):
            return ""

        return str(ui.get("resourceUri", ""))


class TestCall:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "no result kind carries a Prefab view: the stand has no fake_sales tool "
            "and the result family has no AppResult whose sent() puts view and "
            "state into structuredContent (plan items 8-9)"
        ),
    )
    async def test_sandboxed_tool_returns_a_prefab_view(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Тело fake_sales отвечает AppResult с таблицей: tools/call несёт view
        в structuredContent, content — текст для модели."""
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_sales", {"region": "North"})

        shown = _shown(result)
        if shown is None or "view" not in shown:
            raise AssertionError(f"the result carries the view: {shown}")

        if _text(result) != "2 sales rows are shown by their table":
            raise AssertionError(
                f"the content is the text for the model: {_text(result)}"
            )

    async def test_result_carries_text_model_and_status(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "hi", "repeat": 2}
            )

        if result.is_error:
            raise AssertionError(f"the call succeeds: {result}")

        if _text(result) != "hi hi|t0ken":
            raise AssertionError(f"the body got its config: {result}")

        own = [key for key in result.meta or {} if "boba" in key]
        if own != [ResultWire.ELAPSED]:
            raise AssertionError(f"the service puts only the elapsed time: {own}")

    async def test_body_sees_the_caller_of_the_token(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_whoami", {})

        if _text(result) != f"alice|dev|{PROFILE}":
            raise AssertionError(f"the body is called as the token's caller: {result}")

    async def test_server_names_the_call_itself(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Идентификатор вызова выдаёт сервер и отдаёт его в итоге вместе с
        запуском: по этой паре клиент читает журнал вызова."""
        async with _client(url, dev_token) as client:
            first = await client.call_tool_mcp("fake_echo", {"text": "a", "repeat": 1})
            second = await client.call_tool_mcp("fake_echo", {"text": "b", "repeat": 1})

        one = _journal(first)
        two = _journal(second)
        if one.node == two.node:
            raise AssertionError(f"every call gets its own id: {one} and {two}")

        if not one.run or one.run == two.run:
            raise AssertionError(f"every call gets its own run: {one} and {two}")

    async def test_scope_of_the_client_is_the_scope_of_the_call(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Область из аргумента scope_id доходит до тела областью вызова:
        вызовы одной области видят одни и те же файлы."""
        scope = "0b6f6f0e-51d4-4a4b-9f6c-1d6c5f1f7a10"
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_scope", {CallFields.SCOPE: scope})

        if _text(result) != scope:
            raise AssertionError(f"the body runs in the client's scope: {result}")

    async def test_call_without_a_scope_runs_in_the_own_scope_of_the_user(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Клиент без области получает область своего пользователя: её id —
        id пользователя, поэтому у двух пользователей она разная."""
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_scope", {})

        own = str(uuid5(NAMESPACE_URL, "boba-mcp:alice"))
        if _text(result) != own:
            raise AssertionError(f"the default scope is the user's own: {result}")

    async def test_scope_that_is_not_a_path_segment_is_refused(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_scope", {CallFields.SCOPE: "a/b"})

        if not result.is_error:
            raise AssertionError(f"a scope with a separator is refused: {result}")

    async def test_calls_of_one_scope_run_side_by_side(
        self, stand: ServiceStand, url: str, tmp_path: Path, dev_token: str
    ) -> None:
        """Вызовы одной области не вытесняют друг друга: у каждого свой запуск."""
        scope = "0b6f6f0e-51d4-4a4b-9f6c-1d6c5f1f7a11"

        async def slept(name: str) -> Any:
            arguments = {
                "seconds": 1.0,
                "marker": str(tmp_path / name),
                CallFields.SCOPE: scope,
            }
            async with _client(url, dev_token) as client:
                return await client.call_tool_mcp("fake_sleep", arguments)

        first, second = await asyncio.gather(slept("first"), slept("second"))
        if first.is_error or second.is_error:
            raise AssertionError(f"both calls of the scope finish: {first} {second}")

    async def test_server_declares_its_features_on_connect(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Возможности объявлены расширениями: клиент узнаёт ключ
        запечатывания, инструмент-связку, операцию журнала и редакцию
        расширения команд при согласовании протокола, не вызывая
        инструментов."""
        async with _client(url, dev_token) as client:
            capabilities = client.session.server_capabilities

        if capabilities is None:
            raise AssertionError("the protocol is negotiated on connect")

        declared = capabilities.extensions
        if declared is None:
            raise AssertionError(f"the server declares extensions: {capabilities}")

        for feature in (
            SealFeature.ID,
            FilesFeature.ID,
            JournalFeature.ID,
            NestedCallsFeature.ID,
            ScopeFeature.ID,
            CommandsFeature.ID,
        ):
            if feature not in declared:
                raise AssertionError(f"{feature} is declared: {sorted(declared)}")

        commands = CommandsFeature.model_validate(declared[CommandsFeature.ID])
        if commands.version is not CommandsVersion.V2026_10_24:
            raise AssertionError(
                f"the commands extension names its edition: {commands}"
            )

        scoping = ScopeFeature.model_validate(declared[ScopeFeature.ID])
        if scoping.mark != SchemaMark.SCOPE.value:
            raise AssertionError(f"the scope extension names the mark: {scoping}")

        nested = NestedCallsFeature.model_validate(declared[NestedCallsFeature.ID])
        if nested != CallDag().feature():
            raise AssertionError(f"nested calls declare the workflow: {nested}")

    async def test_nested_calls_extension_is_the_workflow_schema(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Форма узла, объявленная расширением вложенных вызовов, — те же
        поля, что в схеме инструмента-связки для модели: модель и клиент
        разбирают одну форму."""
        async with _client(url, dev_token) as client:
            capabilities = client.session.server_capabilities
            listed = await client.list_tools()

        if capabilities is None:
            raise AssertionError("the protocol is negotiated on connect")

        declared = capabilities.extensions
        if declared is None:
            raise AssertionError(f"the server declares extensions: {capabilities}")

        nested = NestedCallsFeature.model_validate(declared[NestedCallsFeature.ID])
        schemas: dict[str, dict[str, Any]] = {}
        for tool in listed:
            schemas[tool.name] = tool.input_schema

        node_schema = schemas[nested.tool]["$defs"][WorkflowNodeCall.__name__]

        fields = sorted(node_schema["properties"])
        expected = sorted([nested.node_key, nested.node_tool, nested.node_args])
        if fields != expected:
            raise AssertionError(f"the node fields are the declared ones: {fields}")

    async def test_linked_nodes_run_as_one_workflow_call(
        self, stand: ServiceStand, url: str, tmp_path: Path, dev_token: str
    ) -> None:
        nodes = [
            {
                "key": "src",
                "tool": "fake_emit",
                "args": {
                    "prefix": "a",
                    "count": 3,
                    "size": 0,
                    "fail_midway": False,
                    "out": "c1",
                },
            },
            {
                "key": "dst",
                "tool": "fake_collect",
                "args": {
                    "marker": str(tmp_path / "done"),
                    "fail": False,
                    "gated": False,
                    "feed": "c1",
                },
            },
        ]
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(WorkflowTool.NAME, {"nodes": nodes})

        if result.is_error:
            raise AssertionError(f"the group succeeds: {result}")

        reply = NestedCallsOutput.model_validate(result.structured_content)

        keys: list[str] = []
        for node in reply.nodes:
            keys.append(node.key)
            heading = result.content[node.blocks[0] - 1]
            expected = mt.TextContent(text=f"[{node.key}] {node.tool} — ok")
            if heading != expected:
                raise AssertionError(f"the blocks of {node.key} follow its heading")

        if keys != ["src", "dst"]:
            raise AssertionError(f"every node reports its outcome in order: {keys}")

        texts: list[str] = []
        for block in result.content:
            if isinstance(block, mt.TextContent):
                texts.append(block.text)

        if texts[0] != "[src] fake_emit — ok":
            raise AssertionError(f"the content opens with the first heading: {texts}")

    async def test_failed_node_fails_the_call_and_only_its_status(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Узлы без общих каналов — разные группы: упал один, у другого
        статус ok, а вызов — сбой по MCP."""
        nodes = [
            {"key": "good", "tool": "fake_echo", "args": {"text": "hi", "repeat": 1}},
            {"key": "bad", "tool": "fake_echo", "args": {"text": "boom", "repeat": 1}},
        ]
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(WorkflowTool.NAME, {"nodes": nodes})

        if not result.is_error:
            raise AssertionError(f"a failed node fails the call: {result}")

        reply = NestedCallsOutput.model_validate(result.structured_content)
        statuses: dict[str, NodeStatus] = {}
        for node in reply.nodes:
            statuses[node.key] = node.status

        if statuses != {"good": NodeStatus.OK, "bad": NodeStatus.FAILED}:
            raise AssertionError(f"only the failed node is failed: {statuses}")

    async def test_workflow_alone_declares_an_output_schema(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            listed = await client.list_tools()

        declared: dict[str, dict[str, Any] | None] = {}
        for tool in listed:
            declared[tool.name] = tool.output_schema

        if declared[WorkflowTool.NAME] != NestedCallsOutput.model_json_schema():
            raise AssertionError(f"workflow returns NestedCallsOutput: {declared}")

        others: list[str] = []
        for name, schema in declared.items():
            if name == WorkflowTool.NAME:
                continue

            if schema is not None:
                others.append(name)

        if others:
            raise AssertionError(f"other tools declare no output schema: {others}")


class Heard:
    """Уведомления прогресса одного вызова, какими их получил клиент: числа
    и текст отчётов тела — через обработчик прогресса, уведомление целиком с
    _meta — через обработчик сообщений сессии (notified)."""

    def __init__(self) -> None:
        self.reports: list[tuple[float, float | None, str | None]] = []
        self.notes: list[mt.ProgressNotificationParams] = []

    async def __call__(
        self, progress: float, total: float | None, message: str | None
    ) -> None:
        if message is None:
            return

        self.reports.append((progress, total, message))

    async def notified(self, message: IncomingMessage) -> None:
        if isinstance(message, mt.ProgressNotification):
            self.notes.append(message.params)


class JournalNotices(ClientExtension):
    """Расширение клиента в тесте: слушает уведомления сервера об открытых
    журналах вызова и объявляется под идентификатором identifier — журнала
    либо чужим, которого сервер не знает."""

    def __init__(self, identifier: str) -> None:
        self.identifier = identifier
        self.heard: list[JournalNotice] = []

    def notifications(self) -> list[NotificationBinding[Any]]:
        binding = NotificationBinding(
            method=JournalFeature.OPENED, params_type=JournalNotice, handler=self._heard
        )

        return [binding]

    async def _heard(self, notice: JournalNotice) -> None:
        self.heard.append(notice)


class TestProgress:
    """Прогресс вызова — только то, что сообщил сам инструмент: стандартными
    уведомлениями notifications/progress, числами и текстом."""

    async def test_reports_of_the_body_reach_the_client(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        heard = Heard()
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_progress", {"steps": 3, "pause": 0.6}, progress_handler=heard
            )

        if result.is_error:
            raise AssertionError(f"the call succeeds: {result}")

        expected = [
            (1.0, 3.0, "step 1 of 3"),
            (2.0, 3.0, "step 2 of 3"),
            (3.0, 3.0, "step 3 of 3"),
        ]
        if heard.reports != expected:
            raise AssertionError(
                f"every report arrives as it was made: {heard.reports}"
            )

    async def test_frequent_reports_are_thinned_and_the_last_one_arrives(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        heard = Heard()
        async with _client(url, dev_token) as client:
            await client.call_tool_mcp(
                "fake_progress", {"steps": 20, "pause": 0.01}, progress_handler=heard
            )

        if len(heard.reports) >= 20:
            raise AssertionError(f"reports are rate limited: {len(heard.reports)}")

        if heard.reports[-1] != (20.0, 20.0, "step 20 of 20"):
            raise AssertionError(f"the final report is delivered: {heard.reports}")

        values = [report[0] for report in heard.reports]
        if values != sorted(set(values)):
            raise AssertionError(f"the progress only grows: {values}")

    async def test_reports_of_workflow_nodes_are_signed_by_their_node(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Связка идёт одним запросом: ключ узла каждого отчёта лежит в _meta
        уведомления под ключом, объявленным расширением, текст — без
        подписи; отчёты узлов не теряются друг из-за друга, а значение
        прогресса запроса только растёт."""
        nodes = [
            {
                "key": "left",
                "tool": "fake_progress",
                "args": {"steps": 3, "pause": 0.6},
            },
            {
                "key": "right",
                "tool": "fake_progress",
                "args": {"steps": 2, "pause": 0.6},
            },
        ]
        heard = Heard()
        client = Client(
            StreamableHttpTransport(url, auth=dev_token),
            message_handler=heard.notified,
        )
        async with client:
            result = await client.call_tool_mcp(
                WorkflowTool.NAME, {"nodes": nodes}, progress_handler=heard
            )

        if result.is_error:
            raise AssertionError(f"the workflow succeeds: {result}")

        key = CallDag().feature().progress_key
        by_node: dict[str, list[str]] = {}
        for note in heard.notes:
            meta = note.meta
            if meta is None:
                raise AssertionError(f"every report names its node in _meta: {note}")

            if note.message is None:
                raise AssertionError(f"every notification is a report: {note}")

            by_node.setdefault(meta[key], []).append(str(note.message))

        expected = {
            "left": [
                "step 1 of 3 (1 of 3)",
                "step 2 of 3 (2 of 3)",
                "step 3 of 3 (3 of 3)",
            ],
            "right": ["step 1 of 2 (1 of 2)", "step 2 of 2 (2 of 2)"],
        }
        if by_node != expected:
            raise AssertionError(f"the reports of the nodes: {by_node}")

        values = [report[0] for report in heard.reports]
        if values != sorted(set(values)):
            raise AssertionError(f"the progress of the request only grows: {values}")

    async def test_running_call_journal_is_noticed_in_the_stream_of_its_call(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Клиенту с расширением журнала сервер сообщает об открытом журнале
        уведомлением расширения в потоке ответа самого вызова, под токеном
        прогресса запроса: адрес тот же, что у ссылок итога."""
        notices = JournalNotices(JournalFeature.ID)
        transport = StreamableHttpTransport(url, auth=dev_token)
        heard = Heard()
        async with Client(transport, extensions=[notices]) as client:
            result = await client.call_tool_mcp(
                "fake_progress", {"steps": 3, "pause": 0.5}, progress_handler=heard
            )

        if result.is_error:
            raise AssertionError(f"the call succeeds: {result}")
        if len(notices.heard) != 1:
            raise AssertionError(f"the journal is noticed once: {notices.heard}")
        notice = notices.heard[0]
        stdout = _journal(result)
        if (notice.run, notice.node, notice.key) != (stdout.run, stdout.node, None):
            raise AssertionError(f"the address of the result: {notice} {stdout}")
        if notice.channels != JournalChannels.VISIBLE:
            raise AssertionError(f"the channels for the user: {notice}")

    async def test_client_without_the_journal_extension_is_not_noticed(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Клиент, который расширения журнала не объявил, уведомлений о нём
        не получает, даже если умеет их слушать: ему — путь ядра, ссылки на
        каналы журнала в итоге."""
        notices = JournalNotices("com.boba/listener")
        transport = StreamableHttpTransport(url, auth=dev_token)
        heard = Heard()
        async with Client(transport, extensions=[notices]) as client:
            result = await client.call_tool_mcp(
                "fake_progress", {"steps": 2, "pause": 0.5}, progress_handler=heard
            )

        if result.is_error:
            raise AssertionError(f"the call succeeds: {result}")
        if notices.heard:
            raise AssertionError(f"no notice without the extension: {notices.heard}")
        if len(heard.reports) != 2:
            raise AssertionError(f"the progress still goes: {heard.reports}")
        _journal(result)

    async def test_running_call_journals_are_listed_as_resources(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Путь ядра для клиента без расширения журнала: журнал идущего
        вызова — ресурсы resources/list вызывающего по видимым каналам;
        поток subscriptions/listen узнаёт об открытии и о конце смены
        списка; вызов кончился — журналов в списке нет, а адрес тот же, что
        у ссылок итога."""
        async with _client(url, dev_token) as client:
            async with listen(client.session, resources_list_changed=True) as stream:
                running = asyncio.create_task(
                    client.call_tool_mcp("fake_progress", {"steps": 4, "pause": 0.5})
                )
                async with asyncio.timeout(30):
                    changed = await anext(stream)
                    listed = await client.list_resources_mcp()

                result = await running
                async with asyncio.timeout(30):
                    ended = await anext(stream)

            after = await client.list_resources_mcp()

        uris = JournalUris(JournalRoutes.TEMPLATE)
        addresses: list[JournalRead] = []
        for resource in listed.resources:
            read = uris.address(str(resource.uri))
            if read is None:
                raise AssertionError(f"{resource.uri} is a journal channel")

            addresses.append(read)

        if type(changed).__name__ != "ResourcesListChanged":
            raise AssertionError(f"the open journal changes the list: {changed}")
        if type(ended).__name__ != "ResourcesListChanged":
            raise AssertionError(f"the end of the call changes the list: {ended}")
        for resource in listed.resources:
            if resource.annotations != mt.Annotations(audience=["user"]):
                raise AssertionError(f"a journal is for the user: {resource}")

        channels = [read.channel for read in addresses]
        if channels != list(JournalChannels.VISIBLE):
            raise AssertionError(f"every visible channel is listed: {channels}")
        for resource in listed.resources:
            meta = resource.meta
            if meta is not None and JournalFeature.ID in meta:
                raise AssertionError(f"a listed journal names no request: {resource}")
        stdout = _journal(result)
        if addresses[0].model_copy(update={"channel": stdout.channel}) != stdout:
            raise AssertionError(f"the address of the result: {addresses} {stdout}")
        if after.resources:
            raise AssertionError(f"a finished call lists no journal: {after}")

    async def test_journal_list_change_reaches_only_its_caller(
        self, stand: ServiceStand, url: str, dev_token: str, weak_token: str
    ) -> None:
        """Смена списка журналов — событие потоков того же пользователя:
        поток другого пользователя её не слышит."""
        async with (
            _client(url, dev_token) as owner,
            _client(url, weak_token) as stranger,
            listen(stranger.session, resources_list_changed=True) as foreign,
            listen(owner.session, resources_list_changed=True) as own,
        ):
            await owner.call_tool_mcp("fake_echo", {"text": "hi", "repeat": 1})
            async with asyncio.timeout(30):
                await anext(own)

            heard_foreign = False
            with suppress(TimeoutError):
                async with asyncio.timeout(1.5):
                    await anext(foreign)
                    heard_foreign = True

        if heard_foreign:
            raise AssertionError("another user does not hear the journals")

    async def test_silent_tool_sends_no_progress(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Инструмент о ходе работы не сообщал: уведомлений нет, вывод
        инструмента в прогресс не попадает."""
        heard = Heard()
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "hi", "repeat": 2}, progress_handler=heard
            )

        if result.is_error or heard.reports:
            raise AssertionError(f"no progress without reports: {heard.reports}")

    async def test_progress_parameter_is_hidden_from_the_model(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            listed = await client.list_tools()

        schema = next(tool for tool in listed if tool.name == "fake_progress")
        properties = sorted(schema.input_schema["properties"])
        if properties != sorted(["intent", "pause", "steps", CallFields.SCOPE]):
            raise AssertionError(f"the model sees its own arguments: {properties}")


class TestJournal:
    """Каналы журнала вызова — ресурсы сервера: итог ссылается на них,
    окна читает resources/read по шаблону с offset и before, координаты
    окна — в _meta содержимого; журнал читает только тот, кто вызывал."""

    async def _echoed(self, client: Client[Any]) -> JournalRead:
        """Адрес журнала закончившегося вызова."""
        result = await client.call_tool_mcp("fake_echo", {"text": "hi", "repeat": 2})
        return _journal(result)

    async def test_windows_of_the_journal_join_at_line_borders(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            address = await self._echoed(client)
            whole = await _window(client, address)
            tail = await _window(client, address.model_copy(update={"offset": 1}))
            last = await _window(client, address.model_copy(update={"before": 5}))

        if "echo progress: hi" not in whole.text or not whole.closed:
            raise AssertionError(f"the window carries the output of the body: {whole}")
        if whole.offset != 0 or whole.end != whole.size:
            raise AssertionError(f"the window names its place in the file: {whole}")
        if not whole.text.endswith(tail.text):
            raise AssertionError(f"a window from the middle starts at a line: {tail}")
        if last.end > 5:
            raise AssertionError(f"a window before an offset ends there: {last}")

    async def test_channel_link_is_a_readable_resource(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "hi", "repeat": 2}
            )
            links: list[mt.ResourceLink] = []
            for block in result.content:
                if isinstance(block, mt.ResourceLink):
                    links.append(block)

            read = await client.read_resource(str(links[0].uri))

        if links[0].mime_type != "text/plain":
            raise AssertionError(f"a journal channel is plain text: {links}")
        if links[0].annotations != mt.Annotations(audience=["user"]):
            raise AssertionError(f"a journal link is for the user: {links}")

        texts: list[str] = []
        for contents in read:
            if isinstance(contents, mt.TextResourceContents):
                texts.append(contents.text)

        if "echo progress: hi" not in "".join(texts):
            raise AssertionError(f"the resource reads the channel window: {read}")

    async def test_journal_template_is_listed(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            templates = await client.list_resource_templates()

        patterns = [template.uri_template for template in templates]
        if patterns != [JournalRoutes.TEMPLATE]:
            raise AssertionError(f"the journal template is listed: {patterns}")
        if templates[0].annotations != mt.Annotations(audience=["user"]):
            raise AssertionError(f"a journal is for the user: {templates[0]}")

    async def test_missing_journal_is_invalid_params(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Ресурса нет — -32602, как требует спецификация, с причиной."""
        missing = JournalRead(run="nope", node="nope", channel=ToolChannel.STDOUT)
        uri = JournalUris(JournalRoutes.TEMPLATE).uri(missing)
        async with _client(url, dev_token) as client:
            with pytest.raises(MCPError) as refused:
                await client.read_resource(uri)

        if refused.value.error.code != mt.INVALID_PARAMS:
            raise AssertionError(f"a missing journal is invalid params: {refused}")
        if "no journal on channel" not in refused.value.error.message:
            raise AssertionError(f"the refusal names its reason: {refused}")

    async def test_operations_are_visible_to_the_app_only(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            listed = await client.list_tools()

        hidden: dict[str, object] = {}
        for tool in listed:
            if tool.name != FileUploadTool.NAME:
                continue

            meta = tool.meta
            if meta is None:
                raise AssertionError(f"{tool.name} declares its visibility")

            hidden[tool.name] = meta.get("ui")

        expected = {"visibility": ["app"]}
        if hidden != {FileUploadTool.NAME: expected}:
            raise AssertionError(f"operations are hidden from the model: {hidden}")

    async def test_journal_of_another_caller_is_not_readable(
        self, stand: ServiceStand, url: str, dev_token: str, weak_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            address = await self._echoed(client)

        async with _client(url, weak_token) as client:
            with pytest.raises(MCPError) as foreign:
                await _window(client, address)

        if foreign.value.error.code != mt.INVALID_PARAMS:
            raise AssertionError(f"a foreign journal is not found: {foreign}")
        if "belongs to another user" not in foreign.value.error.message:
            raise AssertionError(f"a foreign journal is refused: {foreign}")


class TestDiscovery:
    """server/discover объявляет то, что сервер действительно даёт, а поток
    subscriptions/listen подтверждает только события, которые он шлёт, и
    штатно кончается при остановке. Клиент прежней редакции (initialize)
    получает то, что работает на его проводе."""

    async def test_capabilities_are_the_real_ones(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            capabilities = client.server_capabilities
            prompts = await client.list_prompts()

        if capabilities is None:
            raise AssertionError("the protocol is negotiated on connect")
        if capabilities.tools != mt.ToolsCapability(list_changed=False):
            raise AssertionError(f"the tool list never changes: {capabilities}")
        expected = mt.ResourcesCapability(subscribe=True, list_changed=True)
        if capabilities.resources != expected:
            raise AssertionError(f"journals are resources: {capabilities}")
        if capabilities.prompts is not None or capabilities.logging is not None:
            raise AssertionError(f"no prompts and no logs: {capabilities}")
        if capabilities.completions is not None:
            raise AssertionError(f"no completions: {capabilities}")
        if prompts:
            raise AssertionError(f"the server has no prompts: {prompts}")

    async def test_client_of_an_earlier_edition_gets_what_its_wire_carries(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Рукопожатие 2025-11-25 без сессии: инструменты без смены списка,
        ресурсы журналов без подписки и смены списка, без расширений,
        промптов и логов; инструменты всех endpoint'ов проходят схему этой
        редакции, вызов и список журналов работают."""
        legacy = Client(StreamableHttpTransport(url, auth=dev_token), mode="legacy")
        async with legacy as client:
            version = client.session.protocol_version
            capabilities = client.session.server_capabilities
            called = await client.call_tool_mcp(
                "fake_echo", {"text": "hi", "repeat": 1}
            )
            journals = await client.list_resources()

        listed: dict[str, list[str]] = {}
        for profile in (PROFILE, NARROW, ServiceStand.OFFERS):
            token = await stand.dev_token(profile)
            transport = StreamableHttpTransport(stand.url(profile), auth=token)
            async with Client(transport, mode="legacy") as client:
                tools = await client.list_tools()

            listed[profile] = sorted(tool.name for tool in tools)

        assert version == "2025-11-25"
        assert capabilities == mt.ServerCapabilities(
            tools=mt.ToolsCapability(list_changed=False),
            resources=mt.ResourcesCapability(subscribe=False, list_changed=False),
        )
        assert _text(called) == "hi|t0ken", called
        assert journals == [], journals
        assert "workflow" in listed[PROFILE], listed
        assert listed[NARROW] == ["fake_echo", FileUploadTool.NAME], listed
        assert listed[ServiceStand.OFFERS], listed

    async def test_listen_acknowledges_only_what_the_server_sends(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with (
            _client(url, dev_token) as client,
            listen(
                client.session,
                tools_list_changed=True,
                prompts_list_changed=True,
                resources_list_changed=True,
            ) as stream,
        ):
            honored = stream.honored

        if honored != mt.SubscriptionFilter(resources_list_changed=True):
            raise AssertionError(f"only the journal list is honored: {honored}")

    async def test_stop_ends_listen_streams_gracefully(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Остановка процесса кончает поток итогом подписки: клиент видит
        штатный конец, а не обрыв."""
        events: list[object] = []
        async with (
            _client(url, dev_token) as client,
            listen(client.session, resources_list_changed=True) as stream,
        ):
            stand.endpoints.end_subscriptions()
            async with asyncio.timeout(30):
                async for event in stream:
                    events.append(event)

        if events:
            raise AssertionError(f"the stream ends without events: {events}")


class TestFailures:
    async def test_bad_arguments_are_an_error_result_with_a_model(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_echo", {"text": "hi"})

        if not result.is_error:
            raise AssertionError(f"a missing argument is an error: {result}")
        if not _text(result):
            raise AssertionError(f"the refusal names its reason: {result}")

    async def test_body_failure_is_an_error_result(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "boom", "repeat": 1}
            )

        if not result.is_error:
            raise AssertionError(f"a failed body is an error: {result}")

        if "fake backend is down" not in _text(result):
            raise AssertionError(f"the failure names the cause: {result}")

    async def test_stream_tool_alone_is_refused_with_a_hint(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        arguments = {
            "prefix": "a",
            "count": 1,
            "size": 0,
            "fail_midway": False,
            "out": "c1",
        }
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_emit", arguments)

        if not result.is_error:
            raise AssertionError(f"a pump without a partner is refused: {result}")
        if not _text(result):
            raise AssertionError(f"the plan refusal names its reason: {result}")

    async def test_tool_outside_the_role_is_not_callable(
        self, stand: ServiceStand, url: str, weak_token: str
    ) -> None:
        arguments = {
            "prefix": "a",
            "count": 1,
            "size": 0,
            "fail_midway": False,
            "out": "c1",
        }
        async with _client(url, weak_token) as client:
            result = await client.call_tool_mcp("fake_emit", arguments)

        if not result.is_error:
            raise AssertionError(f"role weak has no fake_emit: {result}")

    async def test_unknown_token_is_rejected(
        self, stand: ServiceStand, url: str
    ) -> None:
        headers = {"Authorization": "Bearer not-a-token-of-the-service"}
        async with httpx.AsyncClient() as http:
            reply = await http.post(url, json={}, headers=headers)

        if reply.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"a foreign token means 401: {reply.status_code}")

    async def test_request_without_a_token_is_rejected(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with httpx.AsyncClient() as http:
            reply = await http.post(url, json={})

        if reply.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"no token means 401: {reply.status_code}")


class TestRunLimit:
    @pytest.fixture
    def stand(self, tmp_path: Path, port: int) -> ServiceStand:
        return ServiceStand(tmp_path, RunLimits(max_runs=1, max_waiting=1), port, "")

    async def test_calls_over_the_limit_wait_and_over_the_queue_are_refused(
        self, stand: ServiceStand, url: str, tmp_path: Path, dev_token: str
    ) -> None:
        async def slept(name: str) -> Any:
            arguments = {"seconds": 1.5, "marker": str(tmp_path / name)}
            async with _client(url, dev_token) as client:
                return await client.call_tool_mcp("fake_sleep", arguments)

        first = asyncio.create_task(slept("first"))
        await self._started(tmp_path / "first")
        second = asyncio.create_task(slept("second"))
        await asyncio.sleep(0.3)
        third = await slept("third")

        if not third.is_error:
            raise AssertionError(f"a call over the queue is refused: {third}")

        if "wait for a slot" not in _text(third):
            raise AssertionError(f"the refusal names the run limit: {third}")

        waited = await second
        if waited.is_error or (await first).is_error:
            raise AssertionError(f"the waiting call runs after the first: {waited}")

    @staticmethod
    async def _started(marker: Path) -> None:
        for _ in range(100):
            if marker.exists():
                return

            await asyncio.sleep(0.05)

        raise AssertionError(f"the body did not start: {marker}")


class TestCancellation:
    async def test_dropped_connection_kills_the_running_body(
        self, stand: ServiceStand, url: str, tmp_path: Path, dev_token: str
    ) -> None:
        marker = tmp_path / "pid"
        arguments = {"seconds": 60.0, "marker": str(marker)}

        async def hanging() -> Any:
            async with _client(url, dev_token) as client:
                return await client.call_tool_mcp("fake_sleep", arguments)

        call = asyncio.create_task(hanging())
        for _ in range(100):
            if marker.exists() and marker.read_text(encoding="utf-8"):
                break

            await asyncio.sleep(0.05)

        pid = int(marker.read_text(encoding="utf-8"))
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        for _ in range(100):
            if not Path(f"/proc/{pid}").exists():
                return

            await asyncio.sleep(0.05)

        raise AssertionError(f"the body process {pid} outlived its cancelled call")


class TestEndpoints:
    """Endpoint — профиль конфига: свой набор инструментов и свои роли."""

    async def test_endpoint_offers_the_tools_of_its_profile(
        self, stand: ServiceStand, url: str
    ) -> None:
        token = await stand.dev_token(NARROW)
        async with _client(stand.url(NARROW), token) as client:
            listed = await client.list_tools()

        names = sorted(tool.name for tool in listed)
        if names != ["fake_echo", FileUploadTool.NAME]:
            raise AssertionError(f"the endpoint offers its own tools: {names}")

    async def test_endpoint_outside_the_roles_is_forbidden(
        self, stand: ServiceStand, url: str
    ) -> None:
        headers = {"Authorization": f"Bearer {await stand.weak_token(NARROW)}"}
        async with httpx.AsyncClient() as http:
            reply = await http.post(stand.url(NARROW), json={}, headers=headers)

        if reply.status_code != httpx.codes.FORBIDDEN:
            raise AssertionError(f"role weak has no endpoint narrow: {reply}")

    async def test_token_of_another_endpoint_is_not_accepted(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Роль dev открывает оба endpoint'а, но токен выпущен для одного:
        на втором он не принят, и отказ ведёт клиента ко входу."""
        headers = {"Authorization": f"Bearer {dev_token}"}
        async with httpx.AsyncClient() as http:
            own = await http.post(stand.url(PROFILE), json={}, headers=headers)
            other = await http.post(stand.url(NARROW), json={}, headers=headers)

        if own.status_code == httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"the token is accepted on its endpoint: {own}")

        if other.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"a token of another endpoint is 401: {other}")

        challenge = other.headers.get("www-authenticate", "")
        if "resource_metadata=" not in challenge:
            raise AssertionError(f"the refusal leads to the sign-in: {challenge}")

    async def test_unknown_endpoint_is_not_found(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        headers = {"Authorization": f"Bearer {dev_token}"}
        async with httpx.AsyncClient() as http:
            reply = await http.post(stand.url("nowhere"), json={}, headers=headers)

        if reply.status_code != httpx.codes.NOT_FOUND:
            raise AssertionError(f"an unknown endpoint is 404: {reply}")


class TestProxySignIn:
    """Вход proxy: доверенный клиент меняет подписанное утверждение о
    пользователе на токен сервиса штатным обменом OAuth (grant jwt-bearer)."""

    def _public(self, stand: ServiceStand) -> str:
        return f"http://127.0.0.1:{stand.port}"

    async def _exchanged(
        self,
        stand: ServiceStand,
        login: str,
        roles: str,
        secret: str,
        profile: str = PROFILE,
    ) -> httpx.Response:
        public = self._public(stand)
        assertion = ProxyAssertions(secret, 0).issue(
            CLIENT_ID, login, roles, public, stand.url(profile)
        )
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "resource": stand.url(profile),
        }
        async with httpx.AsyncClient() as http:
            return await http.post(f"{public}/token", data=form)

    async def test_assertion_becomes_the_caller(
        self, stand: ServiceStand, url: str
    ) -> None:
        reply = await self._exchanged(stand, "ivanov", "dev", PROXY_SECRET)
        if reply.status_code != httpx.codes.OK:
            raise AssertionError(f"the signed assertion signs in: {reply.text}")

        token = reply.json()["access_token"]
        async with _client(url, token) as client:
            result = await client.call_tool_mcp("fake_whoami", {})

        if _text(result) != f"ivanov|dev|{PROFILE}":
            raise AssertionError(f"the body runs as the signed-in user: {result}")

    async def test_roles_of_the_sign_in_open_the_endpoints(
        self, stand: ServiceStand, url: str
    ) -> None:
        reply = await self._exchanged(stand, "petrov", "weak", PROXY_SECRET, NARROW)
        headers = {"Authorization": f"Bearer {reply.json()['access_token']}"}
        async with httpx.AsyncClient() as http:
            narrow = await http.post(stand.url(NARROW), json={}, headers=headers)

        if narrow.status_code != httpx.codes.FORBIDDEN:
            raise AssertionError(f"role weak is refused on narrow: {narrow}")

        challenge = narrow.headers.get("www-authenticate", "")
        if "insufficient_scope" not in challenge:
            raise AssertionError(f"the refusal names the missing scope: {challenge}")

    async def test_forged_assertion_is_rejected(
        self, stand: ServiceStand, url: str
    ) -> None:
        reply = await self._exchanged(stand, "ivanov", "dev", "forged")

        if reply.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"a forged assertion is 401: {reply.text}")

    async def test_unknown_client_is_rejected(
        self, stand: ServiceStand, url: str
    ) -> None:
        public = self._public(stand)
        assertion = ProxyAssertions(PROXY_SECRET, 0).issue(
            CLIENT_ID, "ivanov", "dev", public, stand.url(PROFILE)
        )
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
            "client_id": CLIENT_ID,
            "client_secret": "not-the-secret",
        }
        async with httpx.AsyncClient() as http:
            reply = await http.post(f"{public}/token", data=form)

        if reply.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"a wrong client secret is 401: {reply.text}")

    async def test_sign_in_without_roles_is_refused(
        self, stand: ServiceStand, url: str
    ) -> None:
        reply = await self._exchanged(stand, "ivanov", "", PROXY_SECRET)

        if reply.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"a sign-in without roles is refused: {reply.text}")

    async def test_undecided_roles_are_an_oauth_refusal(self) -> None:
        """Сервис не смог решить роли пользователя утверждения (в [auth] нет
        провайдера proxy): обмен отвечает ошибкой OAuth с причиной, а не
        внутренней ошибкой без тела."""
        public = "http://127.0.0.1:1"
        resource = f"{public}/{PROFILE}"
        sealed = SealedValues("stand-session-secret", ServiceMark.SEALED_SALT)
        server = AuthServer(
            public,
            SignIns(None, None, None),
            ServiceTokens(
                public,
                "stand-session-secret",
                300,
                3600,
                "stand",
                ServiceMark.TOKEN_SALT,
            ),
            ProxyAssertions(PROXY_SECRET, 60),
            RegisteredClients(sealed, {}),
            sealed,
            [resource],
        )
        assertion = ProxyAssertions(PROXY_SECRET, 0).issue(
            CLIENT_ID, "ivanov", "dev", public, resource
        )
        client = OAuthClientInformationFull(
            client_id=CLIENT_ID, redirect_uris=[AnyUrl(f"{public}/callback")]
        )
        params = IdentityAssertionParams(assertion=assertion, resource=resource)

        with pytest.raises(TokenError) as refused:
            await server.exchange_identity_assertion(client, params)

        if refused.value.error != "invalid_grant":
            raise AssertionError(f"the refusal is an OAuth grant error: {refused}")

        described = refused.value.error_description or ""
        if "could not decide the roles" not in described:
            raise AssertionError(f"the refusal names the cause: {described}")


class TestHumanSignIn:
    """Вход человека: клиент OAuth регистрируется сам, ведёт браузер на
    authorize, сервис спрашивает логин и пароль формой и возвращает код,
    код меняется на токен с проверкой PKCE."""

    REDIRECT: str = "http://127.0.0.1:53999/callback"

    def _public(self, stand: ServiceStand) -> str:
        return f"http://127.0.0.1:{stand.port}"

    async def _registered(self, http: httpx.AsyncClient, public: str) -> str:
        reply = await http.post(
            f"{public}/register",
            json={
                "redirect_uris": [self.REDIRECT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "stand browser client",
            },
        )
        if reply.status_code != httpx.codes.CREATED:
            raise AssertionError(f"the client registers itself: {reply.text}")

        return str(reply.json()["client_id"])

    async def _login_page(
        self,
        http: httpx.AsyncClient,
        stand: ServiceStand,
        client_id: str,
        challenge: str,
    ) -> str:
        """Идентификатор начатого входа: authorize ведёт на страницу входа."""
        reply = await http.get(
            f"{self._public(stand)}/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self.REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "stand-state",
                "resource": stand.url(PROFILE),
            },
        )
        if reply.status_code != httpx.codes.FOUND:
            raise AssertionError(f"authorize leads to the sign-in page: {reply.text}")

        location = httpx.URL(reply.headers["location"])
        if location.path != "/login":
            raise AssertionError(f"authorize leads to /login, got {location}")

        return location.params["txn"]

    async def _proof(self, http: httpx.AsyncClient, public: str, txn: str) -> str:
        """Подтверждение страницы входа: страница открыта этим браузером, её
        cookie остаётся у него."""
        page = await http.get(f"{public}/login", params={"txn": txn})
        found = re.search(r'name="proof" value="([^"]+)"', page.text)
        if found is None:
            raise AssertionError(f"the sign-in page carries its proof: {page.text}")

        return found.group(1)

    @staticmethod
    def _pkce() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

        return verifier, challenge

    async def test_password_sign_in_issues_a_token(
        self, stand: ServiceStand, url: str
    ) -> None:
        public = self._public(stand)
        verifier, challenge = self._pkce()
        async with httpx.AsyncClient() as http:
            client_id = await self._registered(http, public)
            txn = await self._login_page(http, stand, client_id, challenge)
            page = await http.get(f"{public}/login", params={"txn": txn})
            if page.status_code != httpx.codes.OK or "password" not in page.text:
                raise AssertionError(f"the sign-in page shows the form: {page.text}")

            if "stand browser client" not in page.text:
                raise AssertionError(f"the page names the application: {page.text}")

            if html.escape(self.REDIRECT) not in page.text:
                raise AssertionError(f"the page names the return: {page.text}")

            signed = await http.post(
                f"{public}/login",
                data={
                    "txn": txn,
                    "proof": await self._proof(http, public, txn),
                    "login": LOCAL_LOGIN,
                    "password": LOCAL_PASSWORD,
                },
            )
            if signed.status_code != httpx.codes.FOUND:
                raise AssertionError(
                    f"the sign-in returns to the client: {signed.text}"
                )

            back = httpx.URL(signed.headers["location"])
            if back.params.get("state") != "stand-state":
                raise AssertionError(f"the return carries the client state: {back}")

            issued = await http.post(
                f"{public}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": back.params["code"],
                    "client_id": client_id,
                    "redirect_uri": self.REDIRECT,
                    "code_verifier": verifier,
                    "resource": stand.url(PROFILE),
                },
            )

        if issued.status_code != httpx.codes.OK:
            raise AssertionError(f"the code is exchanged for a token: {issued.text}")

        async with _client(url, issued.json()["access_token"]) as client:
            result = await client.call_tool_mcp("fake_whoami", {})

        expected = f"{LOCAL_LOGIN}|dev|{PROFILE}"
        if _text(result) != expected:
            raise AssertionError(f"the body runs as the signed-in user: {result}")

    async def _signed_in(self, stand: ServiceStand) -> tuple[str, dict[str, Any]]:
        """Клиент и ответ /token после входа формой."""
        public = self._public(stand)
        verifier, challenge = self._pkce()
        async with httpx.AsyncClient() as http:
            client_id = await self._registered(http, public)
            txn = await self._login_page(http, stand, client_id, challenge)
            signed = await http.post(
                f"{public}/login",
                data={
                    "txn": txn,
                    "proof": await self._proof(http, public, txn),
                    "login": LOCAL_LOGIN,
                    "password": LOCAL_PASSWORD,
                },
            )
            back = httpx.URL(signed.headers["location"])
            issued = await http.post(
                f"{public}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": back.params["code"],
                    "client_id": client_id,
                    "redirect_uri": self.REDIRECT,
                    "code_verifier": verifier,
                },
            )

        if issued.status_code != httpx.codes.OK:
            raise AssertionError(f"the code is exchanged for a token: {issued.text}")

        return client_id, issued.json()

    async def test_refresh_token_renews_without_the_form(
        self, stand: ServiceStand, url: str
    ) -> None:
        client_id, issued = await self._signed_in(stand)
        async with httpx.AsyncClient() as http:
            renewed = await http.post(
                f"{self._public(stand)}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": issued["refresh_token"],
                    "client_id": client_id,
                },
            )

        if renewed.status_code != httpx.codes.OK:
            raise AssertionError(
                f"the refresh token renews the sign-in: {renewed.text}"
            )

        pair = renewed.json()
        if pair["access_token"] == issued["access_token"]:
            raise AssertionError("the renewal issues a new access token")

        async with _client(url, pair["access_token"]) as client:
            result = await client.call_tool_mcp("fake_whoami", {})

        expected = f"{LOCAL_LOGIN}|dev|{PROFILE}"
        if _text(result) != expected:
            raise AssertionError(f"the renewed token keeps the user: {result}")

    async def test_access_token_does_not_renew(
        self, stand: ServiceStand, url: str
    ) -> None:
        client_id, issued = await self._signed_in(stand)
        async with httpx.AsyncClient() as http:
            refused = await http.post(
                f"{self._public(stand)}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": issued["access_token"],
                    "client_id": client_id,
                },
            )

        if refused.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"an access token is not a refresh token: {refused}")

    async def test_wrong_password_stays_on_the_page(
        self, stand: ServiceStand, url: str
    ) -> None:
        public = self._public(stand)
        _, challenge = self._pkce()
        async with httpx.AsyncClient() as http:
            client_id = await self._registered(http, public)
            txn = await self._login_page(http, stand, client_id, challenge)
            refused = await http.post(
                f"{public}/login",
                data={
                    "txn": txn,
                    "proof": await self._proof(http, public, txn),
                    "login": LOCAL_LOGIN,
                    "password": "wrong",
                },
            )

        if refused.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"a wrong password is 401: {refused.text}")

        if "Invalid username or password" not in refused.text:
            raise AssertionError(f"the page names the refusal: {refused.text}")

    async def test_sign_in_from_another_browser_is_refused(
        self, stand: ServiceStand, url: str
    ) -> None:
        """Начатый вход открыл один браузер, а форму шлёт другой: верный
        пароль без cookie страницы входа кода не даёт."""
        public = self._public(stand)
        _, challenge = self._pkce()
        async with httpx.AsyncClient() as opener:
            client_id = await self._registered(opener, public)
            txn = await self._login_page(opener, stand, client_id, challenge)
            proof = await self._proof(opener, public, txn)

        async with httpx.AsyncClient() as other:
            refused = await other.post(
                f"{public}/login",
                data={
                    "txn": txn,
                    "proof": proof,
                    "login": LOCAL_LOGIN,
                    "password": LOCAL_PASSWORD,
                },
            )

        if refused.status_code != httpx.codes.FORBIDDEN:
            raise AssertionError(f"a step without the page cookie is 403: {refused}")

        if "location" in refused.headers:
            raise AssertionError(f"no code leaves the service: {refused.headers}")

    async def test_sign_in_without_the_page_proof_is_refused(
        self, stand: ServiceStand, url: str
    ) -> None:
        """Форма с чужого сайта: cookie у браузера есть, а подтверждения со
        страницы входа в форме нет."""
        public = self._public(stand)
        _, challenge = self._pkce()
        async with httpx.AsyncClient() as http:
            client_id = await self._registered(http, public)
            txn = await self._login_page(http, stand, client_id, challenge)
            await self._proof(http, public, txn)
            refused = await http.post(
                f"{public}/login",
                data={"txn": txn, "login": LOCAL_LOGIN, "password": LOCAL_PASSWORD},
            )

        if refused.status_code != httpx.codes.FORBIDDEN:
            raise AssertionError(f"a step without the page proof is 403: {refused}")

        if "location" in refused.headers:
            raise AssertionError(f"no code leaves the service: {refused.headers}")

    async def test_sign_in_without_a_resource_does_not_start(
        self, stand: ServiceStand, url: str
    ) -> None:
        """Клиент не назвал endpoint, для которого просит токен: вход не
        начинается, отказ возвращается клиенту на его адрес возврата."""
        public = self._public(stand)
        _, challenge = self._pkce()
        async with httpx.AsyncClient() as http:
            client_id = await self._registered(http, public)
            reply = await http.get(
                f"{public}/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": self.REDIRECT,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "stand-state",
                },
            )

        if reply.status_code != httpx.codes.FOUND:
            raise AssertionError(f"the refusal returns to the client: {reply.text}")

        back = httpx.URL(reply.headers["location"])
        if back.path == "/login":
            raise AssertionError(f"no sign-in page without a resource: {back}")

        if back.params.get("error") != "invalid_target":
            raise AssertionError(f"the refusal names the resource: {back}")

    async def test_unknown_sign_in_is_refused(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with httpx.AsyncClient() as http:
            reply = await http.get(
                f"{self._public(stand)}/login", params={"txn": "not-started"}
            )

        if reply.status_code != httpx.codes.BAD_REQUEST:
            raise AssertionError(f"a sign-in that was not started is 400: {reply}")


class TestAuthDiscovery:
    """Клиент OAuth находит вход сам: отказ endpoint'а ведёт к метаданным
    ресурса, те — к серверу авторизации."""

    async def test_refusal_leads_to_the_authorization_server(
        self, stand: ServiceStand, url: str
    ) -> None:
        public = f"http://127.0.0.1:{stand.port}"
        async with httpx.AsyncClient() as http:
            refused = await http.post(stand.url(PROFILE), json={})
            if refused.status_code != httpx.codes.UNAUTHORIZED:
                raise AssertionError(f"a call without a token is 401: {refused}")

            challenge = refused.headers.get("www-authenticate", "")
            marker = 'resource_metadata="'
            if marker not in challenge:
                raise AssertionError(
                    f"the refusal names resource metadata: {challenge}"
                )

            address = challenge.split(marker, 1)[1].split('"', 1)[0]
            resource = await http.get(address)
            if resource.status_code != httpx.codes.OK:
                raise AssertionError(f"resource metadata is served: {resource}")

            servers = resource.json()["authorization_servers"]
            if [str(item).rstrip("/") for item in servers] != [public]:
                raise AssertionError(
                    f"the resource names its sign-in server: {servers}"
                )

            metadata = await http.get(
                f"{public}/.well-known/oauth-authorization-server"
            )

        if metadata.status_code != httpx.codes.OK:
            raise AssertionError(f"server metadata is served: {metadata}")

        described = metadata.json()
        if described["token_endpoint"].rstrip("/") != f"{public}/token":
            raise AssertionError(f"metadata names the token endpoint: {described}")

        if JWT_BEARER_GRANT_TYPE not in described["grant_types_supported"]:
            raise AssertionError(
                f"metadata declares the exchange of an assertion: {described}"
            )


class TestServicePrefix:
    """Сервис за прокси без переписывания пути: все маршруты под префиксом
    публичного адреса, метаданные OAuth — в корне хоста по путям RFC 9728 и
    RFC 8414 с префиксом внутри."""

    PREFIX: ClassVar[str] = "/boba-mcp"

    @pytest.fixture
    async def prefixed(self, tmp_path: Path, port: int) -> AsyncIterator[ServiceStand]:
        served = ServiceStand(
            tmp_path, RunLimits(max_runs=4, max_waiting=4), port, self.PREFIX
        )
        async with served.serving():
            yield served

    async def test_health_answers_only_under_the_prefix(
        self, prefixed: ServiceStand
    ) -> None:
        root = f"http://127.0.0.1:{prefixed.port}"
        async with httpx.AsyncClient() as http:
            inside = await http.get(f"{prefixed.public_url}/health")
            outside = await http.get(f"{root}/health")

        if inside.status_code != httpx.codes.OK:
            raise AssertionError(f"the probe answers under the prefix: {inside}")

        if outside.status_code != httpx.codes.NOT_FOUND:
            raise AssertionError(f"the probe is not served outside: {outside}")

    async def test_discovery_leads_to_the_prefixed_sign_in(
        self, prefixed: ServiceStand
    ) -> None:
        root = f"http://127.0.0.1:{prefixed.port}"
        public = prefixed.public_url
        async with httpx.AsyncClient() as http:
            refused = await http.post(prefixed.url(PROFILE), json={})
            challenge = refused.headers.get("www-authenticate", "")
            address = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
            resource = await http.get(address)
            metadata = await http.get(
                f"{root}/.well-known/oauth-authorization-server{self.PREFIX}"
            )

        expected = f"{root}/.well-known/oauth-protected-resource{self.PREFIX}/{PROFILE}"
        if address != expected:
            raise AssertionError(
                f"resource metadata lies at the RFC 9728 path: {address}"
            )

        described = resource.json()
        if str(described["resource"]).rstrip("/") != prefixed.url(PROFILE):
            raise AssertionError(f"the resource is the prefixed endpoint: {described}")

        servers = described["authorization_servers"]
        if [str(item).rstrip("/") for item in servers] != [public]:
            raise AssertionError(f"the resource names the prefixed issuer: {servers}")

        server = metadata.json()
        if str(server["issuer"]).rstrip("/") != public:
            raise AssertionError(f"the issuer is the public address: {server}")

        if server["token_endpoint"] != f"{public}/token":
            raise AssertionError(f"the token endpoint is under the prefix: {server}")

    async def test_proxy_token_calls_the_prefixed_endpoint(
        self, prefixed: ServiceStand
    ) -> None:
        token = await prefixed.dev_token(PROFILE)
        async with _client(prefixed.url(PROFILE), token) as client:
            tools = await client.list_tools()

        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        if "fake_echo" not in names:
            raise AssertionError(
                f"the endpoint under the prefix lists its tools: {names}"
            )


class TestWorkspaceFiles:
    """Файлы workspace по маршруту endpoint'а: потоком в обе стороны."""

    SCOPE: str = "3f0d6b1e-1b0e-4f55-9a52-6f4d2a1c9e01"

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _address(self, url: str, name: str) -> str:
        return f"{url}/files/{self.SCOPE}/upload/{name}"

    @staticmethod
    async def _chunks(payload: bytes, size: int) -> AsyncIterator[bytes]:
        for start in range(0, len(payload), size):
            yield payload[start : start + size]

    async def test_stored_stream_is_read_back_whole_and_by_range(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        payload = bytes(range(256)) * 4096
        address = self._address(url, "data.bin")
        headers = self._headers(dev_token)
        async with httpx.AsyncClient() as http:
            stored = await http.put(
                address, content=self._chunks(payload, 65536), headers=headers
            )
            whole = await http.get(address, headers=headers)
            ranged = await http.get(
                address, headers={**headers, "Range": "bytes=10-19"}
            )
            head = await http.head(address, headers=headers)

        if stored.status_code != 201 or stored.json()["size"] != len(payload):
            raise AssertionError(
                f"the stream is stored: {stored.status_code} {stored.text}"
            )
        if stored.json()["path"] != f"/workspace/{self.SCOPE}/upload/data.bin":
            raise AssertionError(f"the reply names the workspace path: {stored.text}")
        if whole.content != payload:
            raise AssertionError("the file is read back as written")
        if ranged.status_code != 206 or ranged.content != payload[10:20]:
            raise AssertionError(f"a range answers 206: {ranged.status_code}")
        for served in (whole, ranged):
            if served.headers.get("x-content-type-options") != "nosniff":
                raise AssertionError(f"the body is not sniffed: {served.headers}")
        if head.headers.get("content-length") != str(len(payload)):
            raise AssertionError(f"HEAD names the size: {head.headers}")
        if not head.headers.get("etag"):
            raise AssertionError(f"HEAD names the revision: {head.headers}")

    async def test_request_without_a_token_is_refused(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with httpx.AsyncClient() as http:
            reply = await http.put(self._address(url, "note.txt"), content=b"x")

        if reply.status_code != 401:
            raise AssertionError(f"no token, no file: {reply.status_code} {reply.text}")

    async def test_workspace_of_another_user_is_out_of_reach(
        self, stand: ServiceStand, url: str, dev_token: str, weak_token: str
    ) -> None:
        """Адрес не называет пользователя: тот же путь у другого вошедшего
        ведёт в его собственный workspace."""
        address = self._address(url, "private.txt")
        async with httpx.AsyncClient() as http:
            await http.put(address, content=b"secret", headers=self._headers(dev_token))
            other = await http.get(
                address.replace("/service/", "/narrow/"),
                headers=self._headers(await stand.dev_token(NARROW)),
            )
            stranger = await http.get(address, headers=self._headers(weak_token))

        if other.content != b"secret":
            raise AssertionError(f"endpoints share the user's workspace: {other.text}")
        if stranger.status_code != 404:
            raise AssertionError(
                f"another user sees no such file: {stranger.status_code}"
            )

    async def test_address_outside_the_scope_dirs_is_refused(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        address = f"{url}/files/{self.SCOPE}/elsewhere/note.txt"
        async with httpx.AsyncClient() as http:
            reply = await http.put(
                address, content=b"x", headers=self._headers(dev_token)
            )

        if reply.status_code != 400:
            raise AssertionError(f"only scope dirs are addressed: {reply.status_code}")

    async def test_file_upload_names_the_address_for_the_client(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                FileUploadTool.NAME,
                {"name": "report.csv", CallFields.SCOPE: self.SCOPE},
            )
            declared = client.session.server_capabilities

        structured = _shown(result)
        expected = f"/service/files/{self.SCOPE}/upload/report.csv"
        if structured is None or structured.get("path") != expected:
            raise AssertionError(f"the tool names the upload address: {result}")
        if declared is None or declared.extensions is None:
            raise AssertionError("the server declares extensions")

        files = declared.extensions.get(FilesFeature.ID)
        expected_files = {
            "path": "/service/files",
            "upload": FileUploadTool.NAME,
            "workspace": "/workspace",
        }
        if files != expected_files:
            raise AssertionError(f"the files extension is declared: {files}")


class TestConnectionOffers:
    """Предложение соединений по правилам endpoint'а (таблица 6.8) на
    настоящем сервере: схема параметра-соединения по признаку клиента и роли,
    имя заданного соединения сервера доходит до тела, запечатанное значение
    от того, кому предложен только сервер, отвергается. Клиент без
    расширений — тестовый клиент fastmcp без com.boba/connection-seal."""

    TOOL: ClassVar[str] = "fake_connection_host"

    @pytest.fixture
    async def offers(self, stand: ServiceStand) -> AsyncIterator[str]:
        async with stand.serving():
            yield stand.url(ServiceStand.OFFERS)

    async def _token(self, stand: ServiceStand, analyst: bool) -> str:
        if analyst:
            return await stand.analyst_token(ServiceStand.OFFERS)

        return await stand.dev_token(ServiceStand.OFFERS)

    def _client(self, url: str, token: str, sealing: bool) -> Client[Any]:
        transport = StreamableHttpTransport(url, auth=token)
        if not sealing:
            return Client(transport)

        return Client(transport, extensions=[advertise(SealFeature.ID)])

    @pytest.mark.parametrize(
        ("sealing", "analyst", "sealed", "names"),
        [
            (True, True, True, [ServiceStand.DWH]),
            (True, False, True, []),
            (False, True, False, [ServiceStand.DWH]),
        ],
        ids=["sealing-analyst-both", "sealing-dev-sealed", "plain-analyst-server"],
    )
    async def test_schema_of_the_connection_follows_the_offer(  # noqa: PLR0913 — случай таблицы 6.8
        self,
        stand: ServiceStand,
        offers: str,
        sealing: bool,
        analyst: bool,
        sealed: bool,
        names: list[str],
    ) -> None:
        token = await self._token(stand, analyst)
        async with self._client(offers, token, sealing) as client:
            listed = await client.list_tools()

        tool = next(tool for tool in listed if tool.name == self.TOOL)
        declared = tool.input_schema["properties"]["connection"]
        offer = SealedParamSchema().offers(tool.input_schema)["connection"]
        if offer != ConnectionOffer(
            kind="postgres", sealed=sealed, names=dict.fromkeys(names, "")
        ):
            raise AssertionError(f"the parameter follows the offer: {declared}")

        if declared[SchemaMark.CONNECTION.value] != "postgres":
            raise AssertionError(f"the parameter is marked with its kind: {declared}")

        both = sealed and bool(names)
        if both != (SchemaMark.ANY_OF.value in declared):
            raise AssertionError(f"both sources come as anyOf: {declared}")

        for name in names:
            if name not in declared["description"]:
                raise AssertionError(f"the description names {name}: {declared}")

    async def test_nothing_offered_hides_the_tool(
        self, stand: ServiceStand, offers: str
    ) -> None:
        token = await self._token(stand, False)
        async with self._client(offers, token, False) as client:
            listed = await client.list_tools()

        names = [tool.name for tool in listed]
        if self.TOOL in names:
            raise AssertionError(f"a tool with nothing to offer is hidden: {names}")

        if "fake_echo" not in names:
            raise AssertionError(f"tools without connections stay: {names}")

    @pytest.mark.parametrize("sealing", [True, False], ids=["sealing", "plain"])
    async def test_server_connection_reaches_the_body(
        self, stand: ServiceStand, offers: str, sealing: bool
    ) -> None:
        token = await self._token(stand, True)
        async with self._client(offers, token, sealing) as client:
            result = await client.call_tool_mcp(
                self.TOOL, {"connection": ServiceStand.DWH}
            )

        if result.is_error:
            raise AssertionError(f"the server connection is taken: {result}")

        if _text(result) != "dwh.local|dwh":
            raise AssertionError(f"the body gets the server profile: {result}")

    async def test_sealed_value_is_refused_when_only_the_server_is_offered(
        self, stand: ServiceStand, offers: str
    ) -> None:
        token = await self._token(stand, True)
        async with self._client(offers, token, False) as client:
            capabilities = client.session.server_capabilities
            if capabilities is None or capabilities.extensions is None:
                raise AssertionError("the server declares its extensions")

            key = SealFeature.model_validate(
                capabilities.extensions[SealFeature.ID]
            ).key
            profile = {
                "kind": "postgres",
                "host": "db.local",
                "dbname": "x",
                "auth": {"method": "trust", "user": "u"},
            }
            value = ConnectionSeal(key).seal(
                SealedConnection(
                    name="mine",
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    profile=profile,
                )
            )
            result = await client.call_tool_mcp(self.TOOL, {"connection": value})

        text = str(result.content)
        if not result.is_error:
            raise AssertionError(f"the sealed value is refused: {result}")

        expected = "sealed connections are not offered to this caller, use one of: dwh"
        if expected not in text:
            raise AssertionError(f"the refusal names the offer: {text}")

    async def test_plain_client_sees_server_names_and_the_scope(
        self, stand: ServiceStand, offers: str
    ) -> None:
        """Клиент без расширений видит enum имён сервера и параметр scope_id,
        а список инструментов помечен личным со сроком свежести: он зависит
        от ролей токена и возможностей запроса, а реестр собран на старте."""
        token = await self._token(stand, True)
        async with self._client(offers, token, False) as client:
            listed = await client.list_tools_mcp()

        if listed.cache_scope != "private":
            raise AssertionError(f"the list is private: {listed}")

        if listed.ttl_ms <= 0:
            raise AssertionError(f"the list is fresh for a while: {listed}")

        tool = next(tool for tool in listed.tools if tool.name == self.TOOL)
        properties = tool.input_schema["properties"]
        if properties["connection"]["enum"] != [ServiceStand.DWH]:
            raise AssertionError(f"the server names are an enum: {properties}")

        if SchemaMark.SCOPE.value not in properties[CallFields.SCOPE]:
            raise AssertionError(f"the scope parameter is shown: {properties}")


class TestJournalFile:
    """Журнал вызова целым файлом по маршруту endpoint'а."""

    async def test_channel_is_served_to_the_caller_only(
        self, stand: ServiceStand, url: str, dev_token: str, weak_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "hi", "repeat": 2}
            )

        served = _journal(result)
        address = f"{url}/journals/{served.run}/{served.node}/{served.channel.value}"
        async with httpx.AsyncClient() as http:
            mine = await http.get(
                address, headers={"Authorization": f"Bearer {dev_token}"}
            )
            part = await http.get(
                address,
                headers={"Authorization": f"Bearer {dev_token}", "Range": "bytes=0-3"},
            )
            foreign = await http.get(
                address, headers={"Authorization": f"Bearer {weak_token}"}
            )
            anonymous = await http.get(address)

        if mine.status_code != 200 or "echo progress: hi" not in mine.text:
            raise AssertionError(f"the caller gets the channel: {mine.status_code}")
        if part.status_code != 206 or part.content != mine.content[:4]:
            raise AssertionError(f"a range answers 206: {part.status_code}")
        if mine.headers.get("x-content-type-options") != "nosniff":
            raise AssertionError(f"the journal is not sniffed: {mine.headers}")
        if foreign.status_code != 404:
            raise AssertionError(f"another user gets no journal: {foreign.status_code}")
        if anonymous.status_code != 401:
            raise AssertionError(f"no token, no journal: {anonymous.status_code}")
