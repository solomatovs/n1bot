"""Контракт вызова инструмента глазами клиента MCP (pytest -m integration).

Сервер поднят в процессе теста над фейками стенда, клиент — fastmcp Client.
Зафиксировано то, что клиент видит от цепочки запуска: схема инструмента в
списке, приведение аргументов до тела, текст отказа проверки аргументов и
идентификатор вызова в журнале вызова.
"""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import mcp_types as mt
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.client.extension import advertise
from service_stand import PROFILE, ServiceStand

from boba.connections.sealed import SealFeature
from boba.mcp_adapter import JournalUris
from boba.mcp_server.files import JournalRoutes
from boba.mcp_server.server import RunLimits, StreamReadTool
from boba.toolkit.calls import SchemaMark
from boba.toolkit.channels import ToolChannel
from boba.toolkit.mcp import McpResults
from boba.toolkit.wire import JournalRead
from boba.toolrun.call_id import CallFields

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def _text(result: mt.CallToolResult) -> str:
    """Текст итога, каким его получает модель клиента."""
    return McpResults().result("probe", "call", result).llm_view()


@pytest.fixture
def port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def stand(tmp_path: Path, port: int) -> ServiceStand:
    return ServiceStand(tmp_path, RunLimits(max_runs=4, max_waiting=4), port)


@pytest.fixture
async def client(stand: ServiceStand) -> AsyncIterator[Client[Any]]:
    async with stand.serving():
        token = await stand.dev_token(PROFILE)
        transport = StreamableHttpTransport(stand.url(PROFILE), auth=token)
        async with Client(transport) as opened:
            yield opened


class TestToolSchema:
    INTENT: ClassVar[dict[str, Any]] = {
        "default": "",
        "description": (
            "Short line shown to the user as the step title: what this call does "
            "and why, in the language of the conversation. Keep it under ten words."
        ),
        "title": "Intent",
        "type": "string",
    }

    THREAD: ClassVar[dict[str, Any]] = {
        "default": "",
        "description": CallFields.THREAD_DESCRIPTION,
        "title": "Thread Id",
        "type": "string",
        SchemaMark.THREAD.value: True,
    }

    ECHO: ClassVar[dict[str, Any]] = {
        "description": "Повторяет текст, приправив секретом из конфига.",
        "properties": {
            "text": {
                "description": "Что вернуть",
                "minLength": 1,
                "title": "Text",
                "type": "string",
            },
            "repeat": {
                "description": "Сколько раз",
                "minimum": 1,
                "title": "Repeat",
                "type": "integer",
            },
            CallFields.THREAD: THREAD,
            "intent": INTENT,
        },
        "required": ["text", "repeat"],
        "title": "fake_echo",
        "type": "object",
    }

    EMIT_PROPERTIES: ClassVar[list[str]] = [
        "prefix",
        "count",
        "size",
        "fail_midway",
        CallFields.THREAD,
        "out",
        "pipe_bytes",
        "intent",
    ]

    async def test_schema_of_a_tool_is_published_as_the_model_sees_it(
        self, client: Client[Any]
    ) -> None:
        listed = await client.list_tools()

        echo = next(tool for tool in listed if tool.name == "fake_echo")
        if echo.description != self.ECHO["description"]:
            raise AssertionError(f"the description is the docstring: {echo}")
        published = dict(echo.input_schema)
        if published != self.ECHO:
            raise AssertionError(f"the schema of fake_echo: {echo.input_schema}")

        own = [key for key in json.dumps(published).split('"') if "x-boba" in key]
        if own != [SchemaMark.THREAD.value]:
            raise AssertionError(f"the only own mark is the thread: {own}")
        if list(echo.input_schema["properties"]) != list(self.ECHO["properties"]):
            raise AssertionError(
                f"the order of the arguments is kept: {echo.input_schema}"
            )

    async def test_stream_tool_shows_channels_in_place_of_ports(
        self, client: Client[Any]
    ) -> None:
        listed = await client.list_tools()

        emit = next(tool for tool in listed if tool.name == "fake_emit")
        properties = emit.input_schema["properties"]
        if list(properties) != self.EMIT_PROPERTIES:
            raise AssertionError(f"the arguments of fake_emit: {list(properties)}")
        if properties["out"]["type"] != "string" or properties["out"]["minLength"] != 1:
            raise AssertionError(f"an output port is a channel name: {properties}")
        if properties["pipe_bytes"]["default"] != ServiceStand.STREAM.pipe_bytes:
            raise AssertionError(f"the pipe size has its default: {properties}")
        if "out" not in emit.input_schema["required"]:
            raise AssertionError(f"the channel is required: {emit.input_schema}")

    async def test_code_argument_names_its_content_type(
        self, client: Client[Any]
    ) -> None:
        """Аргумент с кодом назван стандартным ключом JSON Schema
        contentMediaType: что это за текст, а не как его показывать."""
        listed = await client.list_tools()

        query = next(tool for tool in listed if tool.name == "fake_query")
        properties = query.input_schema["properties"]
        if properties["sql"].get("contentMediaType") != "application/sql":
            raise AssertionError(f"the query is typed as sql: {properties['sql']}")
        if "contentMediaType" in properties["limit"]:
            raise AssertionError(f"a number has no content type: {properties}")

    async def test_connection_parameter_declares_a_sealed_profile(
        self, stand: ServiceStand
    ) -> None:
        """Параметр-соединение — строка с пометкой вида x-boba-connection, чьё
        содержимое объявлено стандартными ключами JSON Schema: JWE
        (contentMediaType) с содержимым вида postgres внутри (contentSchema,
        константа kind). Endpoint предлагает соединения пользователя клиенту,
        который их запечатывает."""
        async with stand.serving():
            token = await stand.dev_token(PROFILE)
            transport = StreamableHttpTransport(stand.url(PROFILE), auth=token)
            sealing = [advertise(SealFeature.ID)]
            async with Client(transport, extensions=sealing) as opened:
                listed = await opened.list_tools()

        tool = next(tool for tool in listed if tool.name == "fake_connection_host")
        declared = tool.input_schema["properties"]["connection"]
        if declared["type"] != "string" or declared["minLength"] != 1:
            raise AssertionError(f"the connection is sent as a string: {declared}")

        if declared["contentMediaType"] != "application/jose":
            raise AssertionError(f"the content is a compact JWE: {declared}")

        content = declared["contentSchema"]
        if content["properties"]["kind"]["const"] != "postgres":
            raise AssertionError(f"the content schema names the kind: {content}")
        if content["required"] != ["kind"]:
            raise AssertionError(f"the kind is required: {content}")

        expected = [
            "contentMediaType",
            "contentSchema",
            "description",
            "minLength",
            "title",
            "type",
            SchemaMark.CONNECTION.value,
        ]
        if sorted(declared) != expected:
            raise AssertionError(f"standard keys and the mark: {sorted(declared)}")
        if declared[SchemaMark.CONNECTION.value] != "postgres":
            raise AssertionError(f"the mark names the kind: {declared}")


class TestArguments:
    async def test_values_are_coerced_before_the_body(
        self, client: Client[Any]
    ) -> None:
        result = await client.call_tool_mcp("fake_echo", {"text": "hi", "repeat": "2"})

        if result.is_error:
            raise AssertionError(f"a numeric string is accepted: {result}")
        if _text(result) != "hi hi|t0ken":
            raise AssertionError(f"the body got the number: {result}")

    async def test_service_fields_and_strays_do_not_fail_the_call(
        self, client: Client[Any]
    ) -> None:
        arguments = {"text": "hi", "repeat": 1, "intent": "say hi", "stray": True}
        result = await client.call_tool_mcp("fake_echo", arguments)

        if result.is_error:
            raise AssertionError(f"the call succeeds: {result}")
        if _text(result) != "hi|t0ken":
            raise AssertionError(f"the body got its own arguments: {result}")


class TestRefusal:
    MISSING: ClassVar[str] = "ValidationError: repeat: Field required [missing]"

    WRONG: ClassVar[str] = (
        "ValidationError: text: String should have at least 1 character "
        "[string_too_short]; repeat: Input should be greater than or equal to 1 "
        "[greater_than_equal]"
    )

    RAISED_AT: ClassVar[str] = "raised at: boba/toolrun/"

    def _check(self, result: Any, expected: str) -> None:
        if not result.is_error:
            raise AssertionError(f"bad arguments are an error: {result}")

        text = result.content[0].text
        lines = text.split("\n")
        if lines[0] != expected:
            raise AssertionError(f"the refusal names the reasons: {text!r}")
        if len(lines) != 2 or not lines[1].startswith(self.RAISED_AT):
            raise AssertionError(f"the refusal names its place: {text!r}")

        if result.structured_content is not None:
            raise AssertionError(f"the refusal has no output schema: {result}")

        if "Traceback" in result.model_dump_json():
            raise AssertionError(f"the trace stays in the server log: {result}")

    async def test_missing_argument_is_named(self, client: Client[Any]) -> None:
        result = await client.call_tool_mcp("fake_echo", {"text": "hi"})

        self._check(result, self.MISSING)

    async def test_every_wrong_value_is_named(self, client: Client[Any]) -> None:
        result = await client.call_tool_mcp("fake_echo", {"text": "", "repeat": 0})

        self._check(result, self.WRONG)


class TestCallIdInJournal:
    async def test_journal_of_the_call_lives_under_the_id_of_the_server(
        self, client: Client[Any]
    ) -> None:
        """Сервер называет вызов сам и ссылается в итоге на каналы его
        журнала: по адресу из ссылки журнал читается окнами."""
        result = await client.call_tool_mcp("fake_echo", {"text": "hi", "repeat": 1})

        uris = JournalUris(JournalRoutes.RESOURCE)
        found: list[JournalRead] = []
        for block in result.content:
            if not isinstance(block, mt.ResourceLink):
                continue

            if read := uris.address(str(block.uri)):
                found.append(read)

        stdout: list[JournalRead] = []
        for read in found:
            if read.channel is ToolChannel.STDOUT:
                stdout.append(read)

        if len(stdout) != 1:
            raise AssertionError(f"the result links the journal channels: {found}")

        address = stdout[0].model_dump(mode="json", exclude_none=True)
        window = await client.call_tool_mcp(StreamReadTool.NAME, address)
        piece = window.structured_content
        if window.is_error or piece is None:
            raise AssertionError(f"the journal is read by the call id: {window}")
        if "echo progress: hi" not in piece["text"]:
            raise AssertionError(f"the journal holds the output of the body: {piece}")
