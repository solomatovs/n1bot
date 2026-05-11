"""AgentBuilder — fluent-фасад для сборки Agent с разумными дефолтами."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar, Self

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
from boba.tools.domain import ToolContext, ToolResultVisitor, ToolSourceId
from boba.tools.framework import (
    StaticToolSource,
    ToolDecoratorFactory,
    ToolsService,
)


class AgentBuilder:
    """Fluent-фасад: собирает Agent с дефолтной middleware-цепью."""

    INLINE_SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("inline")

    def __init__(self) -> None:
        self._llm_source: StreamSource[LLMContext, LLMEvent] | None = None
        self._tools_service: ToolsService | None = None
        self._inline_factories: list[ToolDecoratorFactory] = []
        self._tool_result_visitor: ToolResultVisitor[str] | None = None
        self._message_service: MessageService | None = None
        self._prompt_providers: list[PromptProvider] = []
        self._agent_config: AgentConfig = AgentConfig()

    def with_llm(self, source: StreamSource[LLMContext, LLMEvent]) -> Self:
        """LLM-источник (обязательно)."""
        self._llm_source = source
        return self

    def with_tools(self, service: ToolsService) -> Self:
        """Готовый реестр инструментов; mutually-exclusive с `use_tools(...)`."""
        self._tools_service = service
        return self

    def use_tools(self, factories: Iterable[ToolDecoratorFactory]) -> Self:
        """Добавить `@tool`-функции под общим source_id `INLINE_SOURCE_ID`."""
        self._inline_factories.extend(factories)
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
        if self._tool_result_visitor is None:
            msg = (
                "AgentBuilder.build: .with_tool_result_visitor(...) "
                "обязателен до .build()"
            )
            raise ValueError(msg)

        tools_service = self._resolve_tools_service()

        message_service = self._message_service or InMemoryMessageService()

        chain = self._build_chain(
            llm_source=self._llm_source,
            tools_service=tools_service,
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

    def _resolve_tools_service(self) -> ToolsService:
        """Выбрать готовый `ToolsService` или собрать из накопленных источников."""
        if self._tools_service is not None and self._inline_factories:
            msg = (
                "AgentBuilder.build: .with_tools(...) и .use_tools(...) "
                "взаимоисключающие — задан один из путей"
            )
            raise ValueError(msg)
        if self._tools_service is not None:
            return self._tools_service
        if not self._inline_factories:
            msg = (
                "AgentBuilder.build: задайте инструменты через "
                ".with_tools(...) или .use_tools(...)"
            )
            raise ValueError(msg)
        sid = self.INLINE_SOURCE_ID
        return ToolsService.from_sources(
            [StaticToolSource(sid, [f.build(sid) for f in self._inline_factories])],
        )

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
