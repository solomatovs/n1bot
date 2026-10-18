"""Порт MCP-сервера: обычный сервер по stdio и HTTP, свой сервер boba-mcp,
серверы прежней редакции в контейнере boba-testapps, публичные серверы в сети.

Обычный сервер написан на официальном SDK и о boba не знает: клиент обязан
работать с ним одним стандартом. Проверки, не зависящие от редакции, идут на
двух обычных серверах (параметр Edition): на SDK mcp редакции 2026-07-28 и на
TypeScript SDK 1.x редакции 2025-11-25; отдельные тесты — только на различия
протокола. Публичные серверы проверяют то же на чужих реализациях. Ответ
tools/call порт отдаёт как есть; текст для модели по правилам MCP собирает
McpResults поверх него.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import httpx
import mcp.types as mt
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.apps import APP_MIME_TYPE
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from standard_server import StandardServer

from boba.auth.config import ProxyAuthConfig
from boba.canvas.keys import ObjectKey, ThreadDir
from boba.canvas.storage import StorageNotFoundError
from boba.config import bind
from boba.connections.sealed import ConnectionSeal, SealedConnection, SealFeature
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    HttpLocation,
    JournalOpening,
    McpCaller,
    McpClientError,
    McpClientIdentity,
    McpServerApps,
    McpServerConfig,
    McpServers,
    McpServersConfig,
    McpToolServer,
    ProxyAuth,
    StdioCommand,
)
from boba.runtime.config import AppLayers, ConfigLayoutError, EnvOverride
from boba.stand.apps_slot import AppServer, AppsSlot
from boba.stand.service_signin import ServiceSignIn
from boba.stand.ui.stand import StandApp, StandFiles
from boba.stand.zygote import RUNTIME
from boba.toolkit.calls import CallViews
from boba.toolkit.channels import ToolChannel
from boba.toolkit.dag import (
    CallDag,
    NestedCallsOutput,
    ToolCard,
    ToolHints,
)
from boba.toolkit.mcp import McpFailure, McpResults, McpToolStubs
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
    MarkdownResult,
)
from boba.toolkit.types import SecretReveal
from boba.toolkit.wire import JournalFeature, JournalRead
from boba.workspace.launcher import ReadWindow

pytestmark = pytest.mark.anyio

SERVER = Path(__file__).with_name("standard_server.py")


def _config(endpoint: HttpEndpoint | StdioCommand) -> McpServerConfig:
    return McpServerConfig(
        endpoint=endpoint,
        connect_timeout_sec=20.0,
        call_timeout_sec=60.0,
    )


class Edition(StrEnum):
    """Редакции MCP обычного сервера в тестах порта: сервер на SDK mcp по
    stdio (2026-07-28) и свой сервер на TypeScript SDK 1.x по HTTP с
    сессиями в контейнере boba-testapps (2025-11-25)."""

    MODERN = "2026-07-28"
    LEGACY = "2025-11-25"


def _tools_of(port: McpToolServer) -> list[str]:
    """Имена инструментов обычного сервера согласованной редакции."""
    if port.discovered().supported_versions == [Edition.LEGACY.value]:
        return TestFailureStand.LEGACY_TOOLS

    return TestFailureStand.STANDARD_TOOLS


def _legacy(slot: AppsSlot, server: AppServer) -> HttpEndpoint:
    return HttpEndpoint(
        scheme="http", host=slot.host, port=server.value, path=slot.PATH
    )


CALLER = McpCaller(login="alice", roles=frozenset({"dev"}))


def _stdout(port: McpToolServer, reply: mt.CallToolResult) -> JournalRead:
    """Адрес канала stdout журнала вызова по ссылке в итоге."""
    links = port.journal_links()
    if links is None:
        raise AssertionError("the server declares its journal")

    for block in reply.content:
        if not isinstance(block, mt.ResourceLink):
            continue

        read = links.address(str(block.uri))
        if read is None:
            continue

        if read.channel is ToolChannel.STDOUT:
            return read

    raise AssertionError(f"the result links the stdout of its journal: {reply}")


def _call(name: str, **args: object) -> mt.CallToolRequestParams:
    return mt.CallToolRequestParams(name=name, arguments=args)


@pytest.fixture(
    params=[
        Edition.MODERN,
        pytest.param(Edition.LEGACY, marks=pytest.mark.integration),
    ],
    ids=str,
)
def edition_endpoint(request: pytest.FixtureRequest) -> HttpEndpoint | StdioCommand:
    """Адрес обычного сервера редакции параметра."""
    if request.param is Edition.MODERN:
        return StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))

    slot: AppsSlot = request.getfixturevalue("apps_slot")

    return _legacy(slot, AppServer.LEGACY)


@pytest.fixture
async def served(
    edition_endpoint: HttpEndpoint | StdioCommand,
) -> AsyncIterator[McpToolServer]:
    """Открытый порт обычного сервера редакции параметра."""
    server = McpToolServer("standard", _config(edition_endpoint), McpServers.OWN)
    await server.open()
    try:
        yield server
    finally:
        await server.close()


@pytest.fixture
async def stdio() -> AsyncIterator[McpToolServer]:
    endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
    server = McpToolServer("standard", _config(endpoint), McpServers.OWN)
    await server.open()
    try:
        yield server
    finally:
        await server.close()


@pytest.fixture
def http_port() -> Iterator[int]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    process = subprocess.Popen(
        [sys.executable, str(SERVER), "streamable-http", str(port)]
    )
    try:
        yield port
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
async def http(http_port: int) -> AsyncIterator[McpToolServer]:
    endpoint = HttpEndpoint(
        scheme="http", host="127.0.0.1", port=http_port, path="/mcp"
    )
    server = McpToolServer("standard", _config(endpoint), McpServers.OWN)
    for _ in range(100):
        try:
            await server.open()
        except McpClientError:
            await asyncio.sleep(0.1)
            continue

        break

    try:
        yield server
    finally:
        await server.close()


class HeardCall:
    """Обработчик прогресса вызова в тесте: копит уведомления сервера о
    ходе одного вызова в порядке прихода — числа, текст и _meta отчётов."""

    def __init__(self) -> None:
        self.reports: list[tuple[float, float | None, str | None]] = []
        self.metas: list[dict[str, object] | None] = []

    async def __call__(self, params: mt.ProgressNotificationParams) -> None:
        self.reports.append((params.progress, params.total, params.message))
        self.metas.append(params.meta)


class HeardJournals:
    """Слушатель журналов вызова в тесте: копит журналы, которые вызов
    открыл на сервере, в порядке, в каком порт их узнал из списка ресурсов."""

    def __init__(self) -> None:
        self.opened: list[JournalOpening] = []

    async def __call__(self, opening: JournalOpening) -> None:
        self.opened.append(opening)


class TestStandardServer:
    """Ответ tools/call — как есть; текст для модели — правила MCP поверх
    него (McpResults)."""

    async def test_tools_are_listed_with_the_server_schema(
        self, served: McpToolServer
    ) -> None:
        names = sorted(tool.name for tool in served.tools())
        if names != _tools_of(served):
            raise AssertionError(f"the server's tools as listed: {names}")

        add = next(tool for tool in served.tools() if tool.name == "add")
        if sorted(add.input_schema["properties"]) != ["a", "b"]:
            raise AssertionError(f"the server's schema is kept: {add.input_schema}")

    async def test_annotations_become_hints(self, served: McpToolServer) -> None:
        stubs = McpToolStubs()
        by_name = {tool.name: tool for tool in served.tools()}

        assert stubs.hints_of(by_name["add"]) == ToolHints(
            read_only=True, destructive=True
        )
        assert stubs.hints_of(by_name["shout"]) == ToolHints(
            read_only=False, destructive=False
        )
        assert stubs.hints_of(by_name["broken"]) == ToolHints()

    async def test_standard_server_declares_no_boba_features(
        self, served: McpToolServer
    ) -> None:
        if served.features():
            raise AssertionError(
                f"a foreign server has no features: {served.features()}"
            )

        if served.nested_calls() is not None:
            raise AssertionError("a foreign server declares no nested calls")

    async def test_extension_settings_off_the_model_do_not_fail_the_open(
        self,
    ) -> None:
        """Лишнее поле в настройках расширения не мешает его разобрать, а
        настройки без обязательного поля оставляют сервер без расширения:
        open() не падает."""
        endpoint = StdioCommand(
            command=sys.executable, args=(str(SERVER), "stdio-extended")
        )
        server = McpToolServer("standard", _config(endpoint), McpServers.OWN)
        await server.open()
        try:
            links = server.journal_links()
        finally:
            await server.close()

        if links is None:
            raise AssertionError("the journal extension with an extra field is parsed")

        read = links.address(f"journal://r1/n1/{ToolChannel.STDOUT.value}")
        expected = JournalRead(run="r1", node="n1", channel=ToolChannel.STDOUT)
        if read != expected:
            raise AssertionError(f"the template of the server is used: {read}")

        if server.nested_calls() is not None:
            raise AssertionError("broken nested calls settings declare nothing")

    async def test_text_result_becomes_a_tool_message(
        self, served: McpToolServer
    ) -> None:
        reply = await served.call(_call("add", a=2, b=3), None, None)
        result = McpResults().result("add", "c1", reply)

        if not isinstance(result, MarkdownResult) or result.text != "5":
            raise AssertionError(f"the text of the result is the content: {result}")

    async def test_tool_error_is_an_error_message_with_the_server_text(
        self, served: McpToolServer
    ) -> None:
        reply = await served.call(_call("broken"), None, None)
        result = McpResults().result("broken", "c1", reply)

        if not isinstance(reply, mt.CallToolResult) or not reply.is_error:
            raise AssertionError(f"the server reports isError: {reply}")
        if not isinstance(result, ErrorResult):
            raise AssertionError(f"the failure is an error artifact: {result}")
        if result.error_kind != McpFailure.TOOL_ERROR:
            raise AssertionError(f"the kind names the mcp tool error: {result}")
        if "no such table: orders" not in result.message:
            raise AssertionError(f"the server's text is kept: {result.message}")

    async def test_non_text_block_is_named_in_the_text(
        self, served: McpToolServer
    ) -> None:
        reply = await served.call(_call("picture"), None, None)
        text = McpResults().result("picture", "c1", reply).llm_view()

        if text != "[image image/png]":
            raise AssertionError(f"an image block is named, not dropped: {text}")

    async def test_model_gets_its_blocks_text_and_marks(
        self, served: McpToolServer
    ) -> None:
        reply = await served.call(_call("blocks", empty=False), None, None)
        text = McpResults().result("blocks", "c1", reply).llm_view()

        expected = (
            "for the model\n"
            "[resource_link report.csv text/csv]\n"
            "[resource file:///notes.txt text/plain]"
        )
        if text != expected:
            raise AssertionError(
                f"the model gets its text blocks and marks of the others: {text}"
            )

    async def test_structured_content_without_blocks_is_json(
        self, served: McpToolServer
    ) -> None:
        reply = await served.call(_call("blocks", empty=True), None, None)
        text = McpResults().result("blocks", "c1", reply).llm_view()

        if text != '{\n  "rows": 2\n}':
            raise AssertionError(f"structured content becomes JSON text: {text}")

    async def test_input_required_comes_back_as_is(self, stdio: McpToolServer) -> None:
        reply = await stdio.call(_call("ask", refuse=False), None, None)

        if not isinstance(reply, mt.InputRequiredResult):
            raise AssertionError(f"the request for input is returned: {reply}")
        if not reply.request_state:
            raise AssertionError(f"the state of the server is kept: {reply}")

    async def test_answered_input_request_finishes_the_call(
        self, stdio: McpToolServer
    ) -> None:
        """Форма сервера и повтор вызова с ответом и тем же requestState."""
        asked = await stdio.call(_call("load_rows", rows=10), None, None)
        if not isinstance(asked, mt.InputRequiredResult):
            raise AssertionError(f"the server asks for the target first: {asked}")

        requests = asked.input_requests or {}
        form = requests["target"]
        if not isinstance(form, mt.ElicitRequest):
            raise AssertionError(f"the input request is a form: {form}")

        answer = mt.ElicitResult(action="accept", content={"table": "sales.orders"})
        again = _call("load_rows", rows=10).model_copy(
            update={
                "input_responses": {"target": answer},
                "request_state": asked.request_state,
            }
        )
        reply = await stdio.call(again, None, None)
        text = McpResults().result("load_rows", "c1", reply).llm_view()

        if text != "loaded 10 rows into sales.orders":
            raise AssertionError(f"the answer reaches the server: {text}")

    async def test_server_error_goes_up_as_is(self, served: McpToolServer) -> None:
        with pytest.raises(MCPError) as refused:
            await served.call(_call("ask", refuse=True), None, None)

        error = refused.value.error
        if error.code != mt.INVALID_PARAMS or "refuses" not in error.message:
            raise AssertionError(f"the server's error is kept: {error}")
        if error.data != {"reason": "the stand refuses"}:
            raise AssertionError(f"the data of the error is kept: {error}")

    async def test_progress_of_the_call_reaches_the_callback(
        self, served: McpToolServer
    ) -> None:
        heard = HeardCall()

        await served.call(_call("progress", steps=2), heard, None)

        expected = [(1.0, 2.0, "step 1 of 2"), (2.0, 2.0, "step 2 of 2")]
        if heard.reports != expected:
            raise AssertionError(f"the reports arrive as they were made: {heard}")


class TestStandardServerOverHttp:
    async def test_call_goes_over_streamable_http(self, http: McpToolServer) -> None:
        reply = await http.call(_call("shout", text="over http"), None, None)
        text = McpResults().result("shout", "c1", reply).llm_view()

        if text != "OVER HTTP":
            raise AssertionError(f"the call reaches the http server: {text}")


class TestUnreachableServer:
    async def test_missing_command_is_a_client_error(self) -> None:
        endpoint = StdioCommand(command="/nonexistent/mcp-server")
        server = McpToolServer("ghost", _config(endpoint), McpServers.OWN)

        with pytest.raises(McpClientError, match=r"ghost|nonexistent"):
            await server.open()

    async def test_tools_before_open_is_a_client_error(self) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        server = McpToolServer("standard", _config(endpoint), McpServers.OWN)

        with pytest.raises(McpClientError, match="before open"):
            server.tools()

    async def test_lost_session_fails_the_call_and_the_next_call_reconnects(
        self, http_port: int, http: McpToolServer
    ) -> None:
        await http.close()

        reply = await http.call(_call("add", a=4, b=4), None, None)
        text = McpResults().result("add", "c1", reply).llm_view()

        if text != "8":
            raise AssertionError(f"the closed session is opened again: {text}")


@pytest.mark.integration
class TestPublicServers:
    """Чужие реализации MCP в сети; без сети тесты падают с причиной."""

    DEEPWIKI = HttpEndpoint(scheme="https", host="mcp.deepwiki.com", path="/mcp")
    CONTEXT7 = HttpEndpoint(scheme="https", host="mcp.context7.com", path="/mcp")

    @staticmethod
    async def _reachable(endpoint: HttpEndpoint) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                await http.get(endpoint.url())
        except httpx.HTTPError as exc:
            pytest.fail(f"{endpoint.url()} is unreachable: {exc}")

    async def test_deepwiki_of_the_initialize_era_lists_tools_and_answers_a_call(
        self,
    ) -> None:
        """deepwiki отвечает только на initialize: клиент согласует с ним
        2025-11-25 и работает тем же портом."""
        await self._reachable(self.DEEPWIKI)
        server = McpToolServer("deepwiki", _config(self.DEEPWIKI), McpServers.OWN)
        await server.open()
        try:
            versions = server.discovered().supported_versions
            names = [tool.name for tool in server.tools()]
            asked = _call(
                "read_wiki_structure", repoName="modelcontextprotocol/python-sdk"
            )
            reply = await server.call(asked, None, None)
        finally:
            await server.close()

        if versions != [Edition.LEGACY.value]:
            raise AssertionError(f"deepwiki speaks the handshake era: {versions}")
        if "read_wiki_structure" not in names:
            raise AssertionError(f"deepwiki offers its tools: {names}")

        text = McpResults().result("read_wiki_structure", "c1", reply).llm_view()
        if not text.strip():
            raise AssertionError(f"deepwiki answers with text: {reply}")

    async def test_context7_lists_tools_and_answers_a_call(self) -> None:
        await self._reachable(self.CONTEXT7)
        server = McpToolServer("context7", _config(self.CONTEXT7), McpServers.OWN)
        await server.open()
        try:
            if server.discovered().supported_versions != [Edition.MODERN.value]:
                raise AssertionError(f"context7 stays modern: {server.discovered()}")

            names = [tool.name for tool in server.tools()]
            if "resolve-library-id" not in names:
                raise AssertionError(f"context7 offers its tools: {names}")

            tool = next(t for t in server.tools() if t.name == "resolve-library-id")
            args: dict[str, object] = {}
            for name in tool.input_schema.get("required", []):
                args[name] = "fastapi"

            reply = await server.call(_call(tool.name, **args), None, None)
        finally:
            await server.close()

        text = McpResults().result(tool.name, "c1", reply).llm_view()
        if not text.strip():
            raise AssertionError(f"context7 answers with text: {reply}")


class TestServerNames:
    """Имя сервера входит в полные имена его инструментов: негодное имя
    секция [mcp.servers] не принимает."""

    @pytest.mark.parametrize(
        ("name", "complaint"),
        [("my__srv", "contains '__'"), ("srv_", "ends with '_'")],
    )
    def test_server_name_that_breaks_tool_names_is_refused(
        self, name: str, complaint: str
    ) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))

        with pytest.raises(ValidationError) as refused:
            McpServersConfig(servers={name: _config(endpoint)})

        if name not in str(refused.value) or complaint not in str(refused.value):
            raise AssertionError(f"the refusal names the server and why: {refused}")


class TestServersOfASession:
    """Порт сервера по имени: открытый, а неназванный или мёртвый — отказ."""

    @staticmethod
    def _servers(**extra: McpServerConfig) -> McpServers:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        config = McpServersConfig(servers={"standard": _config(endpoint), **extra})

        return McpServers(config)

    async def test_named_server_gives_an_opened_port(self) -> None:
        servers = self._servers()
        try:
            port = await servers.port("standard", CALLER)
            names = sorted(tool.name for tool in port.tools())
        finally:
            await servers.stop()

        if names != TestFailureStand.STANDARD_TOOLS:
            raise AssertionError(f"the named server gives its tools: {names}")

    async def test_dead_server_fails_its_sessions_until_it_comes_up(
        self, tmp_path: Path
    ) -> None:
        command = tmp_path / "late-server"
        servers = self._servers(late=_config(StdioCommand(command=str(command))))
        try:
            other = await servers.port("standard", CALLER)
            with pytest.raises(McpClientError, match="'late'"):
                await servers.port("late", CALLER)

            command.write_text(f"#!/bin/sh\nexec {sys.executable} {SERVER} stdio\n")
            command.chmod(0o755)
            late = await servers.port("late", CALLER)
        finally:
            await servers.stop()

        if not other.tools():
            raise AssertionError("a dead server does not hold back other servers")
        if not late.tools():
            raise AssertionError("the next session connects the server that came up")

    async def test_undeclared_server_name_is_an_error(self) -> None:
        servers = self._servers()
        try:
            with pytest.raises(McpClientError, match="not declared"):
                await servers.port("typo", CALLER)
        finally:
            await servers.stop()


class TestServerApps:
    """Секция apps: расширение MCP Apps объявляется только её серверу, ресурс
    приложения читается с его CSP, песочницы серверов не делят источник."""

    ORDERS: ClassVar[str] = "ui://standard/orders.html"
    ROWS: ClassVar[list[dict[str, object]]] = [
        {"id": 7, "customer": "Acme", "status": "new", "total": 120.5}
    ]
    SANDBOX: ClassVar[McpServerApps] = McpServerApps.model_validate(
        {"sandbox_url": "http://localhost:8123/mcp-apps/sandbox.html"}
    )

    async def _open(
        self, endpoint: HttpEndpoint | StdioCommand, apps: McpServerApps | None
    ) -> McpToolServer:
        config = _config(endpoint).model_copy(update={"apps": apps})
        server = McpToolServer("standard", config, McpServers.OWN)
        await server.open()

        return server

    async def test_ui_is_declared_only_to_a_server_with_apps(
        self, edition_endpoint: HttpEndpoint | StdioCommand
    ) -> None:
        drawn = await self._open(edition_endpoint, self.SANDBOX)
        plain = await self._open(edition_endpoint, None)
        try:
            by_app = await drawn.call(_call("orders", rows=self.ROWS), None, None)
            by_blocks = await plain.call(_call("orders", rows=self.ROWS), None, None)
        finally:
            await drawn.close()
            await plain.close()

        app_text = McpResults().result("orders", "c1", by_app).llm_view()
        block_text = McpResults().result("orders", "c2", by_blocks).llm_view()

        assert app_text == "1 orders are shown by their app"
        assert block_text == "1 orders:\n#7 Acme new 120.50"

    async def test_app_resource_is_read_with_its_csp(
        self, edition_endpoint: HttpEndpoint | StdioCommand
    ) -> None:
        server = await self._open(edition_endpoint, self.SANDBOX)
        try:
            read = await server.read_resource(
                mt.ReadResourceRequestParams(uri=self.ORDERS), None
            )
        finally:
            await server.close()

        assert isinstance(read, mt.ReadResourceResult)
        resource = read.contents[0]

        assert resource.mime_type == APP_MIME_TYPE
        assert isinstance(resource, mt.TextResourceContents)
        assert "ui/initialize" in resource.text
        assert resource.meta == {
            "ui": {"csp": {"resourceDomains": ["https://cdn.example.com"]}}
        }

    async def test_missing_resource_is_an_mcp_error(
        self, served: McpToolServer
    ) -> None:
        missing = mt.ReadResourceRequestParams(uri="ui://standard/missing.html")
        with pytest.raises(MCPError):
            await served.read_resource(missing, None)

    def test_sandboxes_of_two_servers_on_one_origin_are_refused(self) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        first = McpServerConfig.model_validate(
            {
                "endpoint": endpoint,
                "connect_timeout_sec": 1.0,
                "call_timeout_sec": 1.0,
                "apps": {"sandbox_url": "https://apps.example.com/one/sandbox.html"},
            }
        )
        second = first.model_copy(
            update={
                "apps": McpServerApps.model_validate(
                    {"sandbox_url": "https://apps.example.com:443/two/sandbox.html"}
                )
            }
        )

        with pytest.raises(ValidationError) as refused:
            McpServersConfig(servers={"one": first, "two": second})

        assert "'one'" in str(refused.value)
        assert "[mcp.servers.two.apps]" in str(refused.value)
        assert "https://apps.example.com" in str(refused.value)

    def test_origin_of_the_sandbox_is_scheme_host_and_port(self) -> None:
        signed = McpServerApps.model_validate(
            {"sandbox_url": "https://u:p@apps.example.com:443/mcp-apps/sandbox.html"}
        )

        assert self.SANDBOX.origin() == "http://localhost:8123"
        assert signed.origin() == "https://apps.example.com"


class BobaMcpStand:
    """Сервер boba-mcp отдельным процессом: клиент видит его только по сети,
    как в приложении."""

    REPO: Path = Path(__file__).resolve().parents[4]
    STAND: Path = REPO / "packages" / "apps" / "boba-mcp" / "tests" / "service_stand.py"

    def __init__(self, workdir: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]

        self._process = subprocess.Popen(
            [sys.executable, str(self.STAND), str(self.port), str(workdir)]
        )
        self._public = f"http://127.0.0.1:{self.port}"
        self._sign_in = ServiceSignIn(
            self._public, "stand-chat", "stand-client-secret", "stand-proxy-secret"
        )

    def endpoint(self) -> HttpEndpoint:
        """Endpoint стенда с токеном пользователя alice с ролью dev."""
        path = "/mcp/service"
        token = self._sign_in.token("alice", "dev", f"{self._public}{path}")

        return HttpEndpoint(
            scheme="http",
            host="127.0.0.1",
            port=self.port,
            path=path,
            auth=BearerAuth(token=SecretStr(token)),
        )

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


@pytest.fixture
def boba_mcp_stand(tmp_path: Path) -> Iterator[BobaMcpStand]:
    process = BobaMcpStand(tmp_path)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture
async def dag(boba_mcp_stand: BobaMcpStand) -> AsyncIterator[McpToolServer]:
    server = McpToolServer("boba", _config(boba_mcp_stand.endpoint()), McpServers.OWN)
    for _ in range(150):
        try:
            await server.open()
        except McpClientError:
            await asyncio.sleep(0.2)
            continue

        break

    try:
        yield server
    finally:
        await server.close()


@pytest.mark.integration
class TestScopeOfTheCall:
    """Область вызова доезжает до тела инструмента сервера."""

    THREAD: ClassVar[str] = "5d0c0a52-7f0e-4c56-9d55-0b7a5f0f3c21"

    async def test_scope_of_the_call_travels_to_the_server(
        self, dag: McpToolServer
    ) -> None:
        reply = await dag.call(_call("fake_scope", thread_id=self.THREAD), None, None)
        text = McpResults().result("fake_scope", "c1", reply).llm_view()

        if self.THREAD not in text:
            raise AssertionError(f"the body runs in the caller's scope: {text}")


@pytest.mark.integration
class TestWorkspaceFiles:
    """Файлы workspace на сервере: клиент пишет и читает их потоком."""

    SCOPE: ClassVar[str] = "6c1b2f7e-9a44-4d0a-8e13-51d2a7b0c4f1"

    @staticmethod
    async def _source(payload: bytes, size: int) -> AsyncIterator[bytes]:
        for start in range(0, len(payload), size):
            yield payload[start : start + size]

    async def test_uploaded_stream_is_read_back_by_window(
        self, dag: McpToolServer
    ) -> None:
        files = dag.files()
        if files is None:
            raise AssertionError(f"the server declares its files: {dag.features()}")

        payload = bytes(range(256)) * 2048
        key = ObjectKey(user_id="-", thread_id=self.SCOPE, name="data.bin")
        stored = await files.upload(key, self._source(payload, 50000))
        stat = await files.stat(key)
        async with await files.open(key, ReadWindow(offset=100, length=1000)) as part:
            window = b"".join([chunk async for chunk in part.chunks])
        async with await files.open(key, ReadWindow.entire()) as whole:
            body = b"".join([chunk async for chunk in whole.chunks])
        beyond = await files.open(key, ReadWindow(offset=len(payload) + 5, length=None))
        await beyond.close()

        if stored != len(payload) or stat.size != len(payload):
            raise AssertionError(f"the size is kept: {stored} {stat}")
        if stat.revision == 0:
            raise AssertionError(f"the revision is known: {stat}")
        if window != payload[100:1100] or part.stat.size != len(payload):
            raise AssertionError("the window is the asked slice of the file")
        if body != payload:
            raise AssertionError("the file is read back as written")
        if beyond.stat.size != len(payload):
            raise AssertionError(f"a window past the end names the size: {beyond.stat}")

    async def test_missing_file_is_a_storage_not_found_error(
        self, dag: McpToolServer
    ) -> None:
        files = dag.files()
        if files is None:
            raise AssertionError(f"the server declares its files: {dag.features()}")

        key = ObjectKey(user_id="-", thread_id=self.SCOPE, name="ghost.bin")
        with pytest.raises(StorageNotFoundError, match="404"):
            await files.stat(key)
        with pytest.raises(StorageNotFoundError, match="404"):
            await files.open(key, ReadWindow.entire())
        if await files.delete(key):
            raise AssertionError("a missing file is not deleted")

    async def test_deleting_the_scope_removes_its_files(
        self, dag: McpToolServer
    ) -> None:
        """Удаление области одной операцией: файлы всех её каталогов уходят,
        повторное удаление пустой области — успех."""
        files = dag.files()
        if files is None:
            raise AssertionError(f"the server declares its files: {dag.features()}")

        scope = str(uuid4())
        upload = ObjectKey(user_id="-", thread_id=scope, name="a.txt")
        diagram = ObjectKey(
            user_id="-", thread_id=scope, name="d.mmd", dir=ThreadDir.MERMAID
        )
        await files.upload(upload, self._source(b"attached", 4))
        await files.upload(diagram, self._source(b"graph TD", 4))

        await files.delete_scope(scope)
        await files.delete_scope(scope)

        for key in (upload, diagram):
            with pytest.raises(StorageNotFoundError, match="404"):
                await files.stat(key)

    async def test_upload_tool_is_for_the_app_only(self, dag: McpToolServer) -> None:
        """Файлы шлёт сам клиент: инструмент адреса загрузки сервер помечает
        видимым только интерфейсу, модели его прячет ретранслятор."""
        visible: dict[str, object] = {}
        for tool in dag.tools():
            meta = tool.meta
            if meta is None:
                continue

            visible[tool.name] = meta.get("ui")

        if visible.get("file_upload") != {"visibility": ["app"]}:
            raise AssertionError(f"the upload tool is for the app only: {visible}")


@pytest.mark.integration
class TestBobaMcpJournal:
    """Вызов своего сервера: отчёты о ходе работы и журнал по адресу из
    итога."""

    async def test_reports_of_the_server_reach_the_callback_of_the_call(
        self, dag: McpToolServer
    ) -> None:
        heard = HeardCall()

        reply = await dag.call(_call("fake_progress", steps=2, pause=0.6), heard, None)

        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the call succeeds: {reply}")
        if heard.reports != [(1.0, 2.0, "step 1 of 2"), (2.0, 2.0, "step 2 of 2")]:
            raise AssertionError(f"the reports arrive as they were made: {heard}")

    async def test_reports_of_workflow_nodes_name_their_node_in_meta(
        self, dag: McpToolServer
    ) -> None:
        """Уведомление доходит до вызова целиком: ключ узла связки лежит в
        _meta под ключом, объявленным расширением вложенных вызовов."""
        nested = dag.nested_calls()
        if nested is None:
            raise AssertionError(f"the server declares nested calls: {dag.features()}")

        nodes = [
            {"key": "left", "tool": "fake_progress", "args": {"steps": 2, "pause": 0.6}}
        ]
        heard = HeardCall()

        reply = await dag.call(_call("workflow", nodes=nodes), heard, None)

        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the workflow succeeds: {reply}")

        expected = [{nested.progress_key: "left"}, {nested.progress_key: "left"}]
        if heard.metas != expected:
            raise AssertionError(f"every report names its node: {heard.metas}")

    async def test_journal_is_read_by_the_address_of_the_result(
        self, dag: McpToolServer
    ) -> None:
        """Журнал идущего вызова порт узнаёт из resources/list по смене
        списка ресурсов: адрес тот же, что у ссылки в итоге, а окно журнала
        читает resources/read."""
        heard = HeardCall()
        journals = HeardJournals()
        reply = await dag.call(_call("fake_echo", text="hi", repeat=2), heard, journals)
        if not isinstance(reply, mt.CallToolResult):
            raise AssertionError(f"the call is answered: {reply}")

        stdout = _stdout(dag, reply)
        window = await dag.journal(stdout)

        if heard.reports:
            raise AssertionError(f"a silent tool sends no progress: {heard.reports}")
        expected = [JournalOpening(run=stdout.run, call=stdout.node, node=None)]
        if journals.opened != expected:
            raise AssertionError(f"the journal is named once: {journals.opened}")
        if window is None or "echo progress: hi" not in window.text:
            raise AssertionError(f"the journal is read from the server: {window}")
        if not window.closed:
            raise AssertionError(f"the journal of a finished call is closed: {window}")

    async def test_journal_channel_is_downloaded_whole_and_by_range(
        self, dag: McpToolServer
    ) -> None:
        reply = await dag.call(_call("fake_echo", text="hi", repeat=2), None, None)
        if not isinstance(reply, mt.CallToolResult):
            raise AssertionError(f"the call is answered: {reply}")

        files = dag.journal_files()
        if files is None:
            raise AssertionError(f"the journal is addressable: {reply}")

        address = _stdout(dag, reply)
        rel = f"{address.run}/{address.node}/{address.channel.value}"
        whole = await files.relay(rel, "")
        body = b"".join([chunk async for chunk in whole.chunks])
        await whole.release()
        part = await files.relay(rel, "bytes=0-3")
        head = b"".join([chunk async for chunk in part.chunks])
        await part.release()
        with pytest.raises(StorageNotFoundError):
            await files.relay(f"{address.run}/ghost/stdout", "")

        if whole.status != 200 or b"echo progress: hi" not in body:
            raise AssertionError(f"the channel is downloaded whole: {body[:200]!r}")
        if "attachment" not in whole.headers.get("content-disposition", ""):
            raise AssertionError(f"the server names the file: {whole.headers}")
        if part.status != 206 or head != body[:4]:
            raise AssertionError(f"a range is relayed as 206: {part.status} {head!r}")

    async def test_missing_journal_is_no_window(self, dag: McpToolServer) -> None:
        if JournalFeature.ID not in dag.features():
            raise AssertionError(f"the journal is declared: {dag.features()}")

        request = JournalRead(run="nope", node="nope", channel=ToolChannel.STDOUT)
        window = await dag.journal(request)

        if window is not None:
            raise AssertionError(f"an unknown call has no journal: {window}")

    async def test_standard_server_has_no_journal(self, stdio: McpToolServer) -> None:
        request = JournalRead(run="r", node="n", channel=ToolChannel.STDOUT)

        window = await stdio.journal(request)

        if window is not None:
            raise AssertionError(f"a standard server keeps no journal: {window}")


@pytest.mark.integration
class TestSignInAsTheUser:
    """Вход proxy: клиент утверждает логин и роли пользователя подписанным
    JWT, сервис меняет утверждение на свой токен и исполняет вызовы от его
    имени."""

    SECRET: SecretStr = SecretStr("stand-proxy-secret")
    CLIENT_SECRET: SecretStr = SecretStr("stand-client-secret")

    def _auth(
        self, process: BobaMcpStand, secret: SecretStr, client_secret: SecretStr
    ) -> ProxyAuth:
        return ProxyAuth(
            issuer=HttpLocation(
                scheme="http", host="127.0.0.1", port=process.port, path="/"
            ),
            client_id="stand-chat",
            client_secret=client_secret,
            secret=secret,
        )

    def _servers(
        self, process: BobaMcpStand, secret: SecretStr, client_secret: SecretStr
    ) -> McpServers:
        auth = self._auth(process, secret, client_secret)
        wide = process.endpoint().model_copy(update={"auth": auth})
        narrow = wide.model_copy(update={"path": "/mcp/narrow"})
        config = McpServersConfig(
            servers={"boba": _config(wide), "narrow": _config(narrow)}
        )

        return McpServers(config)

    async def _port(
        self, servers: McpServers, caller: McpCaller, name: str
    ) -> McpToolServer:
        for _ in range(150):
            try:
                return await servers.port(name, caller)
            except McpClientError:
                await asyncio.sleep(0.2)

        return await servers.port(name, caller)

    async def test_journal_growth_is_heard_only_by_its_caller(  # noqa: PLR0915 — один идущий вызов на все подписки
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        """Ссылку на канал журнала сервер называет, пока вызов идёт;
        subscriptions/listen по ней отдаёт события роста канала тому, кто
        вызывал. Чужому пользователю, подписке на канал без записи и на uri
        не журнала сервер отказывает до подтверждения."""
        servers = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        owner = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        stranger = McpCaller(login="petrov", roles=frozenset({"dev"}))
        heard = HeardCall()
        journals = HeardJournals()
        events: list[str] = []
        try:
            port = await self._port(servers, owner, "boba")
            other = await self._port(servers, stranger, "boba")
            links = port.journal_links()
            if links is None:
                raise AssertionError("the server declares its journal")

            slow = _call("fake_progress", steps=8, pause=0.5)
            running = asyncio.create_task(port.call(slow, heard, journals))
            async with asyncio.timeout(30):
                while not journals.opened:
                    await asyncio.sleep(0.05)

                opened = journals.opened[0]
                read = JournalRead(
                    run=opened.run, node=opened.call, channel=ToolChannel.STDOUT
                )
                uri = links.uri(read)

                # канал появляется с первой записью: так панель и подписывается
                while await port.journal(read) is None:
                    await asyncio.sleep(0.05)

            with pytest.raises(MCPError) as refused:
                async with other.listen(self._channel(uri)):
                    pass

            # у запуска нет такого вызова: журнала нет, подписка на него — отказ
            absent = links.uri(read.model_copy(update={"node": "no-such-call"}))
            with pytest.raises(MCPError) as missing:
                async with port.listen(self._channel(absent)):
                    pass

            with pytest.raises(MCPError) as alien:
                async with port.listen(self._channel("file:///x")):
                    pass

            channel = self._channel(uri)
            async with asyncio.timeout(30), port.listen(channel) as subscription:
                async for event in subscription:
                    events.append(getattr(event, "uri", ""))
                    window = await port.journal(read)
                    if window is not None and window.closed:
                        break

            await running
        finally:
            await servers.stop()

        if refused.value.error.code != mt.INVALID_PARAMS:
            raise AssertionError(f"a foreign run is not found: {refused.value}")
        if "another user" not in str(refused.value):
            raise AssertionError(f"a foreign run is refused: {refused.value}")
        if "no journal on channel" not in str(missing.value):
            raise AssertionError(f"a missing channel is refused: {missing.value}")
        if "expected a journal channel resource" not in str(alien.value):
            raise AssertionError(f"a uri off the journal is refused: {alien.value}")
        if not events:
            raise AssertionError("the growth of the channel is heard")
        if set(events) != {uri}:
            raise AssertionError(f"events name the channel: {events}")

    @staticmethod
    def _channel(uri: str) -> mt.SubscriptionFilter:
        return mt.SubscriptionFilter(resource_subscriptions=[uri])

    async def test_body_runs_as_the_user_of_the_client(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        servers = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            port = await self._port(servers, caller, "boba")
            reply = await port.call(_call("fake_whoami"), None, None)
        finally:
            await servers.stop()

        text = McpResults().result("fake_whoami", "c1", reply).llm_view()
        if not text.startswith("ivanov|dev|"):
            raise AssertionError(f"the body sees the user and the roles: {text}")

    async def test_endpoint_gives_the_tools_of_its_profile(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        servers = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            await self._port(servers, caller, "boba")
            port = await servers.port("narrow", caller)
            names = sorted(tool.name for tool in port.tools())
        finally:
            await servers.stop()

        if names != ["fake_echo", "file_upload"]:
            raise AssertionError(f"the endpoint decides the tools: {names}")

    async def test_endpoint_outside_the_roles_refuses_the_port(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        servers = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        weak = McpCaller(login="petrov", roles=frozenset({"weak"}))
        try:
            await self._port(servers, weak, "boba")
            with pytest.raises(McpClientError, match=r"'narrow'.*HTTP 403"):
                await servers.port("narrow", weak)
        finally:
            await servers.stop()

    async def test_forged_signature_is_refused(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        """proxy_secret обязан совпадать у клиента и сервера: чужой ключ
        утверждения сервер отвергает и называет секрет, который не сошёлся."""
        honest = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        forged = self._servers(boba_mcp_stand, SecretStr("forged"), self.CLIENT_SECRET)
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            await self._port(honest, caller, "boba")
            with pytest.raises(McpClientError) as refused:
                await forged.port("boba", caller)
        finally:
            await honest.stop()
            await forged.stop()

        reason = str(refused.value)
        if "invalid_grant" not in reason:
            raise AssertionError(f"a forged assertion is invalid_grant: {reason}")

        if "[auth.proxy].secret" not in reason:
            raise AssertionError(f"the refusal names the proxy secret: {reason}")

    async def test_foreign_client_secret_is_refused(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        """Секрет клиента обязан совпадать с [mcp.clients.<id>].secret
        сервера: с чужим обмен утверждения отвергается как invalid_client."""
        honest = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        foreign = self._servers(
            boba_mcp_stand, self.SECRET, SecretStr("not-the-client-secret")
        )
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            await self._port(honest, caller, "boba")
            with pytest.raises(McpClientError) as refused:
                await foreign.port("boba", caller)
        finally:
            await honest.stop()
            await foreign.stop()

        reason = str(refused.value)
        if "invalid_client" not in reason:
            raise AssertionError(f"a foreign client secret is invalid_client: {reason}")

        if "client_secret" not in reason:
            raise AssertionError(f"the refusal names the client secret: {reason}")


@pytest.mark.integration
class TestBobaMcpServer:
    """Свой сервер: тот же стандартный путь плюс расширения сервера —
    вложенные вызовы, журнал, файлы."""

    async def test_result_carries_its_text(self, dag: McpToolServer) -> None:
        reply = await dag.call(_call("fake_echo", text="hi", repeat=2), None, None)
        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the call succeeds: {reply}")

        text = McpResults().result("fake_echo", "call-echo", reply).llm_view()
        if text != "hi hi|t0ken":
            raise AssertionError(f"the model reads the text of the result: {text}")

    async def test_nested_calls_name_the_workflow_offered_to_the_model(
        self, dag: McpToolServer
    ) -> None:
        nested = dag.nested_calls()
        if nested is None:
            raise AssertionError(f"the server declares nested calls: {dag.features()}")

        if nested != CallDag().feature():
            raise AssertionError(f"the declared shape is the workflow's: {nested}")

        names: list[str] = []
        for tool in dag.tools():
            names.append(tool.name)

        if nested.tool not in names:
            raise AssertionError(f"the nested calls tool is offered: {names}")

    async def test_workflow_answers_by_its_output_schema(
        self, dag: McpToolServer, tmp_path: Path
    ) -> None:
        """Итог связки проходит проверку outputSchema клиентом SDK и
        разбирается моделью NestedCallsOutput; карточка несёт схему сервера."""
        nodes = [
            {
                "key": "src",
                "tool": "fake_emit",
                "args": {
                    "prefix": "a",
                    "count": 2,
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

        reply = await dag.call(_call("workflow", nodes=nodes), None, None)

        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the workflow succeeds: {reply}")

        answered = NestedCallsOutput.model_validate(reply.structured_content)
        for node in answered.nodes:
            heading = reply.content[node.blocks[0] - 1]
            if heading != mt.TextContent(text=f"[{node.key}] {node.tool} — ok"):
                raise AssertionError(f"the heading of {node.key} leads its blocks")

        stubs = McpToolStubs()
        cards: dict[str, ToolCard] = {}
        for tool in dag.tools():
            cards[tool.name] = stubs.card("boba", tool)

        if cards["workflow"].output != NestedCallsOutput.model_json_schema():
            raise AssertionError(f"the card carries the output schema: {cards}")

    async def test_card_view_comes_from_the_schema_alone(
        self, dag: McpToolServer
    ) -> None:
        """Вид аргументов клиент выводит из схемы: у аргументов без типа
        содержимого особого вида нет, служебные параметры сервер не шлёт."""
        views = CallViews()
        echo = next(tool for tool in dag.tools() if tool.name == "fake_echo")
        if views.seen(echo.input_schema):
            raise AssertionError(f"plain arguments have no special view: {echo}")

        workflow = next(tool for tool in dag.tools() if tool.name == "workflow")
        if views.seen(workflow.input_schema):
            raise AssertionError(f"a built-in node has no special view: {workflow}")

    async def test_failure_names_its_cause(self, dag: McpToolServer) -> None:
        reply = await dag.call(_call("fake_echo", text="boom", repeat=1), None, None)
        if not isinstance(reply, mt.CallToolResult) or not reply.is_error:
            raise AssertionError(f"the server reports isError: {reply}")

        failure = McpResults().result("fake_echo", "call-echo", reply)
        if not isinstance(failure, FailureResult):
            raise AssertionError(f"isError is a failure for the model: {failure}")
        if "fake backend is down" not in failure.llm_view():
            raise AssertionError(f"the cause travels with the text: {failure}")

    async def test_connection_sealed_with_the_declared_key_reaches_the_body(
        self, dag: McpToolServer
    ) -> None:
        """Ключ запечатывания сервер объявляет при подключении; значение,
        запечатанное им для вызывающего, тело получает профилем."""
        declared = dag.features().get(SealFeature.ID)
        if declared is None:
            raise AssertionError(f"the server declares the seal key: {dag.features()}")

        profile = PostgresConfig.model_validate(
            {
                "host": "db.local",
                "dbname": "orders",
                "auth": {"method": "trust", "user": "u"},
            }
        )
        sealed = SealedConnection(
            name="orders",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            profile=SecretReveal.dumped(profile),
        )
        value = ConnectionSeal(SealFeature.model_validate(declared).key).seal(sealed)

        reply = await dag.call(
            _call("fake_connection_host", connection=value), None, None
        )
        result = McpResults().result("fake_connection_host", "c1", reply)

        if not result.ok or result.llm_view() != "db.local|orders":
            raise AssertionError(f"the body got the sealed profile: {result}")

    async def test_plain_name_is_refused_by_the_server(
        self, dag: McpToolServer
    ) -> None:
        reply = await dag.call(
            _call("fake_connection_host", connection="main"), None, None
        )

        if not isinstance(reply, mt.CallToolResult) or not reply.is_error:
            raise AssertionError(f"an unsealed name is refused: {reply}")

    async def test_wrong_token_is_a_client_error(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        endpoint = boba_mcp_stand.endpoint().model_copy(
            update={"auth": BearerAuth(token=SecretStr("stranger"))}
        )
        server = McpToolServer("boba", _config(endpoint), McpServers.OWN)

        for _ in range(150):
            try:
                await server.open()
            except McpClientError as exc:
                if "401" in str(exc):
                    return

                await asyncio.sleep(0.2)
                continue

            raise AssertionError("a token the server does not know must not open")

        raise AssertionError("the server never answered 401 to a stranger token")


class DeclaredClient(BaseModel):
    """Доверенный клиент из [mcp.clients.<client_id>] конфига сервиса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret: SecretStr


class BobaMcpService:
    """Настоящий процесс сервиса на его общем конфиге и site.toml дерева отладки,
    переписанных в рабочий каталог теста с тестовой базой набора: `python -m
    boba.mcp_server --config <каталог>/conf/config.toml --site
    <каталог>/conf/site.toml`, данные процесса — в debug/mcp/data. Порт и способ
    запуска стенд задаёт переопределениями [env]; вход — proxy, ключ
    утверждений и клиент берутся из [auth.proxy] и [mcp.clients] того же
    конфига."""

    HOST: str = "localhost"
    """Имя публичного адреса, который стенд задаёт сервису: издатель токена
    и адрес ресурса обязаны совпадать с тем, как клиент называет сервис."""
    PROFILE: str = "general"
    CLIENT: str = "boba-chat"
    THIRD: Path = RUNTIME.third

    def __init__(self, workdir: Path, database: str) -> None:
        with socket.socket() as probe:
            probe.bind((self.HOST, 0))
            self.port = int(probe.getsockname()[1])

        debug = StandApp.MCP.files()
        raw = AppLayers.compose(debug)
        self._proxy = bind(raw, "auth.proxy", ProxyAuthConfig)
        self._client = bind(raw, f"mcp.clients.{self.CLIENT}", DeclaredClient)

        files = StandFiles(workdir / "conf").write_debug(StandApp.MCP, database)

        self._log = (workdir / "mcp.log").open("wb")
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "boba.mcp_server",
                "--config",
                str(files.config),
                "--site",
                str(files.site),
            ],
            env={
                EnvOverride.BASE.var: str(debug.base()),
                EnvOverride.PORT.var: str(self.port),
                EnvOverride.PUBLIC_URL.var: f"http://{self.HOST}:{self.port}",
                EnvOverride.TOOL_LAUNCHER.var: "process",
                "PATH": f"{self.THIRD / 'bin'}:/usr/local/bin:/usr/bin:/bin",
                "LD_LIBRARY_PATH": str(self.THIRD / "lib"),
            },
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def endpoint(self) -> HttpEndpoint:
        auth = ProxyAuth(
            issuer=HttpLocation(
                scheme="http", host=self.HOST, port=self.port, path="/"
            ),
            client_id=self.CLIENT,
            client_secret=self._client.secret,
            secret=self._proxy.secret,
        )

        return HttpEndpoint(
            scheme="http",
            host=self.HOST,
            port=self.port,
            path=f"/mcp/{self.PROFILE}",
            auth=auth,
        )

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=30)
        self._log.close()


@pytest.mark.integration
@pytest.mark.xdist_group("boba-mcp-service")
class TestBobaMcpService:
    """Приёмка двумя процессами: сервис со всеми плагинами своего конфига,
    клиент чата входит через proxy и ходит к нему по сети."""

    CALLER: ClassVar[McpCaller] = McpCaller(login="tester", roles=frozenset({"wrt"}))

    @pytest.fixture
    def process(self, tmp_path: Path, test_database: str) -> Iterator[BobaMcpService]:
        try:
            StandApp.MCP.files()
        except ConfigLayoutError as exc:
            pytest.fail(f"the service config files are not placed: {exc}")

        process = BobaMcpService(tmp_path, test_database)
        try:
            yield process
        finally:
            process.stop()

    @pytest.fixture
    async def service(self, process: BobaMcpService) -> AsyncIterator[McpToolServer]:
        server = McpToolServer(
            "boba",
            _config(process.endpoint()).on_behalf_of(self.CALLER),
            McpServers.OWN,
        )
        try:
            for _ in range(600):
                try:
                    await server.open()
                except McpClientError:
                    await asyncio.sleep(0.2)
                    continue

                break

            yield server
        finally:
            await server.close()

    async def test_every_tool_is_listed_to_a_client_of_an_earlier_edition(
        self, service: McpToolServer, process: BobaMcpService
    ) -> None:
        """Рукопожатие 2025-11-25 клиентом официального SDK: сервис отдаёт
        такому клиенту tools/list, только если каждый инструмент всех
        плагинов проходит схему этой редакции."""
        endpoint = process.endpoint().on_behalf_of(self.CALLER)
        auth = endpoint.auth.httpx_auth(endpoint.url())
        names: list[str] = []
        cursor: str | None = None
        async with (
            create_mcp_http_client(auth=auth) as http,
            streamable_http_client(endpoint.url(), http_client=http) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            version = session.protocol_version
            while True:
                params = mt.PaginatedRequestParams(cursor=cursor)
                page = await session.list_tools(params=params)
                names.extend(tool.name for tool in page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break

        assert version == Edition.LEGACY.value
        assert sorted(names) == sorted(tool.name for tool in service.tools())

    async def test_plugin_tool_runs_in_the_service_process(
        self, service: McpToolServer
    ) -> None:
        names = [tool.name for tool in service.tools()]
        if "bash" not in names or "workflow" not in names:
            raise AssertionError(f"the service offers its plugin tools: {names}")

        reply = await service.call(
            _call("bash", command="echo from the service"), None, None
        )
        if not isinstance(reply, mt.CallToolResult):
            raise AssertionError(f"the call is answered: {reply}")

        shown: list[str] = []
        for block in reply.content:
            if not isinstance(block, mt.EmbeddedResource):
                continue

            if isinstance(block.resource, mt.TextResourceContents):
                shown.append(block.resource.text)

        if shown != ["from the service"]:
            raise AssertionError(f"the body ran in the service: {reply}")

    async def test_calls_of_one_scope_share_the_files_of_the_scope(
        self, service: McpToolServer
    ) -> None:
        """Файл, записанный вызовом области, виден следующему вызову той же
        области и не виден вызову другой."""
        first = "8a1d7f2c-3b54-4e0a-9c1e-2f4b6d8a0c11"
        other = "8a1d7f2c-3b54-4e0a-9c1e-2f4b6d8a0c12"
        results = McpResults()

        await service.call(
            _call("bash", command="echo kept > note.txt", thread_id=first), None, None
        )
        again = await service.call(
            _call("bash", command="cat note.txt", thread_id=first), None, None
        )
        apart = await service.call(
            _call("bash", command="cat note.txt", thread_id=other), None, None
        )

        seen = results.result("bash", "c1", again).llm_view()
        if "kept" not in seen:
            raise AssertionError(f"the next call of the scope sees the file: {seen}")

        missed = results.result("bash", "c2", apart).llm_view()
        if "kept" in missed:
            raise AssertionError(f"another scope does not see the file: {missed}")


class TestFailureStand:
    """Стенд воспроизводит отказы сервера: задержка дольше срока вызова,
    большой результат, обрыв посреди вызова, смена списка инструментов."""

    STANDARD_TOOLS: ClassVar[list[str]] = [
        "add",
        "archive_orders",
        "ask",
        "blocks",
        "broken",
        "bump",
        "client",
        "die",
        "grow_resources",
        "large",
        "load_rows",
        "orders",
        "picture",
        "progress",
        "refresh_orders",
        "shout",
        "show",
        "sleep_for",
        "toggle_extra",
        "toggle_prompt",
    ]
    LEGACY_TOOLS: ClassVar[list[str]] = [
        "add",
        "archive_orders",
        "ask",
        "blocks",
        "broken",
        "client",
        "forget",
        "large",
        "orders",
        "picture",
        "progress",
        "refresh_orders",
        "shout",
        "sleep_for",
        "slow_report",
        "toggle_extra",
    ]
    """Инструменты своего сервера редакции 2025-11-25 (legacy-server.mjs)."""

    async def _open(
        self, endpoint: HttpEndpoint | StdioCommand, call_timeout_sec: float
    ) -> McpToolServer:
        config = McpServerConfig(
            endpoint=endpoint,
            connect_timeout_sec=20.0,
            call_timeout_sec=call_timeout_sec,
        )
        server = McpToolServer("standard", config, McpServers.OWN)
        await server.open()

        return server

    async def test_large_result_arrives_whole(self, served: McpToolServer) -> None:
        reply = await served.call(_call("large", size=200_000), None, None)
        text = McpResults().result("large", "c1", reply).llm_view()

        assert len(text) == 200_000

    async def test_delay_beyond_the_call_timeout_is_a_client_error(
        self, edition_endpoint: HttpEndpoint | StdioCommand
    ) -> None:
        server = await self._open(edition_endpoint, call_timeout_sec=1.0)
        try:
            with pytest.raises(McpClientError, match="'sleep_for' got no answer"):
                await server.call(_call("sleep_for", seconds=5.0), None, None)
        finally:
            await server.close()

    async def test_server_dying_mid_call_is_a_client_error(self) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        server = await self._open(endpoint, call_timeout_sec=10.0)
        try:
            with pytest.raises(McpClientError, match="'die' got no answer"):
                await server.call(_call("die"), None, None)
        finally:
            await server.close()

    async def test_tool_list_is_read_again_on_refresh(
        self, served: McpToolServer
    ) -> None:
        """Список читается при открытии и заново по refreshed(): новый
        инструмент сервера появляется в списке порта, вызов уходит серверу
        как есть."""
        before = sorted(tool.name for tool in served.tools())
        toggled = await served.call(_call("toggle_extra"), None, None)
        stale = sorted(tool.name for tool in served.tools())
        fresh = await served.refreshed()
        answered = await served.call(_call("extra"), None, None)

        assert before == _tools_of(served)
        assert McpResults().result("toggle_extra", "c1", toggled).llm_view() == (
            "extra on: True"
        )
        assert stale == before
        assert sorted(tool.name for tool in fresh) == sorted([*before, "extra"])
        assert McpResults().result("extra", "c2", answered).llm_view() == (
            "extra answered"
        )


class TestStandardEdition:
    """Порт на каждом запросе и уведомлении редакции 2026-07-28 обычного
    сервера: server/discover, ресурсы и шаблоны, промпты, подсказки, потоки
    subscriptions/listen и личность клиента в _meta запроса."""

    async def test_discover_gives_capabilities_instructions_and_server_info(
        self, stdio: McpToolServer
    ) -> None:
        discovered = stdio.discovered()
        capabilities = discovered.capabilities
        meta = discovered.meta
        if meta is None:
            raise AssertionError(f"the server names itself: {discovered}")

        info = mt.Implementation.model_validate(meta[mt.SERVER_INFO_META_KEY])

        assert discovered.instructions == StandardServer.INSTRUCTIONS
        assert info == mt.Implementation(name="standard", version="1.0")
        assert capabilities.resources == mt.ResourcesCapability(
            subscribe=True, list_changed=True
        )
        assert capabilities.prompts == mt.PromptsCapability(list_changed=True)
        assert capabilities.completions == mt.CompletionsCapability()

    async def test_resources_and_templates_are_listed(
        self, served: McpToolServer
    ) -> None:
        resources = await served.list_resources(None)
        templates = await served.list_resource_templates(None)

        uris = sorted(str(resource.uri) for resource in resources.resources)
        patterns = sorted(item.uri_template for item in templates.resource_templates)

        assert StandardServer.NOTES in uris
        assert StandardServer.LOGO in uris
        assert "standard://items/{item}" in patterns

    async def test_resources_are_read_as_text_and_blob(
        self, served: McpToolServer
    ) -> None:
        notes = await served.read_resource(self._read(StandardServer.NOTES), None)
        logo = await served.read_resource(self._read(StandardServer.LOGO), None)
        item = await served.read_resource(self._read("standard://items/apple"), None)

        assert isinstance(notes, mt.ReadResourceResult)
        assert isinstance(logo, mt.ReadResourceResult)
        assert isinstance(item, mt.ReadResourceResult)
        assert notes.contents == [
            mt.TextResourceContents(
                uri=StandardServer.NOTES,
                mime_type="text/plain",
                text=StandardServer.NOTES_TEXT,
            )
        ]
        assert isinstance(logo.contents[0], mt.BlobResourceContents)
        assert isinstance(item.contents[0], mt.TextResourceContents)
        assert item.contents[0].text == "item apple"

    async def test_resource_and_prompt_asking_for_input_are_given_as_they_are(
        self, stdio: McpToolServer
    ) -> None:
        read = await stdio.read_resource(self._read("standard://ask/weather"), None)
        asking = await stdio.get_prompt(
            mt.GetPromptRequestParams(name="ask_prompt"), None
        )

        assert isinstance(read, mt.InputRequiredResult)
        assert read.request_state is not None
        assert isinstance(asking, mt.InputRequiredResult)

    async def test_missing_resource_is_not_found(self, served: McpToolServer) -> None:
        """«Ресурса нет» — -32602 в 2026-07-28 и -32002 в прежних редакциях."""
        with pytest.raises(MCPError) as missing:
            await served.read_resource(self._read("standard://nothing"), None)

        assert missing.value.error.code in McpToolServer.NOT_FOUND

    async def test_prompts_are_listed_and_given_with_both_roles(
        self, served: McpToolServer
    ) -> None:
        listed = await served.list_prompts(None)
        params = mt.GetPromptRequestParams(
            name="greet", arguments={"name": "Bob", "style": "poem"}
        )
        given = await served.get_prompt(params, None)

        names = sorted(prompt.name for prompt in listed.prompts)
        assert "greet" in names
        assert isinstance(given, mt.GetPromptResult)
        roles = [message.role for message in given.messages]
        assert roles == ["user", "assistant"]
        text = McpResults().prompt(given)
        assert "Greet Bob in the poem style." in text
        assert "[assistant] Hello, Bob!" in text

    async def test_completion_completes_prompt_and_template_arguments(
        self, served: McpToolServer
    ) -> None:
        styles = await served.complete(
            mt.CompleteRequestParams(
                ref=mt.PromptReference(name="greet"),
                argument=mt.CompletionArgument(name="style", value="p"),
            )
        )
        items = await served.complete(
            mt.CompleteRequestParams(
                ref=mt.ResourceTemplateReference(uri="standard://items/{item}"),
                argument=mt.CompletionArgument(name="item", value="ap"),
            )
        )

        assert styles.completion.values == ["plain", "poem", "pirate"]
        assert items.completion.values == ["apple", "apricot"]

    async def test_listen_hears_every_list_change_and_resource_growth(
        self, stdio: McpToolServer
    ) -> None:
        """Поток подтверждает весь набор и приносит смену списков
        инструментов, промптов и ресурсов и рост подписанного ресурса."""
        notifications = mt.SubscriptionFilter(
            tools_list_changed=True,
            prompts_list_changed=True,
            resources_list_changed=True,
            resource_subscriptions=[StandardServer.COUNTER],
        )
        heard: list[str] = []
        async with asyncio.timeout(30), stdio.listen(notifications) as subscription:
            honored = subscription.honored
            await stdio.call(_call("toggle_extra"), None, None)
            await stdio.call(_call("toggle_prompt"), None, None)
            await stdio.call(_call("grow_resources"), None, None)
            await stdio.call(_call("bump"), None, None)
            async for event in subscription:
                heard.append(type(event).__name__)
                if len(heard) == 4:
                    break

        assert honored == notifications
        assert sorted(heard) == [
            "PromptsListChanged",
            "ResourceUpdated",
            "ResourcesListChanged",
            "ToolsListChanged",
        ]

    async def test_relayed_identity_reaches_the_server(self) -> None:
        """Порт от личности агента: сервер видит его clientInfo и
        возможности, а расширения чата — поверх них."""
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        config = McpServersConfig(servers={"standard": _config(endpoint)})
        agent = McpClientIdentity(
            info=mt.Implementation(name="boba-cli", version="9.9"),
            capabilities=mt.ClientCapabilities(
                elicitation=mt.ElicitationCapability(
                    form=mt.FormElicitationCapability()
                )
            ),
        )
        servers = McpServers(config)
        try:
            relayed = await servers.relayed("standard", CALLER, agent)
            own = await servers.port("standard", CALLER)
            seen = await relayed.call(_call("client"), None, None)
            ours = await relayed.call(_call("client"), None, None)
            chat = await own.call(_call("client"), None, None)
        finally:
            await servers.stop()

        assert relayed is not own
        assert isinstance(seen, mt.CallToolResult)
        assert isinstance(ours, mt.CallToolResult)
        assert isinstance(chat, mt.CallToolResult)
        declared = json.loads(McpResults().result("client", "c1", seen).llm_view())
        mine = json.loads(McpResults().result("client", "c3", chat).llm_view())
        assert declared["info"] == {"name": "boba-cli", "version": "9.9"}
        assert declared["capabilities"]["elicitation"] == {"form": {}}
        assert sorted(declared["capabilities"]["extensions"]) == sorted(
            [JournalFeature.ID, SealFeature.ID]
        )
        assert mine["info"] == McpServers.OWN.info.model_dump(exclude_none=True)
        assert "elicitation" not in mine["capabilities"]

    @staticmethod
    def _read(uri: str) -> mt.ReadResourceRequestParams:
        return mt.ReadResourceRequestParams(uri=uri)


@pytest.mark.integration
class TestHandshakeEdition:
    """Различия редакции 2025-11-25, которых требует протокол: версию
    согласует initialize после отказа в server/discover, описание сервера —
    без смены списков и подписки, потоков subscriptions/listen нет, запросов
    сервера посреди вызова клиент не объявляет, конверт личности ставит сама
    сессия, а забытую сервером сессию клиент открывает заново. То же
    согласование — по stdio и на официальных примерах MCP Apps без сессий."""

    ENVELOPE: ClassVar[dict[str, object]] = {
        mt.PROTOCOL_VERSION_META_KEY: Edition.MODERN.value,
        mt.CLIENT_INFO_META_KEY: {"name": "boba-cli", "version": "9.9"},
        mt.CLIENT_CAPABILITIES_META_KEY: {},
    }
    """Конверт 2026-07-28 в _meta запроса агента, как его прислал агент."""
    TRACE: ClassVar[str] = "com.example/trace"

    @pytest.fixture
    async def legacy(self, apps_slot: AppsSlot) -> AsyncIterator[McpToolServer]:
        endpoint = _legacy(apps_slot, AppServer.LEGACY)
        server = McpToolServer("legacy", _config(endpoint), McpServers.OWN)
        await server.open()
        try:
            yield server
        finally:
            await server.close()

    async def test_server_is_described_by_its_initialize_without_streams(
        self, legacy: McpToolServer
    ) -> None:
        discovered = legacy.discovered()
        capabilities = discovered.capabilities
        meta = discovered.meta
        if meta is None:
            raise AssertionError(f"the server names itself: {discovered}")

        info = mt.Implementation.model_validate(meta[mt.SERVER_INFO_META_KEY])

        assert discovered.supported_versions == [Edition.LEGACY.value]
        assert discovered.instructions == StandardServer.INSTRUCTIONS
        assert info == mt.Implementation(name="legacy-standard", version="1.0")
        assert capabilities.tools == mt.ToolsCapability(list_changed=False)
        assert capabilities.resources == mt.ResourcesCapability(
            subscribe=False, list_changed=False
        )
        assert capabilities.prompts == mt.PromptsCapability(list_changed=False)
        assert capabilities.completions == mt.CompletionsCapability()

    async def test_listen_is_refused_with_the_reason(
        self, legacy: McpToolServer
    ) -> None:
        notifications = mt.SubscriptionFilter(tools_list_changed=True)

        with pytest.raises(McpClientError, match="no listen streams"):
            async with legacy.listen(notifications):
                pass

    async def test_requests_of_the_server_and_the_envelope_stay_with_the_client(
        self, apps_slot: AppsSlot
    ) -> None:
        """Агенту с вопросами, сэмплингом и корнями сервер рукопожатия их не
        слышит и пользователя не спрашивает; расширения чата остаются. Ключи
        конверта агента из _meta запроса до сервера не доходят, свои ключи
        запроса доходят."""
        endpoint = _legacy(apps_slot, AppServer.LEGACY)
        config = McpServersConfig(servers={"legacy": _config(endpoint)})
        agent = McpClientIdentity(
            info=mt.Implementation(name="boba-cli", version="9.9"),
            capabilities=mt.ClientCapabilities(
                elicitation=mt.ElicitationCapability(
                    form=mt.FormElicitationCapability()
                ),
                sampling=mt.SamplingCapability(),
                roots=mt.RootsCapability(list_changed=True),
            ),
        )
        meta = mt.RequestParamsMeta()
        for key, value in self.ENVELOPE.items():
            meta[key] = value

        meta[self.TRACE] = "t1"
        traced = mt.CallToolRequestParams(name="client", arguments={}, _meta=meta)
        servers = McpServers(config)
        try:
            relayed = await servers.relayed("legacy", CALLER, agent)
            seen = await relayed.call(traced, None, None)
            asked = await relayed.call(_call("ask", refuse=False), None, None)
        finally:
            await servers.stop()

        declared = json.loads(McpResults().result("client", "c1", seen).llm_view())
        answer = McpResults().result("ask", "c2", asked).llm_view()

        assert declared["info"] == {"name": "boba-cli", "version": "9.9"}
        assert sorted(declared["capabilities"]) == ["extensions"]
        assert sorted(declared["capabilities"]["extensions"]) == sorted(
            [JournalFeature.ID, SealFeature.ID]
        )
        assert sorted(declared["meta"]) == sorted(["progressToken", self.TRACE])
        assert answer == "nobody is asked: the client declares no elicitation"

    async def test_forgotten_session_fails_the_call_and_the_next_call_opens_one(
        self, legacy: McpToolServer
    ) -> None:
        """Сервер забыл сессию (404 на запрос с Mcp-Session-Id): запрос
        кончается ошибкой с причиной, следующий открывает новую сессию."""
        await legacy.call(_call("forget"), None, None)
        await asyncio.sleep(0.5)

        with pytest.raises(McpClientError) as refused:
            await legacy.call(_call("add", a=1, b=2), None, None)

        reply = await legacy.call(_call("add", a=2, b=2), None, None)

        assert "forgot the session" in str(refused.value)
        assert "HTTP 404 with Mcp-Session-Id" in str(refused.value)
        assert McpResults().result("add", "c1", reply).llm_view() == "4"

    async def test_server_over_stdio_falls_back_to_initialize(
        self, apps_slot: AppsSlot
    ) -> None:
        command, *args = apps_slot.stdio()
        endpoint = StdioCommand(command=command, args=tuple(args))
        server = McpToolServer("legacy", _config(endpoint), McpServers.OWN)
        await server.open()
        try:
            versions = server.discovered().supported_versions
            reply = await server.call(_call("shout", text="over stdio"), None, None)
        finally:
            await server.close()

        assert versions == [Edition.LEGACY.value]
        assert McpResults().result("shout", "c1", reply).llm_view() == "OVER STDIO"

    @pytest.mark.parametrize(
        ("app", "tool", "arguments"),
        [
            (AppServer.SEGMENTS, "get-customer-data", {"segment": "All"}),
            (AppServer.DEBUG, "debug-tool", {"contentType": "text"}),
        ],
        ids=["segments", "debug"],
    )
    async def test_official_app_server_gives_its_tool_and_app(
        self,
        apps_slot: AppsSlot,
        app: AppServer,
        tool: str,
        arguments: dict[str, object],
    ) -> None:
        """Пример MCP Apps из npm (без сессии): инструмент со ссылкой на
        приложение, ресурс приложения его вида и вызов."""
        config = _config(_legacy(apps_slot, app)).model_copy(
            update={"apps": TestServerApps.SANDBOX}
        )
        server = McpToolServer("example", config, McpServers.OWN)
        await server.open()
        try:
            versions = server.discovered().supported_versions
            listed = next(item for item in server.tools() if item.name == tool)
            meta = listed.meta
            if meta is None:
                raise AssertionError(f"the tool names its app: {listed}")

            uri = meta["ui"]["resourceUri"]
            read = await server.read_resource(
                mt.ReadResourceRequestParams(uri=uri), None
            )
            reply = await server.call(_call(tool, **arguments), None, None)
        finally:
            await server.close()

        assert versions == [Edition.LEGACY.value]
        assert isinstance(read, mt.ReadResourceResult)
        content = read.contents[0]
        assert isinstance(content, mt.TextResourceContents)
        assert content.mime_type == APP_MIME_TYPE
        assert "<html" in content.text.lower()
        assert isinstance(reply, mt.CallToolResult)
        assert not reply.is_error, reply
