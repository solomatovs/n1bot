"""MCP-сервер сервиса: список инструментов по ролям и вызовы через порт.

Сервер поднят в процессе теста на настоящем порту; клиент — fastmcp Client
по streamable HTTP с токеном. Инструменты — фейки стенда под той же
цепочкой обвязок и исполнителем субпроцессом, что в приложении.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.tests import run_server_async
from service_stand import DEV_TOKEN, PROFILE, WEAK_TOKEN, ServiceStand

from boba.connections.sealed import SealFeature
from boba.dag_service.server import (
    DagTool,
    RunLimitMiddleware,
    RunLimits,
)
from boba.toolkit.wire import WireMeta
from boba.toolrun.call_id import CallFields
from boba.toolrun.stream_calls import WorkflowTool

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def stand(tmp_path: Path) -> ServiceStand:
    return ServiceStand(tmp_path, RunLimits(max_runs=4, max_waiting=4))


@pytest.fixture
async def url(stand: ServiceStand) -> AsyncIterator[str]:
    async with run_server_async(stand.mcp) as served:
        yield served


def _client(url: str, token: str) -> Client[Any]:
    return Client(StreamableHttpTransport(url, auth=token))


class TestToolList:
    async def test_caller_sees_the_tools_of_the_port(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            listed = await client.list_tools()

        names = sorted(tool.name for tool in listed)
        expected = [
            "fake_collect",
            "fake_connection_host",
            "fake_echo",
            "fake_emit",
            "fake_sleep",
            "fake_whoami",
            WorkflowTool.NAME,
        ]
        if names != sorted(expected):
            raise AssertionError(f"the list is the port's tools: {names}")

    async def test_schema_is_the_call_schema_without_service_fields(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            listed = await client.list_tools()

        echo = next(tool for tool in listed if tool.name == "fake_echo")
        own = next(tool for tool in stand.registry.tools if tool.name == "fake_echo")
        if echo.input_schema != stand.schemas.of(own):
            raise AssertionError(f"the schema is published as is: {echo.input_schema}")

        properties = echo.input_schema["properties"]
        if "cfg" in properties:
            raise AssertionError("the injected config is not shown to the client")
        if CallFields.CALL_ID in properties:
            raise AssertionError("the call id field is not shown to the client")

    async def test_weaker_role_sees_fewer_tools(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, WEAK_TOKEN) as client:
            listed = await client.list_tools()

        names = [tool.name for tool in listed]
        if names != ["fake_echo"]:
            raise AssertionError(f"role weak is granted one tool: {names}")


class TestCall:
    async def test_result_carries_text_model_and_status(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "hi", "repeat": 2}
            )

        if result.is_error:
            raise AssertionError(f"the call succeeds: {result}")

        structured = result.structured_content
        if structured is None or structured.get("kind") != "markdown":
            raise AssertionError(f"the artifact travels as its model: {structured}")
        if structured.get("text") != "hi hi|t0ken":
            raise AssertionError(f"the body got its config: {structured}")

        meta = (result.meta or {}).get(WireMeta.NAMESPACE.value)
        if meta is None or meta.get(WireMeta.STATUS.value) != "success":
            raise AssertionError(f"the status travels in _meta: {result.meta}")

    async def test_body_sees_the_caller_of_the_token(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp("fake_whoami", {})

        structured = result.structured_content
        if structured is None or structured.get("text") != f"alice|dev|{PROFILE}":
            raise AssertionError(f"the body is called as the token's caller: {result}")

    async def test_call_id_of_the_client_is_the_id_of_the_call(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp(
                "fake_echo",
                {"text": "hi", "repeat": 1},
                meta={DagTool.META_CALL_ID: "call-of-the-model"},
            )

        own = (result.meta or {}).get(WireMeta.NAMESPACE.value, {})
        if own.get(WireMeta.CALL_ID.value) != "call-of-the-model":
            raise AssertionError(f"the client's call id is kept: {result.meta}")

    async def test_server_declares_its_features_on_connect(
        self, stand: ServiceStand, url: str
    ) -> None:
        """Возможности едут ответом на initialize: клиент узнаёт ключ
        запечатывания и инструмент-связку, не вызывая инструментов."""
        hello = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stand", "version": "1"},
            },
        }
        headers = {
            "Authorization": f"Bearer {DEV_TOKEN}",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient() as http:
            reply = await http.post(url, json=hello, headers=headers)

        if SealFeature.ID not in reply.text:
            raise AssertionError(f"the seal key is declared on connect: {reply.text}")
        if WorkflowTool.FEATURE not in reply.text:
            raise AssertionError(f"the workflow tool is declared: {reply.text}")

    async def test_linked_nodes_run_as_one_workflow_call(
        self, stand: ServiceStand, url: str, tmp_path: Path
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
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp(WorkflowTool.NAME, {"nodes": nodes})

        if result.is_error:
            raise AssertionError(f"the group succeeds: {result}")

        structured = result.structured_content
        if structured is None or structured.get("kind") != "workflow":
            raise AssertionError(f"the outcome is the workflow model: {structured}")
        if len(structured.get("nodes", [])) != len(nodes):
            raise AssertionError(f"every node reports its outcome: {structured}")


class TestFailures:
    async def test_bad_arguments_are_an_error_result_with_a_model(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp("fake_echo", {"text": "hi"})

        if not result.is_error:
            raise AssertionError(f"a missing argument is an error: {result}")
        if result.structured_content is None:
            raise AssertionError("the refusal carries its model, not bare text")

    async def test_body_failure_is_an_error_result(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "boom", "repeat": 1}
            )

        if not result.is_error:
            raise AssertionError(f"a failed body is an error: {result}")

        structured = result.structured_content
        if structured is None or "fake backend is down" not in str(structured):
            raise AssertionError(f"the failure model names the cause: {structured}")

    async def test_stream_tool_alone_is_refused_with_a_hint(
        self, stand: ServiceStand, url: str
    ) -> None:
        arguments = {
            "prefix": "a",
            "count": 1,
            "size": 0,
            "fail_midway": False,
            "out": "c1",
        }
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp("fake_emit", arguments)

        if not result.is_error:
            raise AssertionError(f"a pump without a partner is refused: {result}")
        if result.structured_content is None:
            raise AssertionError("the plan refusal carries its model")

    async def test_tool_outside_the_role_is_not_callable(
        self, stand: ServiceStand, url: str
    ) -> None:
        arguments = {
            "prefix": "a",
            "count": 1,
            "size": 0,
            "fail_midway": False,
            "out": "c1",
        }
        async with _client(url, WEAK_TOKEN) as client:
            result = await client.call_tool_mcp("fake_emit", arguments)

        if not result.is_error:
            raise AssertionError(f"role weak has no fake_emit: {result}")

    async def test_request_without_a_token_is_rejected(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with httpx.AsyncClient() as http:
            reply = await http.post(url, json={})

        if reply.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"no token means 401: {reply.status_code}")


class TestRunLimit:
    @pytest.fixture
    def stand(self, tmp_path: Path) -> ServiceStand:
        return ServiceStand(tmp_path, RunLimits(max_runs=1, max_waiting=1))

    async def test_calls_over_the_limit_wait_and_over_the_queue_are_refused(
        self, stand: ServiceStand, url: str, tmp_path: Path
    ) -> None:
        async def slept(name: str) -> Any:
            arguments = {"seconds": 1.5, "marker": str(tmp_path / name)}
            async with _client(url, DEV_TOKEN) as client:
                return await client.call_tool_mcp("fake_sleep", arguments)

        first = asyncio.create_task(slept("first"))
        await self._started(tmp_path / "first")
        second = asyncio.create_task(slept("second"))
        await asyncio.sleep(0.3)
        third = await slept("third")

        if not third.is_error:
            raise AssertionError(f"a call over the queue is refused: {third}")

        structured = third.structured_content
        if structured is None:
            raise AssertionError("the refusal carries its model")
        if structured.get("error_kind") != RunLimitMiddleware.REFUSED:
            raise AssertionError(f"the refusal names the run limit: {structured}")

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
        self, stand: ServiceStand, url: str, tmp_path: Path
    ) -> None:
        marker = tmp_path / "pid"
        arguments = {"seconds": 60.0, "marker": str(marker)}

        async def hanging() -> Any:
            async with _client(url, DEV_TOKEN) as client:
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
