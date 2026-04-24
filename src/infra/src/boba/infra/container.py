from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass

from boba.adapters.console_sink import TextOutSink
from boba.adapters.llm.openai_terminal import OpenAITerminal, build_openai_client
from boba.adapters.prompt_providers import (
    StaticPromptProvider,
    UserQueryProvider,
)
from boba.adapters.raw_llm_observer import MetricsRawLLMObserver
from boba.adapters.tool_call_reindexer import DuplicateToolCallIndexReindexer
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.meat.agent import Agent
from boba.domain.agent.meat.content_tool_call import (
    StrictJsonContentToolCallMiddleware,
)
from boba.domain.agent.meat.dialogue import AssistantMessagePersistenceMiddleware
from boba.domain.agent.meat.error_routing import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
)
from boba.domain.agent.meat.history import HistoryMiddleware
from boba.domain.agent.meat.llm import LLMInvokeMiddleware, NewLLMRequestMiddleware
from boba.domain.agent.meat.loop_control import (
    IterationCounterMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
)
from boba.domain.agent.meat.prompt import (
    SystemPromptMiddleware,
    UserPromptMiddleware,
)
from boba.domain.agent.meat.sampling import SamplingMiddleware
from boba.domain.agent.meat.tools import (
    RepeatedFormatFailureGuardMiddleware,
    RepeatedToolCallGuardMiddleware,
    ToolExecutionMiddleware,
    ToolsDefinitionMiddleware,
)
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentConfig, AgentContext
from boba.domain.agent.prompt import PromptId, PromptKind, PromptProvider
from boba.domain.config import LLMConfig
from boba.domain.core.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StreamSourceLoop,
    StreamTransformerChain,
)
from boba.domain.core.tools import ToolFactory, ToolsService
from boba.domain.llm.events import LLMEvent
from boba.domain.llm.meat.retry import RetryMiddleware
from boba.domain.llm.models import LLMContext, SamplingParams


@dataclass(frozen=True)
class AgentComponents:
    agent_config: AgentConfig
    sampling: SamplingParams
    prompt_providers: Sequence[PromptProvider]
    message_service: MessageService
    tools_service: ToolsService


def default_static_prompt_providers(
    system_prompt: str,
) -> list[PromptProvider]:
    return [
        StaticPromptProvider(
            PromptId("identity"),
            priority=0,
            content=system_prompt,
            kind=PromptKind.SYSTEM,
        ),
        UserQueryProvider(),
    ]


def create_empty_tools_service() -> ToolsService:
    factory = ToolFactory()
    service = ToolsService(factory)
    service.rebuild_catalog()
    return service


def create_llm_source(
    llm_config: LLMConfig,
    max_attempts: int = 3,
    retry_delay_seconds: float = 0.5,
) -> StreamSource[LLMContext, LLMEvent]:
    """RetryMiddleware → OpenAITerminal.

    Терминал собирается с двумя обязательными зависимостями:

    - ``observer = MetricsRawLLMObserver()`` — сводка по каждому
      запросу в лог (model / chunks / content chars / reasoning /
      tool_calls / elapsed / outcome) на wire-слое, мимо любых
      downstream-middleware.
    - ``chunk_preprocessor_factory`` — per-request
      :class:`DuplicateToolCallIndexReindexer`: чинит коллизию
      ``index`` у параллельных tool_calls от Ollama/LiteLLM.
      Новый инстанс на каждый запрос — у реиндексера своё state.
    """
    return (
        StreamSourceChainBuilder[LLMContext, LLMEvent]()
        .use(
            lambda inner: RetryMiddleware(
                inner,
                max_attempts=max_attempts,
                delay_seconds=retry_delay_seconds,
            )
        )
        .terminal(
            OpenAITerminal(
                build_openai_client(llm_config),
                observer=MetricsRawLLMObserver(),
                preprocessor=StreamTransformerChain(
                    [DuplicateToolCallIndexReindexer()],
                ),
            )
        )
    )


def create_agent_source(
    llm_source: StreamSource[LLMContext, LLMEvent],
    components: AgentComponents,
    *,
    enable_strict_content_tool_call: bool = True,
    enable_repeated_tool_call_guard: bool = True,
    enable_repeated_format_failure_guard: bool = False,
) -> StreamSource[AgentContext, AgentEvent]:
    agent_config = components.agent_config
    message_service = components.message_service
    tools_service = components.tools_service
    prompt_providers = components.prompt_providers
    sampling = components.sampling

    error_router = AgentErrorRouter(message_service)

    chain_builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
    if enable_repeated_format_failure_guard:
        chain_builder.use(
            lambda inner: RepeatedFormatFailureGuardMiddleware(
                inner,
                error_router,
                agent_config.max_consecutive_format_failures,
            )
        )
    chain_builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
    chain_builder.use(NewLLMRequestMiddleware)
    chain_builder.use(IterationCounterMiddleware)
    chain_builder.use(lambda inner: SystemPromptMiddleware(inner, prompt_providers))
    chain_builder.use(lambda inner: UserPromptMiddleware(inner, prompt_providers))
    chain_builder.use(lambda inner: HistoryMiddleware(inner, message_service))
    chain_builder.use(lambda inner: ToolsDefinitionMiddleware(inner, tools_service))
    chain_builder.use(lambda inner: SamplingMiddleware(inner, sampling))
    chain_builder.use(
        lambda inner: ToolExecutionMiddleware(
            inner,
            tools_service,
            message_service,
            error_router,
        )
    )
    if enable_repeated_tool_call_guard:
        chain_builder.use(
            lambda inner: RepeatedToolCallGuardMiddleware(
                inner,
                error_router,
                agent_config.max_consecutive_tool_calls,
            )
        )
    chain_builder.use(
        lambda inner: AssistantMessagePersistenceMiddleware(
            inner,
            message_service,
        )
    )
    if enable_strict_content_tool_call:
        chain_builder.use(StrictJsonContentToolCallMiddleware)

    chain = chain_builder.terminal(LLMInvokeMiddleware(llm_source))

    return StreamSourceLoop(
        source=chain,
        stop_if=StopOnFinished().or_(StopOnAnyFailure()),
    )


def create_agent(
    llm_config: LLMConfig,
    components: AgentComponents,
    *,
    enable_strict_content_tool_call: bool = True,
    enable_repeated_tool_call_guard: bool = True,
    enable_repeated_format_failure_guard: bool = False,
) -> Agent:
    llm_source = create_llm_source(llm_config)
    source = create_agent_source(
        llm_source,
        components,
        enable_strict_content_tool_call=enable_strict_content_tool_call,
        enable_repeated_tool_call_guard=enable_repeated_tool_call_guard,
        enable_repeated_format_failure_guard=enable_repeated_format_failure_guard,
    )
    sink = TextOutSink(sys.stdout, sys.stderr)
    return Agent(source=source, sink=sink)
