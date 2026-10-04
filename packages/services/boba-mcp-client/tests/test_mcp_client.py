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
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import ToolCall
from pydantic import SecretStr

from boba.connections.sealed import ConnectionSeal, SealedConnection, SealFeature
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_client.client import (
    BearerAuth,
    BlockFiles,
    CallSignals,
    DroppedSignals,
    HttpEndpoint,
    JournalAddress,
    JournalAddresses,
    JournalListener,
    McpClientError,
    McpFailure,
    McpServerConfig,
    McpServers,
    McpServersConfig,
    McpToolServer,
    NamedBlocks,
    StdioCommand,
)
from boba.runtime.config import ConfigLocator
from boba.toolkit.channels import ToolChannel
from boba.toolkit.dag import WorkflowResult
from boba.toolkit.result import (
    ErrorResult,
    FailureResult,
    MarkdownResult,
    ShellResult,
)
from boba.toolkit.types import SecretReveal
from boba.toolkit.wire import JournalFeature, JournalRead, JournalSignal

pytestmark = pytest.mark.anyio

SERVER = Path(__file__).with_name("standard_server.py")


def _config(endpoint: HttpEndpoint | StdioCommand, prefix: str = "") -> McpServerConfig:
    return McpServerConfig(
        endpoint=endpoint,
        roles=["*"],
        profiles=["*"],
        tools=["*"],
        prefix=prefix,
        connect_timeout_sec=20.0,
        call_timeout_sec=60.0,
    )


def _call(name: str, **args: object) -> ToolCall:
    return ToolCall(name=name, args=args, id=f"call-{name}", type="tool_call")


@pytest.fixture
async def stdio() -> AsyncIterator[McpToolServer]:
    endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
    server = McpToolServer(
        "standard", _config(endpoint, prefix="std_"), NamedBlocks(), DroppedSignals()
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
        "standard", _config(endpoint), NamedBlocks(), DroppedSignals()
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
        schema = add.args_schema
        if not isinstance(schema, dict):
            raise AssertionError(f"the stub carries the server's JSON schema: {schema}")
        if sorted(schema["properties"]) != ["a", "b"]:
            raise AssertionError(f"the model sees the server's arguments: {schema}")

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

        if message.status != "success" or message.content != "5":
            raise AssertionError(f"the text of the result is the content: {message}")
        if not isinstance(message.artifact, MarkdownResult):
            raise AssertionError(
                f"a plain result is a text artifact: {message.artifact}"
            )
        if message.tool_call_id != "call-std_add":
            raise AssertionError(f"the message answers its call: {message}")

    async def test_tool_error_is_an_error_message_with_the_server_text(
        self, stdio: McpToolServer
    ) -> None:
        message = await stdio.call(_call("std_broken"))

        if message.status != "error":
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
                self, call: ToolCall, index: int, mime: str, data: bytes
            ) -> str:
                received.append((str(call["id"]), index, mime, data))
                return "saved as picture.png"

        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        server = McpToolServer(
            "standard", _config(endpoint), Recording(), DroppedSignals()
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
            "ghost", _config(endpoint), NamedBlocks(), DroppedSignals()
        )

        with pytest.raises(McpClientError, match=r"ghost|nonexistent"):
            await server.open()

    async def test_tools_before_open_is_a_client_error(self) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        server = McpToolServer(
            "standard", _config(endpoint), NamedBlocks(), DroppedSignals()
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

        if message.status != "success" or not str(message.content).strip():
            raise AssertionError(f"deepwiki answers with text: {message}")

    async def test_context7_lists_tools_and_answers_a_call(self) -> None:
        await self._reachable(self.CONTEXT7)
        server = McpToolServer(
            "context7", _config(self.CONTEXT7), NamedBlocks(), DroppedSignals()
        )
        await server.open()
        try:
            names = [tool.name for tool in server.tools()]
            if "resolve-library-id" not in names:
                raise AssertionError(f"context7 offers its tools: {names}")

            tool = next(t for t in server.tools() if t.name == "resolve-library-id")
            schema = tool.args_schema
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
    """Какие серверы и инструменты достаются сессии: грант секции сервера."""

    @staticmethod
    def _servers(**grant: list[str]) -> McpServers:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        base = _config(endpoint).model_dump()
        base.update(grant)
        ghost = _config(StdioCommand(command="/nonexistent/mcp-server")).model_dump()
        config = McpServersConfig.model_validate(
            {"servers": {"standard": base, "ghost": ghost}}
        )

        return McpServers(config, NamedBlocks(), DroppedSignals())

    async def test_granted_session_gets_the_port_and_a_dead_server_is_skipped(
        self,
    ) -> None:
        servers = self._servers()
        await servers.start()
        try:
            ports = await servers.for_session(["DEV"], "general")
            names = sorted(tool.name for port in ports for tool in port.tools())
        finally:
            await servers.stop()

        if names != ["add", "broken", "picture", "shout"]:
            raise AssertionError(f"only the live server gives tools: {names}")

    async def test_profile_and_role_outside_the_grant_get_nothing(self) -> None:
        servers = self._servers(roles=["ADM"], profiles=["search"])
        await servers.start()
        try:
            wrong_role = await servers.for_session(["DEV"], "search")
            wrong_profile = await servers.for_session(["ADM"], "general")
            granted = await servers.for_session(["ADM"], "search")
        finally:
            await servers.stop()

        if wrong_role or wrong_profile:
            raise AssertionError(f"the grant is both role and profile: {wrong_role}")
        if len(granted) != 1:
            raise AssertionError(f"the granted session gets the server: {granted}")

    async def test_tool_list_of_the_section_limits_the_offered_tools(self) -> None:
        servers = self._servers(tools=["add"])
        await servers.start()
        try:
            ports = await servers.for_session(["DEV"], "general")
            names = [tool.name for port in ports for tool in port.tools()]
        finally:
            await servers.stop()

        if names != ["add"]:
            raise AssertionError(f"only the listed tools are offered: {names}")


class DagProcess:
    """Сервер boba-dag отдельным процессом в окружении сервиса: клиент видит
    его только по сети, как в приложении."""

    REPO: Path = Path(__file__).resolve().parents[4]
    PYTHON: Path = REPO / "services" / "boba-dag" / ".venv" / "bin" / "python"
    STAND: Path = REPO / "services" / "boba-dag" / "tests" / "service_stand.py"
    TOKEN: SecretStr = SecretStr("dev-token")

    def __init__(self, workdir: Path) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]

        self._process = subprocess.Popen(
            [str(self.PYTHON), str(self.STAND), str(self.port), str(workdir)]
        )

    def endpoint(self) -> HttpEndpoint:
        return HttpEndpoint(
            scheme="http",
            host="127.0.0.1",
            port=self.port,
            path="/mcp",
            auth=BearerAuth(token=self.TOKEN),
        )

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


@pytest.fixture
def dag_process(tmp_path: Path) -> Iterator[DagProcess]:
    if not DagProcess.PYTHON.exists():
        pytest.skip(f"the service environment is not built: {DagProcess.PYTHON}")

    process = DagProcess(tmp_path)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture
async def dag(dag_process: DagProcess) -> AsyncIterator[McpToolServer]:
    server = McpToolServer(
        "dag", _config(dag_process.endpoint()), NamedBlocks(), DroppedSignals()
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


class HeardSignals(CallSignals, JournalListener):
    """Приёмник сигналов журнала теста: копит сигналы и вызовы, чьи они."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.signals: list[JournalSignal] = []

    def listener(self, server: str, call: ToolCall) -> JournalListener:
        self.calls.append((server, str(call["id"])))
        return self

    async def appended(self, signal: JournalSignal) -> None:
        self.signals.append(signal)


@pytest.mark.integration
class TestBobaDagJournal:
    """Журнал вызова своего сервера: сигналы роста и чтение окнами."""

    async def _opened(self, process: DagProcess, heard: CallSignals) -> McpToolServer:
        server = McpToolServer("dag", _config(process.endpoint()), NamedBlocks(), heard)
        for _ in range(150):
            try:
                await server.open()
            except McpClientError:
                await asyncio.sleep(0.2)
                continue

            return server

        raise AssertionError("the boba-dag stand did not start")

    async def test_call_delivers_journal_signals_and_windows(
        self, dag_process: DagProcess
    ) -> None:
        heard = HeardSignals()
        server = await self._opened(dag_process, heard)
        try:
            call = _call("fake_echo", text="hi", repeat=2)
            message = await server.call(call)
            if message.status != "success":
                raise AssertionError(f"the call succeeds: {message}")

            if heard.calls != [("dag", str(call["id"]))]:
                raise AssertionError(f"the listener is asked per call: {heard.calls}")

            stdout: list[JournalSignal] = []
            for signal in heard.signals:
                if signal.node != call["id"]:
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

        address = JournalAddresses().of(message)
        if address != JournalAddress(server="dag", run=stdout[-1].run):
            raise AssertionError(f"the message keeps the journal address: {address}")

        if window is None or "echo progress: hi" not in window.text:
            raise AssertionError(f"the journal is read from the server: {window}")
        if window.size != stdout[-1].size or not window.closed:
            raise AssertionError(f"the window agrees with the signal: {window}")

    async def test_missing_journal_is_no_window(self, dag_process: DagProcess) -> None:
        server = await self._opened(dag_process, DroppedSignals())
        try:
            if JournalFeature.ID.value not in server.features():
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
        server = McpToolServer("standard", _config(endpoint), NamedBlocks(), heard)
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
class TestBobaDagServer:
    """Свой сервер: тот же стандартный путь плюс модель результата."""

    async def test_result_revives_as_its_model(self, dag: McpToolServer) -> None:
        message = await dag.call(_call("fake_echo", text="hi", repeat=2))

        if message.status != "success":
            raise AssertionError(f"the call succeeds: {message}")

        artifact = message.artifact
        if not isinstance(artifact, MarkdownResult) or artifact.text != "hi hi|t0ken":
            raise AssertionError(f"the server's model is revived: {artifact}")

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

        if message.status != "success" or message.content != "db.local|orders":
            raise AssertionError(f"the body got the sealed profile: {message}")

    async def test_plain_reference_is_refused_by_the_server(
        self, dag: McpToolServer
    ) -> None:
        message = await dag.call(
            _call("fake_connection_host", connection="conn://postgres/main")
        )

        if message.status != "error":
            raise AssertionError(f"an unsealed reference is refused: {message}")

    async def test_wrong_token_is_a_client_error(self, dag_process: DagProcess) -> None:
        endpoint = dag_process.endpoint().model_copy(
            update={"auth": BearerAuth(token=SecretStr("stranger"))}
        )
        server = McpToolServer(
            "dag", _config(endpoint), NamedBlocks(), DroppedSignals()
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


class DagService:
    """Настоящий процесс сервиса на рабочем конфиге: `python -m
    boba.dag_service --config …` в окружении сервиса, адрес и токен — из
    секции [dag] конфига стенда."""

    HOST: str = "127.0.0.1"
    PORT: int = 8650
    TOKEN: SecretStr = SecretStr("dag-dev-token")

    def __init__(self, config: Path, log: Path) -> None:
        self._log = log.open("wb")
        self._process = subprocess.Popen(
            [
                str(DagProcess.PYTHON),
                "-m",
                "boba.dag_service",
                "--config",
                str(config),
            ],
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def endpoint(self) -> HttpEndpoint:
        return HttpEndpoint(
            scheme="http",
            host=self.HOST,
            port=self.PORT,
            path="/mcp",
            auth=BearerAuth(token=self.TOKEN),
        )

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=30)
        self._log.close()


@pytest.mark.integration
@pytest.mark.xdist_group("dag-service")
class TestDagServiceProcess:
    """Приёмка двумя процессами: сервис со всеми плагинами рабочего конфига,
    клиент чата ходит к нему по сети."""

    @pytest.fixture
    async def service(self, tmp_path: Path) -> AsyncIterator[McpToolServer]:
        if not DagProcess.PYTHON.exists():
            pytest.skip(f"the service environment is not built: {DagProcess.PYTHON}")

        process = DagService(ConfigLocator.path(), tmp_path / "dag.log")
        server = McpToolServer(
            "dag", _config(process.endpoint()), NamedBlocks(), DroppedSignals()
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
