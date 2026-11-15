"""ACP v2: запросы клиента, SQL-проекции истории и несохранённые чанки."""

from __future__ import annotations

import json
from typing import Any, ClassVar, Literal, Never

from acp import RequestError
from acp.experimental.v2 import Agent as SdkAgent
from acp.experimental.v2 import schema
from psycopg.types.json import Jsonb
from pydantic import AnyUrl

from boba.agent import models as m
from boba.agent.acp_server import AcpOperations
from boba.agent.acp_validator import AcpVersion, TextMessageChunk
from boba.agent.queries import Query
from boba.agent.runtime import SavedBatch, SessionProtocol, SessionSnapshot, TextDelta
from boba.agent.tools import content_parts


class AcpV2(SdkAgent, SessionProtocol):
    """Обработчик одного подключения v2; AcpAgent выбирает класс при initialize."""

    version: ClassVar[AcpVersion] = AcpVersion.V2

    def __init__(self, operations: AcpOperations) -> None:
        self._operations = operations
        self._store = operations.hub.store

    async def initialize(
        self,
        protocol_version: int,
        info: schema.Implementation,
        capabilities: schema.ClientCapabilities | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        self._operations.capabilities = {}
        if capabilities is not None:
            self._operations.capabilities = capabilities.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )

        return schema.InitializeResponse(
            protocol_version=2,
            info=schema.Implementation(name="boba", version=self._operations.version),
            capabilities=schema.AgentCapabilities.model_validate(
                {
                    "providers": {},
                    "session": {
                        "prompt": {"image": {}, "embeddedContext": {}},
                        "mcp": {"acp": {}},
                        "delete": {},
                        "fork": {},
                    },
                }
            ),
            auth_methods=[],
        )

    async def list_providers(self, **kwargs: Any) -> Any:
        async with self._operations.errors():
            return await self._operations.list_providers(Query.ACP_PROVIDERS)

    async def set_provider(
        self,
        provider_id: str,
        api_type: str,
        base_url: str | AnyUrl,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        async with self._operations.errors():
            await self._operations.set_provider(
                provider_id, api_type, str(base_url), headers
            )

            return {}

    async def disable_provider(self, provider_id: str, **kwargs: Any) -> Any:
        async with self._operations.errors():
            await self._operations.disable_provider(provider_id)

            return {}

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[
            schema.HttpMcpServer
            | schema.AcpMcpServer
            | schema.StdioMcpServer
            | schema.OtherMcpServer
        ]
        | None = None,
        **kwargs: Any,
    ) -> Any:
        operations = self._operations
        async with operations.errors():
            additional = additional_directories
            if additional is None:
                additional = []
            agent = await operations.hub.create(
                operations.owner, operations.granted, cwd, additional=additional
            )
            async with operations.created_session(
                agent, self._servers(mcp_servers), self
            ):
                return (await self._setup(agent.session_id))["created"]

    async def delete_session(self, session_id: str, **kwargs: Any) -> Any:
        operations = self._operations
        async with operations.errors():
            await operations.hub.delete(session_id, operations.owner)
            await operations.unsubscribe(session_id)

            return {}

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[
            schema.HttpMcpServer
            | schema.AcpMcpServer
            | schema.StdioMcpServer
            | schema.OtherMcpServer
        ]
        | None = None,
        **kwargs: Any,
    ) -> Any:
        operations = self._operations
        async with operations.errors():
            additional = additional_directories
            if additional is None:
                additional = []

            agent = await operations.hub.fork(
                session_id, operations.owner, operations.granted, cwd, additional
            )
            async with operations.created_session(
                agent, self._servers(mcp_servers), self
            ):
                return (await self._setup(agent.session_id))["created"]

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[
            schema.HttpMcpServer
            | schema.AcpMcpServer
            | schema.StdioMcpServer
            | schema.OtherMcpServer
        ]
        | None = None,
        replay_from: schema.ReplayFromStartVariant
        | schema.OtherReplayFrom
        | None = None,
        **kwargs: Any,
    ) -> Any:
        operations = self._operations
        async with operations.errors():
            if replay_from is not None:
                if replay_from.type != "start":
                    raise ValueError(f"Unsupported replay cursor: {replay_from.type}")
            agent = await operations.restore(session_id, cwd)
            await operations.open(
                agent,
                self._servers(mcp_servers),
                self,
                replay_history=replay_from is not None,
            )
            return (await self._setup(session_id))["setup"]

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> Any:
        async with self._operations.errors():
            rows = await self._store.rows(Query.SESSION_LIST, self._operations.owner)
            return rows[0]["response"]

    async def prompt(
        self,
        session_id: str,
        prompt: list[
            schema.TextContentBlock
            | schema.ImageContentBlock
            | schema.AudioContentBlock
            | schema.ResourceContentBlock
            | schema.EmbeddedResourceContentBlock
            | schema.OtherContentBlock
        ],
        **kwargs: Any,
    ) -> schema.PromptResponse:
        operations = self._operations
        async with operations.errors():
            blocks = []
            for block in prompt:
                blocks.append(
                    block.model_dump(mode="json", by_alias=True, exclude_none=True)
                )
            content = content_parts(blocks)
            agent = await operations.hub.get(
                session_id, operations.owner, operations.granted
            )
            match content:
                case [m.TextPart(text=text)] if text.startswith("/"):
                    message_id = await operations.command(agent, text)
                case _:
                    message_id = await agent.submit(content)
            return schema.PromptResponse(message_id=message_id)

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: Any,
        *,
        type: str | None = None,
        **kwargs: Any,
    ) -> Any:
        operations = self._operations
        async with operations.errors():
            agent = await operations.hub.get(
                session_id, operations.owner, operations.granted
            )
            await operations.set_option(agent, config_id, value)
            return (await self._setup(session_id))["configured"]

    async def cancel_session(self, session_id: str, **kwargs: Any) -> None:
        operations = self._operations
        async with operations.errors():
            agent = await operations.hub.get(
                session_id, operations.owner, operations.granted
            )
            await agent.cancel()

    async def close_session(self, session_id: str, **kwargs: Any) -> Any:
        operations = self._operations
        async with operations.errors():
            await operations.hub.get(session_id, operations.owner, operations.granted)
            await operations.unsubscribe(session_id)
            return {}

    async def mcp_message(
        self,
        connection_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self._operations.mcp_message(connection_id, method, params)

    async def notify_mcp(
        self,
        connection_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if method != "notifications/tools/list_changed":
            return
        operations = self._operations
        async with operations.errors():
            for sid in operations.subscriptions:
                agent = await operations.hub.get(
                    sid, operations.owner, operations.granted
                )
                await operations.hub.tools.refresh(agent)

    def _servers(self, servers) -> dict:
        values = []
        if servers is not None:
            for server in servers:
                values.append(
                    server.model_dump(mode="json", by_alias=True, exclude_none=True)
                )
        return {"mcpServers": values}

    async def _setup(self, sid: str) -> dict:
        options = await self._store.acp(Query.ACP_OPTIONS, sid, None)
        commands = await self._store.acp(Query.ACP_COMMANDS, sid, None)
        rows = await self._store.rows(
            Query.ACP_SESSION, sid, Jsonb(options["update"]), Jsonb(commands["update"])
        )
        return rows[0]

    def _frame(self, sid: str, update: dict) -> tuple[str, dict]:
        return "session/update", {"sessionId": sid, "update": update}

    def _history_frames(self, draft: dict) -> list[tuple[str, dict]]:
        frames = []
        for row in draft["notifications"]:
            notification = row["notification"]
            frames.append((notification["method"], notification["params"]))
        return frames

    async def initial(self, snapshot: SessionSnapshot) -> list[tuple[str, dict]]:
        sid = snapshot.session_id
        frames = []
        if snapshot.replay_history:
            draft = await self._store.acp(
                Query.ACP_HISTORY, sid, snapshot.after_seq, snapshot.through_seq, True
            )
            frames.extend(self._history_frames(draft))
            state = await self._store.acp(Query.ACP_STATE, sid, snapshot.through_seq)
            frames.append(self._frame(sid, state["replay_complete"]))
        commands = await self._store.acp(Query.ACP_COMMANDS, sid, snapshot.through_seq)
        options = await self._store.acp(Query.ACP_OPTIONS, sid, snapshot.through_seq)
        frames.append(self._frame(sid, commands["update"]))
        frames.append(self._frame(sid, options["update"]))
        frames.extend(self.state(sid, snapshot.running))
        for delta in snapshot.partial:
            frames.append(self.chunk(sid, delta))
        return frames

    async def saved(self, batch: SavedBatch) -> list[tuple[str, dict]]:
        draft = await self._store.acp(
            Query.ACP_HISTORY,
            batch.session_id,
            batch.after_seq,
            batch.through_seq,
            False,
        )
        frames = self._history_frames(draft)
        for record_id in batch.notices:
            notice = await self._store.acp(
                Query.ACP_NOTICE, batch.session_id, record_id
            )
            for update in notice["updates"]:
                frames.append(self._frame(batch.session_id, update))
        return frames

    async def request_action(self, session_id: str, record: m.HistoryRecord) -> dict:
        match record:
            case m.PermissionRequestRecord():
                draft = await self._store.acp(
                    Query.ACP_PERMISSION, session_id, record.record_id
                )
            case m.ElicitationRequestRecord():
                mode = record.payload.mode
                if mode not in self._operations.capabilities.get("elicitation", {}):
                    raise ValueError("Client does not support this elicitation mode")
                draft = await self._store.acp(
                    Query.ACP_ELICITATION, session_id, record.record_id
                )
            case _:
                raise ValueError(f"Unsupported client action record: {record.kind}")

        return await self._operations.connection.send_request(
            draft["method"], draft["params"]
        )

    async def options(self, session_id: str) -> list[tuple[str, dict]]:
        draft = await self._store.acp(Query.ACP_OPTIONS, session_id, None)
        return [self._frame(session_id, draft["update"])]

    def chunk(self, session_id: str, delta: TextDelta) -> tuple[str, dict]:
        channel: Literal["agent_message_chunk", "agent_thought_chunk"] = (
            "agent_message_chunk"
        )
        if delta.kind == "reasoning":
            channel = "agent_thought_chunk"
        update = TextMessageChunk(
            sessionUpdate=channel,
            messageId=json.dumps(
                [delta.record_id, [delta.position]], ensure_ascii=False
            ),
            content=schema.TextContentBlock(text=delta.text),
        )
        return self._frame(
            session_id, update.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    def state(self, session_id: str, running: bool) -> list[tuple[str, dict]]:
        state = "idle"
        if running:
            state = "running"
        return [
            self._frame(session_id, {"sessionUpdate": "state_update", "state": state})
        ]

    async def _unsupported(self, *args: Any, **kwargs: Any) -> Never:
        raise RequestError.method_not_found("ACP operation is not supported by Boba")

    login = _unsupported
    logout = _unsupported
    start_nes = _unsupported
    suggest_nes = _unsupported
    accept_nes = _unsupported
    reject_nes = _unsupported
    close_nes = _unsupported
    did_open = _unsupported
    did_change = _unsupported
    did_close = _unsupported
    did_save = _unsupported
    did_focus = _unsupported
