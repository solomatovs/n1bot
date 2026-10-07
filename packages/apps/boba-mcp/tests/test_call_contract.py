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

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from service_stand import PROFILE, ServiceStand

from boba.mcp_server.server import RunLimits, StreamReadTool
from boba.toolkit.channels import ToolChannel
from boba.toolkit.wire import CallStatus, CallWire, WirePart

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
        if own:
            raise AssertionError(f"the schema carries no marks of its own: {own}")
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

    async def test_connection_parameter_is_a_plain_string(
        self, client: Client[Any]
    ) -> None:
        """Параметр-соединение для модели — обычная строка: описание называет
        вид соединения и форму ссылки, своих ключей в схеме нет."""
        listed = await client.list_tools()

        tool = next(tool for tool in listed if tool.name == "fake_connection_host")
        declared = tool.input_schema["properties"]["connection"]
        if declared["type"] != "string" or declared["minLength"] != 1:
            raise AssertionError(f"the connection is sent as a string: {declared}")

        if "conn://postgres/" not in declared["description"]:
            raise AssertionError(f"the description names the reference: {declared}")

        if sorted(declared) != ["description", "minLength", "title", "type"]:
            raise AssertionError(f"only standard schema keys: {sorted(declared)}")


class TestArguments:
    async def test_values_are_coerced_before_the_body(
        self, client: Client[Any]
    ) -> None:
        result = await client.call_tool_mcp("fake_echo", {"text": "hi", "repeat": "2"})

        sent = result.structured_content
        if result.is_error or sent is None:
            raise AssertionError(f"the call succeeds with a result: {result}")

        structured = sent[WirePart.RESULT.value]
        if structured is None:
            raise AssertionError(f"a numeric string is accepted: {result}")
        if structured.get("text") != "hi hi|t0ken":
            raise AssertionError(f"the body got the number: {structured}")

    async def test_service_fields_and_strays_do_not_fail_the_call(
        self, client: Client[Any]
    ) -> None:
        arguments = {"text": "hi", "repeat": 1, "intent": "say hi", "stray": True}
        result = await client.call_tool_mcp("fake_echo", arguments)

        sent = result.structured_content
        if result.is_error or sent is None:
            raise AssertionError(f"the call succeeds with a result: {result}")

        structured = sent[WirePart.RESULT.value]
        if structured is None:
            raise AssertionError(f"the call succeeds: {result}")
        if structured.get("text") != "hi|t0ken":
            raise AssertionError(f"the body got its own arguments: {structured}")


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

        sent = result.structured_content
        if sent is None:
            raise AssertionError(f"the refusal carries structured content: {result}")

        structured = sent[WirePart.RESULT.value]
        if structured.get("kind") != "exception":
            raise AssertionError(f"the refusal carries its model: {structured}")
        if structured.get("error_kind") != "ValidationError":
            raise AssertionError(f"the kind is the error class: {structured}")
        if structured.get("traceback"):
            raise AssertionError(f"the trace stays in the server log: {structured}")

        served = CallWire.model_validate(sent[WirePart.CALL.value])
        if served.status is not CallStatus.ERROR:
            raise AssertionError(f"the status of the call is error: {served}")

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
        """Сервер называет вызов сам и возвращает идентификатор в итоге: по
        нему и по запуску читается журнал."""
        result = await client.call_tool_mcp("fake_echo", {"text": "hi", "repeat": 1})

        sent = result.structured_content
        if sent is None:
            raise AssertionError(f"the result carries structured content: {result}")

        served = CallWire.model_validate(sent[WirePart.CALL.value])
        address = {
            "run": served.run,
            "node": served.id,
            "channel": ToolChannel.STDOUT.value,
        }
        window = await client.call_tool_mcp(StreamReadTool.NAME, address)
        piece = window.structured_content
        if window.is_error or piece is None:
            raise AssertionError(f"the journal is read by the call id: {window}")
        if "echo progress: hi" not in piece["text"]:
            raise AssertionError(f"the journal holds the output of the body: {piece}")
