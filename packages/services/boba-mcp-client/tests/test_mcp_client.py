"""Порт инструментов над MCP-сервером: обычный сервер по stdio и HTTP,
публичные серверы в сети.

Обычный сервер написан на официальном SDK и о boba не знает: клиент обязан
работать с ним одним стандартом. Публичные серверы проверяют то же на чужих
реализациях.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from boba.auth.config import ProxyAuthConfig
from boba.canvas.keys import ObjectKey
from boba.canvas.storage import StorageNotFoundError
from boba.config.section import bind_section
from boba.connections.sealed import ConnectionSeal, SealedConnection, SealFeature
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import CallContexts
from boba.mcp_client.client import (
    BearerAuth,
    BlockFiles,
    CallSignals,
    DroppedSignals,
    HttpEndpoint,
    HttpLocation,
    JournalListener,
    McpCaller,
    McpClientError,
    McpFailure,
    McpServerConfig,
    McpServers,
    McpServersConfig,
    McpToolServer,
    NamedBlocks,
    ProxyAuth,
    StdioCommand,
)
from boba.runtime.config import EnvOverride
from boba.stand_core.context import CallStand
from boba.toolkit.calls import FieldPlacement, FieldView
from boba.toolkit.channels import ToolChannel
from boba.toolkit.dag import (
    CallDag,
    DagNode,
    JournalAddress,
    NodeBody,
    NodeCalls,
    NodeOutcome,
    ToolCard,
    ToolServer,
    WorkflowResult,
)
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
    MarkdownResult,
    ShellResult,
)
from boba.toolkit.types import SecretReveal
from boba.toolkit.wire import JournalFeature, JournalRead, JournalSignal
from boba.toolrun.hosted import DirectCalls
from boba.workspace.launcher import ReadWindow

pytestmark = pytest.mark.anyio

SERVER = Path(__file__).with_name("standard_server.py")


def _config(endpoint: HttpEndpoint | StdioCommand, prefix: str = "") -> McpServerConfig:
    return McpServerConfig(
        endpoint=endpoint,
        prefix=prefix,
        connect_timeout_sec=20.0,
        call_timeout_sec=60.0,
    )


CALLER = McpCaller(login="alice", roles=frozenset({"dev"}))


def _call(name: str, **args: object) -> DagNode:
    return DagNode.model_validate({"key": f"call-{name}", "tool": name, "args": args})


@pytest.fixture
async def stdio() -> AsyncIterator[McpToolServer]:
    endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
    server = McpToolServer(
        "standard",
        _config(endpoint, prefix="std_"),
        NamedBlocks(),
        DroppedSignals(),
        CallContexts(),
        None,
        DirectCalls(),
    )
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
    server = McpToolServer(
        "standard",
        _config(endpoint),
        NamedBlocks(),
        DroppedSignals(),
        CallContexts(),
        None,
        DirectCalls(),
    )
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


class TestStandardServerOverStdio:
    async def test_tools_are_offered_with_the_prefix_and_the_server_schema(
        self, stdio: McpToolServer
    ) -> None:
        names = sorted(tool.name for tool in stdio.tools())
        if names != ["std_add", "std_broken", "std_picture", "std_shout"]:
            raise AssertionError(f"the server's tools under the prefix: {names}")

        add = next(tool for tool in stdio.tools() if tool.name == "std_add")
        schema = add.parameters
        if not isinstance(schema, dict):
            raise AssertionError(f"the stub carries the server's JSON schema: {schema}")
        if sorted(schema["properties"]) != ["a", "b"]:
            raise AssertionError(f"the model sees the server's arguments: {schema}")
        if add.views is not None:
            raise AssertionError(f"a standard server sends no call view: {add}")

    async def test_standard_server_declares_no_boba_features(
        self, stdio: McpToolServer
    ) -> None:
        if stdio.features():
            raise AssertionError(
                f"a foreign server has no features: {stdio.features()}"
            )

    async def test_text_result_becomes_a_tool_message(
        self, stdio: McpToolServer
    ) -> None:
        message = await stdio.call(_call("std_add", a=2, b=3))

        if message.errored or message.content != "5":
            raise AssertionError(f"the text of the result is the content: {message}")
        if not isinstance(message.artifact, MarkdownResult):
            raise AssertionError(
                f"a plain result is a text artifact: {message.artifact}"
            )
        if message.key != "call-std_add":
            raise AssertionError(f"the message answers its call: {message}")

    async def test_tool_error_is_an_error_message_with_the_server_text(
        self, stdio: McpToolServer
    ) -> None:
        message = await stdio.call(_call("std_broken"))

        if not message.errored:
            raise AssertionError(f"isError becomes the error status: {message}")

        failure = message.artifact
        if not isinstance(failure, ErrorResult):
            raise AssertionError(f"the failure is an error artifact: {failure}")
        if failure.error_kind != McpFailure.TOOL_ERROR:
            raise AssertionError(f"the kind names the mcp tool error: {failure}")
        if "no such table: orders" not in failure.message:
            raise AssertionError(f"the server's text is kept: {failure.message}")

    async def test_non_text_block_is_named_in_the_text(
        self, stdio: McpToolServer
    ) -> None:
        message = await stdio.call(_call("std_picture"))

        if "[image/png, 24 bytes]" not in str(message.content):
            raise AssertionError(f"an image block is named, not dropped: {message}")

    async def test_file_block_goes_to_the_host_of_the_client(self) -> None:
        """Картинку клиент отдаёт хозяину байтами; в тексте — его строка."""
        received: list[tuple[str, int, str, bytes]] = []

        class Recording(BlockFiles):
            async def attached(
                self, call: DagNode, index: int, mime: str, data: bytes
            ) -> str:
                received.append((call.key, index, mime, data))
                return "saved as picture.png"

        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        server = McpToolServer(
            "standard",
            _config(endpoint),
            Recording(),
            DroppedSignals(),
            CallContexts(),
            None,
            DirectCalls(),
        )
        await server.open()
        try:
            message = await server.call(_call("picture"))
        finally:
            await server.close()

        if len(received) != 1:
            raise AssertionError(f"the host gets the block once: {received}")

        call_id, index, mime, data = received[0]
        if (call_id, index, mime) != ("call-picture", 0, "image/png"):
            raise AssertionError(f"the block is described to the host: {received[0]}")
        if not data.startswith(b"\x89PNG"):
            raise AssertionError(f"the host gets the decoded bytes: {data[:8]!r}")
        if message.content != "saved as picture.png":
            raise AssertionError(f"the host's line is the text: {message}")

    async def test_calls_of_one_answer_run_together(self, stdio: McpToolServer) -> None:
        pending = await stdio.submit(
            [_call("std_add", a=1, b=1), _call("std_shout", text="hi")]
        )
        first, second = await asyncio.gather(*pending)

        if (first.content, second.content) != ("2", "HI"):
            raise AssertionError(f"each call gets its own result: {first}, {second}")


class TestStandardServerOverHttp:
    async def test_call_goes_over_streamable_http(self, http: McpToolServer) -> None:
        message = await http.call(_call("shout", text="over http"))

        if message.content != "OVER HTTP":
            raise AssertionError(f"the call reaches the http server: {message}")


class TestUnreachableServer:
    async def test_missing_command_is_a_client_error(self) -> None:
        endpoint = StdioCommand(command="/nonexistent/mcp-server")
        server = McpToolServer(
            "ghost",
            _config(endpoint),
            NamedBlocks(),
            DroppedSignals(),
            CallContexts(),
            None,
            DirectCalls(),
        )

        with pytest.raises(McpClientError, match=r"ghost|nonexistent"):
            await server.open()

    async def test_tools_before_open_is_a_client_error(self) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        server = McpToolServer(
            "standard",
            _config(endpoint),
            NamedBlocks(),
            DroppedSignals(),
            CallContexts(),
            None,
            DirectCalls(),
        )

        with pytest.raises(McpClientError, match="before open"):
            server.tools()

    async def test_lost_session_fails_the_call_and_the_next_call_reconnects(
        self, http_port: int, http: McpToolServer
    ) -> None:
        await http.close()

        message = await http.call(_call("add", a=4, b=4))

        if message.content != "8":
            raise AssertionError(f"the closed session is opened again: {message}")


@pytest.mark.integration
class TestPublicServers:
    """Чужие реализации MCP в сети; без сети тесты пропускаются."""

    DEEPWIKI = HttpEndpoint(scheme="https", host="mcp.deepwiki.com", path="/mcp")
    CONTEXT7 = HttpEndpoint(scheme="https", host="mcp.context7.com", path="/mcp")

    @staticmethod
    async def _reachable(endpoint: HttpEndpoint) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                await http.get(endpoint.url())
        except httpx.HTTPError as exc:
            pytest.skip(f"{endpoint.url()} is unreachable: {exc}")

    async def test_deepwiki_lists_tools_and_answers_a_call(self) -> None:
        await self._reachable(self.DEEPWIKI)
        server = McpToolServer(
            "deepwiki",
            _config(self.DEEPWIKI, prefix="dw_"),
            NamedBlocks(),
            DroppedSignals(),
            CallContexts(),
            None,
            DirectCalls(),
        )
        await server.open()
        try:
            names = [tool.name for tool in server.tools()]
            if "dw_read_wiki_structure" not in names:
                raise AssertionError(f"deepwiki offers its tools: {names}")

            message = await server.call(
                _call(
                    "dw_read_wiki_structure",
                    repoName="modelcontextprotocol/python-sdk",
                )
            )
        finally:
            await server.close()

        if message.errored or not str(message.content).strip():
            raise AssertionError(f"deepwiki answers with text: {message}")

    async def test_context7_lists_tools_and_answers_a_call(self) -> None:
        await self._reachable(self.CONTEXT7)
        server = McpToolServer(
            "context7",
            _config(self.CONTEXT7),
            NamedBlocks(),
            DroppedSignals(),
            CallContexts(),
            None,
            DirectCalls(),
        )
        await server.open()
        try:
            names = [tool.name for tool in server.tools()]
            if "resolve-library-id" not in names:
                raise AssertionError(f"context7 offers its tools: {names}")

            tool = next(t for t in server.tools() if t.name == "resolve-library-id")
            schema = tool.parameters
            if not isinstance(schema, dict):
                raise AssertionError(f"the stub carries a JSON schema: {schema}")

            args: dict[str, object] = {}
            for name in schema.get("required", []):
                args[name] = "fastapi"

            message = await server.call(_call("resolve-library-id", **args))
        finally:
            await server.close()

        if not str(message.content).strip():
            raise AssertionError(f"context7 answers with text: {message}")


class TestServersOfASession:
    """Какие серверы достаются сессии: те, что назвал её профиль."""

    @staticmethod
    def _servers() -> McpServers:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        ghost = _config(StdioCommand(command="/nonexistent/mcp-server"))
        config = McpServersConfig(
            servers={"standard": _config(endpoint), "ghost": ghost}
        )

        return McpServers(
            config, NamedBlocks(), DroppedSignals(), CallContexts(), DirectCalls()
        )

    async def test_named_servers_give_ports_and_a_dead_server_is_skipped(
        self,
    ) -> None:
        servers = self._servers()
        await servers.start()
        try:
            ports = await servers.for_session(CALLER, ["standard", "ghost"])
            names = sorted(tool.name for port in ports for tool in port.tools())
        finally:
            await servers.stop()

        if names != ["add", "broken", "picture", "shout"]:
            raise AssertionError(f"only the live server gives tools: {names}")

    async def test_session_without_named_servers_gets_nothing(self) -> None:
        servers = self._servers()
        await servers.start()
        try:
            ports = await servers.for_session(CALLER, [])
        finally:
            await servers.stop()

        if ports:
            raise AssertionError(f"a profile without servers gets no ports: {ports}")

    async def test_undeclared_server_name_is_an_error(self) -> None:
        servers = self._servers()
        try:
            with pytest.raises(McpClientError, match="not declared"):
                await servers.for_session(CALLER, ["typo"])
        finally:
            await servers.stop()


class BobaMcpStand:
    """Сервер boba-mcp отдельным процессом: клиент видит его только по сети,
    как в приложении."""

    REPO: Path = Path(__file__).resolve().parents[4]
    STAND: Path = REPO / "packages" / "apps" / "boba-mcp" / "tests" / "service_stand.py"
    TOKEN: SecretStr = SecretStr("dev-token")

    def __init__(self, workdir: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]

        self._process = subprocess.Popen(
            [sys.executable, str(self.STAND), str(self.port), str(workdir)]
        )

    def endpoint(self) -> HttpEndpoint:
        return HttpEndpoint(
            scheme="http",
            host="127.0.0.1",
            port=self.port,
            path="/mcp/service",
            auth=BearerAuth(token=self.TOKEN),
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
    server = McpToolServer(
        "boba",
        _config(boba_mcp_stand.endpoint()),
        NamedBlocks(),
        DroppedSignals(),
        CallContexts(),
        None,
        DirectCalls(),
    )
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
    """Область вызова клиента доезжает до тела инструмента сервера."""

    THREAD: ClassVar[str] = "5d0c0a52-7f0e-4c56-9d55-0b7a5f0f3c21"

    async def test_scope_of_the_current_call_travels_to_the_server(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        stand = CallStand()
        server = McpToolServer(
            "boba",
            _config(boba_mcp_stand.endpoint()),
            NamedBlocks(),
            DroppedSignals(),
            stand.contexts,
            None,
            DirectCalls(),
        )
        for _ in range(150):
            try:
                await server.open()
            except McpClientError:
                await asyncio.sleep(0.2)
                continue

            break

        try:
            with stand.contexts.applied(stand.context(self.THREAD)):
                message = await server.call(_call("fake_scope"))
        finally:
            await server.close()

        if self.THREAD not in str(message.content):
            raise AssertionError(f"the body runs in the caller's scope: {message}")


class StartedTools(NodeCalls):
    """Реализация NodeCalls, запоминающая вызовы узлов, которые клиент провёл
    к телу: по ним хозяин клиента с лентой рисует шаги."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    async def conducted(
        self, card: ToolCard, node: DagNode, body: NodeBody
    ) -> NodeOutcome:
        self.started.append((card.name, node.key))

        return await body(node)


@pytest.mark.integration
class TestLinkedCalls:
    """Потоковые вызовы одного ответа модели уходят серверу одной связкой:
    сервер связывает их каналами, а итог у каждого вызова свой."""

    async def test_stream_calls_of_one_response_meet_on_the_server(
        self, dag: McpToolServer, tmp_path: Path
    ) -> None:
        marker = tmp_path / "collected"
        emit = DagNode.model_validate(
            {
                "key": "call-emit",
                "tool": "fake_emit",
                "args": {
                    "prefix": "a",
                    "count": 3,
                    "size": 0,
                    "fail_midway": False,
                    "out": "c1",
                },
            }
        )
        collect = DagNode.model_validate(
            {
                "key": "call-collect",
                "tool": "fake_collect",
                "args": {
                    "marker": str(marker),
                    "fail": False,
                    "gated": False,
                    "feed": "c1",
                },
            }
        )

        pending = await dag.submit([emit, collect])
        emitted, collected = await asyncio.gather(*pending)

        if emitted.errored or collected.errored:
            raise AssertionError(
                f"both calls of the group succeed: {emitted} {collected}"
            )
        if emitted.key != "call-emit":
            raise AssertionError(f"each call gets its own outcome: {emitted}")
        if collected.key != "call-collect":
            raise AssertionError(f"each call gets its own outcome: {collected}")
        if isinstance(emitted.artifact, WorkflowResult):
            raise AssertionError(
                f"the outcome is the tool's, not the group's: {emitted}"
            )
        if not marker.exists():
            raise AssertionError("the reader ran to its end on the writer's stream")

    async def test_workflow_call_runs_as_the_calls_of_its_nodes(
        self, boba_mcp_stand: BobaMcpStand, tmp_path: Path
    ) -> None:
        """Вызов связки — несколько вызовов инструментов, запущенных вместе:
        у каждого узла свой вызов под идентификатором связки с номером узла,
        и те же идентификаторы несёт итог связки для истории."""
        conduct = StartedTools()
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
        whole = DagNode.model_validate(
            {"key": "call-wf", "tool": "workflow", "args": {"nodes": nodes}}
        )

        dag = McpToolServer(
            "boba",
            _config(boba_mcp_stand.endpoint()),
            NamedBlocks(),
            DroppedSignals(),
            CallContexts(),
            None,
            conduct,
        )
        for _ in range(150):
            try:
                await dag.open()
            except McpClientError:
                await asyncio.sleep(0.2)
                continue

            break

        try:
            message = await dag.call(whole)
        finally:
            await dag.close()

        artifact = message.artifact
        if not isinstance(artifact, WorkflowResult):
            raise AssertionError(f"the outcome is the workflow result: {message}")

        ids = [node.call_id for node in artifact.nodes]
        if ids != ["call-wf_0", "call-wf_1"]:
            raise AssertionError(f"nodes are numbered calls of the workflow: {ids}")
        if [node.key for node in artifact.nodes] != ["src", "dst"]:
            raise AssertionError(f"nodes keep the names of the model: {artifact}")
        started = sorted(conduct.started)
        if started != [("fake_collect", "call-wf_1"), ("fake_emit", "call-wf_0")]:
            raise AssertionError(f"each node is a tool call of its own: {started}")

    async def test_lonely_stream_call_goes_as_an_ordinary_call(
        self, dag: McpToolServer
    ) -> None:
        emit = DagNode.model_validate(
            {
                "key": "call-lonely",
                "tool": "fake_emit",
                "args": {
                    "prefix": "a",
                    "count": 1,
                    "size": 0,
                    "fail_midway": False,
                    "out": "c1",
                },
            }
        )

        message = await dag.call(emit)

        if not message.errored:
            raise AssertionError(f"a stream without a reader is refused: {message}")


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


class HeardSignals(CallSignals, JournalListener):
    """Приёмник сигналов журнала теста: копит сигналы и вызовы, чьи они."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.signals: list[JournalSignal] = []

    def listener(self, server: str, call: DagNode) -> JournalListener:
        self.calls.append((server, call.key))
        return self

    async def appended(self, signal: JournalSignal) -> None:
        self.signals.append(signal)


@pytest.mark.integration
class TestBobaMcpJournal:
    """Журнал вызова своего сервера: сигналы роста и чтение окнами."""

    async def _opened(self, process: BobaMcpStand, heard: CallSignals) -> McpToolServer:
        server = McpToolServer(
            "boba",
            _config(process.endpoint()),
            NamedBlocks(),
            heard,
            CallContexts(),
            None,
            DirectCalls(),
        )
        for _ in range(150):
            try:
                await server.open()
            except McpClientError:
                await asyncio.sleep(0.2)
                continue

            return server

        raise AssertionError("the boba-mcp stand did not start")

    async def test_call_delivers_journal_signals_and_windows(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        heard = HeardSignals()
        server = await self._opened(boba_mcp_stand, heard)
        try:
            call = _call("fake_echo", text="hi", repeat=2)
            message = await server.call(call)
            if message.errored:
                raise AssertionError(f"the call succeeds: {message}")

            if heard.calls != [("boba", call.key)]:
                raise AssertionError(f"the listener is asked per call: {heard.calls}")

            stdout: list[JournalSignal] = []
            for signal in heard.signals:
                if signal.node != call.key:
                    raise AssertionError(f"signals name the model's call: {signal}")

                if signal.channel == ToolChannel.STDOUT.value:
                    stdout.append(signal)

            if not stdout or not stdout[-1].closed:
                raise AssertionError(f"the output channel is closed: {heard.signals}")

            request = JournalRead(
                run=stdout[-1].run, node=stdout[-1].node, channel=ToolChannel.STDOUT
            )
            window = await server.journal(request)
        finally:
            await server.close()

        address = message.journal
        if address != JournalAddress(server="boba", run=stdout[-1].run):
            raise AssertionError(f"the message keeps the journal address: {address}")

        if window is None or "echo progress: hi" not in window.text:
            raise AssertionError(f"the journal is read from the server: {window}")
        if window.size != stdout[-1].size or not window.closed:
            raise AssertionError(f"the window agrees with the signal: {window}")

    async def test_journal_channel_is_downloaded_whole_and_by_range(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        server = await self._opened(boba_mcp_stand, DroppedSignals())
        try:
            call = _call("fake_echo", text="hi", repeat=2)
            message = await server.call(call)
            address = message.journal
            files = server.journal_files()
            if address is None or files is None:
                raise AssertionError(f"the journal is addressable: {message}")

            rel = f"{address.run}/{call.key}/{ToolChannel.STDOUT.value}"
            whole = await files.relay(rel, "")
            body = b"".join([chunk async for chunk in whole.chunks])
            await whole.release()
            part = await files.relay(rel, "bytes=0-3")
            head = b"".join([chunk async for chunk in part.chunks])
            await part.release()
            with pytest.raises(StorageNotFoundError):
                await files.relay(f"{address.run}/ghost/stdout", "")
        finally:
            await server.close()

        if whole.status != 200 or b"echo progress: hi" not in body:
            raise AssertionError(f"the channel is downloaded whole: {body[:200]!r}")
        if "attachment" not in whole.headers.get("content-disposition", ""):
            raise AssertionError(f"the server names the file: {whole.headers}")
        if part.status != 206 or head != body[:4]:
            raise AssertionError(f"a range is relayed as 206: {part.status} {head!r}")

    async def test_missing_journal_is_no_window(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        server = await self._opened(boba_mcp_stand, DroppedSignals())
        try:
            if JournalFeature.ID not in server.features():
                raise AssertionError(f"the journal is declared: {server.features()}")

            request = JournalRead(run="nope", node="nope", channel=ToolChannel.STDOUT)
            window = await server.journal(request)
        finally:
            await server.close()

        if window is not None:
            raise AssertionError(f"an unknown call has no journal: {window}")

    async def test_standard_server_has_no_journal(self) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        heard = HeardSignals()
        server = McpToolServer(
            "standard",
            _config(endpoint),
            NamedBlocks(),
            heard,
            CallContexts(),
            None,
            DirectCalls(),
        )
        await server.open()
        try:
            await server.call(_call("add", a=2, b=3))
            request = JournalRead(run="r", node="n", channel=ToolChannel.STDOUT)
            window = await server.journal(request)
        finally:
            await server.close()

        if heard.calls or window is not None:
            raise AssertionError(f"a standard server is not asked: {heard.calls}")


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

        return McpServers(
            config, NamedBlocks(), DroppedSignals(), CallContexts(), DirectCalls()
        )

    async def _ports(
        self, servers: McpServers, caller: McpCaller, names: Sequence[str]
    ) -> Sequence[ToolServer]:
        for _ in range(150):
            ports = await servers.for_session(caller, names)
            if ports:
                return ports

            await asyncio.sleep(0.2)

        return []

    async def test_body_runs_as_the_user_of_the_client(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        servers = self._servers(boba_mcp_stand, self.SECRET)
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            ports = await self._ports(servers, caller, ["boba"])
            if len(ports) != 1:
                raise AssertionError(f"the user gets the port of the service: {ports}")

            message = await ports[0].call(_call("fake_whoami"))
        finally:
            await servers.stop()

        if not str(message.content).startswith("ivanov|dev|"):
            raise AssertionError(f"the body sees the user and the roles: {message}")

    async def test_endpoint_gives_the_tools_of_its_profile(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        servers = self._servers(boba_mcp_stand, self.SECRET)
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            await self._ports(servers, caller, ["boba"])
            ports = await servers.for_session(caller, ["narrow"])
            names = sorted(tool.name for port in ports for tool in port.tools())
        finally:
            await servers.stop()

        if names != ["fake_echo"]:
            raise AssertionError(f"the endpoint decides the tools: {names}")

    async def test_endpoint_outside_the_roles_gives_no_port(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        servers = self._servers(boba_mcp_stand, self.SECRET)
        weak = McpCaller(login="petrov", roles=frozenset({"weak"}))
        try:
            await self._ports(servers, weak, ["boba"])
            ports = await servers.for_session(weak, ["narrow"])
        finally:
            await servers.stop()

        if ports:
            raise AssertionError(f"role weak has no endpoint narrow: {ports}")

    async def test_forged_signature_opens_no_port(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        honest = self._servers(boba_mcp_stand, self.SECRET)
        forged = self._servers(boba_mcp_stand, SecretStr("forged"))
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            await self._ports(honest, caller, ["boba"])
            ports = await forged.for_session(caller, ["boba"])
        finally:
            await honest.stop()
            await forged.stop()

        if ports:
            raise AssertionError(f"a forged signature opens no port: {ports}")


@pytest.mark.integration
class TestBobaMcpServer:
    """Свой сервер: тот же стандартный путь плюс модель результата."""

    async def test_result_revives_as_its_model(self, dag: McpToolServer) -> None:
        message = await dag.call(_call("fake_echo", text="hi", repeat=2))

        if message.errored:
            raise AssertionError(f"the call succeeds: {message}")

        artifact = message.artifact
        if not isinstance(artifact, MarkdownResult) or artifact.text != "hi hi|t0ken":
            raise AssertionError(f"the server's model is revived: {artifact}")

    async def test_card_carries_the_call_view_of_the_server(
        self, dag: McpToolServer
    ) -> None:
        echo = next(tool for tool in dag.tools() if tool.name == "fake_echo")

        hidden = FieldView(placement=FieldPlacement.HIDDEN, display=None)
        if echo.views != {"cfg": hidden}:
            raise AssertionError(f"the view of the server's call model: {echo}")

        workflow = next(tool for tool in dag.tools() if tool.name == CallDag.WORKFLOW)
        if workflow.views is not None:
            raise AssertionError(f"a built-in node has no call view: {workflow}")

    async def test_failure_keeps_the_server_failure_model(
        self, dag: McpToolServer
    ) -> None:
        message = await dag.call(_call("fake_echo", text="boom", repeat=1))

        failure = message.artifact
        if not isinstance(failure, FailureResult):
            raise AssertionError(f"the failure model of the server: {failure}")
        if isinstance(failure, ErrorResult):
            if failure.error_kind == McpFailure.TOOL_ERROR:
                raise AssertionError(f"not the generic client failure: {failure}")
        if "fake backend is down" not in failure.llm_view():
            raise AssertionError(f"the cause travels with the model: {failure}")

    async def test_workflow_outcome_is_revived_with_its_nodes(
        self, dag: McpToolServer, tmp_path: Path
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

        message = await dag.call(_call("workflow", nodes=nodes))

        outcome = message.artifact
        if not isinstance(outcome, WorkflowResult):
            raise AssertionError(f"the workflow model is revived: {message}")
        if len(outcome.nodes) != len(nodes):
            raise AssertionError(f"every node reports its outcome: {outcome}")

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
            login="alice",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            profile=SecretReveal.dumped(profile),
        )
        value = ConnectionSeal(SealFeature.model_validate(declared).key).seal(sealed)

        message = await dag.call(_call("fake_connection_host", connection=value))

        if message.errored or message.content != "db.local|orders":
            raise AssertionError(f"the body got the sealed profile: {message}")

    async def test_plain_reference_is_refused_by_the_server(
        self, dag: McpToolServer
    ) -> None:
        message = await dag.call(
            _call("fake_connection_host", connection="conn://postgres/main")
        )

        if not message.errored:
            raise AssertionError(f"an unsealed reference is refused: {message}")

    async def test_wrong_token_is_a_client_error(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        endpoint = boba_mcp_stand.endpoint().model_copy(
            update={"auth": BearerAuth(token=SecretStr("stranger"))}
        )
        server = McpToolServer(
            "boba",
            _config(endpoint),
            NamedBlocks(),
            DroppedSignals(),
            CallContexts(),
            None,
            DirectCalls(),
        )

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
    """Настоящий процесс сервиса на его собственном конфиге: `python -m
    boba.mcp_server --config compose/mcp/conf/config.toml`. Порт и способ
    запуска стенд задаёт переопределениями [env]; вход — proxy, ключ
    утверждений и клиент берутся из [auth.proxy] и [mcp.clients] того же
    конфига."""

    HOST: str = "localhost"
    """Имя публичного адреса, который стенд задаёт сервису: издатель токена
    и адрес ресурса обязаны совпадать с тем, как клиент называет сервис."""
    PROFILE: str = "general"
    CLIENT: str = "boba-chat"
    CONFIG: Path = BobaMcpStand.REPO / "compose" / "mcp" / "conf" / "config.toml"
    THIRD: Path = BobaMcpStand.REPO / "build" / "src" / "sandbox" / "third"

    def __init__(self, log: Path) -> None:
        with socket.socket() as probe:
            probe.bind((self.HOST, 0))
            self.port = int(probe.getsockname()[1])

        self._proxy = bind_section(self.CONFIG, "auth.proxy", ProxyAuthConfig)
        self._client = bind_section(
            self.CONFIG, f"mcp.clients.{self.CLIENT}", DeclaredClient
        )
        self._log = log.open("wb")
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "boba.mcp_server",
                "--config",
                str(self.CONFIG),
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
    def call_stand(self) -> CallStand:
        return CallStand()

    @pytest.fixture
    async def service(
        self, tmp_path: Path, call_stand: CallStand
    ) -> AsyncIterator[McpToolServer]:
        if not BobaMcpService.CONFIG.exists():
            pytest.skip(f"the service config is not placed: {BobaMcpService.CONFIG}")

        process = BobaMcpService(tmp_path / "mcp.log")
        caller = McpCaller(login="tester", roles=frozenset({"wrt"}))
        server = McpToolServer(
            "boba",
            _config(process.endpoint()),
            NamedBlocks(),
            DroppedSignals(),
            call_stand.contexts,
            caller,
            DirectCalls(),
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
            process.stop()

    async def test_plugin_tool_runs_in_the_service_process(
        self, service: McpToolServer
    ) -> None:
        names = [tool.name for tool in service.tools()]
        if "bash" not in names or "workflow" not in names:
            raise AssertionError(f"the service offers its plugin tools: {names}")

        message = await service.call(_call("bash", command="echo from the service"))

        artifact = message.artifact
        if not isinstance(artifact, ShellResult):
            raise AssertionError(f"the shell model is revived: {message}")
        if artifact.stdout.strip() != "from the service":
            raise AssertionError(f"the body ran in the service: {artifact}")

    async def test_calls_of_one_scope_share_the_files_of_the_scope(
        self, service: McpToolServer, call_stand: CallStand
    ) -> None:
        """Файл, записанный вызовом области, виден следующему вызову той же
        области и не виден вызову другой."""
        first = "8a1d7f2c-3b54-4e0a-9c1e-2f4b6d8a0c11"
        other = "8a1d7f2c-3b54-4e0a-9c1e-2f4b6d8a0c12"

        with call_stand.contexts.applied(call_stand.context(first)):
            await service.call(_call("bash", command="echo kept > note.txt"))
            again = await service.call(_call("bash", command="cat note.txt"))

        with call_stand.contexts.applied(call_stand.context(other)):
            apart = await service.call(_call("bash", command="cat note.txt"))

        seen = again.artifact
        if not isinstance(seen, ShellResult) or seen.stdout.strip() != "kept":
            raise AssertionError(f"the next call of the scope sees the file: {again}")

        missed = apart.artifact
        if not isinstance(missed, ShellResult) or missed.stdout.strip() == "kept":
            raise AssertionError(f"another scope does not see the file: {apart}")
