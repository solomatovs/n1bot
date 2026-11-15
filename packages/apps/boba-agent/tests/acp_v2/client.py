from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

from acp._transport import memory_transport_pair
from acp.connection import Connection, StreamDirection, StreamEvent
from acp.experimental.v2 import schema
from agent_stand import AgentSystem, McpServer

from boba.agent.acp_connection import AcpAgent
from boba.agent.acp_server import SessionHub
from boba.agent.acp_validator import (
    AcpVersion,
    MessageChunk,
    MessageUpdate,
    NotificationParams,
)


class V2Client:
    """Клиент ACP v2: собственные SDK-модели, входящие события и жизненный цикл."""

    def __init__(
        self, hub: SessionHub, owner: str, timeout: float, granted: frozenset[str]
    ) -> None:
        self.timeout = timeout
        self.updates: list[NotificationParams] = []
        self.wire: list[dict[str, Any]] = []
        self.permissions: list[schema.RequestPermissionRequest] = []
        self.elicitations: list[schema.CreateFormSessionElicitationRequest] = []
        self.permission = "allow"
        self.changed = asyncio.Condition()
        self.adapter = AcpAgent(
            hub, owner, granted, "test", maximum_protocol_version=AcpVersion.V2
        )
        agent_end, client_end = memory_transport_pair()
        self.server = self.adapter.connect(agent_end)
        self.connection = Connection(self.receive, client_end)
        self.connection.add_observer(self.observe)
        self.mcp = McpServer(self.connection)

    async def initialize(self) -> None:
        request = schema.InitializeRequest.model_validate(
            {
                "protocolVersion": 2,
                "info": {"name": "agent-v2-test", "version": "1"},
                "capabilities": {"elicitation": {"form": {}}},
            }
        )
        result = await self.connection.send_request(
            "initialize",
            request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        response = schema.InitializeResponse.model_validate(result)
        assert response.protocol_version == 2

    def observe(self, event: StreamEvent) -> None:
        if event.direction is StreamDirection.INCOMING:
            self.wire.append(event.message)

    def received_kinds(self) -> Iterator[str]:
        for frame in self.wire:
            if "result" in frame:
                yield "response"
                continue

            if frame.get("method") == "session/update":
                yield frame["params"]["update"]["sessionUpdate"]

    async def receive(self, method: str, params: Any, is_notification: bool) -> Any:
        match method:
            case "session/update":
                assert is_notification

                schema.UpdateSessionNotification.model_validate(params)
                update = NotificationParams.model_validate(params)
                async with self.changed:
                    self.updates.append(update)
                    self.changed.notify_all()

                return None

            case "session/request_permission":
                assert not is_notification

                permission = schema.RequestPermissionRequest.model_validate(params)
                self.permissions.append(permission)

                return {"outcome": {"outcome": "selected", "optionId": self.permission}}

            case "elicitation/create":
                assert not is_notification

                question = schema.CreateFormSessionElicitationRequest.model_validate(
                    params
                )
                self.elicitations.append(question)

                return {"action": "accept", "content": {"confirm": True}}

            case _:
                return await self.mcp.receive(method, params, is_notification)

    async def new_session(self) -> str:
        result = await self.connection.send_request(
            "session/new",
            {
                "cwd": "/workspace",
                "mcpServers": [
                    {"type": "acp", "name": "workspace", "serverId": "workspace"}
                ],
            },
        )
        parsed = schema.NewSessionResponse.model_validate(result)

        await self.until("state_update")
        return parsed.session_id

    async def resume(self, session_id: str) -> None:
        result = await self.connection.send_request(
            "session/resume",
            {
                "sessionId": session_id,
                "cwd": "/workspace",
                "replayFrom": {"type": "start"},
            },
        )
        schema.ResumeSessionResponse.model_validate(result)
        await self.until("replay_complete")

    async def prompt(self, session_id: str, text: str) -> str:
        result = await self.connection.send_request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        )

        return schema.PromptResponse.model_validate(result).message_id

    async def until(self, kind: str, *, message_id: str | None = None) -> None:
        async with asyncio.timeout(self.timeout), self.changed:
            await self.changed.wait_for(lambda: self._received(kind, message_id))

    def _received(self, kind: str, message_id: str | None) -> bool:
        for item in self.updates:
            if item.update.get("sessionUpdate") != kind:
                continue

            if message_id is None:
                return True

            if item.update.get("messageId") == message_id:
                return True

        return False

    def messages(self, kind: str) -> tuple[MessageUpdate, ...]:
        selected = (
            MessageUpdate.model_validate(item.update)
            for item in self.updates
            if item.update.get("sessionUpdate") == kind
        )

        return tuple(selected)

    def chunks(self) -> tuple[MessageChunk, ...]:
        selected = (
            MessageChunk.model_validate(item.update)
            for item in self.updates
            if item.update.get("sessionUpdate") == "agent_message_chunk"
        )

        return tuple(selected)

    async def close(self) -> None:
        self.mcp.release_tool.set()
        self.mcp.connect_ready.set()
        await self.adapter.close_all()
        await self.connection.close()
        await self.server.close()


class V2System(AgentSystem):
    """Стенд создаёт только клиентов ACP v2; хранилище и инференс общие."""

    async def client(
        self, *, owner: str | None = None, granted: frozenset[str] = frozenset({"test"})
    ) -> V2Client:
        if owner is None:
            owner = self.owner

        self.owners.add(owner)
        client = V2Client(self.hub, owner, self.timeout, granted)
        self.connections.push_async_callback(client.close)
        await client.initialize()

        return client
