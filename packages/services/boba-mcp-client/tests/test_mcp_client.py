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
import os
import socket
import subprocess
import sys
from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Protocol
from urllib.parse import urlsplit
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
from boba.canvas.keys import ObjectKey, ScopeDir, WorkspaceMount
from boba.canvas.storage import StorageNotFoundError
from boba.config import bind
from boba.connections.sealed import ConnectionSeal, SealedConnection, SealFeature
from boba.core.secrets import SecretReveal
from boba.db.postgres.connection import PostgresConfig
from boba.mcp_client.client import (
    BearerAuth,
    HttpEndpoint,
    HttpLocation,
    McpCaller,
    McpClientError,
    McpClientIdentity,
    McpServerApps,
    McpServerConfig,
    McpServerLimits,
    McpServers,
    McpServersConfig,
    McpToolServer,
    McpUnansweredError,
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
from boba.toolkit.wire import JournalFeature, JournalNotice, JournalRead, ScopeFeature
from boba.workspace.launcher import ReadWindow

pytestmark = pytest.mark.anyio

SERVER = Path(__file__).with_name("standard_server.py")


def _config(endpoint: HttpEndpoint | StdioCommand) -> McpServerConfig:
    return McpServerConfig(
        endpoint=endpoint,
        limits=McpServerLimits(connect_timeout_sec=20.0, call_timeout_sec=60.0),
    )


class Edition(StrEnum):
    """Редакции MCP обычного сервера в тестах порта: сервер на SDK mcp по
    stdio (2026-07-28) и свой сервер на TypeScript SDK 1.x по HTTP с
    сессиями в контейнере boba-testapps (2025-11-25)."""

    MODERN = "2026-07-28"
    LEGACY = "2025-11-25"


async def _tools_of(port: McpToolServer) -> list[str]:
    """Имена инструментов обычного сервера согласованной редакции."""
    if (await port.described(OWN)).result.supported_versions == [Edition.LEGACY.value]:
        return TestFailureStand.LEGACY_TOOLS

    return TestFailureStand.STANDARD_TOOLS


def _legacy(slot: AppsSlot, server: AppServer) -> HttpEndpoint:
    return HttpEndpoint(
        service=HttpLocation(scheme="http", host=slot.host, port=server.value, path=""),
        path=slot.PATH,
    )


CALLER = McpCaller(login="alice", roles=frozenset({"dev"}))
OWN = McpServers.OWN


async def _stdout(port: McpToolServer, reply: mt.CallToolResult) -> JournalRead:
    """Адрес канала stdout журнала вызова по ссылке в итоге."""
    links = (await port.described(OWN)).links()
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
    server = McpToolServer("standard", _config(edition_endpoint))
    await server.described(OWN)
    try:
        yield server
    finally:
        await server.close()


@pytest.fixture
async def stdio() -> AsyncIterator[McpToolServer]:
    endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
    server = McpToolServer("standard", _config(endpoint))
    await server.described(OWN)
    try:
        yield server
    finally:
        await server.close()


@pytest.fixture
def http_port(request: pytest.FixtureRequest) -> Iterator[int]:
    """Порт обычного сервера по HTTP; косвенный параметр — добавочные
    аргументы сервера (--ttl-ms, --retired)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    extra: tuple[str, ...] = getattr(request, "param", ())
    process = subprocess.Popen(
        [sys.executable, str(SERVER), "streamable-http", str(port), *extra]
    )
    try:
        yield port
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
async def http(http_port: int) -> AsyncIterator[McpToolServer]:
    endpoint = HttpEndpoint(
        service=HttpLocation(scheme="http", host="127.0.0.1", port=http_port, path=""),
        path="/mcp",
    )
    server = McpToolServer("standard", _config(endpoint))
    for _ in range(100):
        try:
            await server.described(OWN)
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
    """Слушатель журналов вызова в тесте: копит уведомления о журналах,
    которые вызов открыл на сервере, в порядке прихода, и в момент первого
    — число подключений клиента к серверу (peers)."""

    def __init__(self, peers: ServerPeers) -> None:
        self.opened: list[JournalNotice] = []
        self.peers: list[int] = []
        self._peers = peers

    async def __call__(self, notice: JournalNotice) -> None:
        self.opened.append(notice)
        self.peers.append(self._peers.count())


class ServerPeers(Protocol):
    """Сколько подключений тест держит к серверу прямо сейчас."""

    @abstractmethod
    def count(self) -> int: ...


class TcpPeers(ServerPeers):
    """Реализация ServerPeers по таблицам TCP ядра: установленные
    соединения на порт сервера (/proc/net/tcp, tcp6 — удалённый порт
    в шестнадцатеричном виде после двоеточия, состояние 01)."""

    TABLES: ClassVar[tuple[Path, ...]] = (
        Path("/proc/net/tcp"),
        Path("/proc/net/tcp6"),
    )
    ESTABLISHED: ClassVar[str] = "01"

    def __init__(self, port: int) -> None:
        self._port = f"{port:04X}"

    def count(self) -> int:
        established = list(self._established())

        return len(established)

    def _established(self) -> Iterator[str]:
        for table in self.TABLES:
            with table.open() as rows:
                next(rows)
                for row in rows:
                    fields = row.split()
                    remote = fields[2].rsplit(":", 1)[1]
                    if remote != self._port:
                        continue

                    if fields[3] != self.ESTABLISHED:
                        continue

                    yield fields[2]


class ChildServers(ServerPeers):
    """Реализация ServerPeers для сервера stdio: подключение — процесс
    сервера script, запущенный самим тестом (/proc/<pid>/stat и cmdline)."""

    def __init__(self, script: Path) -> None:
        self._script = str(script).encode()
        self._parent = str(os.getpid())

    def count(self) -> int:
        children = list(self._children())

        return len(children)

    def _children(self) -> Iterator[Path]:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue

            try:
                stat = (entry / "stat").read_text()
                cmdline = (entry / "cmdline").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue

            parent = stat.rsplit(")", 1)[1].split()[1]
            if parent != self._parent:
                continue

            if self._script not in cmdline:
                continue

            yield entry


class TestStandardServer:
    """Ответ tools/call — как есть; текст для модели — правила MCP поверх
    него (McpResults)."""

    async def test_tools_are_listed_with_the_server_schema(
        self, served: McpToolServer
    ) -> None:
        names = sorted(tool.name for tool in (await served.tools(OWN)).tools)
        if names != await _tools_of(served):
            raise AssertionError(f"the server's tools as listed: {names}")

        add = next(
            tool for tool in (await served.tools(OWN)).tools if tool.name == "add"
        )
        if sorted(add.input_schema["properties"]) != ["a", "b"]:
            raise AssertionError(f"the server's schema is kept: {add.input_schema}")

    async def test_annotations_become_hints(self, served: McpToolServer) -> None:
        stubs = McpToolStubs()
        by_name = {tool.name: tool for tool in (await served.tools(OWN)).tools}

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
        if (await served.described(OWN)).features():
            raise AssertionError("a foreign server has no features")

        if (await served.described(OWN)).nested_calls() is not None:
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
        server = McpToolServer("standard", _config(endpoint))
        await server.described(OWN)
        try:
            links = (await server.described(OWN)).links()
        finally:
            await server.close()

        if links is None:
            raise AssertionError("the journal extension with an extra field is parsed")

        read = links.address(f"journal://r1/n1/{ToolChannel.STDOUT.value}")
        expected = JournalRead(run="r1", node="n1", channel=ToolChannel.STDOUT)
        if read != expected:
            raise AssertionError(f"the template of the server is used: {read}")

        if (await server.described(OWN)).nested_calls() is not None:
            raise AssertionError("broken nested calls settings declare nothing")

    async def test_text_result_becomes_a_tool_message(
        self, served: McpToolServer
    ) -> None:
        reply = await served.call(OWN, _call("add", a=2, b=3), None, None, None, None)
        result = McpResults().result("add", "c1", reply)

        if not isinstance(result, MarkdownResult) or result.text != "5":
            raise AssertionError(f"the text of the result is the content: {result}")

    async def test_tool_error_is_an_error_message_with_the_server_text(
        self, served: McpToolServer
    ) -> None:
        reply = await served.call(OWN, _call("broken"), None, None, None, None)
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
        reply = await served.call(OWN, _call("picture"), None, None, None, None)
        text = McpResults().result("picture", "c1", reply).llm_view()

        if text != "[image image/png]":
            raise AssertionError(f"an image block is named, not dropped: {text}")

    async def test_model_gets_its_blocks_text_and_marks(
        self, served: McpToolServer
    ) -> None:
        reply = await served.call(
            OWN, _call("blocks", empty=False), None, None, None, None
        )
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
        reply = await served.call(
            OWN, _call("blocks", empty=True), None, None, None, None
        )
        text = McpResults().result("blocks", "c1", reply).llm_view()

        if text != '{\n  "rows": 2\n}':
            raise AssertionError(f"structured content becomes JSON text: {text}")

    async def test_input_required_comes_back_as_is(self, stdio: McpToolServer) -> None:
        reply = await stdio.call(
            OWN, _call("ask", refuse=False), None, None, None, None
        )

        if not isinstance(reply, mt.InputRequiredResult):
            raise AssertionError(f"the request for input is returned: {reply}")
        if not reply.request_state:
            raise AssertionError(f"the state of the server is kept: {reply}")

    async def test_answered_input_request_finishes_the_call(
        self, stdio: McpToolServer
    ) -> None:
        """Форма сервера и повтор вызова с ответом и тем же requestState."""
        asked = await stdio.call(
            OWN, _call("load_rows", rows=10), None, None, None, None
        )
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
        reply = await stdio.call(OWN, again, None, None, None, None)
        text = McpResults().result("load_rows", "c1", reply).llm_view()

        if text != "loaded 10 rows into sales.orders":
            raise AssertionError(f"the answer reaches the server: {text}")

    async def test_server_error_goes_up_as_is(self, served: McpToolServer) -> None:
        with pytest.raises(MCPError) as refused:
            await served.call(OWN, _call("ask", refuse=True), None, None, None, None)

        error = refused.value.error
        if error.code != mt.INVALID_PARAMS or "refuses" not in error.message:
            raise AssertionError(f"the server's error is kept: {error}")
        if error.data != {"reason": "the stand refuses"}:
            raise AssertionError(f"the data of the error is kept: {error}")

    async def test_progress_of_the_call_reaches_the_callback(
        self, served: McpToolServer
    ) -> None:
        heard = HeardCall()

        await served.call(OWN, _call("progress", steps=2), None, heard, None, None)

        expected = [(1.0, 2.0, "step 1 of 2"), (2.0, 2.0, "step 2 of 2")]
        if heard.reports != expected:
            raise AssertionError(f"the reports arrive as they were made: {heard}")


class TestStandardServerOverHttp:
    async def test_call_goes_over_streamable_http(self, http: McpToolServer) -> None:
        reply = await http.call(
            OWN, _call("shout", text="over http"), None, None, None, None
        )
        text = McpResults().result("shout", "c1", reply).llm_view()

        if text != "OVER HTTP":
            raise AssertionError(f"the call reaches the http server: {text}")


class TestUnreachableServer:
    async def test_missing_command_is_a_client_error(self) -> None:
        endpoint = StdioCommand(command="/nonexistent/mcp-server")
        server = McpToolServer("ghost", _config(endpoint))

        with pytest.raises(McpClientError, match=r"ghost|nonexistent"):
            await server.described(OWN)

    async def test_closed_port_is_unanswered_with_a_short_reason(self) -> None:
        """Сервер не слушает порт: полный текст называет адрес и причину
        httpx для журнала, короткая причина для человека — «нет соединения»."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        location = HttpLocation(scheme="http", host="127.0.0.1", port=port, path="")
        endpoint = HttpEndpoint(service=location, path="/mcp")
        server = McpToolServer("closed", _config(endpoint))

        with pytest.raises(McpUnansweredError) as caught:
            await server.described(OWN)

        assert caught.value.brief == "no connection", caught.value
        assert f"127.0.0.1:{port}" in str(caught.value), caught.value

    async def test_lost_session_fails_the_call_and_the_next_call_reconnects(
        self, http_port: int, http: McpToolServer
    ) -> None:
        await http.close()

        reply = await http.call(OWN, _call("add", a=4, b=4), None, None, None, None)
        text = McpResults().result("add", "c1", reply).llm_view()

        if text != "8":
            raise AssertionError(f"the closed session is opened again: {text}")


@pytest.mark.integration
class TestPublicServers:
    """Чужие реализации MCP в сети; без сети тесты падают с причиной."""

    DEEPWIKI = HttpEndpoint(
        service=HttpLocation(scheme="https", host="mcp.deepwiki.com", path=""),
        path="/mcp",
    )
    CONTEXT7 = HttpEndpoint(
        service=HttpLocation(scheme="https", host="mcp.context7.com", path=""),
        path="/mcp",
    )

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
        server = McpToolServer("deepwiki", _config(self.DEEPWIKI))
        await server.described(OWN)
        try:
            versions = (await server.described(OWN)).result.supported_versions
            names = [tool.name for tool in (await server.tools(OWN)).tools]
            asked = _call(
                "read_wiki_structure", repoName="modelcontextprotocol/python-sdk"
            )
            reply = await server.call(OWN, asked, None, None, None, None)
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
        server = McpToolServer("context7", _config(self.CONTEXT7))
        await server.described(OWN)
        try:
            if (await server.described(OWN)).result.supported_versions != [
                Edition.MODERN.value
            ]:
                raise AssertionError(
                    f"context7 stays modern: {(await server.described(OWN)).result}"
                )

            names = [tool.name for tool in (await server.tools(OWN)).tools]
            if "resolve-library-id" not in names:
                raise AssertionError(f"context7 offers its tools: {names}")

            tool = next(
                t
                for t in (await server.tools(OWN)).tools
                if t.name == "resolve-library-id"
            )
            args: dict[str, object] = {}
            for name in tool.input_schema.get("required", []):
                args[name] = "fastapi"

            reply = await server.call(
                OWN, _call(tool.name, **args), None, None, None, None
            )
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
            names = sorted(tool.name for tool in (await port.tools(OWN)).tools)
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
            late = await servers.port("late", CALLER)
            with pytest.raises(McpClientError, match="'late'"):
                await late.tools(OWN)

            command.write_text(f"#!/bin/sh\nexec {sys.executable} {SERVER} stdio\n")
            command.chmod(0o755)
            others = await other.tools(OWN)
            came_up = await late.tools(OWN)
        finally:
            await servers.stop()

        if not others.tools:
            raise AssertionError("a dead server does not hold back other servers")
        if not came_up.tools:
            raise AssertionError("the next request connects the server that came up")

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
        server = McpToolServer("standard", config)
        await server.described(OWN)

        return server

    async def test_ui_is_declared_only_to_a_server_with_apps(
        self, edition_endpoint: HttpEndpoint | StdioCommand
    ) -> None:
        drawn = await self._open(edition_endpoint, self.SANDBOX)
        plain = await self._open(edition_endpoint, None)
        try:
            by_app = await drawn.call(
                OWN, _call("orders", rows=self.ROWS), None, None, None, None
            )
            by_blocks = await plain.call(
                OWN, _call("orders", rows=self.ROWS), None, None, None, None
            )
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
                OWN, mt.ReadResourceRequestParams(uri=self.ORDERS), None
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
            await served.read_resource(OWN, missing, None)

    def test_sandboxes_of_two_servers_on_one_origin_are_refused(self) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        first = McpServerConfig.model_validate(
            {
                "endpoint": endpoint,
                "limits": {"connect_timeout_sec": 1.0, "call_timeout_sec": 1.0},
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

    async def test_prefab_tool_names_its_renderer_and_structured_view(
        self, apps_slot: AppsSlot
    ) -> None:
        """Инструмент Prefab сервера fastmcp[apps]: tools/list несёт
        _meta.ui.resourceUri рендерера, ресурс читается типом приложения с
        CSP только на источник статики стенда — ни CDN, ни шрифтов извне, —
        итог вызова несёт view в structuredContent и текст для модели."""
        server = await self._open(_legacy(apps_slot, AppServer.PREFAB), self.SANDBOX)
        try:
            listed = await server.tools(OWN)
            sales = next(tool for tool in listed.tools if tool.name == "sales")
            assert sales.meta is not None, sales
            uri = sales.meta["ui"]["resourceUri"]
            read = await server.read_resource(
                OWN, mt.ReadResourceRequestParams(uri=uri), None
            )
            reply = await server.call(
                OWN, _call("sales", region="North"), None, None, None, None
            )
        finally:
            await server.close()

        assert uri.startswith("ui://prefab/tool/"), uri
        assert uri.endswith("/renderer.html"), uri
        assert isinstance(read, mt.ReadResourceResult)
        page = read.contents[0]
        assert page.mime_type == APP_MIME_TYPE
        assert isinstance(page, mt.TextResourceContents)
        assert apps_slot.renderer_url() in page.text
        assert page.meta is not None
        domains: list[str] = []
        for listed_domains in page.meta["ui"]["csp"].values():
            domains.extend(listed_domains)
        assert domains, page.meta
        for domain in domains:
            assert urlsplit(domain).hostname == apps_slot.host, domains

        assert isinstance(reply, mt.CallToolResult), reply
        text = McpResults().result("sales", "c1", reply).llm_view()
        assert text == "2 sales rows are shown by their table"
        assert reply.structured_content is not None
        assert reply.structured_content["view"]["type"] == "Div", reply


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
            service=HttpLocation(
                scheme="http", host="127.0.0.1", port=self.port, path=""
            ),
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
            await server.described(OWN)
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
        reply = await dag.call(
            OWN, _call("fake_scope", scope_id=self.THREAD), None, None, None, None
        )
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

    async def test_workspace_of_the_server_stays_with_its_port(
        self, dag: McpToolServer
    ) -> None:
        """Каталог workspace сервера — часть его описания у порта: клиент не
        кладёт его в общую точку процесса, ключи файлов итогов разбирает тот,
        кто держит порт этого сервера."""
        workspace = await dag.workspace()
        files = await dag.files()

        assert workspace == "/workspace"
        assert files is not None
        with pytest.raises(RuntimeError, match="not configured"):
            WorkspaceMount.path()

    async def test_uploaded_stream_is_read_back_by_window(
        self, dag: McpToolServer
    ) -> None:
        files = await dag.files()
        if files is None:
            raise AssertionError("the server declares its files")

        payload = bytes(range(256)) * 2048
        key = ObjectKey(user_id="-", scope_id=self.SCOPE, name="data.bin")
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
        files = await dag.files()
        if files is None:
            raise AssertionError("the server declares its files")

        key = ObjectKey(user_id="-", scope_id=self.SCOPE, name="ghost.bin")
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
        files = await dag.files()
        if files is None:
            raise AssertionError("the server declares its files")

        scope = str(uuid4())
        upload = ObjectKey(user_id="-", scope_id=scope, name="a.txt")
        diagram = ObjectKey(
            user_id="-", scope_id=scope, name="d.mmd", dir=ScopeDir.MERMAID
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
        for tool in (await dag.tools(OWN)).tools:
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

        reply = await dag.call(
            OWN, _call("fake_progress", steps=2, pause=0.6), None, heard, None, None
        )

        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the call succeeds: {reply}")
        if heard.reports != [(1.0, 2.0, "step 1 of 2"), (2.0, 2.0, "step 2 of 2")]:
            raise AssertionError(f"the reports arrive as they were made: {heard}")

    async def test_reports_of_workflow_nodes_name_their_node_in_meta(
        self, dag: McpToolServer
    ) -> None:
        """Уведомление доходит до вызова целиком: ключ узла связки лежит в
        _meta под ключом, объявленным расширением вложенных вызовов."""
        nested = (await dag.described(OWN)).nested_calls()
        if nested is None:
            raise AssertionError("the server declares nested calls")

        nodes = [
            {"key": "left", "tool": "fake_progress", "args": {"steps": 2, "pause": 0.6}}
        ]
        heard = HeardCall()

        reply = await dag.call(
            OWN, _call("workflow", nodes=nodes), None, heard, None, None
        )

        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the workflow succeeds: {reply}")

        expected = [{nested.progress_key: "left"}, {nested.progress_key: "left"}]
        if heard.metas != expected:
            raise AssertionError(f"every report names its node: {heard.metas}")

    async def test_journal_is_noticed_in_the_stream_of_its_call(
        self, dag: McpToolServer, boba_mcp_stand: BobaMcpStand
    ) -> None:
        """Журнал идущего вызова порт узнаёт уведомлением расширения журнала
        в потоке ответа самого вызова, под его токеном прогресса: адрес тот
        же, что у ссылки в итоге, окно журнала читает resources/read, а
        второго подключения к серверу у порта нет."""
        heard = HeardCall()
        journals = HeardJournals(TcpPeers(boba_mcp_stand.port))
        reply = await dag.call(
            OWN,
            _call("fake_progress", steps=3, pause=0.5),
            None,
            heard,
            journals,
            None,
        )
        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the call succeeds: {reply}")

        stdout = await _stdout(dag, reply)
        window = await dag.journal(stdout)

        if len(heard.reports) != 3:
            raise AssertionError(f"the progress goes on: {heard.reports}")
        if len(journals.opened) != 1:
            raise AssertionError(f"the journal is named once: {journals.opened}")
        notice = journals.opened[0]
        if (notice.run, notice.node, notice.key) != (stdout.run, stdout.node, None):
            raise AssertionError(f"the address of the result: {notice} {stdout}")
        if stdout.channel not in notice.channels:
            raise AssertionError(f"the channels of the journal: {notice}")
        if journals.peers != [1]:
            raise AssertionError(f"one connection serves the call: {journals.peers}")
        if window is None or "step 3 of 3" not in window.text:
            raise AssertionError(f"the journal is read from the server: {window}")
        if not window.closed:
            raise AssertionError(f"the journal of a finished call is closed: {window}")

    async def test_journal_of_a_workflow_node_names_its_key(
        self, dag: McpToolServer, boba_mcp_stand: BobaMcpStand
    ) -> None:
        """Журнал узла связки уведомление называет ключом узла от автора."""
        nodes = [
            {"key": "slow", "tool": "fake_progress", "args": {"steps": 3, "pause": 0.5}}
        ]
        journals = HeardJournals(TcpPeers(boba_mcp_stand.port))
        reply = await dag.call(
            OWN, _call("workflow", nodes=nodes), None, None, journals, None
        )

        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the workflow succeeds: {reply}")
        keys = [notice.key for notice in journals.opened]
        if keys != ["slow"]:
            raise AssertionError(f"the journal names its node: {journals.opened}")

    async def test_journal_channel_is_downloaded_whole_and_by_range(
        self, dag: McpToolServer
    ) -> None:
        reply = await dag.call(
            OWN, _call("fake_echo", text="hi", repeat=2), None, None, None, None
        )
        if not isinstance(reply, mt.CallToolResult):
            raise AssertionError(f"the call is answered: {reply}")

        files = await dag.journal_files()
        if files is None:
            raise AssertionError(f"the journal is addressable: {reply}")

        address = await _stdout(dag, reply)
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
        if JournalFeature.ID not in (await dag.described(OWN)).features():
            raise AssertionError("the journal is declared")

        request = JournalRead(run="nope", node="nope", channel=ToolChannel.STDOUT)
        window = await dag.journal(request)

        if window is not None:
            raise AssertionError(f"an unknown call has no journal: {window}")

    async def test_standard_server_has_no_journal(self, stdio: McpToolServer) -> None:
        request = JournalRead(run="r", node="n", channel=ToolChannel.STDOUT)

        window = await stdio.journal(request)

        if window is not None:
            raise AssertionError(f"a standard server keeps no journal: {window}")

    async def test_call_of_a_server_without_notices_keeps_one_connection(
        self, stdio: McpToolServer
    ) -> None:
        """Обычный сервер объявляет журнал, но уведомлений о нём не шлёт:
        вызов со слушателем журналов идёт тем же подключением — процессом
        сервера, итог приходит как есть, а слушатель ничего не слышит."""
        processes = ChildServers(SERVER)
        journals = HeardJournals(processes)
        heard = HeardCall()

        reply = await stdio.call(
            OWN, _call("progress", steps=2), None, heard, journals, None
        )

        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the call succeeds: {reply}")
        if len(heard.reports) != 2:
            raise AssertionError(f"the progress arrives: {heard.reports}")
        if journals.opened:
            raise AssertionError(f"no journal is noticed: {journals.opened}")
        if processes.count() != 1:
            raise AssertionError(f"one server process: {processes.count()}")


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
        """Порт сервера name пользователя caller, когда стенд поднялся."""
        port = await servers.port(name, caller)
        for _ in range(150):
            try:
                await port.described(OWN)
            except McpClientError:
                await asyncio.sleep(0.2)
                continue

            break

        return port

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
        journals = HeardJournals(TcpPeers(boba_mcp_stand.port))
        events: list[str] = []
        try:
            port = await self._port(servers, owner, "boba")
            other = await self._port(servers, stranger, "boba")
            links = (await port.described(OWN)).links()
            if links is None:
                raise AssertionError("the server declares its journal")

            slow = _call("fake_progress", steps=8, pause=0.5)
            running = asyncio.create_task(
                port.call(OWN, slow, None, heard, journals, None)
            )
            async with asyncio.timeout(30):
                while not journals.opened:
                    await asyncio.sleep(0.05)

                opened = journals.opened[0]
                read = JournalRead(
                    run=opened.run, node=opened.node, channel=ToolChannel.STDOUT
                )
                uri = links.uri(read)

                # канал появляется с первой записью: так панель и подписывается
                while await port.journal(read) is None:
                    await asyncio.sleep(0.05)

            with pytest.raises(MCPError) as refused:
                async with other.listen(OWN, self._channel(uri)):
                    pass

            # у запуска нет такого вызова: журнала нет, подписка на него — отказ
            absent = links.uri(read.model_copy(update={"node": "no-such-call"}))
            with pytest.raises(MCPError) as missing:
                async with port.listen(OWN, self._channel(absent)):
                    pass

            with pytest.raises(MCPError) as alien:
                async with port.listen(OWN, self._channel("file:///x")):
                    pass

            channel = self._channel(uri)
            async with asyncio.timeout(30), port.listen(OWN, channel) as subscription:
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
            reply = await port.call(OWN, _call("fake_whoami"), None, None, None, None)
        finally:
            await servers.stop()

        text = McpResults().result("fake_whoami", "c1", reply).llm_view()
        if not text.startswith("ivanov|dev|"):
            raise AssertionError(f"the body sees the user and the roles: {text}")

    async def test_evicted_port_lets_its_call_finish(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        """Личный порт вытеснен, пока через него идёт вызов: вызов
        доходит до конца, порт закрывается после него."""

        class Few(McpServers):
            KEEP: ClassVar[int] = 1

        auth = self._auth(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        endpoint = boba_mcp_stand.endpoint().model_copy(update={"auth": auth})
        servers = Few(McpServersConfig(servers={"boba": _config(endpoint)}))
        owner = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        other = McpCaller(login="petrov", roles=frozenset({"dev"}))
        try:
            port = await self._port(servers, owner, "boba")
            slow = _call("fake_progress", steps=6, pause=0.5)
            running = asyncio.create_task(port.call(OWN, slow, None, None, None, None))
            await asyncio.sleep(1.0)
            await self._port(servers, other, "boba")
            reply = await running
        finally:
            await servers.stop()

        assert isinstance(reply, mt.CallToolResult)
        assert not reply.is_error, reply

    async def test_requests_of_one_port_do_not_wait_for_a_long_call(
        self, boba_mcp_stand: BobaMcpStand, tmp_path: Path
    ) -> None:
        """Чат и агент делят подключение: пока идёт долгий вызов агента,
        запрос панели того же пользователя отвечает сразу, а не после
        вызова — токен входа выдаётся без замка провайдера OAuth."""
        servers = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        marker = tmp_path / "sleep.pid"
        try:
            port = await self._port(servers, caller, "boba")
            slow = _call("fake_sleep", seconds=4.0, marker=str(marker))
            running = asyncio.create_task(port.call(OWN, slow, None, None, None, None))
            async with asyncio.timeout(10):
                while not marker.exists():
                    await asyncio.sleep(0.05)

            started = asyncio.get_running_loop().time()
            await port.list_resources(OWN, None)
            waited = asyncio.get_running_loop().time() - started
            reply = await running
        finally:
            await servers.stop()

        assert waited < 2.0, waited
        assert isinstance(reply, mt.CallToolResult)
        assert not reply.is_error, reply

    async def test_cancelled_call_leaves_the_sign_in_usable(
        self, boba_mcp_stand: BobaMcpStand, tmp_path: Path
    ) -> None:
        """Идущий вызов отменён посреди ответа: вход провайдера OAuth
        освобождается в задаче вызова, и следующие запросы того же
        подключения идут как прежде."""
        servers = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        marker = tmp_path / "sleep.pid"
        try:
            port = await self._port(servers, caller, "boba")
            slow = _call("fake_sleep", seconds=30.0, marker=str(marker))
            running = asyncio.create_task(port.call(OWN, slow, None, None, None, None))
            async with asyncio.timeout(10):
                while not marker.exists():
                    await asyncio.sleep(0.05)

            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running

            listed = await port.list_resources(OWN, None)
            reply = await port.call(OWN, _call("fake_whoami"), None, None, None, None)
        finally:
            await servers.stop()

        assert isinstance(listed, mt.ListResourcesResult)
        assert isinstance(reply, mt.CallToolResult)
        assert not reply.is_error, reply

    async def test_private_lists_of_two_users_stay_apart(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        """Список инструментов зависит от ролей токена и приходит private: у
        каждого пользователя свой порт и свой кэш, описание — public."""
        servers = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        wide = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        weak = McpCaller(login="petrov", roles=frozenset({"weak"}))
        try:
            wide_port = await self._port(servers, wide, "boba")
            described = await wide_port.described(OWN)
            wide_tools = await wide_port.tools(OWN)
            weak_port = await self._port(servers, weak, "boba")
            weak_tools = await weak_port.tools(OWN)
        finally:
            await servers.stop()

        assert described.result.cache_scope == "public"
        assert described.result.ttl_ms > 0
        assert wide_tools.cache_scope == "private"
        assert wide_tools.ttl_ms > 0
        assert "fake_progress" in [tool.name for tool in wide_tools.tools]
        assert "fake_progress" not in [tool.name for tool in weak_tools.tools]

    async def test_endpoint_gives_the_tools_of_its_profile(
        self, boba_mcp_stand: BobaMcpStand
    ) -> None:
        servers = self._servers(boba_mcp_stand, self.SECRET, self.CLIENT_SECRET)
        caller = McpCaller(login="ivanov", roles=frozenset({"dev"}))
        try:
            await self._port(servers, caller, "boba")
            port = await servers.port("narrow", caller)
            names = sorted(tool.name for tool in (await port.tools(OWN)).tools)
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
            narrow = await servers.port("narrow", weak)
            with pytest.raises(McpClientError, match=r"'narrow'.*HTTP 403"):
                await narrow.described(OWN)
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
            port = await forged.port("boba", caller)
            with pytest.raises(McpClientError) as refused:
                await port.described(OWN)
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
            port = await foreign.port("boba", caller)
            with pytest.raises(McpClientError) as refused:
                await port.described(OWN)
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
        reply = await dag.call(
            OWN, _call("fake_echo", text="hi", repeat=2), None, None, None, None
        )
        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the call succeeds: {reply}")

        text = McpResults().result("fake_echo", "call-echo", reply).llm_view()
        if text != "hi hi|t0ken":
            raise AssertionError(f"the model reads the text of the result: {text}")

    async def test_nested_calls_name_the_workflow_offered_to_the_model(
        self, dag: McpToolServer
    ) -> None:
        nested = (await dag.described(OWN)).nested_calls()
        if nested is None:
            raise AssertionError("the server declares nested calls")

        if nested != CallDag().feature():
            raise AssertionError(f"the declared shape is the workflow's: {nested}")

        names: list[str] = []
        for tool in (await dag.tools(OWN)).tools:
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

        reply = await dag.call(
            OWN, _call("workflow", nodes=nodes), None, None, None, None
        )

        if not isinstance(reply, mt.CallToolResult) or reply.is_error:
            raise AssertionError(f"the workflow succeeds: {reply}")

        answered = NestedCallsOutput.model_validate(reply.structured_content)
        for node in answered.nodes:
            heading = reply.content[node.blocks[0] - 1]
            if heading != mt.TextContent(text=f"[{node.key}] {node.tool} — ok"):
                raise AssertionError(f"the heading of {node.key} leads its blocks")

        stubs = McpToolStubs()
        cards: dict[str, ToolCard] = {}
        for tool in (await dag.tools(OWN)).tools:
            cards[tool.name] = stubs.card("boba", tool)

        if cards["workflow"].output != NestedCallsOutput.model_json_schema():
            raise AssertionError(f"the card carries the output schema: {cards}")

    async def test_card_view_comes_from_the_schema_alone(
        self, dag: McpToolServer
    ) -> None:
        """Вид аргументов клиент выводит из схемы: у аргументов без типа
        содержимого особого вида нет, служебные параметры сервер не шлёт."""
        views = CallViews()
        echo = next(
            tool for tool in (await dag.tools(OWN)).tools if tool.name == "fake_echo"
        )
        if views.seen(echo.input_schema):
            raise AssertionError(f"plain arguments have no special view: {echo}")

        workflow = next(
            tool for tool in (await dag.tools(OWN)).tools if tool.name == "workflow"
        )
        if views.seen(workflow.input_schema):
            raise AssertionError(f"a built-in node has no special view: {workflow}")

    async def test_failure_names_its_cause(self, dag: McpToolServer) -> None:
        reply = await dag.call(
            OWN, _call("fake_echo", text="boom", repeat=1), None, None, None, None
        )
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
        declared = (await dag.described(OWN)).features().get(SealFeature.ID)
        if declared is None:
            raise AssertionError("the server declares the seal key")

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
            OWN, _call("fake_connection_host", connection=value), None, None, None, None
        )
        result = McpResults().result("fake_connection_host", "c1", reply)

        if not result.ok or result.llm_view() != "db.local|orders":
            raise AssertionError(f"the body got the sealed profile: {result}")

    async def test_plain_name_is_refused_by_the_server(
        self, dag: McpToolServer
    ) -> None:
        reply = await dag.call(
            OWN,
            _call("fake_connection_host", connection="main"),
            None,
            None,
            None,
            None,
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
                await server.described(OWN)
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
        service = HttpLocation(scheme="http", host=self.HOST, port=self.port, path="")
        auth = ProxyAuth(
            issuer=service,
            client_id=self.CLIENT,
            client_secret=self._client.secret,
            secret=self._proxy.secret,
        )

        return HttpEndpoint(service=service, path=f"/mcp/{self.PROFILE}", auth=auth)

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
        )
        try:
            for _ in range(600):
                try:
                    await server.described(OWN)
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
        """Клиент официального SDK без расширений: рукопожатие 2025-11-25
        видит те же инструменты всех плагинов, что server/discover 2026-07-28
        — SDK сервиса отдаёт старому клиенту tools/list, только если каждый
        инструмент проходит схему его редакции."""
        legacy = await self._listed(process, Edition.LEGACY)
        modern = await self._listed(process, Edition.MODERN)

        assert legacy, legacy
        assert legacy == modern

    async def _listed(self, process: BobaMcpService, edition: Edition) -> list[str]:
        """Имена инструментов сервиса сессией SDK, согласованной на edition."""
        endpoint = process.endpoint().on_behalf_of(self.CALLER)
        auth = endpoint.auth.httpx_auth(endpoint.url())
        names: list[str] = []
        cursor: str | None = None
        async with (
            create_mcp_http_client(auth=auth) as http,
            streamable_http_client(endpoint.url(), http_client=http) as streams,
            ClientSession(*streams) as session,
        ):
            if edition is Edition.LEGACY:
                await session.initialize()
            else:
                await session.discover()

            assert session.protocol_version == edition.value
            while True:
                params = mt.PaginatedRequestParams(cursor=cursor)
                page = await session.list_tools(params=params)
                names.extend(tool.name for tool in page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break

        return sorted(names)

    async def test_plugin_tool_runs_in_the_service_process(
        self, service: McpToolServer
    ) -> None:
        names = [tool.name for tool in (await service.tools(OWN)).tools]
        if "bash" not in names or "workflow" not in names:
            raise AssertionError(f"the service offers its plugin tools: {names}")

        reply = await service.call(
            OWN, _call("bash", command="echo from the service"), None, None, None, None
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
            OWN,
            _call("bash", command="echo kept > note.txt", scope_id=first),
            None,
            None,
            None,
            None,
        )
        again = await service.call(
            OWN,
            _call("bash", command="cat note.txt", scope_id=first),
            None,
            None,
            None,
            None,
        )
        apart = await service.call(
            OWN,
            _call("bash", command="cat note.txt", scope_id=other),
            None,
            None,
            None,
            None,
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
        "area",
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
        "pid",
        "progress",
        "refresh_orders",
        "region",
        "retire_modern",
        "retitle",
        "shout",
        "show",
        "sleep_for",
        "toggle_extra",
        "toggle_prompt",
    ]
    LEGACY_TOOLS: ClassVar[list[str]] = [
        "add",
        "answers",
        "archive_orders",
        "ask",
        "blocks",
        "broken",
        "client",
        "forget",
        "large",
        "load_rows",
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
            limits=McpServerLimits(
                connect_timeout_sec=20.0, call_timeout_sec=call_timeout_sec
            ),
        )
        server = McpToolServer("standard", config)
        await server.described(OWN)

        return server

    async def test_large_result_arrives_whole(self, served: McpToolServer) -> None:
        reply = await served.call(
            OWN, _call("large", size=200_000), None, None, None, None
        )
        text = McpResults().result("large", "c1", reply).llm_view()

        assert len(text) == 200_000

    async def test_delay_beyond_the_call_timeout_is_a_client_error(
        self, edition_endpoint: HttpEndpoint | StdioCommand
    ) -> None:
        server = await self._open(edition_endpoint, call_timeout_sec=1.0)
        try:
            with pytest.raises(McpClientError, match="'sleep_for' got no answer"):
                await server.call(
                    OWN, _call("sleep_for", seconds=5.0), None, None, None, None
                )
        finally:
            await server.close()

    async def test_server_dying_mid_call_is_a_client_error(self) -> None:
        endpoint = StdioCommand(command=sys.executable, args=(str(SERVER), "stdio"))
        server = await self._open(endpoint, call_timeout_sec=10.0)
        try:
            with pytest.raises(McpClientError, match="'die' got no answer"):
                await server.call(OWN, _call("die"), None, None, None, None)
        finally:
            await server.close()

    async def test_changed_tool_list_is_read_again(self, served: McpToolServer) -> None:
        """Сервер сменил список: у современного без подсказок кэша ответ
        свеж 0 и перечитывается при каждом обращении, у прежнего
        notifications/tools/list_changed его сессии сбрасывает кэш. Новый
        инструмент сразу в списке порта, вызов уходит серверу как есть."""
        before = sorted(tool.name for tool in (await served.tools(OWN)).tools)
        toggled = await served.call(OWN, _call("toggle_extra"), None, None, None, None)
        fresh: list[str] = []
        async with asyncio.timeout(10):
            while "extra" not in fresh:
                listed = await served.tools(OWN)
                fresh = sorted(tool.name for tool in listed.tools)
                await asyncio.sleep(0.05)

        answered = await served.call(OWN, _call("extra"), None, None, None, None)

        assert before == await _tools_of(served)
        assert McpResults().result("toggle_extra", "c1", toggled).llm_view() == (
            "extra on: True"
        )
        assert fresh == sorted([*before, "extra"])
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
        discovered = (await stdio.described(OWN)).result
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
        resources = await served.list_resources(OWN, None)
        templates = await served.list_resource_templates(OWN, None)

        uris = sorted(str(resource.uri) for resource in resources.resources)
        patterns = sorted(item.uri_template for item in templates.resource_templates)

        assert StandardServer.NOTES in uris
        assert StandardServer.LOGO in uris
        assert "standard://items/{item}" in patterns

    async def test_resources_are_read_as_text_and_blob(
        self, served: McpToolServer
    ) -> None:
        notes = await served.read_resource(OWN, self._read(StandardServer.NOTES), None)
        logo = await served.read_resource(OWN, self._read(StandardServer.LOGO), None)
        item = await served.read_resource(
            OWN, self._read("standard://items/apple"), None
        )

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
        read = await stdio.read_resource(
            OWN, self._read("standard://ask/weather"), None
        )
        asking = await stdio.get_prompt(
            OWN, mt.GetPromptRequestParams(name="ask_prompt"), None
        )

        assert isinstance(read, mt.InputRequiredResult)
        assert read.request_state is not None
        assert isinstance(asking, mt.InputRequiredResult)

    async def test_missing_resource_is_not_found(self, served: McpToolServer) -> None:
        """«Ресурса нет» — -32602 в 2026-07-28 и -32002 в прежних редакциях."""
        with pytest.raises(MCPError) as missing:
            await served.read_resource(OWN, self._read("standard://nothing"), None)

        assert missing.value.error.code in McpToolServer.NOT_FOUND

    async def test_prompts_are_listed_and_given_with_both_roles(
        self, served: McpToolServer
    ) -> None:
        listed = await served.list_prompts(OWN, None)
        params = mt.GetPromptRequestParams(
            name="greet", arguments={"name": "Bob", "style": "poem"}
        )
        given = await served.get_prompt(OWN, params, None)

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
            OWN,
            mt.CompleteRequestParams(
                ref=mt.PromptReference(name="greet"),
                argument=mt.CompletionArgument(name="style", value="p"),
            ),
        )
        items = await served.complete(
            OWN,
            mt.CompleteRequestParams(
                ref=mt.ResourceTemplateReference(uri="standard://items/{item}"),
                argument=mt.CompletionArgument(name="item", value="ap"),
            ),
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
        async with (
            asyncio.timeout(30),
            stdio.listen(OWN, notifications) as subscription,
        ):
            honored = subscription.honored
            await stdio.call(OWN, _call("toggle_extra"), None, None, None, None)
            await stdio.call(OWN, _call("toggle_prompt"), None, None, None, None)
            await stdio.call(OWN, _call("grow_resources"), None, None, None, None)
            await stdio.call(OWN, _call("bump"), None, None, None, None)
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

    async def test_identity_of_each_request_reaches_one_server_process(
        self,
    ) -> None:
        """Личность — свойство запроса: запросы агента и чата идут одним
        процессом stdio-сервера, и сервер видит у каждого его clientInfo и
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
            port = await servers.port("standard", CALLER)
            seen = await port.call(agent, _call("client"), None, None, None, None)
            chat = await port.call(OWN, _call("client"), None, None, None, None)
            by_agent = await port.call(agent, _call("pid"), None, None, None, None)
            by_chat = await port.call(OWN, _call("pid"), None, None, None, None)
        finally:
            await servers.stop()

        assert isinstance(seen, mt.CallToolResult)
        assert isinstance(chat, mt.CallToolResult)
        pids = McpResults()
        assert pids.result("pid", "p1", by_agent).llm_view() == (
            pids.result("pid", "p2", by_chat).llm_view()
        )
        declared = json.loads(McpResults().result("client", "c1", seen).llm_view())
        mine = json.loads(McpResults().result("client", "c3", chat).llm_view())
        assert declared["info"] == {"name": "boba-cli", "version": "9.9"}
        assert declared["capabilities"]["elicitation"] == {"form": {}}
        assert sorted(declared["capabilities"]["extensions"]) == sorted(
            [JournalFeature.ID, SealFeature.ID, ScopeFeature.ID]
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
    без смены списков и подписки, потоков subscriptions/listen нет, из
    запросов сервера посреди вызова клиент объявляет только вопросы
    пользователю и отдаёт их вызову, конверт личности ставит сама сессия, а
    забытую сервером сессию клиент открывает заново. То же
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
        server = McpToolServer("legacy", _config(endpoint))
        await server.described(OWN)
        try:
            yield server
        finally:
            await server.close()

    async def test_server_is_described_by_its_initialize_without_streams(
        self, legacy: McpToolServer
    ) -> None:
        discovered = (await legacy.described(OWN)).result
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

    @pytest.mark.parametrize(
        ("server", "version"),
        [
            (AppServer.LEGACY, Edition.LEGACY.value),
            (AppServer.OLDER, "2025-06-18"),
            (AppServer.DUAL, "2025-06-18"),
        ],
        ids=["handshake", "older-only", "dual-of-a-later-edition"],
    )
    async def test_highest_common_version_is_negotiated(
        self, apps_slot: AppsSlot, server: AppServer, version: str
    ) -> None:
        """Версия — наибольшая общая: на initialize с самой новой версией
        рукопожатия сервер отвечает своей самой новой, а сервер более поздней
        редакции называет свои версии отказом -32022 на server/discover, и
        клиент переходит на рукопожатие."""
        endpoint = _legacy(apps_slot, server)
        port = McpToolServer(server.name.lower(), _config(endpoint))
        await port.described(OWN)
        try:
            versions = (await port.described(OWN)).result.supported_versions
            reply = await port.call(OWN, _call("add", a=2, b=3), None, None, None, None)
        finally:
            await port.close()

        assert versions == [version]
        assert McpResults().result("add", "c1", reply).llm_view() == "5"

    async def test_server_of_a_later_edition_only_is_refused_with_the_versions(
        self, apps_slot: AppsSlot
    ) -> None:
        """Общей версии нет: сервер знает только 2027-01-01 и рукопожатия не
        ведёт — подключение отказано, текст называет версии обеих сторон."""
        endpoint = _legacy(apps_slot, AppServer.AHEAD)
        server = McpToolServer("ahead", _config(endpoint))
        try:
            with pytest.raises(McpClientError) as refused:
                await server.described(OWN)
        finally:
            await server.close()

        text = str(refused.value)
        assert "'ahead'" in text, text
        assert "MCP error -32022" in text, text
        assert "2027-01-01" in text, text
        assert Edition.MODERN.value in text, text
        assert "2025-06-18" in text, text

    async def test_listen_is_refused_with_the_reason(
        self, legacy: McpToolServer
    ) -> None:
        notifications = mt.SubscriptionFilter(tools_list_changed=True)

        with pytest.raises(McpClientError, match="no listen streams"):
            async with legacy.listen(OWN, notifications):
                pass

    AGENT: ClassVar[McpClientIdentity] = McpClientIdentity(
        info=mt.Implementation(name="boba-cli", version="9.9"),
        capabilities=mt.ClientCapabilities(
            elicitation=mt.ElicitationCapability(form=mt.FormElicitationCapability()),
            sampling=mt.SamplingCapability(),
            roots=mt.RootsCapability(list_changed=True),
        ),
    )
    """Агент с вопросами, сэмплингом и корнями."""

    async def test_requests_of_the_server_and_the_envelope_stay_with_the_client(
        self, apps_slot: AppsSlot
    ) -> None:
        """Агенту с вопросами, сэмплингом и корнями сервер рукопожатия слышит
        только вопросы; расширения чата остаются. Вопрос вне вызова, который
        их принимает, сервер получает отказом с причиной. Ключи конверта
        агента из _meta запроса до сервера не доходят, свои ключи запроса
        доходят."""
        endpoint = _legacy(apps_slot, AppServer.LEGACY)
        config = McpServersConfig(servers={"legacy": _config(endpoint)})
        meta = mt.RequestParamsMeta()
        for key, value in self.ENVELOPE.items():
            meta[key] = value

        meta[self.TRACE] = "t1"
        traced = mt.CallToolRequestParams(name="client", arguments={}, _meta=meta)
        servers = McpServers(config)
        try:
            port = await servers.port("legacy", CALLER)
            seen = await port.call(self.AGENT, traced, None, None, None, None)
            with pytest.raises(MCPError) as unasked:
                await port.call(
                    self.AGENT, _call("ask", refuse=False), None, None, None, None
                )
        finally:
            await servers.stop()

        declared = json.loads(McpResults().result("client", "c1", seen).llm_view())

        assert declared["info"] == {"name": "boba-cli", "version": "9.9"}
        assert sorted(declared["capabilities"]) == ["elicitation", "extensions"]
        assert declared["capabilities"]["elicitation"] == {"form": {}}
        assert sorted(declared["capabilities"]["extensions"]) == sorted(
            [JournalFeature.ID, SealFeature.ID, ScopeFeature.ID]
        )
        assert sorted(declared["meta"]) == sorted(["progressToken", self.TRACE])
        assert "expected one such call open on the session, got 0" in str(unasked.value)

    async def test_questions_of_the_call_go_to_its_asker(
        self, apps_slot: AppsSlot
    ) -> None:
        """Два вопроса сервера посреди одного вызова получает приёмник
        вопросов этого вызова, ответы доходят до сервера, итог — по ним."""
        endpoint = _legacy(apps_slot, AppServer.LEGACY)
        server = McpToolServer("legacy", _config(endpoint))
        heard: list[mt.ElicitRequestParams] = []
        answers = [
            mt.ElicitResult(action="accept", content={"table": "tmp.orders_copy"}),
            mt.ElicitResult(action="accept", content={"go": True}),
        ]

        async def asked(params: mt.ElicitRequestParams) -> mt.ElicitResult:
            heard.append(params)
            return answers[len(heard) - 1]

        loading = _call("load_rows", rows=7, confirm=True)
        try:
            reply = await server.call(self.AGENT, loading, None, None, None, asked)
        finally:
            await server.close()

        assert _text("load_rows", reply) == "loaded 7 rows into tmp.orders_copy"
        assert len(heard) == 2
        first, second = heard
        assert isinstance(first, mt.ElicitRequestFormParams), first
        assert first.message.startswith("load_rows needs the target table")
        assert sorted(first.requested_schema["properties"]) == ["table"]
        assert isinstance(second, mt.ElicitRequestFormParams), second
        assert second.message == (
            "load_rows asks to start the copy of 7 rows into tmp.orders_copy."
        )

    async def test_question_with_two_asking_calls_is_refused(
        self, apps_slot: AppsSlot
    ) -> None:
        """Вопрос сервера, когда на сессии открыты два вызова с приёмниками
        вопросов, не уходит никому: запрос сервера 2025-11-25 не называет
        свой вызов, сервер получает отказ с причиной."""
        endpoint = _legacy(apps_slot, AppServer.LEGACY)
        server = McpToolServer("legacy", _config(endpoint))
        heard: list[mt.ElicitRequestParams] = []

        async def asked(params: mt.ElicitRequestParams) -> mt.ElicitResult:
            heard.append(params)
            return mt.ElicitResult(action="decline")

        sleeping = _call("sleep_for", seconds=2)
        loading = _call("load_rows", rows=8, confirm=False)
        try:
            await server.described(self.AGENT)
            slept = asyncio.ensure_future(
                server.call(self.AGENT, sleeping, None, None, None, asked)
            )
            await asyncio.sleep(0.3)
            with pytest.raises(MCPError) as refused:
                await server.call(self.AGENT, loading, None, None, None, asked)

            await slept
        finally:
            await server.close()

        assert heard == []
        assert "expected one such call open on the session, got 2" in str(refused.value)

    async def test_forgotten_session_fails_the_call_and_the_next_call_opens_one(
        self, legacy: McpToolServer
    ) -> None:
        """Сервер забыл сессию (404 на запрос с Mcp-Session-Id): запрос
        кончается ошибкой с причиной, следующий открывает новую сессию."""
        await legacy.call(OWN, _call("forget"), None, None, None, None)
        await asyncio.sleep(0.5)

        with pytest.raises(McpClientError) as refused:
            await legacy.call(OWN, _call("add", a=1, b=2), None, None, None, None)

        reply = await legacy.call(OWN, _call("add", a=2, b=2), None, None, None, None)

        assert "forgot the session" in str(refused.value)
        assert "HTTP 404 with Mcp-Session-Id" in str(refused.value)
        assert McpResults().result("add", "c1", reply).llm_view() == "4"

    async def test_server_over_stdio_falls_back_to_initialize(
        self, apps_slot: AppsSlot
    ) -> None:
        command, *args = apps_slot.stdio()
        endpoint = StdioCommand(command=command, args=tuple(args))
        server = McpToolServer("legacy", _config(endpoint))
        await server.described(OWN)
        try:
            versions = (await server.described(OWN)).result.supported_versions
            reply = await server.call(
                OWN, _call("shout", text="over stdio"), None, None, None, None
            )
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
        server = McpToolServer("example", config)
        await server.described(OWN)
        try:
            versions = (await server.described(OWN)).result.supported_versions
            listed = next(
                item for item in (await server.tools(OWN)).tools if item.name == tool
            )
            meta = listed.meta
            if meta is None:
                raise AssertionError(f"the tool names its app: {listed}")

            uri = meta["ui"]["resourceUri"]
            read = await server.read_resource(
                OWN, mt.ReadResourceRequestParams(uri=uri), None
            )
            reply = await server.call(
                OWN, _call(tool, **arguments), None, None, None, None
            )
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


def _cached(ttl_ms: int) -> StdioCommand:
    """Обычный сервер по stdio с подсказками кэша описания и списка."""
    return StdioCommand(
        command=sys.executable,
        args=(str(SERVER), "stdio", "--ttl-ms", str(ttl_ms)),
    )


def _text(name: str, reply: mt.CallToolResult | mt.InputRequiredResult) -> str:
    return McpResults().result(name, "c1", reply).llm_view()


class TestCachedAnswers:
    """Описание сервера и список инструментов порт держит кэшем SDK по
    правилам caching: свежие ttlMs, перечитываются после срока, уведомление
    о смене списка сбрасывает кэш раньше срока, срок отдаётся оставшимся."""

    AGENT: ClassVar[McpClientIdentity] = McpClientIdentity(
        info=mt.Implementation(name="boba-cli", version="9.9"),
        capabilities=mt.ClientCapabilities(),
    )

    async def test_expired_description_is_read_again(self) -> None:
        """Сервер сменил инструкции: пока описание свежо, порт отдаёт
        прежнее, а после ttlMs перечитывает server/discover."""
        server = McpToolServer("standard", _config(_cached(1000)))
        try:
            first = await server.described(OWN)
            await server.call(
                OWN, _call("retitle", text="renamed"), None, None, None, None
            )
            fresh = await server.described(OWN)
            await asyncio.sleep(1.5)
            again = await server.described(OWN)
        finally:
            await server.close()

        assert first.result.instructions == StandardServer.INSTRUCTIONS
        assert first.result.cache_scope == "public"
        assert fresh.result.instructions == StandardServer.INSTRUCTIONS
        assert again.result.instructions == "renamed"

    async def test_served_answer_carries_the_time_it_has_left(self) -> None:
        server = McpToolServer("standard", _config(_cached(60_000)))
        try:
            first = await server.tools(OWN)
            await asyncio.sleep(1.2)
            later = await server.tools(OWN)
        finally:
            await server.close()

        assert first.cache_scope == "private"
        assert 0 < later.ttl_ms <= 60_000 - 1000, later.ttl_ms

    async def test_expired_tool_list_is_read_again(self) -> None:
        """Сервер молча добавил инструмент: до срока список прежний, после
        срока — новый."""
        server = McpToolServer("standard", _config(_cached(1000)))
        try:
            before = await server.tools(OWN)
            await server.call(OWN, _call("toggle_extra"), None, None, None, None)
            stale = await server.tools(OWN)
            await asyncio.sleep(1.5)
            fresh = await server.tools(OWN)
        finally:
            await server.close()

        names = [tool.name for tool in fresh.tools]
        assert [tool.name for tool in stale.tools] == [
            tool.name for tool in before.tools
        ]
        assert "extra" in names, names

    async def test_list_change_of_the_listen_stream_resets_the_fresh_list(
        self,
    ) -> None:
        """Список свеж ещё минуту, но событие смены списка потока
        subscriptions/listen сбрасывает его раньше, чем событие получит
        читатель: перечитанный список уже новый."""
        server = McpToolServer("standard", _config(_cached(60_000)))
        notifications = mt.SubscriptionFilter(tools_list_changed=True)
        try:
            await server.tools(OWN)
            async with (
                asyncio.timeout(30),
                server.listen(OWN, notifications) as subscription,
            ):
                await server.call(OWN, _call("toggle_extra"), None, None, None, None)
                async for _event in subscription:
                    break

            fresh = await server.tools(OWN)
        finally:
            await server.close()

        assert "extra" in [tool.name for tool in fresh.tools]

    async def test_lists_of_two_identities_are_cached_apart(self) -> None:
        """Ответ зависит от возможностей запроса: кэш у каждой личности свой,
        а процесс сервера у них один."""
        server = McpToolServer("standard", _config(_cached(60_000)))
        try:
            await server.tools(OWN)
            await server.call(OWN, _call("toggle_extra"), None, None, None, None)
            own = await server.tools(OWN)
            agent = await server.tools(self.AGENT)
            by_agent = await server.call(
                self.AGENT, _call("pid"), None, None, None, None
            )
            by_chat = await server.call(OWN, _call("pid"), None, None, None, None)
        finally:
            await server.close()

        assert "extra" not in [tool.name for tool in own.tools]
        assert "extra" in [tool.name for tool in agent.tools]
        assert _text("pid", by_agent) == _text("pid", by_chat)


class TestRenegotiation:
    """Подключение заново и пересогласование: после переподключения
    заголовки x-mcp-header идут по перечитанному списку, сервер, который
    перестал говорить на 2026-07-28, согласуется заново, а отказ HTTP
    одного запроса не достаётся соседнему."""

    async def test_param_header_survives_a_reconnection(
        self, http: McpToolServer
    ) -> None:
        """Подключение закрыто — кэша и схем сессии нет: сервер отвергает
        вызов HeaderMismatch, порт перечитывает список и повторяет вызов."""
        await http.close()

        reply = await http.call(
            OWN, _call("region", region="eu-west"), None, None, None, None
        )

        assert _text("region", reply) == "region eu-west"

    async def test_card_of_the_call_gives_its_header_at_once(
        self, http: McpToolServer
    ) -> None:
        listed = await http.tools(OWN)
        card = next(tool for tool in listed.tools if tool.name == "region")
        await http.close()

        reply = await http.call(
            OWN, _call("region", region="eu"), card, None, None, None
        )

        assert _text("region", reply) == "region eu"

    @pytest.mark.parametrize("mode", ["version", "session"])
    async def test_server_that_left_the_modern_edition_is_negotiated_again(
        self, http: McpToolServer, mode: str
    ) -> None:
        """Сервер перестал говорить на 2026-07-28 — отказ -32022 либо 400
        сервера прежней редакции: порт согласует версию заново
        рукопожатием и повторяет запрос."""
        await http.call(OWN, _call("retire_modern", mode=mode), None, None, None, None)

        reply = await http.call(OWN, _call("add", a=2, b=3), None, None, None, None)
        described = await http.described(OWN)

        assert _text("add", reply) == "5"
        assert described.result.supported_versions == [Edition.LEGACY.value]

    @pytest.mark.parametrize("http_port", [("--retired", "version")], indirect=True)
    async def test_refusal_of_one_request_stays_with_it(
        self, http: McpToolServer
    ) -> None:
        """Запрос сессии прежней редакции получил 404 — сессия забыта и
        закрыта; соседний запрос, оборванный закрытием, называет обрыв, а
        не забытую сессию."""
        slow = asyncio.create_task(
            http.call(OWN, _call("sleep_for", seconds=3.0), None, None, None, None)
        )
        await asyncio.sleep(0.5)
        with pytest.raises(McpClientError) as forgot:
            await http.call(OWN, _call("lost"), None, None, None, None)

        with pytest.raises(McpClientError) as cut:
            await slow

        reply = await http.call(OWN, _call("add", a=1, b=1), None, None, None, None)

        assert "forgot the session" in str(forgot.value)
        assert "forgot the session" not in str(cut.value), str(cut.value)
        assert "got no answer" in str(cut.value), str(cut.value)
        assert _text("add", reply) == "2"
