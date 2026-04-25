from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass

from boba.adapters.console_sink import TextOutSink
from boba.adapters.llm.openai_terminal import OpenAITerminal, build_openai_client
from boba.adapters.prompt_providers import UserQueryProvider
from boba.adapters.raw_llm_observer import MetricsRawLLMObserver
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
from boba.domain.agent.meat.turn import InitialUserQueryMiddleware
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentConfig, AgentContext
from boba.domain.agent.prompt import PromptProvider
from boba.domain.agent.turn.reducers import (
    HistoryReducer,
    ModelReducer,
    SamplingReducer,
    SystemPromptReducer,
    ToolsReducer,
)
from boba.domain.agent.turn.spec import TurnSpec
from boba.domain.config import LLMConfig
from boba.domain.core.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.domain.core.tools import ToolsService
from boba.domain.llm.events import LLMEvent
from boba.domain.llm.models import LLMContext, SamplingParams
from boba.infra.plugins import PluginContext, PluginLoader
from boba.infra.prompts import PromptLoader


@dataclass(frozen=True)
class AgentComponents:
    agent_config: AgentConfig
    sampling: SamplingParams
    prompt_providers: Sequence[PromptProvider]
    message_service: MessageService
    tools_service: ToolsService


def build_prompt_providers(loader: PromptLoader) -> Sequence[PromptProvider]:
    """Application-level список :class:`PromptProvider` для агента.

    К провайдерам, загруженным :class:`PromptLoader` из директории
    ``BOBA_PROMPTS_DIR`` (текстовые ``.md``/``.txt`` и ``.py``-плагины),
    добавляется :class:`UserQueryProvider` — инфраструктурный провайдер,
    превращающий ``AgentRequest.query`` в USER-блок. Он не часть
    «контента» промптов и не лежит в директории.
    """
    return (*loader.providers(), UserQueryProvider())


def create_tools_service(loader: PluginLoader, ctx: PluginContext) -> ToolsService:
    """Per-request сборка :class:`ToolsService` через :class:`PluginLoader`.

    ``loader`` — application-singleton (создаётся один раз на старте,
    discovery выполнен в его конструкторе). ``ctx`` — свежий per-request,
    несёт сессионный :class:`ProjectWorkspaceShell` и application-level
    DI. Каждый плагин получает один и тот же ``ctx``.
    """
    return loader.build_tools_service(ctx)


def create_llm_source(
    llm_config: LLMConfig,
) -> StreamSource[LLMContext, LLMEvent]:
    return StreamSourceChainBuilder[LLMContext, LLMEvent]().terminal(
        OpenAITerminal(
            build_openai_client(llm_config),
            observer=MetricsRawLLMObserver(),
        )
    )


def build_turn_spec(components: AgentComponents) -> TurnSpec:
    spec = TurnSpec()
    spec.register(ModelReducer())
    spec.register(SystemPromptReducer(components.prompt_providers))
    spec.register(HistoryReducer())
    spec.register(ToolsReducer(components.tools_service))
    spec.register(SamplingReducer(components.sampling))
    return spec


def create_agent_source(
    llm_source: StreamSource[LLMContext, LLMEvent],
    components: AgentComponents,
) -> StreamSource[AgentContext, AgentEvent]:
    message_service = components.message_service
    prompt_providers = components.prompt_providers

    error_router = AgentErrorRouter()
    turn_spec = build_turn_spec(components)

    chain_builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
    chain_builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
    chain_builder.use(IterationCounterMiddleware)
    chain_builder.use(lambda inner: InitialUserQueryMiddleware(inner, prompt_providers))
    chain_builder.use(
        lambda inner: ToolExecutionMiddleware(inner, components.tools_service)
    )
    chain_builder.use(
        lambda inner: AssistantMessagePersistenceMiddleware(
            inner,
            message_service,
        )
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
) -> Agent:
    llm_source = create_llm_source(llm_config)
    source = create_agent_source(
        llm_source,
        components,
    )

    return Agent(source=source, sink=TextOutSink(sys.stdout, sys.stderr))
