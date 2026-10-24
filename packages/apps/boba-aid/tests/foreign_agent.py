"""Чужой агент ACP v2 для тестов чата: о boba не знает.

Запускается субпроцессом из тестов агента вкладки:
`python foreign_agent.py [--silent-sessions] --settings <путь>` (файл
настроек чата не читает). Принимает серверы чата записями type "acp",
открывает сессии и отвечает на вопрос сразу пустым ходом — эхо вопроса
`user_message` и idle; вопрос с маркером `notice:` перед idle присылает
пометку `notice` с текстом после маркера, вопрос с маркером `detach`
закрывает свой вывод и ход не кончает, с маркером `answer-late` ответ на
session/prompt приходит через LATE_SEC, как на вопрос, вставленный после
идущего хода, а с ним и `exit-before-answer` процесс вместо ответа выходит. Ветку
session/fork держит, но среза по
messageId вопроса в `capabilities.session.fork._meta` не объявляет; с
`--silent-sessions` на session/new не отвечает вовсе.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from uuid import uuid4

from acp.experimental.v2 import Agent, schema
from acp.experimental.v2.agent import AgentSideConnection, run_agent


class ForeignAgent(Agent):
    """Реализация Agent ACP v2 без расширений boba: сессия — id, ход —
    сразу idle."""

    PROTOCOL_VERSION: int = 2
    SILENT: str = "--silent-sessions"
    NOTICE: str = "notice:"
    DETACH: str = "detach"
    LATE: str = "answer-late"
    EXIT: str = "exit-before-answer"
    LATE_SEC: float = 2.0

    # SDK считает метод протокола неподдержанным
    login = Agent.login
    logout = Agent.logout
    list_providers = Agent.list_providers
    set_provider = Agent.set_provider
    disable_provider = Agent.disable_provider
    list_sessions = Agent.list_sessions
    delete_session = Agent.delete_session
    fork_session = Agent.fork_session
    resume_session = Agent.resume_session
    set_config_option = Agent.set_config_option
    mcp_message = Agent.mcp_message
    notify_mcp = Agent.notify_mcp
    start_nes = Agent.start_nes
    suggest_nes = Agent.suggest_nes
    accept_nes = Agent.accept_nes
    reject_nes = Agent.reject_nes
    close_nes = Agent.close_nes
    did_open = Agent.did_open
    did_change = Agent.did_change
    did_close = Agent.did_close
    did_save = Agent.did_save
    did_focus = Agent.did_focus

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
        if self.SILENT in sys.argv:
            await asyncio.Event().wait()

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
        text = self._text(prompt)
        if self.LATE in text:
            await asyncio.sleep(self.LATE_SEC)

        if self.EXIT in text:
            os._exit(1)

        message_id = str(uuid4())
        turn = asyncio.create_task(self._idle(session_id, message_id, prompt))
        self._turns.add(turn)
        turn.add_done_callback(self._turns.discard)

        return schema.PromptResponse(message_id=message_id)

    async def _idle(self, session_id: str, message_id: str, prompt: list[Any]) -> None:
        connection = self._connection
        if connection is None:
            return

        await asyncio.sleep(0.05)
        echo = schema.UserMessageUpdate(message_id=message_id, content=prompt)
        await connection.session_update(session_id=session_id, update=echo)
        text = self._text(prompt)
        if self.DETACH in text:
            os.close(sys.stdout.fileno())
            await asyncio.Event().wait()

        _, marked, notice = text.partition(self.NOTICE)
        if marked:
            update = schema.SessionNotice(severity="error", title=notice.strip())
            await connection.session_update(session_id=session_id, update=update)

        idle = schema.IdleSessionStateUpdate(stop_reason="end_turn")
        await connection.session_update(session_id=session_id, update=idle)

    @staticmethod
    def _text(prompt: list[Any]) -> str:
        texts: list[str] = []
        for block in prompt:
            if isinstance(block, schema.TextContentBlock):
                texts.append(block.text)

        return " ".join(texts)


if __name__ == "__main__":
    asyncio.run(run_agent(ForeignAgent()))
