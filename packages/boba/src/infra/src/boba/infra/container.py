"""Сборка agent-source и Agent без привязки к LLM-адаптеру.

LLM-source строит caller (через boba.adapter.openai.create_llm_source
или альтернативный pip-пакет), сюда приходит уже готовый
StreamSource[LLMContext, LLMEvent]. Все middleware/reducer
агентского слоя — adapter-agnostic, поэтому модуль не тянет зависимостей
на конкретные SDK.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from boba.domain.agent import (
    Agent,
    AgentConfig,
    AgentContext,
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    AgentEvent,
    AgentRequestSamplingReducer,
    AssistantMessagePersistenceMiddleware,
    DialogueWriter,
    HistoryReducer,
    IterationCounterMiddleware,
    LLMInvokeMiddleware,
    MessageService,
    ModelReducer,
    PromptProvider,
    StopOnAnyFailure,
    StopOnFinished,
    SystemPromptReducer,
    ToolExecutionMiddleware,
    ToolsReducer,
    TurnSpec,
)
from boba.domain.core.patterns import (
    StreamSink,
    StreamSource,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.domain.core.tools import ToolContext, ToolsService
from boba.domain.llm.events import LLMEvent
from boba.domain.llm.models import LLMContext


@dataclass(frozen=True)
class AgentComponents:
    agent_config: AgentConfig
    prompt_providers: Sequence[PromptProvider]
    message_service: MessageService
    tools_service: ToolsService


def build_turn_spec(components: AgentComponents) -> TurnSpec:
    spec = TurnSpec()
    spec.register(ModelReducer())
    spec.register(SystemPromptReducer(components.prompt_providers))
    spec.register(HistoryReducer())
    spec.register(ToolsReducer(components.tools_service))
    spec.register(AgentRequestSamplingReducer())
    return spec


def create_agent_source(
    llm_source: StreamSource[LLMContext, LLMEvent],
    components: AgentComponents,
    tool_ctx: ToolContext,
    writer: DialogueWriter,
) -> StreamSource[AgentContext, AgentEvent]:
    message_service = components.message_service

    error_router = AgentErrorRouter(writer)
    turn_spec = build_turn_spec(components)

    chain_builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
    chain_builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
    chain_builder.use(IterationCounterMiddleware)
    chain_builder.use(
        lambda inner: ToolExecutionMiddleware(
            inner, components.tools_service, tool_ctx, writer
        )
    )
    chain_builder.use(
        lambda inner: AssistantMessagePersistenceMiddleware(inner, writer)
    )
    chain = chain_builder.terminal(
        LLMInvokeMiddleware(llm_source, turn_spec, message_service)
    )

    return StreamSourceLoop(
        source=chain,
        stop_if=StopOnFinished().or_(StopOnAnyFailure()),
    )


def create_agent(
    llm_source: StreamSource[LLMContext, LLMEvent],
    components: AgentComponents,
    tool_ctx: ToolContext,
    sink: StreamSink[AgentContext, AgentEvent],
) -> Agent:
    writer = DialogueWriter(components.message_service)
    source = create_agent_source(
        llm_source,
        components,
        tool_ctx,
        writer,
    )

    return Agent(source=source, sink=sink, writer=writer)
