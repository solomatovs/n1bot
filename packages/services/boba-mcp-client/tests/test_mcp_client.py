"""Порт MCP-сервера: обычный сервер по stdio и HTTP, свой сервер boba-mcp,
публичные серверы в сети.

Обычный сервер написан на официальном SDK и о boba не знает: клиент обязан
работать с ним одним стандартом. Публичные серверы проверяют то же на чужих
реализациях. Ответ tools/call порт отдаёт как есть; текст для модели по
правилам MCP собирает McpResults поверх него.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import httpx
import mcp.types as mt
import pytest
from mcp.shared.dispatcher import ProgressFnT
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from boba.auth.config import ProxyAuthConfig
from boba.canvas.keys import ObjectKey
from boba.canvas.storage import StorageNotFoundError
from boba.config import bind
from boba.connections.sealed import ConnectionSeal, SealedConnection, SealFeature
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    HttpLocation,
    McpCaller,
    McpClientError,
    McpServerConfig,
    McpServers,
    McpServersConfig,
    McpToolServer,
    ProxyAuth,
    StdioCommand,
)
from boba.runtime.config import AppLayers, ConfigLayoutError, EnvOverride
from boba.stand.service_signin import ServiceSignIn
from boba.stand.ui.stand import StandApp
from boba.toolkit.calls import CallViews
from boba.toolkit.channels import ToolChannel
from boba.toolkit.dag import CallDag, DagNode, ToolCard, ToolHints
from boba.toolkit.mcp import McpFailure, McpResults, McpToolStubs
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
    MarkdownResult,
    ShellResult,
)
from boba.toolkit.types import SecretReveal
from boba.toolkit.wire import (
    JournalFeature,
    JournalRead,
    NestedCallsReply,
    ResultWire,
    WireResult,
)
from boba.workspace.launcher import ReadWindow

pytestmark = pytest.mark.anyio

SERVER = Path(__file__).with_name("standard_server.py")


def _config(endpoint: HttpEndpoint | StdioCommand) -> McpServerConfig:
    return McpServerConfig(
        endpoint=endpoint,
        connect_timeout_sec=20.0,
        call_timeout_sec=60.0,
    )


CALLER = McpCaller(login="alice", roles=frozenset({"dev"}))


def _call(name: str, **args: object) -> mt.CallToolRequestParams:
    return mt.CallToolRequestParams(name=name, arguments=args)


@pytest.fixture
async def stdio() -> AsyncIterator[McpToolServer]:
    endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
    server = McpToolServer("standard", _config(endpoint))
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
    server = McpToolServer("standard", _config(endpoint))
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


class HeardCall(ProgressFnT):
    """Реализация ProgressFnT теста: копит отчёты сервера о ходе одного
    вызова в порядке прихода."""

    def __init__(self) -> None:
        self.reports: list[tuple[float, float | None, str | None]] = []

    async def __call__(
        self, progress: float, total: float | None, message: str | None
    ) -> None:
        self.reports.append((progress, total, message))


class TestStandardServerOverStdio:
    """Ответ tools/call — как есть; текст для модели — правила MCP поверх
    него (McpResults)."""

    async def test_tools_are_listed_with_the_server_schema(
        self, stdio: McpToolServer
    ) -> None:
        names = sorted(tool.name for tool in stdio.tools())
        if names != TestFailureStand.STANDARD_TOOLS:
            raise AssertionError(f"the server's tools as listed: {names}")

        add = next(tool for tool in stdio.tools() if tool.name == "add")
        if sorted(add.input_schema["properties"]) != ["a", "b"]:
            raise AssertionError(f"the server's schema is kept: {add.input_schema}")

    async def test_annotations_become_hints(self, stdio: McpToolServer) -> None:
        stubs = McpToolStubs()
        by_name = {tool.name: tool for tool in stdio.tools()}

        assert stubs.hints_of(by_name["add"]) == ToolHints(
            read_only=True, destructive=True
        )
        assert stubs.hints_of(by_name["shout"]) == ToolHints(
            read_only=False, destructive=False
        )
        assert stubs.hints_of(by_name["broken"]) == ToolHints()

    async def test_standard_server_declares_no_boba_features(
        self, stdio: McpToolServer
    ) -> None:
        if stdio.features():
            raise AssertionError(
                f"a foreign server has no features: {stdio.features()}"
            )

        if stdio.nested_calls() is not None:
            raise AssertionError("a foreign server declares no nested calls")

    async def test_text_result_becomes_a_tool_message(
        self, stdio: McpToolServer
    ) -> None:
        reply = await stdio.call(_call("add", a=2, b=3), None, None)
        result = McpResults().result("add", "c1", reply)

        if not isinstance(result, MarkdownResult) or result.text != "5":
            raise AssertionError(f"the text of the result is the content: {result}")

    async def test_tool_error_is_an_error_message_with_the_server_text(
        self, stdio: McpToolServer
    ) -> None:
        reply = await stdio.call(_call("broken"), None, None)
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
        self, stdio: McpToolServer
    ) -> None:
        reply = await stdio.call(_call("picture"), None, None)
        text = McpResults().result("picture", "c1", reply).llm_view()

        if text != "[image image/png]":
            raise AssertionError(f"an image block is named, not dropped: {text}")

    async def test_model_gets_its_blocks_text_and_marks(
        self, stdio: McpToolServer
    ) -> None:
        reply = await stdio.call(_call("blocks", empty=False), None, None)
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
        self, stdio: McpToolServer
    ) -> None:
        reply = await stdio.call(_call("blocks", empty=True), None, None)
        text = McpResults().result("blocks", "c1", reply).llm_view()

        if text != '{\n  "rows": 2\n}':
            raise AssertionError(f"structured content becomes JSON text: {text}")

    async def test_input_required_comes_back_as_is(self, stdio: McpToolServer) -> None:
        reply = await stdio.call(_call("ask", refuse=False), None, None)

        if not isinstance(reply, mt.InputRequiredResult):
            raise AssertionError(f"the request for input is returned: {reply}")
        if not reply.request_state:
            raise AssertionError(f"the state of the server is kept: {reply}")

    async def test_server_error_goes_up_as_is(self, stdio: McpToolServer) -> None:
        with pytest.raises(MCPError) as refused:
            await stdio.call(_call("ask", refuse=True), None, None)

        error = refused.value.error
        if error.code != mt.INVALID_PARAMS or "refuses" not in error.message:
            raise AssertionError(f"the server's error is kept: {error}")
        if error.data != {"reason": "the stand refuses"}:
            raise AssertionError(f"the data of the error is kept: {error}")

    async def test_progress_of_the_call_reaches_the_callback(
        self, stdio: McpToolServer
    ) -> None:
        heard = HeardCall()

        await stdio.call(_call("progress", steps=2), None, heard)

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
        server = McpToolServer("ghost", _config(endpoint))

        with pytest.raises(McpClientError, match=r"ghost|nonexistent"):
            await server.open()

    async def test_tools_before_open_is_a_client_error(self) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        server = McpToolServer("standard", _config(endpoint))

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

    async def test_server_without_discover_is_refused_with_the_versions(
        self,
    ) -> None:
        """deepwiki отвечает только на initialize: клиент его не берёт."""
        await self._reachable(self.DEEPWIKI)
        server = McpToolServer("deepwiki", _config(self.DEEPWIKI))
        try:
            with pytest.raises(McpClientError) as refused:
                await server.open()
        finally:
            await server.close()

        text = str(refused.value)
        if "'deepwiki'" not in text:
            raise AssertionError(f"the refusal names the server: {text}")
        if "initialize" not in text:
            raise AssertionError(f"the refusal says why: {text}")
        if "2026-07-28" not in text:
            raise AssertionError(f"the refusal lists the expected versions: {text}")

    async def test_context7_lists_tools_and_answers_a_call(self) -> None:
        await self._reachable(self.CONTEXT7)
        server = McpToolServer("context7", _config(self.CONTEXT7))
        await server.open()
        try:
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
    server = McpToolServer("boba", _config(boba_mcp_stand.endpoint()))
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
        reply = await dag.call(_call("fake_scope"), self.THREAD, None)
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

    async def test_upload_tool_is_not_offered_to_the_model(
        self, dag: McpToolServer
    ) -> None:
        names = [tool.name for tool in dag.tools()]
        if "file_upload" in names:
            raise AssertionError(f"the client uploads files itself: {names}")


@pytest.mark.integration
class TestBobaMcpJournal:
    """Вызов своего сервера: отчёты о ходе работы и журнал по адресу из
    итога."""

    async def test_reports_of_the_server_reach_the_callback_of_the_call(
        self, dag: McpToolServer
    ) -> None:
        heard = HeardCall()

        reply = await dag.call(_call("fake_progress", steps=2, pause=0.6), None, heard)

        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the call succeeds: {reply}")
        if heard.reports != [(1.0, 2.0, "step 1 of 2"), (2.0, 2.0, "step 2 of 2")]:
            raise AssertionError(f"the reports arrive as they were made: {heard}")

    async def test_journal_is_read_by_the_address_of_the_result(
        self, dag: McpToolServer
    ) -> None:
        heard = HeardCall()
        call = DagNode(key="call-echo", tool="mcp__boba__fake_echo")
        reply = await dag.call(_call("fake_echo", text="hi", repeat=2), None, heard)
        if not isinstance(reply, mt.CallToolResult):
            raise AssertionError(f"the call is answered: {reply}")

        wire = WireResult(
            content=reply.content,
            structured=reply.structured_content,
            is_error=reply.is_error,
        )
        outcome = ResultWire().revived(wire, call, "boba")
        if outcome is None or outcome.journal is None:
            raise AssertionError(f"the result names its journal: {reply}")

        address = outcome.journal
        request = JournalRead(
            run=address.run, node=address.call, channel=ToolChannel.STDOUT
        )
        window = await dag.journal(request)

        if heard.reports:
            raise AssertionError(f"a silent tool sends no progress: {heard.reports}")
        if window is None or "echo progress: hi" not in window.text:
            raise AssertionError(f"the journal is read from the server: {window}")
        if not window.closed:
            raise AssertionError(f"the journal of a finished call is closed: {window}")

    async def test_journal_channel_is_downloaded_whole_and_by_range(
        self, dag: McpToolServer
    ) -> None:
        call = DagNode(key="call-echo", tool="mcp__boba__fake_echo")
        reply = await dag.call(_call("fake_echo", text="hi", repeat=2), None, None)
        if not isinstance(reply, mt.CallToolResult):
            raise AssertionError(f"the call is answered: {reply}")

        wire = WireResult(
            content=reply.content,
            structured=reply.structured_content,
            is_error=reply.is_error,
        )
        outcome = ResultWire().revived(wire, call, "boba")
        files = dag.journal_files()
        if outcome is None or outcome.journal is None or files is None:
            raise AssertionError(f"the journal is addressable: {reply}")

        address = outcome.journal
        rel = f"{address.run}/{address.call}/{ToolChannel.STDOUT.value}"
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

    def _auth(self, process: BobaMcpStand, secret: SecretStr) -> ProxyAuth:
        return ProxyAuth(
            issuer=HttpLocation(
                scheme="http", host="127.0.0.1", port=process.port, path="/"
            ),
            client_id="stand-chat",
            client_secret=SecretStr("stand-client-secret"),
            secret=secret,
        )

    def _servers(self, process: BobaMcpStand, secret: SecretStr) -> McpServers:
        auth = self._auth(process, secret)
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

    async def test_body_runs_as_the_user_of_the_client(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        servers = self._servers(boba_mcp_stand, self.SECRET)
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
        servers = self._servers(boba_mcp_stand, self.SECRET)
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            await self._port(servers, caller, "boba")
            port = await servers.port("narrow", caller)
            names = sorted(tool.name for tool in port.tools())
        finally:
            await servers.stop()

        if names != ["fake_echo"]:
            raise AssertionError(f"the endpoint decides the tools: {names}")

    async def test_endpoint_outside_the_roles_refuses_the_port(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        servers = self._servers(boba_mcp_stand, self.SECRET)
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
        honest = self._servers(boba_mcp_stand, self.SECRET)
        forged = self._servers(boba_mcp_stand, SecretStr("forged"))
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            await self._port(honest, caller, "boba")
            with pytest.raises(McpClientError, match=r"'boba'.*invalid_grant"):
                await forged.port("boba", caller)
        finally:
            await honest.stop()
            await forged.stop()


@pytest.mark.integration
class TestBobaMcpServer:
    """Свой сервер: тот же стандартный путь плюс модель результата в
    structuredContent."""

    async def test_result_carries_its_model(self, dag: McpToolServer) -> None:
        call = DagNode(key="call-echo", tool="mcp__boba__fake_echo")
        reply = await dag.call(_call("fake_echo", text="hi", repeat=2), None, None)
        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the call succeeds: {reply}")

        wire = WireResult(
            content=reply.content,
            structured=reply.structured_content,
            is_error=reply.is_error,
        )
        outcome = ResultWire().revived(wire, call, "boba")
        if outcome is None:
            raise AssertionError(f"the server sends its model: {reply}")

        artifact = outcome.artifact
        if not isinstance(artifact, MarkdownResult) or artifact.text != "hi hi|t0ken":
            raise AssertionError(f"the server's model is revived: {artifact}")

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
        разбирается моделью NestedCallsReply; карточка несёт схему сервера."""
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

        answered = NestedCallsReply.model_validate(reply.structured_content)
        for node in answered.nodes:
            heading = reply.content[node.blocks[0] - 1]
            if heading != mt.TextContent(text=f"[{node.key}] {node.tool} — ok"):
                raise AssertionError(f"the heading of {node.key} leads its blocks")

        stubs = McpToolStubs()
        cards: dict[str, ToolCard] = {}
        for tool in dag.tools():
            cards[tool.name] = stubs.card("boba", tool)

        if cards["workflow"].output != NestedCallsReply.model_json_schema():
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

    async def test_failure_keeps_the_server_failure_model(
        self, dag: McpToolServer
    ) -> None:
        call = DagNode(key="call-echo", tool="mcp__boba__fake_echo")
        reply = await dag.call(_call("fake_echo", text="boom", repeat=1), None, None)
        if not isinstance(reply, mt.CallToolResult) or not reply.is_error:
            raise AssertionError(f"the server reports isError: {reply}")

        wire = WireResult(
            content=reply.content,
            structured=reply.structured_content,
            is_error=reply.is_error,
        )
        outcome = ResultWire().revived(wire, call, "boba")
        if outcome is None:
            raise AssertionError(f"the server sends its failure model: {reply}")

        failure = outcome.artifact
        if not isinstance(failure, FailureResult):
            raise AssertionError(f"the failure model of the server: {failure}")
        if "fake backend is down" not in failure.llm_view():
            raise AssertionError(f"the cause travels with the model: {failure}")

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
        server = McpToolServer("boba", _config(endpoint))

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
    """Настоящий процесс сервиса на его общем конфиге и site.toml дерева отладки:
    `python -m boba.mcp_server --config <пакет>/conf/config.toml --site
    debug/mcp/conf/site.toml`, данные процесса — в debug/mcp/data. Порт и способ
    запуска стенд задаёт переопределениями [env]; вход — proxy, ключ
    утверждений и клиент берутся из [auth.proxy] и [mcp.clients] того же
    конфига."""

    HOST: str = "localhost"
    """Имя публичного адреса, который стенд задаёт сервису: издатель токена
    и адрес ресурса обязаны совпадать с тем, как клиент называет сервис."""
    PROFILE: str = "general"
    CLIENT: str = "boba-chat"
    THIRD: Path = BobaMcpStand.REPO / "runtime" / "third"

    def __init__(self, log: Path) -> None:
        with socket.socket() as probe:
            probe.bind((self.HOST, 0))
            self.port = int(probe.getsockname()[1])

        files = StandApp.MCP.files()
        raw = AppLayers.compose(files)
        self._proxy = bind(raw, "auth.proxy", ProxyAuthConfig)
        self._client = bind(raw, f"mcp.clients.{self.CLIENT}", DeclaredClient)
        self._log = log.open("wb")
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

    @pytest.fixture
    async def service(self, tmp_path: Path) -> AsyncIterator[McpToolServer]:
        try:
            StandApp.MCP.files()
        except ConfigLayoutError as exc:
            pytest.fail(f"the service config files are not placed: {exc}")

        process = BobaMcpService(tmp_path / "mcp.log")
        caller = McpCaller(login="tester", roles=frozenset({"wrt"}))
        server = McpToolServer("boba", _config(process.endpoint()).on_behalf_of(caller))
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
            process.stop()

    async def test_plugin_tool_runs_in_the_service_process(
        self, service: McpToolServer
    ) -> None:
        names = [tool.name for tool in service.tools()]
        if "bash" not in names or "workflow" not in names:
            raise AssertionError(f"the service offers its plugin tools: {names}")

        call = DagNode(key="call-bash", tool="mcp__boba__bash")
        reply = await service.call(
            _call("bash", command="echo from the service"), None, None
        )
        if not isinstance(reply, mt.CallToolResult):
            raise AssertionError(f"the call is answered: {reply}")

        wire = WireResult(
            content=reply.content,
            structured=reply.structured_content,
            is_error=reply.is_error,
        )
        outcome = ResultWire().revived(wire, call, "boba")
        if outcome is None:
            raise AssertionError(f"the service sends its model: {reply}")

        artifact = outcome.artifact
        if not isinstance(artifact, ShellResult):
            raise AssertionError(f"the shell model is revived: {artifact}")
        if artifact.stdout.strip() != "from the service":
            raise AssertionError(f"the body ran in the service: {artifact}")

    async def test_calls_of_one_scope_share_the_files_of_the_scope(
        self, service: McpToolServer
    ) -> None:
        """Файл, записанный вызовом области, виден следующему вызову той же
        области и не виден вызову другой."""
        first = "8a1d7f2c-3b54-4e0a-9c1e-2f4b6d8a0c11"
        other = "8a1d7f2c-3b54-4e0a-9c1e-2f4b6d8a0c12"
        results = McpResults()

        await service.call(_call("bash", command="echo kept > note.txt"), first, None)
        again = await service.call(_call("bash", command="cat note.txt"), first, None)
        apart = await service.call(_call("bash", command="cat note.txt"), other, None)

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
        "ask",
        "blocks",
        "broken",
        "die",
        "large",
        "panel_refresh",
        "picture",
        "progress",
        "shout",
        "sleep_for",
        "toggle_extra",
    ]

    async def _open(self, call_timeout_sec: float) -> McpToolServer:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        config = McpServerConfig(
            endpoint=endpoint,
            connect_timeout_sec=20.0,
            call_timeout_sec=call_timeout_sec,
        )
        server = McpToolServer("standard", config)
        await server.open()

        return server

    async def test_large_result_arrives_whole(self, stdio: McpToolServer) -> None:
        reply = await stdio.call(_call("large", size=200_000), None, None)
        text = McpResults().result("large", "c1", reply).llm_view()

        assert len(text) == 200_000

    async def test_delay_beyond_the_call_timeout_is_a_client_error(self) -> None:
        server = await self._open(call_timeout_sec=1.0)
        try:
            with pytest.raises(McpClientError, match="'sleep_for' got no answer"):
                await server.call(_call("sleep_for", seconds=5.0), None, None)
        finally:
            await server.close()

    async def test_server_dying_mid_call_is_a_client_error(self) -> None:
        server = await self._open(call_timeout_sec=10.0)
        try:
            with pytest.raises(McpClientError, match="'die' got no answer"):
                await server.call(_call("die"), None, None)
        finally:
            await server.close()

    async def test_tool_list_is_read_at_open(self, stdio: McpToolServer) -> None:
        """Список читается при открытии: сервер меняет его, уведомление
        клиент не слушает, а вызов уходит серверу как есть."""
        before = sorted(tool.name for tool in stdio.tools())
        toggled = await stdio.call(_call("toggle_extra"), None, None)
        answered = await stdio.call(_call("extra"), None, None)

        assert before == self.STANDARD_TOOLS
        assert McpResults().result("toggle_extra", "c1", toggled).llm_view() == (
            "extra on: True"
        )
        assert sorted(tool.name for tool in stdio.tools()) == before
        assert McpResults().result("extra", "c2", answered).llm_view() == (
            "extra answered"
        )
