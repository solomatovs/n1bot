"""Ретранслятор mcp/message без агента: запросы MCP по serverId регистрации
исполняют настоящие серверы — обычный сервер на SDK mcp по stdio, сервер
редакции 2025-11-25 на TypeScript SDK 1.x в контейнере boba-testapps и стенд
boba-mcp со входом proxy. Проверяются строки таблицы ошибок MCP-over-ACP,
встроенные методы SDK, видимость инструментов для модели и приложения MCP
Apps, ход вызова и запечатывание соединений пользователя."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from importlib.resources import files
from typing import Any, ClassVar
from uuid import uuid4

import mcp_types as mt
import pytest
from acp.exceptions import RequestError
from aid_stand import (
    HolderSurface,
    NoConnectionStore,
    RelayStand,
    ServiceProcess,
    TabSession,
)
from chainlit.context import context_var
from mcp.server.apps import APP_MIME_TYPE
from mcp.shared.exceptions import MCPError

from boba.aid.agent.relay import ChatTools, McpRegistration, McpRegistrations, McpRelay
from boba.aid.canvas.tools import CanvasOpener
from boba.aid.rendering.tool import AppResource, ToolSource
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.connection_broker.sealing import McpServerInfo
from boba.connection_broker.store import ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import ConnectionOffer, SealedParamSchema, SealFeature
from boba.connections.secrets import SecretCipher
from boba.connections.stored import GrantTarget
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import DelegatedTicket, NoUserCredential, Scope
from boba.identity.errors import AuthenticationError, RefusalError
from boba.mcp_client.client import (
    HttpEndpoint,
    HttpLocation,
    McpServerApps,
    McpServerConfig,
    McpServerLimits,
    McpServers,
    McpServersConfig,
    StdioCommand,
)
from boba.messaging import Envelope, JournalOpened
from boba.stand.apps_slot import AppServer, AppsSlot
from boba.stand.refs import StandRefs
from boba.stand.signin import SignInStand
from boba.stand.toolstand import ProbeTools
from boba.stand_core.context import CallStand
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.wire import ResultWire
from boba.toolrun.call_id import CallFields
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.hosted import ToolHosting
from boba.toolrun.stream_calls import WorkflowCall, WorkflowTool
from boba.transport.acp.protocol import (
    AcpErrorCode,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
    McpVersion,
    ToolSourceRequest,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def tab(auth_token: str) -> TabSession:
    """Вкладка пользователя ivanov, вошедшего паролем с ролью, которой
    инструменты chat стенда ретранслятора видны (RelayStand.ROLE)."""
    session = TabSession()
    session.password_sign_in(frozenset({RelayStand.ROLE}))

    return session


@pytest.fixture
def relay_stand(call_stand: CallStand) -> RelayStand:
    """Ретранслятор стенда без соединений пользователя."""
    credentials = KerberosCredentialSource(None, NoRefresh())

    return RelayStand(NoConnectionStore(), lambda: credentials, call_stand.contexts)


class TestMcpRelay:
    """Ретранслятор над обычным сервером: таблица ошибок, встроенные
    методы SDK, видимость инструментов, ход вызова и отказ запечатывания
    серверу, который не принимает соединений. Сервер редакции 2025-11-25
    агент видит обычным сервером 2026-07-28."""

    ENVELOPE: ClassVar[dict[str, Any]] = {
        mt.PROTOCOL_VERSION_META_KEY: McpVersion.V2026_07_28.value,
        mt.CLIENT_CAPABILITIES_META_KEY: {},
    }

    @pytest.fixture
    async def relay(
        self, relay_stand: RelayStand, standard: McpServerConfig
    ) -> AsyncIterator[McpRelay]:
        servers = McpServers(McpServersConfig(servers={"standard": standard}))
        try:
            yield relay_stand.relay(servers)
        finally:
            await servers.stop()

    ASKING: ClassVar[dict[str, Any]] = {
        mt.CLIENT_CAPABILITIES_META_KEY: {"elicitation": {"form": {}}},
    }
    """_meta агента, который задаёт вопросы пользователю формами."""

    @pytest.fixture
    async def legacy_relay(
        self, relay_stand: RelayStand, apps_slot: AppsSlot
    ) -> AsyncIterator[McpRelay]:
        """Ретранслятор над своим сервером редакции 2025-11-25."""
        servers = self._legacy(apps_slot, 60.0)
        try:
            yield relay_stand.relay(servers)
        finally:
            await servers.stop()

    @pytest.fixture
    async def brief_relay(
        self, relay_stand: RelayStand, apps_slot: AppsSlot
    ) -> AsyncIterator[McpRelay]:
        """Ретранслятор над сервером редакции 2025-11-25 со сроком вызова 2 с."""
        servers = self._legacy(apps_slot, 2.0)
        try:
            yield relay_stand.relay(servers)
        finally:
            await servers.stop()

    def _legacy(self, apps_slot: AppsSlot, call_timeout_sec: float) -> McpServers:
        endpoint = HttpEndpoint(
            service=HttpLocation(
                scheme="http", host=apps_slot.host, port=AppServer.LEGACY.value, path=""
            ),
            path=apps_slot.PATH,
        )
        legacy = McpServerConfig(
            endpoint=endpoint,
            limits=McpServerLimits(
                connect_timeout_sec=20.0, call_timeout_sec=call_timeout_sec
            ),
        )

        return McpServers(McpServersConfig(servers={"legacy": legacy}))

    def _loading(self, rows: int, confirm: bool) -> dict[str, Any]:
        return {
            "name": "load_rows",
            "arguments": {"rows": rows, "confirm": confirm},
            "_meta": self.ASKING,
        }

    def _retried(
        self,
        call: dict[str, Any],
        asked: mt.InputRequiredResult,
        answer: dict[str, Any],
    ) -> dict[str, Any]:
        """Повтор вызова call с ответом answer на единственный вопрос asked."""
        assert asked.input_requests is not None, asked
        [key] = asked.input_requests

        return {
            **call,
            "inputResponses": {key: answer},
            "requestState": asked.request_state,
        }

    async def _asked(
        self, relay: McpRelay, server_id: str, call: dict[str, Any]
    ) -> mt.InputRequiredResult:
        outcome, _ = await self._answer(
            relay, self._message(server_id, "tools/call", call)
        )
        assert isinstance(outcome, McpResultOutcome), outcome

        return mt.InputRequiredResult.model_validate(outcome.result)

    async def _answers(
        self, relay: McpRelay, server_id: str, rows: int
    ) -> list[dict[str, Any]]:
        """Ответы, которые сервер получил на вопросы load_rows с rows строк."""
        call = {"name": "answers", "arguments": {"rows": rows}}
        outcome, _ = await self._answer(
            relay, self._message(server_id, "tools/call", call)
        )
        assert isinstance(outcome, McpResultOutcome), outcome
        block = mt.CallToolResult.model_validate(outcome.result).content[0]
        assert isinstance(block, mt.TextContent), block

        return json.loads(block.text)

    def _message(
        self, server_id: str, method: str, params: dict[str, Any]
    ) -> McpMessage:
        sent = dict(params)
        sent["_meta"] = {**self.ENVELOPE, **params.get("_meta", {})}

        return McpMessage(
            server_id=server_id,
            request_id=f"mcp:{uuid4().hex}",
            method=method,
            params=sent,
        )

    async def _answer(
        self, relay: McpRelay, message: McpMessage
    ) -> tuple[McpOutcome, list[McpMessage]]:
        heard: asyncio.Queue[McpMessage] = asyncio.Queue()
        outcome = await relay.answer(message, heard.put)
        notes: list[McpMessage] = []
        while not heard.empty():
            notes.append(heard.get_nowait())

        return outcome, notes

    def _server_id(self, stand: RelayStand, tab: TabSession, server: str) -> str:
        registration = tab.registration(uuid4(), SignInStand.PROFILE)

        return stand.registrations.register(registration, [server])[0].server_id

    async def test_unknown_server_id_is_an_external_error(
        self, relay: McpRelay
    ) -> None:
        message = self._message("ghost", "tools/list", {})

        with pytest.raises(RequestError) as refused:
            await relay.answer(message, asyncio.Queue[McpMessage]().put)

        assert refused.value.code == AcpErrorCode.UNKNOWN_SERVER
        assert "'ghost'" in str(refused.value)

    async def test_request_id_in_work_is_an_external_error(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab, "standard")
        slow = self._message(
            server_id,
            "tools/call",
            {"name": "sleep_for", "arguments": {"seconds": 0.5}},
        )
        running = asyncio.ensure_future(self._answer(relay, slow))
        await asyncio.sleep(0.2)

        with pytest.raises(RequestError) as refused:
            await relay.answer(slow, asyncio.Queue[McpMessage]().put)

        outcome, _notes = await running
        assert refused.value.code == AcpErrorCode.INVALID_PARAMS
        assert "already in work" in str(refused.value)
        assert isinstance(outcome, McpResultOutcome), outcome

    async def test_envelope_and_version_are_inner_errors(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab, "standard")
        bare = McpMessage(
            server_id=server_id, request_id="mcp:bare", method="tools/list", params={}
        )
        old = self._message(
            server_id,
            "tools/list",
            {"_meta": {mt.PROTOCOL_VERSION_META_KEY: "2025-11-25"}},
        )

        unwrapped, _ = await self._answer(relay, bare)
        outdated, _ = await self._answer(relay, old)

        assert isinstance(unwrapped, McpErrorOutcome), unwrapped
        assert unwrapped.error.code == mt.INVALID_PARAMS
        assert isinstance(outdated, McpErrorOutcome), outdated
        assert outdated.error.code == mt.UNSUPPORTED_PROTOCOL_VERSION

    async def test_discover_is_the_server_behind_the_registration(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """server/discover отдаёт возможности, инструкции и serverInfo
        сервера за регистрацией; ping и методы вне редакции SDK отвечает
        -32601."""
        server_id = self._server_id(relay_stand, tab, "standard")

        discovered, _ = await self._answer(
            relay, self._message(server_id, "server/discover", {})
        )
        pinged, _ = await self._answer(relay, self._message(server_id, "ping", {}))
        unknown, _ = await self._answer(
            relay, self._message(server_id, "logging/setLevel", {"level": "info"})
        )

        assert isinstance(discovered, McpResultOutcome), discovered
        result = mt.DiscoverResult.model_validate(discovered.result)
        assert McpVersion.V2026_07_28.value in result.supported_versions
        assert result.instructions, result
        assert result.meta is not None
        assert result.meta[mt.SERVER_INFO_META_KEY]["name"] == "standard"
        assert result.capabilities.prompts == mt.PromptsCapability(list_changed=True)
        assert result.capabilities.logging is None
        # ping в редакции 2026-07-28 не определён: SDK отвечает -32601
        assert isinstance(pinged, McpErrorOutcome), pinged
        assert pinged.error.code == mt.METHOD_NOT_FOUND
        assert isinstance(unknown, McpErrorOutcome), unknown
        assert unknown.error.code == mt.METHOD_NOT_FOUND

    async def test_initialize_of_an_earlier_edition_is_refused_with_the_versions(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """Рукопожатия у ретранслятора нет: initialize — отказ -32022 с
        перечнем его версий, а не ошибка конверта."""
        server_id = self._server_id(relay_stand, tab, "standard")
        handshake = McpMessage(
            server_id=server_id,
            request_id="mcp:init",
            method="initialize",
            params={
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "old-agent", "version": "1"},
            },
        )

        outcome, _ = await self._answer(relay, handshake)

        assert isinstance(outcome, McpErrorOutcome), outcome
        assert outcome.error.code == mt.UNSUPPORTED_PROTOCOL_VERSION
        assert outcome.error.data == {
            "supported": [McpVersion.V2026_07_28.value],
            "requested": "2025-11-25",
        }
        assert "initialize" in outcome.error.message

    @pytest.mark.integration
    async def test_server_of_the_handshake_era_is_discovered_as_a_modern_one(
        self, legacy_relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """server/discover агенту — редакции 2026-07-28 с инструкциями и
        serverInfo из initialize сервера и без смены списков: потоков
        подписки у такого сервера нет."""
        server_id = self._server_id(relay_stand, tab, "legacy")

        discovered, _ = await self._answer(
            legacy_relay, self._message(server_id, "server/discover", {})
        )

        assert isinstance(discovered, McpResultOutcome), discovered
        result = mt.DiscoverResult.model_validate(discovered.result)
        assert result.supported_versions == [McpVersion.V2026_07_28.value]
        assert result.instructions, result
        assert result.meta is not None
        assert result.meta[mt.SERVER_INFO_META_KEY]["name"] == "legacy-standard"
        assert result.capabilities.tools == mt.ToolsCapability(list_changed=False)
        assert result.capabilities.resources == mt.ResourcesCapability(
            subscribe=False, list_changed=False
        )

    @pytest.mark.integration
    async def test_tools_of_the_handshake_era_hide_the_app_and_task_only_ones(
        self, legacy_relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """Инструмент только для приложения и инструмент, который сервер
        исполняет только задачей, агент не видит и вызвать не может."""
        server_id = self._server_id(relay_stand, tab, "legacy")
        task_only = {"name": "slow_report", "arguments": {}}

        listed, _ = await self._answer(
            legacy_relay, self._message(server_id, "tools/list", {})
        )
        called, _ = await self._answer(
            legacy_relay, self._message(server_id, "tools/call", task_only)
        )

        assert isinstance(listed, McpResultOutcome), listed
        tools = mt.ListToolsResult.model_validate(listed.result).tools
        names = [tool.name for tool in tools]
        assert "add" in names, names
        assert "archive_orders" in names, names
        assert "refresh_orders" not in names, names
        assert "slow_report" not in names, names
        assert isinstance(called, McpErrorOutcome), called
        assert called.error.code == mt.INVALID_PARAMS
        assert "'slow_report'" in called.error.message

    @pytest.mark.integration
    async def test_call_reaches_the_handshake_era_without_the_envelope_of_the_agent(
        self, legacy_relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """Вызов проходит, ход идёт агенту; конверт версии и личности агента
        из _meta запроса серверу не уходит — его ставит сессия."""
        server_id = self._server_id(relay_stand, tab, "legacy")
        echo = {
            "name": "client",
            "arguments": {},
            "_meta": {"progressToken": "call_002"},
        }

        outcome, _ = await self._answer(
            legacy_relay, self._message(server_id, "tools/call", echo)
        )

        assert isinstance(outcome, McpResultOutcome), outcome
        reply = mt.CallToolResult.model_validate(outcome.result)
        block = reply.content[0]
        assert isinstance(block, mt.TextContent), reply
        seen = json.loads(block.text)
        assert sorted(seen["meta"]) == ["progressToken"], seen
        assert seen["info"] == McpServers.OWN.info.model_dump(exclude_none=True)

    @pytest.mark.integration
    async def test_two_questions_of_the_handshake_era_come_as_input_requests(
        self, legacy_relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """Сервер 2025-11-25 спрашивает дважды посреди одного вызова: агент
        получает каждый вопрос просьбой о вводе с requestState ретранслятора,
        повторы с ответами доходят до открытого вызова, итог — по ответам."""
        server_id = self._server_id(relay_stand, tab, "legacy")
        call = self._loading(31, True)

        target = await self._asked(legacy_relay, server_id, call)
        chosen = {"action": "accept", "content": {"table": "sales.orders_stage"}}
        confirm = await self._asked(
            legacy_relay, server_id, self._retried(call, target, chosen)
        )
        go = {"action": "accept", "content": {"go": True}}
        done, _ = await self._answer(
            legacy_relay,
            self._message(server_id, "tools/call", self._retried(call, confirm, go)),
        )
        answers = await self._answers(legacy_relay, server_id, 31)

        assert target.request_state is not None
        assert target.request_state.startswith(McpRelay.HELD), target
        assert target.input_requests is not None
        [asked] = target.input_requests.values()
        assert isinstance(asked, mt.ElicitRequest), asked
        assert isinstance(asked.params, mt.ElicitRequestFormParams), asked
        assert asked.params.message == (
            "load_rows needs the target table before it can start the copy."
        )
        assert confirm.request_state == target.request_state
        assert confirm.input_requests is not None
        [again] = confirm.input_requests.values()
        assert isinstance(again, mt.ElicitRequest), again
        assert again.params.message == (
            "load_rows asks to start the copy of 31 rows into sales.orders_stage."
        )
        assert isinstance(done, McpResultOutcome), done
        reply = mt.CallToolResult.model_validate(done.result)
        assert not reply.is_error, reply
        assert reply.content == [
            mt.TextContent(text="loaded 31 rows into sales.orders_stage")
        ]
        assert answers == [
            {
                "rows": 31,
                "step": "target",
                "action": "accept",
                "content": {"table": "sales.orders_stage"},
            },
            {
                "rows": 31,
                "step": "confirm",
                "action": "accept",
                "content": {"go": True},
            },
        ]

    @pytest.mark.integration
    async def test_declined_question_of_the_handshake_era_reaches_the_server(
        self, legacy_relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab, "legacy")
        call = self._loading(32, False)

        target = await self._asked(legacy_relay, server_id, call)
        done, _ = await self._answer(
            legacy_relay,
            self._message(
                server_id,
                "tools/call",
                self._retried(call, target, {"action": "decline"}),
            ),
        )
        answers = await self._answers(legacy_relay, server_id, 32)

        assert isinstance(done, McpResultOutcome), done
        reply = mt.CallToolResult.model_validate(done.result)
        assert reply.is_error, reply
        assert reply.content == [mt.TextContent(text="the user answered decline")]
        assert answers == [
            {"rows": 32, "step": "target", "action": "decline", "content": None}
        ]

    @pytest.mark.integration
    async def test_question_without_an_answer_in_time_is_cancelled(
        self, brief_relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """Агент не ответил за срок вызова сервера: сервер получил cancel,
        вызов убран, поздний повтор — ошибка с причиной."""
        server_id = self._server_id(relay_stand, tab, "legacy")
        call = self._loading(33, False)

        target = await self._asked(brief_relay, server_id, call)
        await asyncio.sleep(3.0)
        late, _ = await self._answer(
            brief_relay,
            self._message(
                server_id,
                "tools/call",
                self._retried(
                    call, target, {"action": "accept", "content": {"table": "x"}}
                ),
            ),
        )
        answers = await self._answers(brief_relay, server_id, 33)

        assert isinstance(late, McpErrorOutcome), late
        assert late.error.code == mt.INVALID_PARAMS
        assert "names no call the relay holds open" in late.error.message
        assert "call_timeout_sec of [mcp.servers.legacy]" in late.error.message
        assert answers == [
            {"rows": 33, "step": "target", "action": "cancel", "content": None}
        ]

    @pytest.mark.integration
    async def test_retry_of_another_registration_is_refused(
        self, legacy_relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """requestState открытого вызова чужой регистрации не отдаёт ему
        ответ; свой повтор после этого проходит."""
        server_id = self._server_id(relay_stand, tab, "legacy")
        other = self._server_id(relay_stand, tab, "legacy")
        call = self._loading(34, False)
        chosen = {"action": "accept", "content": {"table": "tmp.orders_copy"}}

        target = await self._asked(legacy_relay, server_id, call)
        stolen, _ = await self._answer(
            legacy_relay,
            self._message(other, "tools/call", self._retried(call, target, chosen)),
        )
        done, _ = await self._answer(
            legacy_relay,
            self._message(server_id, "tools/call", self._retried(call, target, chosen)),
        )

        assert isinstance(stolen, McpErrorOutcome), stolen
        assert "another server registration" in stolen.error.message
        assert isinstance(done, McpResultOutcome), done
        reply = mt.CallToolResult.model_validate(done.result)
        assert reply.content == [
            mt.TextContent(text="loaded 34 rows into tmp.orders_copy")
        ]

    async def test_tool_for_the_app_is_neither_listed_nor_called(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab, "standard")
        call = {"name": "refresh_orders", "arguments": {}}

        listed, _ = await self._answer(
            relay, self._message(server_id, "tools/list", {})
        )
        called, _ = await self._answer(
            relay, self._message(server_id, "tools/call", call)
        )

        assert isinstance(listed, McpResultOutcome), listed
        names = [
            tool.name for tool in mt.ListToolsResult.model_validate(listed.result).tools
        ]
        assert "add" in names, names
        assert "orders" in names, names
        assert "archive_orders" in names, names
        assert "refresh_orders" not in names, names
        assert isinstance(called, McpErrorOutcome), called
        assert called.error.code == mt.INVALID_PARAMS
        assert "'refresh_orders'" in called.error.message

    async def test_scope_mark_of_a_server_without_the_extension_is_left_alone(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """Сервер помечает параметр как область вызова, но расширения
        com.boba/scope не объявляет: агент видит параметр в списке, и
        ретранслятор ничего в него не ставит — договор есть только по
        объявленному расширению."""
        server_id = self._server_id(relay_stand, tab, "standard")
        call = {"name": "area", "arguments": {}}

        listed, _ = await self._answer(
            relay, self._message(server_id, "tools/list", {})
        )
        called, _ = await self._answer(
            relay, self._message(server_id, "tools/call", call)
        )

        assert isinstance(listed, McpResultOutcome), listed
        tools = mt.ListToolsResult.model_validate(listed.result).tools
        area = next(tool for tool in tools if tool.name == "area")
        assert "scope_id" in area.input_schema["properties"], area
        assert isinstance(called, McpResultOutcome), called
        reply = mt.CallToolResult.model_validate(called.result)
        assert not reply.is_error, reply
        assert "area ''" in str(reply.content), reply

    async def test_progress_reaches_the_agent_under_its_token(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab, "standard")
        call = {
            "name": "progress",
            "arguments": {"steps": 2},
            "_meta": {"progressToken": "call_001"},
        }
        message = self._message(server_id, "tools/call", call)

        outcome, notes = await self._answer(relay, message)

        assert isinstance(outcome, McpResultOutcome), outcome
        reply = mt.CallToolResult.model_validate(outcome.result)
        assert not reply.is_error, reply
        assert [note.method for note in notes] == ["notifications/progress"] * 2
        for note in notes:
            assert note.server_id == server_id
            assert note.request_id == message.request_id
            progress = mt.ProgressNotificationParams.model_validate(note.params)
            assert progress.progress_token == "call_001"

    async def test_shown_file_without_live_tabs_comes_back_as_is(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """Файл, который сервер просит показать, открывать некому: у треда
        нет живых вкладок на этом инстансе — ответ агенту как есть."""
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = relay_stand.registrations.register(registration, ["standard"])[
            0
        ].server_id
        path = f"/workspace/{registration.thread_id}/upload/data.bin"
        shown = {"name": "show", "arguments": {"path": path, "mime": "x/unknown"}}

        outcome, _ = await self._answer(
            relay, self._message(server_id, "tools/call", shown)
        )

        assert isinstance(outcome, McpResultOutcome), outcome
        reply = mt.CallToolResult.model_validate(outcome.result)
        assert not reply.is_error, reply
        assert len(reply.content) == 2, reply

    async def test_refused_show_is_an_error_of_the_call(
        self,
        relay_stand: RelayStand,
        standard: McpServerConfig,
        tab: TabSession,
        call_stand: CallStand,
    ) -> None:
        """Панель живой вкладки не показала файл (вьювера для MIME нет):
        агент получает isError с причиной в том же вызове. Путь файла
        разбирается по каталогу workspace, который объявил сервер."""
        shows = self._flagged(standard, "--workspace", "/workspace")
        servers = McpServers(McpServersConfig(servers={"standard": shows}))
        canvas = CanvasOpener(HolderSurface(context_var.get()))
        relay = relay_stand.relay_with(servers, relay_stand.chat, canvas)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = relay_stand.registrations.register(registration, ["standard"])[
            0
        ].server_id
        path = f"/workspace/{registration.thread_id}/upload/data.bin"
        shown = {"name": "show", "arguments": {"path": path, "mime": "x/unknown"}}
        try:
            outcome, _ = await self._answer(
                relay, self._message(server_id, "tools/call", shown)
            )
        finally:
            await servers.stop()

        assert isinstance(outcome, McpResultOutcome), outcome
        reply = mt.CallToolResult.model_validate(outcome.result)
        assert reply.is_error, reply
        refusal = reply.content[-1]
        assert isinstance(refusal, mt.TextContent), reply
        assert refusal.text.startswith(f"{path} is not shown in the canvas: "), refusal
        assert "x/unknown" in refusal.text, refusal

    async def test_input_required_and_server_errors_come_back_as_they_are(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab, "standard")
        asking = {"name": "ask", "arguments": {"refuse": False}}
        refusing = {"name": "ask", "arguments": {"refuse": True}}

        asked, _ = await self._answer(
            relay, self._message(server_id, "tools/call", asking)
        )
        refused, _ = await self._answer(
            relay, self._message(server_id, "tools/call", refusing)
        )

        assert isinstance(asked, McpResultOutcome), asked
        assert mt.InputRequiredResult.model_validate(asked.result).request_state
        assert isinstance(refused, McpErrorOutcome), refused
        assert refused.error.code == mt.INVALID_PARAMS
        assert refused.error.data == {"reason": "the stand refuses"}

    async def test_dead_server_is_an_external_error(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        server_id = self._server_id(relay_stand, tab, "standard")
        message = self._message(server_id, "tools/call", {"name": "die"})

        with pytest.raises(RequestError) as refused:
            await relay.answer(message, asyncio.Queue[McpMessage]().put)

        assert refused.value.code == AcpErrorCode.BACKEND_FAILED
        assert "'die'" in str(refused.value)

    async def test_relay_gives_the_time_left_and_the_scope_of_the_server(
        self, relay_stand: RelayStand, standard: McpServerConfig, tab: TabSession
    ) -> None:
        """Подсказки кэша сервера доходят до агента: описание и список — с
        оставшимся сроком свежести и областью кэша сервера, а не с нулём."""
        cached = self._cached(standard, 60_000)
        servers = McpServers(McpServersConfig(servers={"standard": cached}))
        try:
            relay = relay_stand.relay(servers)
            server_id = self._server_id(relay_stand, tab, "standard")
            discovered, _ = await self._answer(
                relay, self._message(server_id, "server/discover", {})
            )
            listed, _ = await self._answer(
                relay, self._message(server_id, "tools/list", {})
            )
        finally:
            await servers.stop()

        assert isinstance(discovered, McpResultOutcome), discovered
        assert isinstance(listed, McpResultOutcome), listed
        described = mt.DiscoverResult.model_validate(discovered.result)
        tools = mt.ListToolsResult.model_validate(listed.result)
        assert 0 < described.ttl_ms <= 60_000, described.ttl_ms
        assert described.cache_scope == "public"
        assert 0 < tools.ttl_ms <= 60_000, tools.ttl_ms
        assert tools.cache_scope == "private"

    async def test_relay_reads_the_expired_description_again(
        self, relay_stand: RelayStand, standard: McpServerConfig, tab: TabSession
    ) -> None:
        """Сервер сменил инструкции: после срока свежести ретранслятор
        перечитывает server/discover и отдаёт агенту новые."""
        cached = self._cached(standard, 1000)
        servers = McpServers(McpServersConfig(servers={"standard": cached}))
        try:
            relay = relay_stand.relay(servers)
            server_id = self._server_id(relay_stand, tab, "standard")
            first, _ = await self._answer(
                relay, self._message(server_id, "server/discover", {})
            )
            retitle = {"name": "retitle", "arguments": {"text": "renamed"}}
            await self._answer(relay, self._message(server_id, "tools/call", retitle))
            await asyncio.sleep(1.5)
            again, _ = await self._answer(
                relay, self._message(server_id, "server/discover", {})
            )
        finally:
            await servers.stop()

        assert isinstance(first, McpResultOutcome), first
        assert isinstance(again, McpResultOutcome), again
        before = mt.DiscoverResult.model_validate(first.result)
        after = mt.DiscoverResult.model_validate(again.result)
        assert before.instructions != "renamed"
        assert after.instructions == "renamed"

    @pytest.mark.parametrize("method", ["server/discover", "tools/list"])
    async def test_unreachable_server_is_an_external_error(
        self, relay_stand: RelayStand, tab: TabSession, method: str
    ) -> None:
        """Сервер профиля не поднимается: агент получает ошибку ACP
        BACKEND_FAILED, а не ответ MCP, и не считает сервер живым."""
        endpoint = StdioCommand(command="/nonexistent/mcp-server")
        ghost = McpServerConfig(
            endpoint=endpoint,
            limits=McpServerLimits(connect_timeout_sec=5.0, call_timeout_sec=5.0),
        )
        servers = McpServers(McpServersConfig(servers={"ghost": ghost}))
        try:
            relay = relay_stand.relay(servers)
            server_id = self._server_id(relay_stand, tab, "ghost")
            with pytest.raises(RequestError) as refused:
                await relay.answer(
                    self._message(server_id, method, {}),
                    asyncio.Queue[McpMessage]().put,
                )
        finally:
            await servers.stop()

        assert refused.value.code == AcpErrorCode.BACKEND_FAILED
        assert "'ghost'" in str(refused.value)

    @staticmethod
    def _cached(standard: McpServerConfig, ttl_ms: int) -> McpServerConfig:
        """Обычный сервер стенда с подсказками кэша описания и списка."""
        return TestMcpRelay._flagged(standard, "--ttl-ms", str(ttl_ms))

    @staticmethod
    def _flagged(standard: McpServerConfig, *flags: str) -> McpServerConfig:
        """Обычный сервер стенда с флагами запуска flags."""
        endpoint = standard.endpoint
        if not isinstance(endpoint, StdioCommand):
            raise AssertionError(f"the standard server runs over stdio: {endpoint}")

        args = (*endpoint.args, *flags)

        return standard.model_copy(
            update={"endpoint": endpoint.model_copy(update={"args": args})}
        )

    async def test_sealing_is_refused_to_a_server_without_the_seal_feature(
        self, relay_stand: RelayStand
    ) -> None:
        offer = ConnectionOffer(kind="postgres", sealed=True, names={})
        declared = SealedParamSchema().declared(offer)
        schema = {
            "type": "object",
            "properties": {"connection": {"type": "string", **declared}},
        }
        server = McpServerInfo(name="standard", features={})

        with pytest.raises(RefusalError) as refused:
            await relay_stand.sealing.seal(
                "mcp__standard__query", {"connection": "orders"}, schema, server
            )

        assert SealFeature.ID in str(refused.value)
        assert "'standard'" in str(refused.value)

    async def test_server_name_goes_as_is_when_only_the_server_is_offered(
        self, relay_stand: RelayStand
    ) -> None:
        """Схема без ветки запечатанного значения: клиент не запечатывает и
        ключа сервера не требует, имя соединения сервера уходит как есть."""
        offer = ConnectionOffer(kind="postgres", sealed=False, names={"dwh": "dwh"})
        declared = SealedParamSchema().declared(offer)
        schema = {
            "type": "object",
            "properties": {"connection": {"type": "string", **declared}},
        }
        server = McpServerInfo(name="standard", features={})

        sent = await relay_stand.sealing.seal(
            "mcp__standard__query", {"connection": "dwh"}, schema, server
        )

        assert sent == {"connection": "dwh"}, sent


class TestAppRequests:
    """Запросы приложения MCP Apps: инструменты только своего сервера с "app"
    в visibility, ресурсы своего сервера; сервер без секции apps и сервер
    chat приложению отказывают, отказ уходит приложению ошибкой JSON-RPC."""

    SANDBOX: ClassVar[McpServerApps] = McpServerApps.model_validate(
        {"chat_url": "http://127.0.0.1:8122/aid/", "port": 8123}
    )

    @pytest.fixture
    async def relay(
        self, relay_stand: RelayStand, standard: McpServerConfig
    ) -> AsyncIterator[McpRelay]:
        drawn = standard.model_copy(update={"apps": self.SANDBOX})
        config = McpServersConfig(servers={"apps": drawn, "standard": standard})
        servers = McpServers(config)
        try:
            yield relay_stand.relay(servers)
        finally:
            await servers.stop()

    PROFILE_SERVERS: ClassVar[frozenset[str]] = frozenset({"apps", "standard"})
    """Серверы профиля треда регистрации приложения."""

    @staticmethod
    def _of(tab: TabSession, server: str, servers: frozenset[str]) -> McpRegistration:
        registration = tab.registration(uuid4(), SignInStand.PROFILE)

        return replace(registration, server=server, servers=servers)

    async def test_app_calls_the_tools_for_the_app(
        self, relay: McpRelay, tab: TabSession
    ) -> None:
        registration = self._of(tab, "apps", self.PROFILE_SERVERS)
        refresh = mt.CallToolRequestParams(name="refresh_orders", arguments={})
        orders = mt.CallToolRequestParams(name="orders", arguments={"rows": []})

        refreshed = await relay.app_call(registration, refresh)
        shown = await relay.app_call(registration, orders)

        assert not refreshed.is_error, refreshed
        assert refreshed.content == [mt.TextContent(text="orders refreshed")]
        assert shown.content == [mt.TextContent(text="0 orders are shown by their app")]

    async def test_tool_for_the_model_is_refused_to_the_app(
        self, relay: McpRelay, tab: TabSession
    ) -> None:
        registration = self._of(tab, "apps", self.PROFILE_SERVERS)
        archive = mt.CallToolRequestParams(name="archive_orders", arguments={})

        with pytest.raises(MCPError) as refused:
            await relay.app_call(registration, archive)

        answered = await relay.app_answer(relay.app_call(registration, archive))

        assert refused.value.error.code == mt.INVALID_PARAMS
        assert "offers the app no tool 'archive_orders'" in refused.value.error.message
        assert answered["error"]["code"] == mt.INVALID_PARAMS
        assert "'archive_orders'" in answered["error"]["message"]

    @pytest.mark.parametrize("server", ["standard", ChatTools.NAME])
    async def test_server_without_apps_refuses_the_app(
        self, relay: McpRelay, tab: TabSession, server: str
    ) -> None:
        registration = self._of(tab, server, self.PROFILE_SERVERS)
        orders = mt.CallToolRequestParams(name="orders", arguments={"rows": []})
        read = mt.ReadResourceRequestParams(uri="ui://standard/orders.html")

        with pytest.raises(MCPError) as called:
            await relay.app_call(registration, orders)

        with pytest.raises(MCPError) as reading:
            await relay.app_read(registration, read)

        assert called.value.error.code == mt.INVALID_PARAMS
        assert f"{server!r}" in called.value.error.message
        assert reading.value.error.code == mt.INVALID_PARAMS

    async def test_server_outside_the_profile_of_the_thread_refuses_the_app(
        self, relay: McpRelay, tab: TabSession
    ) -> None:
        """Сервер с секцией apps, которого профиль треда не даёт: приложение
        и любой код вкладки его не зовут и не читают."""
        registration = self._of(tab, "apps", frozenset({"standard"}))
        orders = mt.CallToolRequestParams(name="orders", arguments={"rows": []})
        read = mt.ReadResourceRequestParams(uri="ui://standard/orders.html")

        with pytest.raises(MCPError) as called:
            await relay.app_call(registration, orders)

        with pytest.raises(MCPError) as reading:
            await relay.app_read(registration, read)

        assert called.value.error.code == mt.INVALID_PARAMS
        assert "is not a server of profile" in called.value.error.message
        assert reading.value.error.code == mt.INVALID_PARAMS

    def _request(
        self, stand: RelayStand, tab: TabSession, server: str, name: str
    ) -> ToolSourceRequest:
        """Запрос источника вызова name сервера server от агента вкладки."""
        registration = self._of(tab, server, self.PROFILE_SERVERS)
        server_id = stand.registrations.register(registration, [server])[0].server_id

        return ToolSourceRequest(
            session_id="s-1",
            tool_call_id="call-1",
            server_id=server_id,
            name=name,
            arguments={"rows": []},
        )

    async def test_source_of_an_app_call_holds_the_card_and_the_whole_resource(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """Источник вызова инструмента с приложением у сервера с секцией apps:
        карточка, описание сервера и ресурс приложения целиком с _meta — по
        нему лента нарисует вызов, не спрашивая сервер."""
        request = self._request(relay_stand, tab, "apps", "orders")

        reply = await relay.source(request)

        source = ToolSource.model_validate(reply.source)
        assert source.server == "apps"
        assert source.tool.name == "orders"
        assert source.tool.meta is not None
        assert source.tool.meta["ui"]["resourceUri"] == "ui://standard/orders.html"
        assert source.description.capabilities.tools is not None
        assert source.nodes == []
        assert isinstance(source.app, AppResource), source.app
        (content,) = source.app.resource.contents
        assert content.mime_type == APP_MIME_TYPE
        assert content.meta is not None
        assert content.meta["ui"]["csp"] == {
            "resourceDomains": ["https://cdn.example.com"]
        }

    async def test_source_of_a_server_without_apps_has_no_resource(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        request = self._request(relay_stand, tab, "standard", "orders")

        reply = await relay.source(request)

        source = ToolSource.model_validate(reply.source)
        assert source.tool.name == "orders"
        assert source.app is None

    async def test_source_of_an_unknown_tool_or_server_is_an_external_error(
        self, relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        unknown = self._request(relay_stand, tab, "apps", "no_such_tool")
        ghost = unknown.model_copy(update={"server_id": "ghost"})

        with pytest.raises(RequestError) as refused:
            await relay.source(unknown)

        with pytest.raises(RequestError) as unregistered:
            await relay.source(ghost)

        assert refused.value.code == AcpErrorCode.BACKEND_FAILED
        assert "lists no tool 'no_such_tool'" in str(refused.value)
        assert unregistered.value.code == AcpErrorCode.UNKNOWN_SERVER

    async def test_app_reads_the_resources_of_its_server(
        self, relay: McpRelay, tab: TabSession
    ) -> None:
        registration = self._of(tab, "apps", self.PROFILE_SERVERS)
        read = mt.ReadResourceRequestParams(uri="ui://standard/orders.html")

        answered = await relay.app_answer(relay.app_read(registration, read))

        resource = mt.ReadResourceResult.model_validate(answered).contents[0]
        assert resource.mime_type == APP_MIME_TYPE
        assert answered["contents"][0]["_meta"]["ui"]["csp"] == {
            "resourceDomains": ["https://cdn.example.com"]
        }

    @pytest.fixture
    async def notes_relay(
        self, relay_stand: RelayStand, apps_slot: AppsSlot
    ) -> AsyncIterator[McpRelay]:
        """Ретранслятор над сервером notes контейнера boba-testapps —
        FastMCPApp на fastmcp[apps] с секцией apps."""
        endpoint = HttpEndpoint(
            service=HttpLocation(
                scheme="http", host=apps_slot.host, port=AppServer.NOTES.value, path=""
            ),
            path=apps_slot.PATH,
        )
        notes = McpServerConfig(
            endpoint=endpoint,
            limits=McpServerLimits(connect_timeout_sec=20.0, call_timeout_sec=60.0),
            apps=self.SANDBOX,
        )
        servers = McpServers(McpServersConfig(servers={"notes": notes}))
        try:
            yield relay_stand.relay(servers)
        finally:
            await servers.stop()

    @staticmethod
    def _tool_calls(node: Any) -> Iterator[str]:
        """Имена инструментов в действиях toolCall представления Prefab."""
        if isinstance(node, dict):
            if node.get("action") == "toolCall":
                yield str(node["tool"])

            for value in node.values():
                yield from TestAppRequests._tool_calls(value)

        if isinstance(node, list):
            for item in node:
                yield from TestAppRequests._tool_calls(item)

    async def _as_model(
        self, relay: McpRelay, server_id: str, method: str, params: dict[str, Any]
    ) -> McpOutcome:
        heard: asyncio.Queue[McpMessage] = asyncio.Queue()
        message = McpMessage(
            server_id=server_id,
            request_id=f"mcp:{uuid4().hex}",
            method=method,
            params={**params, "_meta": TestMcpRelay.ENVELOPE},
        )

        return await relay.answer(message, heard.put)

    async def test_fastmcpapp_backend_tool_is_called_by_the_app_under_its_listed_name_and_hidden_from_the_model(  # noqa: E501 — имя теста называет проверку целиком
        self, notes_relay: McpRelay, relay_stand: RelayStand, tab: TabSession
    ) -> None:
        """FastMCPApp: модель видит только вход notes_board и зовёт его; в
        представлении действие toolCall названо именем из tools/list; этим
        именем приложение зовёт backend save, а модели он не виден и
        недоступен."""
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = relay_stand.registrations.register(registration, ["notes"])[0]
        listed = await self._as_model(
            notes_relay, server_id.server_id, "tools/list", {}
        )
        opened = await self._as_model(
            notes_relay,
            server_id.server_id,
            "tools/call",
            {"name": "notes_board", "arguments": {}},
        )
        assert isinstance(listed, McpResultOutcome), listed
        assert isinstance(opened, McpResultOutcome), opened
        names = [
            tool.name for tool in mt.ListToolsResult.model_validate(listed.result).tools
        ]
        board = mt.CallToolResult.model_validate(opened.result)
        actions = set(self._tool_calls(board.structured_content))

        assert names == ["notes_board"], names
        assert len(actions) == 1, board.structured_content
        save = actions.pop()

        app = replace(registration, server="notes", servers=frozenset({"notes"}))
        saved = await notes_relay.app_call(
            app, mt.CallToolRequestParams(name=save, arguments={"text": "eggs"})
        )
        refused = await self._as_model(
            notes_relay,
            server_id.server_id,
            "tools/call",
            {"name": save, "arguments": {"text": "by the model"}},
        )

        assert not saved.is_error, saved
        assert saved.structured_content is not None
        assert {"text": "eggs"} in saved.structured_content["notes"], saved
        assert isinstance(refused, McpErrorOutcome), refused
        assert refused.error.code == mt.INVALID_PARAMS
        assert f"{save!r}" in refused.error.message


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    """Хранилище соединений пользователей стенда ретранслятора."""
    schema = "relay_connections"
    cipher = SecretCipher(secrets.token_bytes(32))
    schema_dir = files("boba.connection_broker") / "schema"
    async with pool.connection() as conn:
        await PostgresSchema(schema).apply_files(conn, schema_dir, {})

    return ConnectionStore(pool, schema, cipher, ConnectionTypes.discover())


@dataclass(frozen=True)
class RelayCall:
    """Случай вызова через ретранслятор: инструмент, аргументы модели и
    ожидаемый ответ сервера — isError и куски его текста."""

    name: str
    arguments: Mapping[str, Any]
    errored: bool
    expected: Sequence[str]


@pytest.mark.integration
class TestRelaySealing:
    """Ретранслятор запечатывает соединения пользователя через CallSealing:
    невыданное имя — отказ isError, выданное доходит до тела сервера. У
    инструмента со вложенными вызовами, объявленного сервером, соединения
    запечатываются в каждом узле; отказ узла — isError всего вызова с
    ключом узла, вызов не по объявленной форме уходит серверу как есть.
    Когда сервер предлагает и свои соединения (endpoint offers, роль
    analyst), имя соединения сервера уходит как есть, а совпавшее с именем
    соединения пользователя — запечатывается с предупреждением."""

    ENVELOPE: ClassVar[dict[str, Any]] = TestMcpRelay.ENVELOPE

    @pytest.mark.parametrize(
        "case",
        [
            RelayCall(
                name="fake_connection_host",
                arguments={"connection": "missing"},
                errored=True,
                expected=["is not among the connections"],
            ),
            RelayCall(
                name="fake_connection_host",
                arguments={"connection": "orders"},
                errored=False,
                expected=["db.local|orders"],
            ),
            RelayCall(
                name="workflow",
                arguments={
                    "nodes": [
                        {
                            "key": "n0",
                            "tool": "fake_connection_host",
                            "args": {"connection": "orders"},
                        }
                    ]
                },
                errored=False,
                expected=["db.local|orders"],
            ),
            RelayCall(
                name="workflow",
                arguments={
                    "nodes": [
                        {
                            "key": "n0",
                            "tool": "fake_connection_host",
                            "args": {"connection": "missing"},
                        }
                    ]
                },
                errored=True,
                expected=[
                    "node 'n0'",
                    "fake_connection_host",
                    "is not among the connections",
                ],
            ),
            RelayCall(
                name="workflow",
                arguments={
                    "nodes": [{"key": "n0", "tool": "no_such_tool", "args": {}}]
                },
                errored=True,
                expected=[
                    "node 'n0'",
                    "offers the caller of the call no tool 'no_such_tool'",
                ],
            ),
            RelayCall(
                name="workflow",
                arguments={
                    "nodes": [
                        {
                            "key": "n0",
                            "tool": "mcp__boba__fake_connection_host",
                            "args": {"connection": "orders"},
                        }
                    ]
                },
                errored=True,
                expected=[
                    "node 'n0': 'mcp__boba__fake_connection_host' is the name "
                    "the agent gives the tool 'fake_connection_host'",
                    "name the tool of the node 'fake_connection_host'",
                ],
            ),
            RelayCall(
                name="workflow",
                arguments={"nodes": "n0"},
                errored=True,
                expected=["does not match its schema", "nodes"],
            ),
        ],
        ids=[
            "refused",
            "sealed",
            "node-sealed",
            "node-refused",
            "node-unknown-tool",
            "node-agent-name",
            "off-shape-as-is",
        ],
    )
    async def test_sealing_is_applied_to_the_call(
        self,
        service_process: ServiceProcess,
        store: ConnectionStore,
        call_stand: CallStand,
        tab: TabSession,
        case: RelayCall,
    ) -> None:
        config = service_process.servers()
        answer = await self._called(config, store, call_stand, tab, ["orders"], case)

        assert answer.is_error is case.errored, answer
        for part in case.expected:
            assert part in str(answer.content), answer

    @pytest.mark.parametrize(
        ("owned", "logged", "case"),
        [
            (
                [],
                [],
                RelayCall(
                    name="fake_connection_host",
                    arguments={"connection": "dwh"},
                    errored=False,
                    expected=["dwh.local|dwh"],
                ),
            ),
            (
                ["dwh"],
                ["connection dwh of kind postgres is both yours"],
                RelayCall(
                    name="fake_connection_host",
                    arguments={"connection": "dwh"},
                    errored=False,
                    expected=["db.local|orders"],
                ),
            ),
        ],
        ids=["server-name-as-is", "collision-user-wins"],
    )
    async def test_server_connections_are_offered(  # noqa: PLR0913 — случай собирается фикстурами стендов
        self,
        service_process: ServiceProcess,
        store: ConnectionStore,
        call_stand: CallStand,
        tab: TabSession,
        owned: Sequence[str],
        logged: Sequence[str],
        case: RelayCall,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Endpoint offers предлагает роли analyst и соединение сервера dwh:
        имя сервера уходит как есть, совпавшее с именем соединения
        пользователя запечатывается с предупреждением."""
        config = service_process.servers()
        boba = config.servers["boba"]
        endpoint = boba.endpoint.model_copy(update={"path": "/offers"})
        served = boba.model_copy(update={"endpoint": endpoint})
        offers = config.model_copy(update={"servers": {"boba": served}})
        tab.password_sign_in(frozenset({"analyst"}))
        answer = await self._called(offers, store, call_stand, tab, owned, case)

        assert answer.is_error is case.errored, answer
        for part in case.expected:
            assert part in str(answer.content), answer

        for part in logged:
            assert part in caplog.text, caplog.text

    async def _called(  # noqa: PLR0913 — вызов собирается фикстурами стендов
        self,
        config: McpServersConfig,
        store: ConnectionStore,
        call_stand: CallStand,
        tab: TabSession,
        owned: Sequence[str],
        case: RelayCall,
    ) -> mt.CallToolResult:
        """Итог вызова case через ретранслятор к серверам config, когда у
        пользователя вкладки есть соединения owned; ждёт, пока стенд сервиса
        начнёт отдавать список инструментов."""
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(lambda: store, lambda: credentials, call_stand.contexts)
        servers = McpServers(config)
        relay = stand.relay(servers)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, ["boba"])[0].server_id
        profile = PostgresConfig.model_validate(
            {
                "host": "db.local",
                "dbname": "orders",
                "auth": {"method": "trust", "user": "u"},
            }
        )
        owner = GrantTarget.user(registration.user_id)
        for name in owned:
            connection_id = await store.add(name, profile)
            await store.grant(connection_id, owner)

        heard: asyncio.Queue[McpMessage] = asyncio.Queue()
        listing = McpMessage(
            server_id=server_id,
            request_id="mcp:listing",
            method="tools/list",
            params={"_meta": self.ENVELOPE},
        )
        try:
            listed: McpOutcome | None = None
            for _ in range(150):
                try:
                    listed = await relay.answer(listing, heard.put)
                except RequestError:
                    await asyncio.sleep(0.2)
                    continue

                break

            assert isinstance(listed, McpResultOutcome), (
                f"the service stand did not list its tools within 30s: {listed}"
            )
            called = await relay.answer(
                McpMessage(
                    server_id=server_id,
                    request_id="mcp:call",
                    method="tools/call",
                    params={
                        "name": case.name,
                        "arguments": case.arguments,
                        "_meta": self.ENVELOPE,
                    },
                ),
                heard.put,
            )
        finally:
            await servers.stop()

        assert isinstance(called, McpResultOutcome), called

        return mt.CallToolResult.model_validate(called.result)

    async def test_source_of_a_workflow_call_holds_the_cards_of_its_nodes(
        self,
        service_process: ServiceProcess,
        store: ConnectionStore,
        call_stand: CallStand,
        tab: TabSession,
    ) -> None:
        """Источник вызова инструмента со вложенными вызовами несёт карточки
        инструментов его узлов: по ним лента рисует вход каждого узла."""
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(lambda: store, lambda: credentials, call_stand.contexts)
        servers = McpServers(service_process.servers())
        relay = stand.relay(servers)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, ["boba"])[0].server_id
        node = {"key": "n0", "tool": "fake_connection_host", "args": {}}
        request = ToolSourceRequest(
            session_id="s-1",
            tool_call_id="call-1",
            server_id=server_id,
            name="workflow",
            arguments={"nodes": [node, node]},
        )
        try:
            reply = None
            for _ in range(150):
                try:
                    reply = await relay.source(request)
                except RequestError:
                    await asyncio.sleep(0.2)
                    continue

                break
        finally:
            await servers.stop()

        assert reply is not None, "the service stand gave no source within 30s"
        source = ToolSource.model_validate(reply.source)
        assert source.tool.name == "workflow"
        assert [card.name for card in source.nodes] == ["fake_connection_host"]
        assert source.app is None

    async def test_thread_is_hidden_from_the_model_and_filled_by_the_relay(
        self,
        service_process: ServiceProcess,
        call_stand: CallStand,
        tab: TabSession,
    ) -> None:
        """Параметр области агент в списке не видит, а вызов идёт в треде
        вкладки: сервер объявил расширение области вызова, и ретранслятор
        ставит тред в параметр с объявленной пометкой x-boba-scope."""
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(
            NoConnectionStore(), lambda: credentials, call_stand.contexts
        )
        servers = McpServers(service_process.servers())
        relay = stand.relay(servers)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, ["boba"])[0].server_id
        heard: asyncio.Queue[McpMessage] = asyncio.Queue()
        listing = McpMessage(
            server_id=server_id,
            request_id="mcp:listing",
            method="tools/list",
            params={"_meta": self.ENVELOPE},
        )
        try:
            listed: McpOutcome | None = None
            for _ in range(150):
                try:
                    listed = await relay.answer(listing, heard.put)
                except RequestError:
                    await asyncio.sleep(0.2)
                    continue

                break

            called = await relay.answer(
                McpMessage(
                    server_id=server_id,
                    request_id="mcp:call",
                    method="tools/call",
                    params={
                        "name": "fake_scope",
                        "arguments": {},
                        "_meta": self.ENVELOPE,
                    },
                ),
                heard.put,
            )
        finally:
            await servers.stop()

        assert isinstance(listed, McpResultOutcome), listed
        tools = mt.ListToolsResult.model_validate(listed.result).tools
        scoped = next(tool for tool in tools if tool.name == "fake_scope")
        assert CallFields.SCOPE not in scoped.input_schema["properties"], scoped
        assert isinstance(called, McpResultOutcome), called
        answer = mt.CallToolResult.model_validate(called.result)
        assert not answer.is_error, answer
        assert registration.thread_id in str(answer.content), answer


@pytest.mark.integration
class TestRelayJournals:
    """Журнал, который вызов агента открыл на сервере boba-mcp, ретранслятор
    узнаёт уведомлением расширения журнала в потоке ответа самого вызова и
    называет ленте треда сообщением JournalOpened под идентификатором вызова
    агента — его токеном прогресса; у связки — с ключом узла от автора."""

    ENVELOPE: ClassVar[dict[str, Any]] = TestMcpRelay.ENVELOPE

    @pytest.mark.parametrize(
        ("name", "arguments", "node"),
        [
            ("fake_progress", {"steps": 3, "pause": 0.5}, None),
            (
                "workflow",
                {
                    "nodes": [
                        {
                            "key": "slow",
                            "tool": "fake_progress",
                            "args": {"steps": 3, "pause": 0.5},
                        }
                    ]
                },
                "slow",
            ),
        ],
        ids=["call", "node"],
    )
    async def test_running_call_journal_is_named_to_the_thread(  # noqa: PLR0913 — случай собирается фикстурами стендов
        self,
        service_process: ServiceProcess,
        call_stand: CallStand,
        tab: TabSession,
        name: str,
        arguments: dict[str, Any],
        node: str | None,
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(
            NoConnectionStore(), lambda: credentials, call_stand.contexts
        )
        servers = McpServers(service_process.servers())
        relay = stand.relay(servers)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, ["boba"])[0].server_id
        opened: list[JournalOpened] = []

        async def heard(envelope: Envelope) -> None:
            if isinstance(envelope.message, JournalOpened):
                opened.append(envelope.message)

        stand.bus.subscribe(Scope.chat(registration.thread_id), heard)
        notes: asyncio.Queue[McpMessage] = asyncio.Queue()
        meta = {**self.ENVELOPE, "progressToken": "agent-call-7"}
        try:
            for _ in range(150):
                try:
                    await relay.answer(
                        McpMessage(
                            server_id=server_id,
                            request_id=f"mcp:{uuid4().hex}",
                            method="tools/list",
                            params={"_meta": self.ENVELOPE},
                        ),
                        notes.put,
                    )
                except RequestError:
                    await asyncio.sleep(0.2)
                    continue

                break

            called = await relay.answer(
                McpMessage(
                    server_id=server_id,
                    request_id="mcp:call",
                    method="tools/call",
                    params={"name": name, "arguments": arguments, "_meta": meta},
                ),
                notes.put,
            )
        finally:
            await servers.stop()

        assert isinstance(called, McpResultOutcome), called
        answer = mt.CallToolResult.model_validate(called.result)
        assert not answer.is_error, answer
        assert len(opened) == 1, opened
        journal = opened[0]
        assert journal.call_id == "agent-call-7"
        assert journal.server == "boba"
        assert journal.node == node
        address = f"//{journal.run}/{journal.call}/"
        linked: list[str] = []
        for block in answer.content:
            if not isinstance(block, mt.ResourceLink):
                continue

            if address in str(block.uri):
                linked.append(str(block.uri))

        assert linked, (answer.content, journal)


@pytest.mark.integration
class TestChatServer:
    """Сервер chat в процессе чата через ретранслятор: собственные
    инструменты чата по ролям вкладки, connection_list отдаёт соединения,
    выданные пользователю регистрации; совпавшие имена инструментов —
    ошибка доставки без исхода MCP."""

    ENVELOPE: ClassVar[dict[str, Any]] = TestMcpRelay.ENVELOPE

    async def test_own_tools_are_listed_and_called_in_process(
        self, store: ConnectionStore, call_stand: CallStand, tab: TabSession
    ) -> None:
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(lambda: store, lambda: credentials, call_stand.contexts)
        relay = stand.relay(McpServers(McpServersConfig(servers={})))
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, [ChatTools.NAME])[
            0
        ].server_id
        profile = PostgresConfig.model_validate(
            {
                "host": "db.local",
                "dbname": "orders",
                "auth": {"method": "trust", "user": "u"},
            }
        )
        connection_id = await store.add("orders", profile)
        await store.grant(connection_id, GrantTarget.user(registration.user_id))

        heard: asyncio.Queue[McpMessage] = asyncio.Queue()

        listed = await relay.answer(
            McpMessage(
                server_id=server_id,
                request_id="mcp:listing",
                method="tools/list",
                params={"_meta": self.ENVELOPE},
            ),
            heard.put,
        )
        called = await relay.answer(
            McpMessage(
                server_id=server_id,
                request_id="mcp:call",
                method="tools/call",
                params={
                    "name": "connection_list",
                    "arguments": {},
                    "_meta": self.ENVELOPE,
                },
            ),
            heard.put,
        )

        assert isinstance(listed, McpResultOutcome), listed
        names: list[str] = []
        for tool in mt.ListToolsResult.model_validate(listed.result).tools:
            names.append(tool.name)

        assert sorted(names) == ["connection_list", "connection_search"], names
        assert isinstance(called, McpResultOutcome), called
        answer = mt.CallToolResult.model_validate(called.result)
        assert answer.is_error is False, answer
        assert "orders" in str(answer.content), answer
        assert answer.meta is not None, "the step name keeps the call duration"
        assert isinstance(answer.meta.get(ResultWire.ELAPSED), int), answer.meta

    async def test_colliding_tool_names_are_an_external_error(
        self, call_stand: CallStand, tab: TabSession
    ) -> None:
        refs = StandRefs(call_stand.contexts)
        drain = DevNullTool(refs.ambient)
        tools = refs.tool_stand(())
        tools.adopt(
            [
                ToolHosting().hosted(drain.build()),
                ProbeTools().recorder(WorkflowTool.NAME, WorkflowCall),
            ]
        )
        registry = tools.registry(
            tools.access(RelayStand.ROLE, SignInStand.PROFILE), ()
        )
        streaming = StreamSpecs({DevNullTool.NAME: drain.spec()})
        own = frozenset({WorkflowTool.NAME})
        chat = ChatTools(
            replace(registry, own=own, specs=streaming), refs.runs, RelayStand.VERSION
        )
        credentials = KerberosCredentialSource(None, NoRefresh())
        stand = RelayStand(
            NoConnectionStore(), lambda: credentials, call_stand.contexts
        )
        servers = McpServers(McpServersConfig(servers={}))
        relay = stand.relay_with(servers, chat, stand.canvas)
        registration = tab.registration(uuid4(), SignInStand.PROFILE)
        server_id = stand.registrations.register(registration, [ChatTools.NAME])[
            0
        ].server_id
        message = McpMessage(
            server_id=server_id,
            request_id="mcp:listing",
            method="tools/list",
            params={"_meta": self.ENVELOPE},
        )

        with pytest.raises(RequestError) as refused:
            await relay.answer(message, asyncio.Queue[McpMessage]().put)

        assert refused.value.code == AcpErrorCode.BACKEND_FAILED
        assert f"tool {WorkflowTool.NAME!r} is offered by two servers" in str(
            refused.value
        )


class TestRegistrations:
    """Регистрации серверов процесса агента: свой serverId на каждый сервер
    до снятия, профиль треда в субъекте и актуальность входа пользователя —
    роли и делегированный билет kerberos читаются из живой сессии в момент
    вызова."""

    async def test_each_server_gets_its_own_server_id_until_revoked(self) -> None:
        registrations = McpRegistrations()
        registration = TabSession().registration(uuid4(), SignInStand.PROFILE)

        servers = registrations.register(registration, ["boba_general", "chat"])
        ids: list[str] = []
        for server in servers:
            ids.append(server.server_id)

        names: list[str] = []
        kinds: list[str] = []
        for server in servers:
            names.append(server.name)
            kinds.append(server.type)

        assert names == ["boba_general", "chat"]
        assert kinds == ["acp", "acp"]
        assert len(set(ids)) == 2
        for server in servers:
            registered = registrations.of(server.server_id)
            assert registered.server == server.name
            assert registered.thread_id == registration.thread_id

        registrations.revoke(ids[0])
        with pytest.raises(RequestError) as revoked:
            registrations.of(ids[0])

        assert revoked.value.code == AcpErrorCode.UNKNOWN_SERVER
        assert registrations.of(ids[1]).server == "chat"

    async def test_subject_takes_the_profile_of_the_thread(
        self, auth_token: str
    ) -> None:
        registrations = McpRegistrations()
        tab = TabSession()
        tab.password_sign_in(frozenset({"DEV"}))
        user_id = uuid4()
        server = registrations.register(tab.registration(user_id, "search"), ["chat"])[
            0
        ]

        context = registrations.of(server.server_id).context()

        assert context.subject.user_id == user_id
        assert context.subject.login == TabSession.LOGIN
        assert context.subject.roles == frozenset({"DEV"})
        assert context.subject.profile == "search"
        assert isinstance(context.credential, NoUserCredential)

    async def test_credential_follows_the_renewed_session_token(
        self, auth_token: str
    ) -> None:
        registrations = McpRegistrations()
        tab = TabSession()
        tab.sign_in(frozenset({"DEV"}), "sealed-ticket-at-launch")
        server = registrations.register(
            tab.registration(uuid4(), SignInStand.PROFILE), ["chat"]
        )[0]
        registration = registrations.of(server.server_id)
        launched = registration.context()

        tab.sign_in(frozenset({"DEV", "ADM"}), "sealed-ticket-renewed")
        renewed = registration.context()

        assert isinstance(launched.credential, DelegatedTicket)
        assert launched.credential.sealed == "sealed-ticket-at-launch"
        assert isinstance(renewed.credential, DelegatedTicket)
        assert renewed.credential.sealed == "sealed-ticket-renewed"
        assert renewed.credential.principal == TabSession.PRINCIPAL
        assert renewed.subject.roles == frozenset({"DEV", "ADM"})

    async def test_registration_without_a_sign_in_token_is_refused(self) -> None:
        registrations = McpRegistrations()
        registration = TabSession().registration(uuid4(), SignInStand.PROFILE)
        server = registrations.register(registration, ["chat"])[0]

        with pytest.raises(AuthenticationError, match="no sign-in token"):
            registrations.of(server.server_id).context()
