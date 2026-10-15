"""Чужой агент ACP v2 для тестов чата: о boba не знает.

Запускается субпроцессом из тестов агента вкладки:
`python foreign_agent.py --settings <путь>` (файл настроек чата не читает).
Принимает серверы чата записями type "acp", открывает сессии и отвечает на
вопрос сразу пустым ходом; ветку session/fork держит, но среза по messageId
вопроса в `capabilities.session.fork._meta` не объявляет.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from acp.experimental.v2 import schema
from acp.experimental.v2.agent import AgentSideConnection, run_agent


class ForeignAgent:
    """Агент без расширений boba: сессия — id, ход — сразу idle."""

    PROTOCOL_VERSION: int = 2

    def __init__(self) -> None:
        self._connection: AgentSideConnection | None = None
        self._turns: set[asyncio.Task[None]] = set()

    def on_connect(self, connection: AgentSideConnection) -> None:
        self._connection = connection

    async def initialize(
        self,
        protocol_version: int,
        info: schema.Implementation,
        capabilities: schema.ClientCapabilities | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        session = schema.SessionCapabilities(
            prompt=schema.PromptCapabilities(),
            mcp=schema.McpCapabilities(acp=schema.McpAcpCapabilities()),
            fork=schema.SessionForkCapabilities(),
        )

        return schema.InitializeResponse(
            protocol_version=self.PROTOCOL_VERSION,
            info=schema.Implementation(name="foreign", version="1"),
            capabilities=schema.AgentCapabilities(session=session),
        )

    async def new_session(self, **kwargs: Any) -> schema.NewSessionResponse:
        return schema.NewSessionResponse(session_id=str(uuid4()))

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse:
        return schema.CloseSessionResponse()

    async def cancel_session(self, session_id: str, **kwargs: Any) -> None:
        return

    async def prompt(
        self, session_id: str, prompt: list[Any], **kwargs: Any
    ) -> schema.PromptResponse:
        turn = asyncio.create_task(self._idle(session_id))
        self._turns.add(turn)
        turn.add_done_callback(self._turns.discard)

        return schema.PromptResponse(message_id=str(uuid4()))

    async def _idle(self, session_id: str) -> None:
        connection = self._connection
        if connection is None:
            return

        await asyncio.sleep(0.05)
        idle = schema.IdleSessionStateUpdate(stop_reason="end_turn")
        await connection.session_update(session_id=session_id, update=idle)


if __name__ == "__main__":
    asyncio.run(run_agent(ForeignAgent()))
