"""MCP-сервер сервиса: список инструментов по ролям и вызовы через порт.

Сервер поднят в процессе теста на настоящем порту; клиент — fastmcp Client
по streamable HTTP с токеном. Инструменты — фейки стенда под той же
цепочкой обвязок и исполнителем субпроцессом, что в приложении.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from service_stand import (
    DEV_TOKEN,
    NARROW,
    PROFILE,
    PROXY_SECRET,
    WEAK_TOKEN,
    ServiceStand,
)

from boba.auth.proxy import ProxySignature
from boba.connections.sealed import SealFeature
from boba.identity.signin import ProxyRequest
from boba.mcp_server.server import (
    RunLimitMiddleware,
    RunLimits,
    StreamReadTool,
)
from boba.toolkit.channels import ToolChannel
from boba.toolkit.wire import JournalFeature, JournalSignal, RequestMeta, WireMeta
from boba.toolrun.call_id import CallFields
from boba.toolrun.stream_calls import WorkflowTool

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def stand(tmp_path: Path, port: int) -> ServiceStand:
    return ServiceStand(tmp_path, RunLimits(max_runs=4, max_waiting=4), port)


@pytest.fixture
async def url(stand: ServiceStand) -> AsyncIterator[str]:
    async with stand.serving():
        yield stand.url(PROFILE)


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
            "fake_scope",
            "fake_sleep",
            "fake_whoami",
            StreamReadTool.NAME,
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
        if names != ["fake_echo", StreamReadTool.NAME]:
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
                meta={RequestMeta.CALL_ID: "call-of-the-model"},
            )

        own = (result.meta or {}).get(WireMeta.NAMESPACE.value, {})
        if own.get(WireMeta.CALL_ID.value) != "call-of-the-model":
            raise AssertionError(f"the client's call id is kept: {result.meta}")

    async def test_scope_of_the_client_is_the_scope_of_the_call(
        self, stand: ServiceStand, url: str
    ) -> None:
        """Область из _meta доходит до тела: вызовы одного разговора видят
        одни и те же файлы."""
        scope = "0b6f6f0e-51d4-4a4b-9f6c-1d6c5f1f7a10"
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp(
                "fake_scope", {}, meta={RequestMeta.SCOPE: scope}
            )

        structured = result.structured_content
        if structured is None or structured.get("text") != scope:
            raise AssertionError(f"the body runs in the client's scope: {result}")

    async def test_call_without_a_scope_runs_in_the_own_scope_of_the_user(
        self, stand: ServiceStand, url: str
    ) -> None:
        """Клиент без области получает область своего пользователя: её id —
        id пользователя, поэтому у двух пользователей она разная."""
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp("fake_scope", {})

        own = str(uuid5(NAMESPACE_URL, "boba-mcp:alice"))
        structured = result.structured_content
        if structured is None or structured.get("text") != own:
            raise AssertionError(f"the default scope is the user's own: {result}")

    async def test_scope_that_is_not_a_path_segment_is_refused(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            result = await client.call_tool_mcp(
                "fake_scope", {}, meta={RequestMeta.SCOPE: "a/b"}
            )

        if not result.is_error:
            raise AssertionError(f"a scope with a separator is refused: {result}")

    async def test_calls_of_one_scope_run_side_by_side(
        self, stand: ServiceStand, url: str, tmp_path: Path
    ) -> None:
        """Вызовы одной области не вытесняют друг друга: у каждого свой запуск."""
        scope = "0b6f6f0e-51d4-4a4b-9f6c-1d6c5f1f7a11"

        async def slept(name: str) -> Any:
            arguments = {"seconds": 1.0, "marker": str(tmp_path / name)}
            async with _client(url, DEV_TOKEN) as client:
                return await client.call_tool_mcp(
                    "fake_sleep", arguments, meta={RequestMeta.SCOPE: scope}
                )

        first, second = await asyncio.gather(slept("first"), slept("second"))
        if first.is_error or second.is_error:
            raise AssertionError(f"both calls of the scope finish: {first} {second}")

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
        if JournalFeature.ID.value not in reply.text:
            raise AssertionError(f"the journal operation is declared: {reply.text}")

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


class TestJournal:
    """Журнал вызова: сигналы роста уведомлениями прогресса и чтение окнами."""

    async def _echoed(self, client: Client[Any]) -> tuple[Any, list[JournalSignal]]:
        signals: list[JournalSignal] = []

        async def on_progress(
            progress: float, total: float | None, message: str | None
        ) -> None:
            if message is None:
                raise AssertionError(f"a signal carries its model: {progress}")

            signals.append(JournalSignal.model_validate_json(message))

        result = await client.call_tool_mcp(
            "fake_echo", {"text": "hi", "repeat": 2}, progress_handler=on_progress
        )

        return result, signals

    async def test_call_signals_the_growth_of_its_journal(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            result, signals = await self._echoed(client)

        own = (result.meta or {}).get(WireMeta.NAMESPACE.value, {})
        run = own.get(WireMeta.RUN.value)
        if not run:
            raise AssertionError(f"the result names its run: {result.meta}")

        stdout: list[JournalSignal] = []
        for signal in signals:
            if signal.run != run or signal.node != own.get(WireMeta.CALL_ID.value):
                raise AssertionError(f"a signal names the run and the call: {signal}")

            if signal.channel == ToolChannel.STDOUT.value:
                stdout.append(signal)

        if not stdout:
            raise AssertionError(f"the output channel is signalled: {signals}")

        last = stdout[-1]
        if not last.closed or last.size == 0:
            raise AssertionError(f"the last signal arrives before the result: {last}")

    async def test_windows_of_the_journal_join_at_line_borders(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            _result, signals = await self._echoed(client)
            address = {
                "run": signals[-1].run,
                "node": signals[-1].node,
                "channel": ToolChannel.STDOUT.value,
            }
            whole = await client.call_tool_mcp(StreamReadTool.NAME, address)
            tail = await client.call_tool_mcp(
                StreamReadTool.NAME, {**address, "offset": 1}
            )

        piece = whole.structured_content
        if whole.is_error or piece is None:
            raise AssertionError(f"the journal is readable after the call: {whole}")
        if "echo progress: hi" not in piece["text"] or not piece["closed"]:
            raise AssertionError(f"the window carries the output of the body: {piece}")
        if piece["offset"] != 0 or piece["end"] != piece["size"]:
            raise AssertionError(f"the window names its place in the file: {piece}")

        rest = tail.structured_content
        if rest is None or not piece["text"].endswith(rest["text"]):
            raise AssertionError(f"a window from the middle starts at a line: {rest}")

    async def test_journal_of_another_caller_is_not_readable(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(url, DEV_TOKEN) as client:
            _result, signals = await self._echoed(client)

        address = {
            "run": signals[-1].run,
            "node": signals[-1].node,
            "channel": ToolChannel.STDOUT.value,
        }
        async with _client(url, WEAK_TOKEN) as client:
            foreign = await client.call_tool_mcp(StreamReadTool.NAME, address)

        if not foreign.is_error:
            raise AssertionError(f"a journal is read only by its caller: {foreign}")

        refusal = foreign.structured_content
        if refusal is None or refusal.get("status") != StreamReadTool.FORBIDDEN:
            raise AssertionError(f"a foreign journal is refused with 403: {foreign}")


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
        return ServiceStand(tmp_path, RunLimits(max_runs=1, max_waiting=1), port)

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


class TestEndpoints:
    """Endpoint — профиль конфига: свой набор инструментов и свои роли."""

    async def test_endpoint_offers_the_tools_of_its_profile(
        self, stand: ServiceStand, url: str
    ) -> None:
        async with _client(stand.url(NARROW), DEV_TOKEN) as client:
            listed = await client.list_tools()

        names = sorted(tool.name for tool in listed)
        if names != ["fake_echo", StreamReadTool.NAME]:
            raise AssertionError(f"the endpoint offers its own tools: {names}")

    async def test_endpoint_outside_the_roles_is_forbidden(
        self, stand: ServiceStand, url: str
    ) -> None:
        headers = {"Authorization": f"Bearer {WEAK_TOKEN}"}
        async with httpx.AsyncClient() as http:
            reply = await http.post(stand.url(NARROW), json={}, headers=headers)

        if reply.status_code != httpx.codes.FORBIDDEN:
            raise AssertionError(f"role weak has no endpoint narrow: {reply}")

    async def test_unknown_endpoint_is_not_found(
        self, stand: ServiceStand, url: str
    ) -> None:
        headers = {"Authorization": f"Bearer {DEV_TOKEN}"}
        async with httpx.AsyncClient() as http:
            reply = await http.post(stand.url("nowhere"), json={}, headers=headers)

        if reply.status_code != httpx.codes.NOT_FOUND:
            raise AssertionError(f"an unknown endpoint is 404: {reply}")


class TestProxySignIn:
    """Вход proxy: доверенный клиент называет логин и роли под подписью."""

    def _headers(self, login: str, roles: str, secret: str) -> dict[str, str]:
        request = ProxyRequest(
            login=login, timestamp=str(int(time.time())), signature="", roles=roles
        )
        signed = ProxySignature(secret).sign(request)

        return {
            "X-Remote-User": login,
            "X-Boba-Timestamp": request.timestamp,
            "X-Boba-Signature": signed,
            "X-Remote-Roles": roles,
        }

    async def _signed_in(self, stand: ServiceStand, headers: dict[str, str]) -> Any:
        address = f"http://127.0.0.1:{stand.port}/auth/proxy"
        async with httpx.AsyncClient() as http:
            return await http.post(address, headers=headers)

    async def test_signed_login_and_roles_become_the_caller(
        self, stand: ServiceStand, url: str
    ) -> None:
        reply = await self._signed_in(
            stand, self._headers("ivanov", "dev", PROXY_SECRET)
        )
        if reply.status_code != httpx.codes.OK:
            raise AssertionError(f"the signed request signs in: {reply.text}")

        token = reply.json()["access_token"]
        async with _client(url, token) as client:
            result = await client.call_tool_mcp("fake_whoami", {})

        structured = result.structured_content
        if structured is None or structured.get("text") != f"ivanov|dev|{PROFILE}":
            raise AssertionError(f"the body runs as the signed-in user: {result}")

    async def test_roles_of_the_sign_in_open_the_endpoints(
        self, stand: ServiceStand, url: str
    ) -> None:
        reply = await self._signed_in(
            stand, self._headers("petrov", "weak", PROXY_SECRET)
        )
        headers = {"Authorization": f"Bearer {reply.json()['access_token']}"}
        async with httpx.AsyncClient() as http:
            narrow = await http.post(stand.url(NARROW), json={}, headers=headers)

        if narrow.status_code != httpx.codes.FORBIDDEN:
            raise AssertionError(f"role weak is refused on narrow: {narrow}")

    async def test_wrong_signature_is_rejected(
        self, stand: ServiceStand, url: str
    ) -> None:
        reply = await self._signed_in(stand, self._headers("ivanov", "dev", "forged"))

        if reply.status_code != httpx.codes.UNAUTHORIZED:
            raise AssertionError(f"a forged signature is 401: {reply}")

    async def test_sign_in_without_roles_is_forbidden(
        self, stand: ServiceStand, url: str
    ) -> None:
        reply = await self._signed_in(stand, self._headers("ivanov", "", PROXY_SECRET))

        if reply.status_code != httpx.codes.FORBIDDEN:
            raise AssertionError(f"a sign-in without roles is 403: {reply}")
