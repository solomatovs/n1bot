from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from boba.adapters.llm.openai_terminal import OpenAITerminal, build_openai_client
from boba.adapters.raw_llm_observer import RawLLMObserver
from boba.domain.agent.dialogue_writer import DialogueWriter
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.meat.agent import Agent
from boba.domain.agent.meat.dialogue import AssistantMessagePersistenceMiddleware
from boba.domain.agent.meat.error_routing import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
)
from boba.domain.agent.meat.llm import LLMInvokeMiddleware
from boba.domain.agent.meat.loop_control import (
    IterationCounterMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
)
from boba.domain.agent.meat.tools import (
    ToolExecutionMiddleware,
)
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentConfig, AgentContext
from boba.domain.agent.prompt import PromptProvider
from boba.domain.agent.turn.reducers import (
    AgentRequestSamplingReducer,
    HistoryReducer,
    ModelReducer,
    SystemPromptReducer,
    ToolsReducer,
)
from boba.domain.agent.turn.spec import TurnSpec
from boba.domain.config import LLMConfig
from boba.domain.core.patterns import (
    StreamSink,
    StreamSource,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.domain.core.tools import ToolContext, ToolsService
from boba.domain.llm.events import LLMEvent
from boba.domain.llm.models import LLMContext
from boba.infra.prompt_loader import PromptLoader


@dataclass(frozen=True)
class AgentComponents:
    agent_config: AgentConfig
    prompt_providers: Sequence[PromptProvider]
    message_service: MessageService
    tools_service: ToolsService


def build_prompt_providers(loader: PromptLoader) -> Sequence[PromptProvider]:
    """Application-level список :class:`PromptProvider` (system-prompt).

    Все провайдеры идут от :class:`PromptLoader` из директории
    ``BOBA_PROMPTS_DIR`` (текстовые ``.md``/``.txt``-блоки). USER-блок
    через PromptFactory не собирается — пользовательское сообщение
    приходит уже отформатированным в ``AgentRequest.query``.
    """
    return loader.prompt_providers()


def create_llm_source(
    llm_config: LLMConfig,
    observer: RawLLMObserver,
) -> StreamSource[LLMContext, LLMEvent]:
    return StreamSourceChainBuilder[LLMContext, LLMEvent]().terminal(
        OpenAITerminal(
            build_openai_client(llm_config),
            observer=observer,
        )
    )


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
    llm_config: LLMConfig,
    components: AgentComponents,
    tool_ctx: ToolContext,
    observer: RawLLMObserver,
    sink: StreamSink[AgentContext, AgentEvent],
) -> Agent:
    writer = DialogueWriter(components.message_service)
    llm_source = create_llm_source(llm_config, observer)
    source = create_agent_source(
        llm_source,
        components,
        tool_ctx,
        writer,
    )

    return Agent(source=source, sink=sink, writer=writer)
