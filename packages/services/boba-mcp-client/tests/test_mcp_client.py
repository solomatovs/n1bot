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
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import ToolCall
from pydantic import SecretStr

from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    McpClientError,
    McpFailure,
    McpServerConfig,
    McpToolServer,
    StdioCommand,
)
from boba.toolkit.dag import WorkflowResult
from boba.toolkit.result import ErrorResult, FailureResult, MarkdownResult

pytestmark = pytest.mark.anyio

SERVER = Path(__file__).with_name("standard_server.py")


def _config(endpoint: HttpEndpoint | StdioCommand, prefix: str = "") -> McpServerConfig:
    return McpServerConfig(
        endpoint=endpoint,
        prefix=prefix,
        connect_timeout_sec=20.0,
        call_timeout_sec=60.0,
    )


def _call(name: str, **args: object) -> ToolCall:
    return ToolCall(name=name, args=args, id=f"call-{name}", type="tool_call")


@pytest.fixture
async def stdio() -> AsyncIterator[McpToolServer]:
    endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
    server = McpToolServer("standard", _config(endpoint, prefix="std_"))
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

        if "[image image/png" not in str(message.content):
            raise AssertionError(f"an image block is named, not dropped: {message}")

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
        server = McpToolServer("deepwiki", _config(self.DEEPWIKI, prefix="dw_"))
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
        server = McpToolServer("context7", _config(self.CONTEXT7))
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
    server = McpToolServer("dag", _config(dag_process.endpoint()))
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

    async def test_wrong_token_is_a_client_error(self, dag_process: DagProcess) -> None:
        endpoint = dag_process.endpoint().model_copy(
            update={"auth": BearerAuth(token=SecretStr("stranger"))}
        )
        server = McpToolServer("dag", _config(endpoint))

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
