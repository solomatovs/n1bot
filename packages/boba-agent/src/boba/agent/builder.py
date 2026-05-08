"""AgentBuilder — fluent-фасад для сборки Agent с разумными дефолтами."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Self

from boba.agent.events import AgentEvent
from boba.agent.messages import InMemoryMessageService, MessageService, MessageWriter
from boba.agent.middleware import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    AssistantMessagePersistenceMiddleware,
    IterationCounterMiddleware,
    LLMInvokeMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
    ToolExecutionMiddleware,
)
from boba.agent.orchestrator import Agent, AgentConfig, AgentContext
from boba.agent.prompt import PromptProvider
from boba.llm.events import LLMEvent
from boba.llm.models import LLMContext
from boba.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.tools.domain import ToolContext, ToolResultVisitor
from boba.tools.framework import ToolsService


class AgentBuilder:
    """Fluent-фасад: собирает Agent с дефолтной middleware-цепью."""

    def __init__(self) -> None:
        self._llm_source: StreamSource[LLMContext, LLMEvent] | None = None
        self._tools_service: ToolsService | None = None
        self._tool_result_visitor: ToolResultVisitor[str] | None = None
        self._message_service: MessageService | None = None
        self._prompt_providers: list[PromptProvider] = []
        self._agent_config: AgentConfig = AgentConfig()

    def with_llm(self, source: StreamSource[LLMContext, LLMEvent]) -> Self:
        """LLM-источник (обязательно)."""
        self._llm_source = source
        return self

    def with_tools(self, service: ToolsService) -> Self:
        """Реестр инструментов (обязательно)."""
        self._tools_service = service
        return self

    def with_tool_result_visitor(self, visitor: ToolResultVisitor[str]) -> Self:
        """Сериализация результата tool'а в строку для LLM (обязательно)."""
        self._tool_result_visitor = visitor
        return self

    def with_messages(self, service: MessageService) -> Self:
        """Хранилище истории; дефолт — InMemoryMessageService()."""
        self._message_service = service
        return self

    def with_prompts(self, providers: Iterable[PromptProvider]) -> Self:
        """Провайдеры system-prompt блоков; дефолт — пусто."""
        self._prompt_providers = list(providers)
        return self

    def with_config(self, config: AgentConfig) -> Self:
        """Лимиты агентского лупа; дефолт — AgentConfig()."""
        self._agent_config = config
        return self

    def agent_config(self) -> AgentConfig:
        """Текущий AgentConfig (нужен для Agent.run)."""
        return self._agent_config

    def build(
        self,
        *,
        tool_ctx: ToolContext,
    ) -> Agent:
        """Собрать Agent. tool_ctx — per-call DI, не часть билдера."""
        if self._llm_source is None:
            msg = "AgentBuilder.build: .with_llm(...) обязателен до .build()"
            raise ValueError(msg)
        if self._tools_service is None:
            msg = "AgentBuilder.build: .with_tools(...) обязателен до .build()"
            raise ValueError(msg)
        if self._tool_result_visitor is None:
            msg = (
                "AgentBuilder.build: .with_tool_result_visitor(...) "
                "обязателен до .build()"
            )
            raise ValueError(msg)

        message_service = self._message_service or InMemoryMessageService()

        chain = self._build_chain(
            llm_source=self._llm_source,
            tools_service=self._tools_service,
            visitor=self._tool_result_visitor,
            prompt_providers=self._prompt_providers,
            message_service=message_service,
            tool_ctx=tool_ctx,
        )
        source = StreamSourceLoop(
            source=chain,
            stop_if=StopOnFinished().or_(StopOnAnyFailure()),
        )
        return Agent(source=source, writer=message_service, reader=message_service)

    @staticmethod
    def _build_chain(  # noqa: PLR0913
        *,
        llm_source: StreamSource[LLMContext, LLMEvent],
        tools_service: ToolsService,
        visitor: ToolResultVisitor[str],
        prompt_providers: list[PromptProvider],
        message_service: MessageService,
        tool_ctx: ToolContext,
    ) -> StreamSource[AgentContext, AgentEvent]:
        writer: MessageWriter = message_service
        error_router = AgentErrorRouter(writer)
        builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
        builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
        builder.use(IterationCounterMiddleware)
        builder.use(
            lambda inner: ToolExecutionMiddleware(
                inner,
                tools_service,
                tool_ctx,
                writer,
                visitor,
            ),
        )
        builder.use(
            lambda inner: AssistantMessagePersistenceMiddleware(inner, writer),
        )
        return builder.terminal(
            LLMInvokeMiddleware(
                llm_source,
                prompt_providers,
                tools_service,
                message_service,
            ),
        )
