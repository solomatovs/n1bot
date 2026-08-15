"""Контракт трасера: сбой ленты не должен ломать учёт прогонов langchain.

Ход идёт как в проде: langgraph поверх фейкового провайдера, стрим сообщениями,
колбэки доставляет настоящий callback-менеджер langchain. Ленту роняет эмиттер
chainlit, у которого умер сокет вкладки, — так же ведёт себя отправка шага при
недоступном хранилище элементов.

Инвариант один: run_map трасера ведётся независимо от отрисовки. Иначе сбой
отрисовки старта инструмента отдаётся каскадом — on_tool_end падает с
TracerException «No indexed run ID», и пользователь получает в чат вторую
ошибку, за которой нет ни одной настоящей причины.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest
from chainlit.context import ChainlitContext, context_var
from chainlit.emitter import BaseChainlitEmitter
from chainlit.session import HTTPSession
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import SecretStr
from ui.fake_llm import FakeLlmApp, ScenarioName

from boba.chainlit.agent.chat_model import ReasoningChatOpenAI
from boba.chainlit.chat.agent_tracer import AgentTracer
from boba.chainlit.rendering.chat_view import ChatView, LiveSink

pytestmark = pytest.mark.anyio

THREAD = "7f0d2d1c-6a63-4d0e-9a0e-0d7d6c9a1f42"
USER = "7"
CALL_ID = "call_stream_logs"
TOOL_NAME = "stream_logs_usage"


class DeadSocketEmitter(BaseChainlitEmitter):
    """Эмиттер мёртвой вкладки: отправка шага срывается, остальное молчит."""

    async def send_step(self, step_dict: Any) -> None:
        msg = "socket is gone"
        raise ConnectionError(msg)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
async def chainlit_context() -> AsyncIterator[None]:
    """Контекст хода с эмиттером мёртвой вкладки: лента недоступна."""
    session = HTTPSession(id="probe-session", client_type="webapp", thread_id=THREAD)
    emitter = DeadSocketEmitter(session)
    token = context_var.set(ChainlitContext(session, emitter))

    yield

    context_var.reset(token)


@pytest.fixture
async def provider() -> AsyncIterator[httpx.AsyncClient]:
    """Фейковый OpenAI-совместимый провайдер прямо в процессе теста."""
    app = FakeLlmApp(token_delay_sec=0.0).asgi()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fake-llm",
    )

    try:
        yield client
    finally:
        await client.aclose()


class TestTracerRunIndex:
    """Учёт прогонов трасера не зависит от того, дошла ли лента до вкладки."""

    LOST_RUN: ClassVar[str] = "No indexed run ID"
    """След каскада в логах: закрытие прогона не нашло его старта."""

    @staticmethod
    def _tools() -> list[BaseTool]:
        def usage() -> str:
            return "journal usage"

        def cleanup(thread_id: str) -> str:
            msg = f"unknown thread: {thread_id}"
            raise ValueError(msg)

        return [
            StructuredTool.from_function(
                func=usage,
                name=TOOL_NAME,
                description="Journal usage",
            ),
            StructuredTool.from_function(
                func=cleanup,
                name="stream_logs_cleanup",
                description="Purge the journal of a thread",
            ),
        ]

    @staticmethod
    def _tracer() -> AgentTracer:
        view = ChatView(THREAD, LiveSink(), user_name="tester")
        view.begin_turn("turn-1")

        return AgentTracer(view)

    async def _turn(
        self, provider: httpx.AsyncClient, scenario: ScenarioName
    ) -> AgentTracer:
        """Ход как в проде: агент langgraph, стрим сообщениями, живой трасер."""
        chat = ReasoningChatOpenAI(
            http_async_client=provider,
            model="fake-model",
            base_url="http://fake-llm/v1",
            api_key=SecretStr("fake-key"),
        )
        tracer = self._tracer()
        agent = create_agent(
            model=chat,
            tools=self._tools(),
            system_prompt="test agent",
        )
        stream = agent.astream(
            {"messages": [HumanMessage(content=scenario.value)]},
            stream_mode="messages",
            config={"callbacks": [tracer]},
        )

        async for _chunk in stream:
            pass

        return tracer

    @classmethod
    def _lost_runs(cls, caplog: pytest.LogCaptureFixture) -> list[str]:
        found: list[str] = []
        for record in caplog.records:
            message = record.getMessage()
            if cls.LOST_RUN not in message:
                continue

            found.append(f"{record.name}: {message}")

        return found

    async def test_tool_run_is_indexed_when_step_render_fails(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Прогон инструмента учтён, даже если шаг не удалось отправить в ленту."""
        tracer = self._tracer()
        run_id = uuid4()

        with caplog.at_level(logging.DEBUG):
            await tracer.on_tool_start(
                {"name": TOOL_NAME},
                "{}",
                run_id=run_id,
                inputs={},
                tool_call_id=CALL_ID,
            )

        assert str(run_id) in tracer.run_map

    async def test_failed_step_render_does_not_break_tool_end(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Закрытие прогона не срывается вслед за упавшей отрисовкой старта."""
        tracer = self._tracer()
        run_id = uuid4()

        with caplog.at_level(logging.DEBUG):
            await tracer.on_tool_start(
                {"name": TOOL_NAME},
                "{}",
                run_id=run_id,
                inputs={},
                tool_call_id=CALL_ID,
            )
            await tracer.on_tool_end(
                ToolMessage(content="hi", tool_call_id=CALL_ID),
                run_id=run_id,
            )

        assert self._lost_runs(caplog) == []

    async def test_turn_with_tool_does_not_cascade(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Полный ход с инструментом: недоступная лента даёт ошибки отрисовки,
        но ни одной ошибки об утерянном прогоне."""
        with caplog.at_level(logging.DEBUG):
            await self._turn(provider, ScenarioName.TOOL)

        assert self._lost_runs(caplog) == []

    async def test_failed_tool_turn_does_not_cascade(
        self, provider: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Аварийный инструмент: on_tool_error тоже обязан найти свой прогон."""
        with (
            caplog.at_level(logging.DEBUG),
            pytest.raises(ValueError, match="unknown thread"),
        ):
            await self._turn(provider, ScenarioName.TOOL_ERROR)

        assert self._lost_runs(caplog) == []
