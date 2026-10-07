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
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
import mcp_types as mt
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import JWT_BEARER_GRANT_TYPE, OAuthClientInformationFull
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

from boba.auth.proxy import ProxyAssertions
from boba.connections.sealed import SealFeature
from boba.identity.session import SignInProvider
from boba.identity.signin import SignInMetadata
from boba.mcp_server.auth import (
    RegisteredClients,
    SealedValues,
    ServiceTokens,
    TokenHolder,
)
from boba.mcp_server.server import (
    FileUploadTool,
    RunLimits,
    ServerRefusal,
    StreamReadTool,
)
from boba.toolkit.calls import CallViews
from boba.toolkit.channels import ToolChannel
from boba.toolkit.wire import (
    CallStatus,
    CallWire,
    FilesFeature,
    JournalFeature,
    RequestFields,
    WirePart,
)
from boba.toolrun.hosted import ToolSchema
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
    """Данные ответа: у итога инструмента — его результат (часть
    WirePart.RESULT), у ответа операции сервиса — её модель целиком."""
    structured = result.structured_content
    if structured is None:
        return None

    if WirePart.CALL.value not in structured:
        return structured

    return structured[WirePart.RESULT.value]


def _served(result: mt.CallToolResult) -> CallWire:
    """Сведения о вызове из итога инструмента."""
    structured = result.structured_content
    if structured is None:
        raise AssertionError(f"the result carries structured content: {result}")

    return CallWire.model_validate(structured[WirePart.CALL.value])


class TestToolList:
    async def test_caller_sees_the_tools_of_the_port(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            listed = await client.list_tools()

        names = sorted(tool.name for tool in listed)
        expected = [
            "fake_collect",
            "fake_connection_host",
            "fake_echo",
            "fake_emit",
            "fake_progress",
            "fake_query",
            "fake_scope",
            "fake_sleep",
            "fake_whoami",
            FileUploadTool.NAME,
            StreamReadTool.NAME,
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

    async def test_weaker_role_sees_fewer_tools(
        self, stand: ServiceStand, url: str, weak_token: str
    ) -> None:
        async with _client(url, weak_token) as client:
            listed = await client.list_tools()

        names = [tool.name for tool in listed]
        if names != ["fake_echo", FileUploadTool.NAME, StreamReadTool.NAME]:
            raise AssertionError(f"role weak is granted one tool: {names}")


class TestCall:
    async def test_result_carries_text_model_and_status(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "hi", "repeat": 2}
            )

        if result.is_error:
            raise AssertionError(f"the call succeeds: {result}")

        structured = _shown(result)
        if structured is None or structured.get("kind") != "markdown":
            raise AssertionError(f"the artifact travels as its model: {structured}")
        if structured.get("text") != "hi hi|t0ken":
            raise AssertionError(f"the body got its config: {structured}")

        if _served(result).status is not CallStatus.SUCCESS:
            raise AssertionError(f"the result names the status of the call: {result}")

        own = [key for key in result.meta or {} if "boba" in key]
        if own:
            raise AssertionError(f"the service puts nothing in _meta: {result.meta}")

    async def test_body_sees_the_caller_of_the_token(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_whoami", {})

        structured = _shown(result)
        if structured is None or structured.get("text") != f"alice|dev|{PROFILE}":
            raise AssertionError(f"the body is called as the token's caller: {result}")

    async def test_server_names_the_call_itself(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Идентификатор вызова выдаёт сервер и отдаёт его в итоге вместе с
        запуском: по этой паре клиент читает журнал вызова."""
        async with _client(url, dev_token) as client:
            first = await client.call_tool_mcp("fake_echo", {"text": "a", "repeat": 1})
            second = await client.call_tool_mcp("fake_echo", {"text": "b", "repeat": 1})

        one = _served(first)
        two = _served(second)
        if one.id == two.id:
            raise AssertionError(f"every call gets its own id: {one} and {two}")

        if not one.run or one.run == two.run:
            raise AssertionError(f"every call gets its own run: {one} and {two}")

    async def test_scope_of_the_client_is_the_scope_of_the_call(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Область из _meta доходит до тела: вызовы одного разговора видят
        одни и те же файлы."""
        scope = "0b6f6f0e-51d4-4a4b-9f6c-1d6c5f1f7a10"
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_scope", {}, meta={RequestFields.SCOPE: scope}
            )

        structured = _shown(result)
        if structured is None or structured.get("text") != scope:
            raise AssertionError(f"the body runs in the client's scope: {result}")

    async def test_call_without_a_scope_runs_in_the_own_scope_of_the_user(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Клиент без области получает область своего пользователя: её id —
        id пользователя, поэтому у двух пользователей она разная."""
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_scope", {})

        own = str(uuid5(NAMESPACE_URL, "boba-mcp:alice"))
        structured = _shown(result)
        if structured is None or structured.get("text") != own:
            raise AssertionError(f"the default scope is the user's own: {result}")

    async def test_scope_that_is_not_a_path_segment_is_refused(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_scope", {}, meta={RequestFields.SCOPE: "a/b"}
            )

        if not result.is_error:
            raise AssertionError(f"a scope with a separator is refused: {result}")

    async def test_calls_of_one_scope_run_side_by_side(
        self, stand: ServiceStand, url: str, tmp_path: Path, dev_token: str
    ) -> None:
        """Вызовы одной области не вытесняют друг друга: у каждого свой запуск."""
        scope = "0b6f6f0e-51d4-4a4b-9f6c-1d6c5f1f7a11"

        async def slept(name: str) -> Any:
            arguments = {"seconds": 1.0, "marker": str(tmp_path / name)}
            async with _client(url, dev_token) as client:
                return await client.call_tool_mcp(
                    "fake_sleep", arguments, meta={RequestFields.SCOPE: scope}
                )

        first, second = await asyncio.gather(slept("first"), slept("second"))
        if first.is_error or second.is_error:
            raise AssertionError(f"both calls of the scope finish: {first} {second}")

    async def test_server_declares_its_features_on_connect(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        """Возможности объявлены расширениями: клиент узнаёт ключ
        запечатывания, инструмент-связку и операцию журнала при согласовании
        протокола, не вызывая инструментов."""
        async with _client(url, dev_token) as client:
            capabilities = client.session.server_capabilities

        if capabilities is None:
            raise AssertionError("the protocol is negotiated on connect")

        declared = capabilities.extensions
        if declared is None:
            raise AssertionError(f"the server declares extensions: {capabilities}")

        for feature in (SealFeature.ID, FilesFeature.ID, JournalFeature.ID):
            if feature not in declared:
                raise AssertionError(f"{feature} is declared: {sorted(declared)}")

        files = FilesFeature.model_validate(declared[FilesFeature.ID])
        if files.scope != RequestFields.SCOPE:
            raise AssertionError(f"the files extension names the scope key: {files}")

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

        structured = _shown(result)
        if structured is None or structured.get("kind") != "workflow":
            raise AssertionError(f"the outcome is the workflow model: {structured}")
        if len(structured.get("nodes", [])) != len(nodes):
            raise AssertionError(f"every node reports its outcome: {structured}")


class Heard:
    """Уведомления прогресса одного вызова, какими их получил клиент."""

    def __init__(self) -> None:
        self.reports: list[tuple[float, float | None, str | None]] = []

    async def __call__(
        self, progress: float, total: float | None, message: str | None
    ) -> None:
        self.reports.append((progress, total, message))


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
        """Связка идёт одним запросом: отчёт каждого узла подписан его
        ключом, отчёты узлов не теряются друг из-за друга, а значение
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
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                WorkflowTool.NAME, {"nodes": nodes}, progress_handler=heard
            )

        if result.is_error:
            raise AssertionError(f"the workflow succeeds: {result}")

        texts = [str(report[2]) for report in heard.reports]
        left = [text for text in texts if text.startswith("left: ")]
        right = [text for text in texts if text.startswith("right: ")]
        if len(left) + len(right) != len(texts):
            raise AssertionError(f"every report names its node: {texts}")

        expected_left = [
            "left: step 1 of 3 (1 of 3)",
            "left: step 2 of 3 (2 of 3)",
            "left: step 3 of 3 (3 of 3)",
        ]
        if left != expected_left:
            raise AssertionError(f"the reports of the left node: {left}")

        expected_right = ["right: step 1 of 2 (1 of 2)", "right: step 2 of 2 (2 of 2)"]
        if right != expected_right:
            raise AssertionError(f"the reports of the right node: {right}")

        values = [report[0] for report in heard.reports]
        if values != sorted(set(values)):
            raise AssertionError(f"the progress of the request only grows: {values}")

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
        if properties != ["intent", "pause", "steps"]:
            raise AssertionError(f"the model sees its own arguments: {properties}")


class TestJournal:
    """Журнал вызова читается окнами по адресу из итога вызова: запуск и
    идентификатор вызова на сервере."""

    async def _echoed(self, client: Client[Any]) -> dict[str, str]:
        """Адрес журнала закончившегося вызова."""
        result = await client.call_tool_mcp("fake_echo", {"text": "hi", "repeat": 2})
        served = _served(result)
        if not served.run:
            raise AssertionError(f"the result names its run: {result}")

        return {
            "run": served.run,
            "node": served.id,
            "channel": ToolChannel.STDOUT.value,
        }

    async def test_windows_of_the_journal_join_at_line_borders(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            address = await self._echoed(client)
            whole = await client.call_tool_mcp(StreamReadTool.NAME, address)
            tail = await client.call_tool_mcp(
                StreamReadTool.NAME, {**address, "offset": 1}
            )

        piece = _shown(whole)
        if whole.is_error or piece is None:
            raise AssertionError(f"the journal is readable after the call: {whole}")
        if "echo progress: hi" not in piece["text"] or not piece["closed"]:
            raise AssertionError(f"the window carries the output of the body: {piece}")
        if piece["offset"] != 0 or piece["end"] != piece["size"]:
            raise AssertionError(f"the window names its place in the file: {piece}")

        rest = _shown(tail)
        if rest is None or not piece["text"].endswith(rest["text"]):
            raise AssertionError(f"a window from the middle starts at a line: {rest}")

    async def test_journal_of_another_caller_is_not_readable(
        self, stand: ServiceStand, url: str, dev_token: str, weak_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            address = await self._echoed(client)

        async with _client(url, weak_token) as client:
            foreign = await client.call_tool_mcp(StreamReadTool.NAME, address)

        if not foreign.is_error:
            raise AssertionError(f"a journal is read only by its caller: {foreign}")

        refusal = _shown(foreign)
        if refusal is None or refusal.get("error_kind") != ServerRefusal.FORBIDDEN:
            raise AssertionError(
                f"a foreign journal is refused as forbidden: {foreign}"
            )


class TestFailures:
    async def test_bad_arguments_are_an_error_result_with_a_model(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp("fake_echo", {"text": "hi"})

        if not result.is_error:
            raise AssertionError(f"a missing argument is an error: {result}")
        if _shown(result) is None:
            raise AssertionError("the refusal carries its model, not bare text")

    async def test_body_failure_is_an_error_result(
        self, stand: ServiceStand, url: str, dev_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "boom", "repeat": 1}
            )

        if not result.is_error:
            raise AssertionError(f"a failed body is an error: {result}")

        structured = _shown(result)
        if structured is None or "fake backend is down" not in str(structured):
            raise AssertionError(f"the failure model names the cause: {structured}")

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
        if _shown(result) is None:
            raise AssertionError("the plan refusal carries its model")

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
        return ServiceStand(tmp_path, RunLimits(max_runs=1, max_waiting=1), port)

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

        structured = _shown(third)
        if structured is None:
            raise AssertionError("the refusal carries its model")
        if structured.get("error_kind") != ServerRefusal.RUN_LIMIT:
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
        if names != ["fake_echo", FileUploadTool.NAME, StreamReadTool.NAME]:
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

        structured = _shown(result)
        if structured is None or structured.get("text") != f"ivanov|dev|{PROFILE}":
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

        structured = _shown(result)
        expected = f"{LOCAL_LOGIN}|dev|{PROFILE}"
        if structured is None or structured.get("text") != expected:
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

        structured = _shown(result)
        expected = f"{LOCAL_LOGIN}|dev|{PROFILE}"
        if structured is None or structured.get("text") != expected:
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


class TestSessionGeneration:
    """Поколение сессий: токены другого поколения сервис не принимает —
    так рестарт процесса или смена generation снимает все входы разом."""

    PUBLIC: str = "http://127.0.0.1:8650"
    SECRET: str = "stand-session-secret"

    def _tokens(self, generation: str, max_sec: int = 3600) -> ServiceTokens:
        return ServiceTokens(self.PUBLIC, self.SECRET, 300, max_sec, generation)

    def _holder(self, started: int) -> TokenHolder:
        sign_in = SignInMetadata(
            provider=SignInProvider.KERBEROS.value,
            roles=frozenset({"dev"}),
            profiles=frozenset({PROFILE}),
            principal="ivanov@LOSHARA.COM",
            sealed_ticket="sealed-ticket",
        )

        return TokenHolder(
            login="ivanov",
            sign_in=sign_in,
            started=started,
            resource=f"{self.PUBLIC}/mcp/{PROFILE}",
        )

    def test_token_carries_the_sign_in(self) -> None:
        """Токен несёт то, что вход знает о себе: по нему вызов получает
        делегированный билет входа kerberos."""
        tokens = self._tokens("stand")
        issued = tokens.session(self._holder(int(time.time())), "stand-client")

        access = tokens.read(issued.access_token)
        if access is None or access.scopes != [PROFILE]:
            raise AssertionError(f"the token opens the granted endpoint: {access}")

        if access.resource != f"{self.PUBLIC}/mcp/{PROFILE}":
            raise AssertionError(f"the token names its endpoint: {access}")

        metadata = SignInMetadata.parse((access.claims or {})["metadata"])
        ticket = metadata.ticket()
        if ticket is None or ticket.sealed != "sealed-ticket":
            raise AssertionError(f"the token carries the sealed ticket: {metadata}")

        refresh = tokens.refreshed(str(issued.refresh_token))
        if refresh is None:
            raise AssertionError("the refresh token is accepted")

        renewed = tokens.renewed(refresh)
        again = tokens.read(renewed.access_token)
        if again is None or again.resource != access.resource:
            raise AssertionError(f"the renewal keeps the endpoint: {again}")

        kept = SignInMetadata.parse(((again and again.claims) or {})["metadata"])
        if kept.ticket() != ticket:
            raise AssertionError(f"the renewal keeps the sign-in: {kept}")

    def test_tokens_of_another_generation_are_rejected(self) -> None:
        before = self._tokens("before-restart")
        after = self._tokens("after-restart")
        issued = before.session(self._holder(int(time.time())), "stand-client")
        refresh = str(issued.refresh_token)

        if before.read(issued.access_token) is None:
            raise AssertionError("the issuing generation accepts its access token")

        if before.refreshed(refresh) is None:
            raise AssertionError("the issuing generation accepts its refresh token")

        if after.read(issued.access_token) is not None:
            raise AssertionError("another generation rejects the access token")

        if after.refreshed(refresh) is not None:
            raise AssertionError("another generation rejects the refresh token")

    def test_renewal_stops_at_the_session_limit(self) -> None:
        tokens = self._tokens("stand", max_sec=600)
        fresh = tokens.session(self._holder(int(time.time()) - 500), "stand-client")
        left = fresh.expires_in
        if left is None or left > 100:
            raise AssertionError(f"the token ends with the session: {left}")

        with pytest.raises(TokenError, match="over its limit"):
            tokens.session(self._holder(int(time.time()) - 601), "stand-client")


class TestRegisteredClients:
    """Клиент, зарегистрировавшийся сам, остаётся известен после рестарта:
    его запись едет в его же идентификаторе, хранить её сервису негде."""

    SECRET: str = "stand-session-secret"

    def _client(self) -> OAuthClientInformationFull:
        return OAuthClientInformationFull(
            client_id="issued-by-the-handler",
            redirect_uris=[AnyUrl("http://127.0.0.1:53999/callback")],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="none",
            client_name="stand browser client",
        )

    def test_registration_survives_a_restart(self) -> None:
        client = self._client()
        RegisteredClients(SealedValues(self.SECRET), {}).register(client)
        issued = str(client.client_id)

        found = RegisteredClients(SealedValues(self.SECRET), {}).find(issued)

        if found is None or found.client_id != issued:
            raise AssertionError(f"another process knows the client: {found}")

        if found.redirect_uris != client.redirect_uris:
            raise AssertionError(f"the registration is kept whole: {found}")

    def test_foreign_identifier_is_unknown(self) -> None:
        client = self._client()
        RegisteredClients(SealedValues("another-secret"), {}).register(client)

        clients = RegisteredClients(SealedValues(self.SECRET), {})

        if clients.find(str(client.client_id)) is not None:
            raise AssertionError("an identifier of another service is not accepted")

        if clients.find("f625f2db-d8a0-4146-958f-94c6bf9ef636") is not None:
            raise AssertionError("a made-up identifier is not accepted")


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
                address.replace("/mcp/service/", "/mcp/narrow/"),
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
                {"name": "report.csv"},
                meta={RequestFields.SCOPE: self.SCOPE},
            )
            declared = client.session.server_capabilities

        structured = _shown(result)
        expected = f"/mcp/service/files/{self.SCOPE}/upload/report.csv"
        if structured is None or structured.get("path") != expected:
            raise AssertionError(f"the tool names the upload address: {result}")
        if declared is None or declared.extensions is None:
            raise AssertionError("the server declares extensions")

        files = declared.extensions.get(FilesFeature.ID)
        expected_files = {
            "path": "/mcp/service/files",
            "upload": FileUploadTool.NAME,
            "workspace": "/workspace",
            "scope": RequestFields.SCOPE,
        }
        if files != expected_files:
            raise AssertionError(f"the files extension is declared: {files}")


class TestJournalFile:
    """Журнал вызова целым файлом по маршруту endpoint'а."""

    async def test_channel_is_served_to_the_caller_only(
        self, stand: ServiceStand, url: str, dev_token: str, weak_token: str
    ) -> None:
        async with _client(url, dev_token) as client:
            result = await client.call_tool_mcp(
                "fake_echo", {"text": "hi", "repeat": 2}
            )

        served = _served(result)
        address = f"{url}/journals/{served.run}/{served.id}/{ToolChannel.STDOUT.value}"
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
        if foreign.status_code != 404:
            raise AssertionError(f"another user gets no journal: {foreign.status_code}")
        if anonymous.status_code != 401:
            raise AssertionError(f"no token, no journal: {anonymous.status_code}")
