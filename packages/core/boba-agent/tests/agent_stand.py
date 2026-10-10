"""Стенд агента для тестов: фейковый провайдер в процессе и сборка агента.

Поднимает FakeLlmApp сервером uvicorn внутри цикла событий, строит профиль
модели на один из двух протоколов и собирает Agent на хранилище JSONL
во временном каталоге. События агента копятся в списке.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import uvicorn

from boba.agent.agent import Agent, AgentParts
from boba.agent.events import AgentEvent, EventSink
from boba.agent.history import SessionId
from boba.agent.profile import (
    CompactionSettings,
    ModelProfile,
    ReasoningReturn,
    TurnLimits,
)
from boba.agent.session import Session
from boba.history.jsonl import JsonlHistoryStore
from boba.llm.chat import ChatModel
from boba.llm.http.ollama import OllamaProvider
from boba.llm.http.openai import OpenAiProvider
from boba.llm.providers import (
    ChatModelConfig,
    LlmProvider,
    LlmProviders,
    LlmProviderTypes,
)
from boba.stand.ui.fake_llm import FakeLlmApp
from boba.stand.ui.stand import free_port
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import HttpConnection, NoneAuth, UrlScheme

VERSION = "0.0.26.dev4"
SYSTEM_PROMPT = ("You are the stand agent.", "Answer briefly.")


class Protocol(StrEnum):
    OPENAI = "openai"
    OLLAMA = "ollama"

    @property
    def path(self) -> str:
        if self is Protocol.OPENAI:
            return "/v1"

        return "/"


class Collected(EventSink):
    """Реализация EventSink: копит события в списке."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def emit(self, event: AgentEvent) -> None:
        self.events.append(event)

    def of(self, kind: type[AgentEvent]) -> list[AgentEvent]:
        found: list[AgentEvent] = []
        for event in self.events:
            if isinstance(event, kind):
                found.append(event)

        return found


class FakeServer:
    """FakeLlmApp под uvicorn в текущем цикле событий."""

    def __init__(self, token_delay_sec: float = 0.0, hang_sec: float = 3.0) -> None:
        self.app = FakeLlmApp(token_delay_sec=token_delay_sec, hang_sec=hang_sec)
        self.port = free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app.asgi(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=1,
            )
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task

    def requests(self) -> list[dict[str, object]]:
        return list(self.app.requests)


class StandAgents:
    """Собирает агентов на стенд: один транспорт на все."""

    TIMEOUT_SEC: ClassVar[float] = 1.0

    def __init__(self, server: FakeServer, root: Path) -> None:
        self._server = server
        self._root = root
        self._providers = LlmProviders(LlmProviderTypes.installed())

    def profile(
        self,
        protocol: Protocol,
        system_turns: bool = False,
        reasoning: ReasoningReturn = ReasoningReturn.NEVER,
    ) -> ModelProfile:
        connection = HttpConnection(
            scheme=UrlScheme.HTTP,
            host="127.0.0.1",
            port=self._server.port,
            path=protocol.path,
            timeout_sec=self.TIMEOUT_SEC,
            retry_attempts=1,
            auth=NoneAuth(method="none"),
        )
        provider: LlmProvider
        if protocol is Protocol.OPENAI:
            provider = OpenAiProvider(
                kind="openai", connection=connection, transport=HttpTransportConfig()
            )
        else:
            provider = OllamaProvider(
                kind="ollama", connection=connection, transport=HttpTransportConfig()
            )

        return ModelProfile(
            chat=ChatModelConfig(provider=provider, model="fake-model"),
            context_window=8000,
            max_output_tokens=512,
            system_turns=system_turns,
            reasoning_return=reasoning,
        )

    def model(self, profile: ModelProfile) -> ChatModel:
        return self._providers.chat(profile.chat)

    def agent(
        self,
        profile: ModelProfile,
        sink: Collected,
        limits: TurnLimits | None = None,
        system_prompt: Sequence[str] = SYSTEM_PROMPT,
    ) -> Agent:
        if limits is None:
            limits = TurnLimits(retry_delays_sec=(0.0, 0.0, 0.0))

        parts = AgentParts(
            profile=profile,
            limits=limits,
            compaction=CompactionSettings(),
            system_prompt=system_prompt,
            tools=(),
            store=JsonlHistoryStore(self._root),
            model=self.model(profile),
            sink=sink,
            version=VERSION,
        )

        return Agent(parts)

    async def aclose(self) -> None:
        await self._providers.aclose()


class Served:
    """Открытый агент с запущенной задачей обслуживания очереди."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self._task: asyncio.Task[None] | None = None

    async def open(self, session_id: SessionId, resume: bool) -> Session:
        session = await self.agent.open(session_id, resume)
        self._task = asyncio.create_task(self.agent.serve())

        return session

    async def close(self) -> None:
        await self.agent.close()
        if self._task is not None:
            await self._task
