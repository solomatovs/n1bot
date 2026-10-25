"""Методы MCP 2026-07-28 настоящим путём: `boba --stdio` v2 с фейковой моделью →
ретранслятор чата (mcp/message) → обычный сервер на SDK mcp по stdio.

Сервер стандартный (standard_server.py клиента MCP): на нём есть каждый
запрос и каждое уведомление редакции. Агент согласует его server/discover
(инструкции — вложением хода), читает инструменты и промпты, держит поток
subscriptions/listen смены списков через ретранслятор и поднимает его снова,
когда сервер вернулся после обрыва, читает
ресурсы своими инструментами и раскрывает промпт сервера по вопросу-команде;
ретранслятор передаёт серверу личность агента. Что агент предложил модели и
что она получила, видно по запросам фейковой модели. Методы, которые агент
не зовёт сам (completion/complete, подписка на ресурс, просьбы о вводе в
resources/read и prompts/get), проверяются запросом mcp/message прямо в
ретранслятор, как их пришлёт агент.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import mcp_types as mt
import pytest
from acp.experimental.v2 import schema
from acp_stand import Agents, FakeModel, ModelRequests, RecordingUpdates
from aid_stand import TabSession

from boba.aid.agent.client import AgentProcess, AgentRequestError
from boba.aid.agent.relay import McpRelay
from boba.mcp_client.client import McpServerConfig, McpServersConfig
from boba.stand.signin import SignInStand
from boba.stand.site import Stand
from boba.stand.ui.fake_llm import ScenarioName
from boba.transport.acp.protocol import (
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
    McpVersion,
)

pytestmark = pytest.mark.anyio

SERVER = "standard"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Контекст chainlit этим тестам не нужен: они живут ниже обработчиков."""


@pytest.fixture
async def agents(
    tmp_path: Path,
    model: FakeModel,
    stand: Stand,
    standard: McpServerConfig,
    auth_token: str,
) -> AsyncIterator[Agents]:
    """Процессы агента над обычным сервером; auth_token — секрет входа
    стенда: им подписан вход вкладки, чью регистрацию получает агент."""
    made = Agents(
        tmp_path / "agents",
        model,
        stand,
        McpServersConfig(servers={SERVER: standard}),
    )
    try:
        yield made
    finally:
        await made.close_all()


class TestAgentThroughRelay:
    """Агент зовёт сервер только через ретранслятор чата; каждый метод
    редакции, которым он пользуется, проверяется по его следу у модели."""

    INSTRUCTIONS: ClassVar[str] = (
        "<system-reminder>\n"
        "# MCP Server Instructions\n\n"
        "The following MCP servers have provided instructions for how to use "
        "their tools and resources:\n\n"
        f"## {SERVER}\n"
        "Call add for sums; read standard://notes first.\n"
        "</system-reminder>"
    )
    """Вложение с инструкциями сервера, как его читает модель."""

    @staticmethod
    def _call(name: str, arguments: Mapping[str, Any]) -> str:
        spec = {"name": name, "arguments": dict(arguments)}

        return f"{ScenarioName.CALL.value} {json.dumps(spec)}"

    async def _session(
        self, agents: Agents, thread: str
    ) -> tuple[AgentProcess, RecordingUpdates, str]:
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))
        servers = agents.registrations.register(
            tab.registration(uuid4(), SignInStand.PROFILE), [SERVER]
        )
        updates = RecordingUpdates()
        process = await agents.start(updates, thread, servers)
        opened = await process.open_session()

        return process, updates, opened.session_id

    async def test_discover_gives_the_instructions_as_an_attachment_of_the_turn(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """server/discover: инструкции сервера модель читает вложением
        mcp_instructions_delta первого хода дословно по docs/prompt_assembly,
        системный промпт их не несёт."""
        process, _, session_id = await self._session(agents, "discover")

        _, idle = await agents.turn(process, session_id, ScenarioName.ANSWER.value)

        requests = ModelRequests(model)
        system = requests.system()
        assert idle.stop_reason == "end_turn"
        assert "# MCP Server Instructions" not in system, system
        assert requests.saw(self.INSTRUCTIONS), requests.read()

    async def test_server_sees_the_client_info_of_the_agent(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """tools/call: ретранслятор несёт серверу clientInfo и возможности
        агента, а сверху — расширения, которые исполняет сам чат. Чат
        объявил агенту формы, поэтому агент объявляет серверу elicitation
        ровно с формой: ссылку (url) он не исполняет."""
        process, _, session_id = await self._session(agents, "client-info")

        await agents.turn(process, session_id, self._call("mcp__standard__client", {}))

        seen = json.loads(ModelRequests(model).tool_result())
        assert seen["info"]["name"] == "boba", seen
        capabilities = seen["capabilities"]
        assert capabilities["elicitation"] == {"form": {}}, capabilities
        assert "com.boba/connection-seal" in json.dumps(capabilities), capabilities

    async def test_new_tool_of_the_server_is_offered_after_the_list_changes(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """subscriptions/listen и tools/list_changed: сервер добавил
        инструмент — агент перечитал список, модель видит его в следующем
        ходе, вызов уходит серверу."""
        process, _, session_id = await self._session(agents, "tools-changed")
        requests = ModelRequests(model)

        await agents.turn(
            process, session_id, self._call("mcp__standard__toggle_extra", {})
        )
        before = requests.offered().named("extra")
        offered = await self._offered_after(
            agents, process, session_id, requests, "mcp__standard__extra"
        )
        await agents.turn(process, session_id, self._call("mcp__standard__extra", {}))

        assert before == "extra", "the first turn knew the old list"
        assert offered == "mcp__standard__extra"
        assert "extra answered" in requests.read()

    async def _offered_after(
        self,
        agents: Agents,
        process: AgentProcess,
        session_id: str,
        requests: ModelRequests,
        name: str,
    ) -> str:
        """Ходы до того, как модели предложат инструмент name: поток смены
        списка приходит своим путём, а не ответом на вызов."""
        offered = ""
        for _ in range(20):
            await agents.turn(process, session_id, ScenarioName.ANSWER.value)
            offered = requests.offered().named(name)
            if offered == name:
                return offered

            await asyncio.sleep(0.2)

        return offered

    async def test_prompt_changed_after_the_server_drops_reaches_its_command(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """Сервер упал и вернулся к следующему ходу: смена его промптов
        после этого доходит до агента, и вопрос-команда нового промпта
        раскрывается его сообщениями."""
        process, _, session_id = await self._session(agents, "relisten")

        await agents.turn(process, session_id, self._call("mcp__standard__die", {}))
        await agents.turn(process, session_id, ScenarioName.ANSWER.value)
        await agents.turn(
            process, session_id, self._call("mcp__standard__toggle_prompt", {})
        )
        await agents.turn(
            process,
            session_id,
            f"/mcp__standard__extra_prompt {ScenarioName.ANSWER.value}",
        )

        read = ModelRequests(model).read()
        assert "the extra prompt" in read, read

    async def test_resources_are_listed_and_read_by_the_agent_tools(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """resources/list, resources/templates/list и resources/read:
        собственные инструменты агента над ресурсами серверов."""
        process, _, session_id = await self._session(agents, "resources")
        requests = ModelRequests(model)

        await agents.turn(process, session_id, self._call("list_mcp_resources", {}))
        listed = requests.read()
        read_args = {"server": SERVER, "uri": "standard://items/apple"}
        await agents.turn(
            process, session_id, self._call("read_mcp_resource", read_args)
        )
        item = requests.read()
        await agents.turn(
            process,
            session_id,
            self._call(
                "read_mcp_resource", {"server": SERVER, "uri": "standard://notes"}
            ),
        )
        notes = requests.read()

        offered = requests.offered()
        assert offered.named("list_mcp_resources") == "list_mcp_resources"
        assert "standard: standard://notes" in listed, listed
        assert "template standard://items/{item}" in listed, listed
        assert "item apple" in item, item
        assert "the notes of the standard server" in notes, notes

    async def test_resource_asking_for_input_is_an_error_for_the_model(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """resources/read с просьбой о вводе (input_required) доходит до
        агента как есть; повтора с ответами агент не делает, модель получает
        отказ с причиной."""
        process, _, session_id = await self._session(agents, "resource-asks")
        args = {"server": SERVER, "uri": "standard://ask/weather"}

        await agents.turn(process, session_id, self._call("read_mcp_resource", args))

        read = ModelRequests(model).read()
        assert "the server asked for more input" in read, read

    async def test_prompt_of_the_server_is_expanded_by_its_command(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """prompts/list и prompts/get: вопрос-команда с полным именем
        промпта раскрывается сообщениями промпта в служебный контекст
        вопроса. Промпты сервера агент объявляет командами из истории
        сессии вместе со своими."""
        process, updates, session_id = await self._session(agents, "prompts")

        await agents.turn(
            process,
            session_id,
            f"/mcp__standard__greet Bob poem {ScenarioName.ANSWER.value}",
        )

        read = ModelRequests(model).read()
        announced = self._commands(updates)[-1].split()
        assert "compact" in announced, announced
        assert "mcp__standard__greet" in announced, announced
        assert "Greet Bob in the poem" in read, read
        assert "[assistant] Hello, Bob!" in read, read

    async def test_changed_prompt_list_is_read_again_for_its_command(
        self, agents: Agents, model: FakeModel
    ) -> None:
        """notifications/prompts/list_changed: агент перечитал промпты, и
        вопрос-команда нового промпта раскрывается его сообщениями, а не
        уходит модели голым текстом."""
        process, _, session_id = await self._session(agents, "prompts-changed")

        await agents.turn(
            process, session_id, self._call("mcp__standard__toggle_prompt", {})
        )
        await agents.turn(
            process,
            session_id,
            f"/mcp__standard__extra_prompt {ScenarioName.ANSWER.value}",
        )

        read = ModelRequests(model).read()
        assert "the extra prompt" in read, read

    async def test_missing_argument_of_a_command_is_refused(
        self, agents: Agents
    ) -> None:
        """Обязательный аргумент промпта не назван — вопрос отвергнут с
        причиной, ход не начат."""
        process, _, session_id = await self._session(agents, "prompt-refused")
        block = schema.TextContentBlock(text="/mcp__standard__greet")

        with pytest.raises(AgentRequestError, match="argument 'name' is required"):
            await process.prompt(session_id, [block])

    @staticmethod
    def _commands(updates: RecordingUpdates) -> list[str]:
        """Объявления команд сессии по порядку: имена через пробел."""
        announced: list[str] = []
        for _, update in updates.updates:
            if not isinstance(update, schema.AvailableCommandsUpdate):
                continue

            names: list[str] = []
            for command in update.available_commands:
                names.append(command.name)

            announced.append(" ".join(names))

        return announced


class TestRelayedRequests:
    """Запросы, которые агент шлёт серверу как есть, прямо в ретранслятор:
    ответы, просьбы о вводе и поток подписки сервера доходят до агента как
    есть, сервер chat отвечает только на инструменты."""

    ENVELOPE: ClassVar[dict[str, Any]] = {
        mt.PROTOCOL_VERSION_META_KEY: McpVersion.V2026_07_28.value,
        mt.CLIENT_INFO_META_KEY: {"name": "boba", "version": "0"},
        mt.CLIENT_CAPABILITIES_META_KEY: {},
    }

    @pytest.fixture
    def relay(self, agents: Agents) -> McpRelay:
        return agents.relay

    def _server_id(self, agents: Agents, server: str) -> str:
        tab = TabSession()
        tab.password_sign_in(frozenset({"dev"}))
        registration = tab.registration(uuid4(), SignInStand.PROFILE)

        return agents.registrations.register(registration, [server])[0].server_id

    def _message(
        self, server_id: str, method: str, params: dict[str, Any]
    ) -> McpMessage:
        sent = dict(params)
        sent["_meta"] = dict(self.ENVELOPE)

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

    async def test_completion_completes_prompt_and_template_arguments(
        self, relay: McpRelay, agents: Agents
    ) -> None:
        server_id = self._server_id(agents, SERVER)
        prompt = {
            "ref": {"type": "ref/prompt", "name": "greet"},
            "argument": {"name": "style", "value": "p"},
        }
        template = {
            "ref": {"type": "ref/resource", "uri": "standard://items/{item}"},
            "argument": {"name": "item", "value": "ap"},
        }

        styles, _ = await self._answer(
            relay, self._message(server_id, "completion/complete", prompt)
        )
        items, _ = await self._answer(
            relay, self._message(server_id, "completion/complete", template)
        )

        assert isinstance(styles, McpResultOutcome), styles
        assert isinstance(items, McpResultOutcome), items
        assert mt.CompleteResult.model_validate(styles.result).completion.values == [
            "plain",
            "poem",
            "pirate",
        ]
        assert mt.CompleteResult.model_validate(items.result).completion.values == [
            "apple",
            "apricot",
        ]

    async def test_input_required_of_read_and_prompt_reaches_the_agent(
        self, relay: McpRelay, agents: Agents
    ) -> None:
        """Просьба сервера о вводе в resources/read и prompts/get —
        агенту как есть: повтор с ответами — дело агента."""
        server_id = self._server_id(agents, SERVER)

        read, _ = await self._answer(
            relay,
            self._message(
                server_id, "resources/read", {"uri": "standard://ask/weather"}
            ),
        )
        prompt, _ = await self._answer(
            relay, self._message(server_id, "prompts/get", {"name": "ask_prompt"})
        )

        assert isinstance(read, McpResultOutcome), read
        assert isinstance(prompt, McpResultOutcome), prompt
        assert read.result["resultType"] == "input_required", read.result
        assert prompt.result["resultType"] == "input_required", prompt.result

    async def test_missing_resource_is_invalid_params_for_the_agent(
        self, relay: McpRelay, agents: Agents
    ) -> None:
        server_id = self._server_id(agents, SERVER)

        missing, _ = await self._answer(
            relay,
            self._message(server_id, "resources/read", {"uri": "standard://nothing"}),
        )

        assert isinstance(missing, McpErrorOutcome), missing
        assert missing.error.code == mt.INVALID_PARAMS

    async def test_listen_relays_ack_events_and_the_end_of_the_stream(
        self, relay: McpRelay, agents: Agents
    ) -> None:
        """subscriptions/listen: подтверждение сервера, событие роста
        подписанного ресурса под subscriptionId агента; отмена агентом
        закрывает поток к серверу."""
        server_id = self._server_id(agents, SERVER)
        listen = self._message(
            server_id,
            "subscriptions/listen",
            {
                "notifications": {
                    "resourceSubscriptions": ["standard://counter"],
                    "toolsListChanged": True,
                }
            },
        )
        heard: asyncio.Queue[McpMessage] = asyncio.Queue()
        stream = asyncio.create_task(relay.answer(listen, heard.put))
        try:
            async with asyncio.timeout(30):
                ack = await heard.get()
                bump = self._message(
                    server_id, "tools/call", {"name": "bump", "arguments": {}}
                )
                await relay.answer(bump, asyncio.Queue[McpMessage]().put)
                updated = await heard.get()
        finally:
            stream.cancel()

        with pytest.raises(asyncio.CancelledError):
            await stream

        assert ack.method == "notifications/subscriptions/acknowledged", ack
        assert ack.request_id == listen.request_id
        assert ack.params is not None
        assert ack.params["notifications"] == {
            "toolsListChanged": True,
            "resourceSubscriptions": ["standard://counter"],
        }
        meta = ack.params["_meta"]
        assert meta["io.modelcontextprotocol/subscriptionId"] == listen.request_id
        assert updated.method == "notifications/resources/updated", updated
        assert updated.params is not None
        assert updated.params["uri"] == "standard://counter"

    async def test_chat_server_serves_tools_only(
        self, relay: McpRelay, agents: Agents
    ) -> None:
        """Сервер chat объявляет одни инструменты без смены списка;
        ресурсы, промпты, подсказки и потоки у него — -32601."""
        server_id = self._server_id(agents, "chat")

        discovered, _ = await self._answer(
            relay, self._message(server_id, "server/discover", {})
        )
        refused: list[int] = []
        for method, params in self.CHAT_REFUSED:
            outcome, _ = await self._answer(
                relay, self._message(server_id, method, dict(params))
            )
            assert isinstance(outcome, McpErrorOutcome), (method, outcome)
            refused.append(outcome.error.code)

        assert isinstance(discovered, McpResultOutcome), discovered
        result = mt.DiscoverResult.model_validate(discovered.result)
        assert result.capabilities == mt.ServerCapabilities(
            tools=mt.ToolsCapability(list_changed=False)
        )
        assert result.meta is not None
        assert result.meta[mt.SERVER_INFO_META_KEY]["name"] == "chat"
        assert refused == [mt.METHOD_NOT_FOUND] * len(self.CHAT_REFUSED)

    CHAT_REFUSED: ClassVar[Sequence[tuple[str, Mapping[str, Any]]]] = (
        ("resources/list", {}),
        ("resources/templates/list", {}),
        ("resources/read", {"uri": "chat://x"}),
        ("prompts/list", {}),
        ("prompts/get", {"name": "x"}),
        (
            "completion/complete",
            {
                "ref": {"type": "ref/prompt", "name": "x"},
                "argument": {"name": "a", "value": ""},
            },
        ),
        ("subscriptions/listen", {"notifications": {"toolsListChanged": True}}),
    )
