"""Dishka-провайдеры приложения."""

from __future__ import annotations

from pathlib import Path

from dishka import Provider, Scope, from_context, provide

from boba.adapters.aggregating_llm_request_factory import AggregatingLLMRequestFactory
from boba.adapters.console_sink import ConsoleSink
from boba.adapters.fs_workspace import FsWorkspaceManager
from boba.adapters.history_sink import HistorySink
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.adapters.jsonl_history import JsonLinesHistoryService
from boba.adapters.openai_completion import (
    LoggingLLMMiddleware,
    OpenAIMiddleware,
    StupidRetryLLMMiddleware,
)
from boba.adapters.prompt_providers import (
    EnvironmentPromptProvider,
    GitPromptProvider,
    PromptProvider,
    StaticPromptProvider,
    UserQueryProvider,
)
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.llm_request_factory import LLMRequestFactory
from boba.domain.agent.meat import (
    Agent,
    AgentContext,
    AssistantMessagePersistenceMiddleware,
    HistoryReplayMiddleware,
    IterationCounterMiddleware,
    StopOnFinished,
    StopOnMaxIterations,
    SystemPromptMiddleware,
    ToolExecutionMiddleware,
    ToolsDefinitionMiddleware,
    UserPromptMiddleware,
)
from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig
from boba.domain.core.history import HistoryService
from boba.domain.core.messages import MessageService
from boba.domain.core.patterns import (
    Stream,
    StreamChainBuilder,
    StreamLoop,
    StreamPipeline,
)
from boba.domain.core.promt import PromptId, PromptKind
from boba.domain.core.tools import (
    ToolFactory,
    ToolsService,
)
from boba.domain.core.workspace import (
    WorkspaceId,
    WorkspaceManager,
    WorkspaceService,
)


class AppProvider(Provider):
    """Singleton-сервисы: конфигурация."""

    scope = Scope.APP

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    @provide
    def config(self) -> AppConfig:
        return self._config

    @provide
    def workspace_manager(self, config: AppConfig) -> WorkspaceManager:
        return FsWorkspaceManager(Path(config.workspace_base_dir))

    @provide
    def agent_config(self, config: AppConfig) -> AgentConfig:
        return config.agent

    @provide
    def prompt_providers(self) -> list[PromptProvider]:
        return [
            StaticPromptProvider(
                PromptId("identity"),
                priority=0,
                content="Ты — ассистент Boba. Отвечай кратко и по делу.",
                kind=PromptKind.SYSTEM,
            ),
            EnvironmentPromptProvider(),
            GitPromptProvider(),
            UserQueryProvider(),
        ]

    @provide
    def tool_factory(self) -> ToolFactory:
        """Агрегатор источников инструментов.

        Сюда регистрируются ``ToolSource``-ы (builtin-пачка, MCP-клиент,
        plugin-директория). При ``build()`` отдаёт плоский список Tool.
        Пока пусто — источники добавятся при появлении первых инструментов.
        """
        return ToolFactory()

    @provide
    def tools_service(self, factory: ToolFactory) -> ToolsService:
        """Сервис инструментов поверх :class:`ToolFactory`.

        Собирает каталог из текущих источников фабрики один раз при
        wiring'е через :meth:`ToolsService.rebuild_catalog`. Если позже
        источники меняются в рантайме (подключился MCP, загрузился плагин)
        — нужно вызвать ``rebuild_catalog()`` повторно; сам DI-провайдер
        этого не делает.

        Маршрутизация ``ToolCall → Tool`` — через внутренний
        :class:`ExecutorDispatcher` сервиса. Если понадобится что-то
        поверх (retry, conditional routing) — оборачивай сервис снаружи
        как обычный ``Executor[None, ToolCall, ToolResult]``.
        """
        service = ToolsService(factory)
        service.rebuild_catalog()
        return service


class RequestProvider(Provider):
    """Per-request сервисы."""

    scope = Scope.REQUEST

    workspace_id = from_context(provides=WorkspaceId | None, scope=Scope.REQUEST)

    @provide
    def workspace_service(
        self,
        workspace_id: WorkspaceId | None,
        manager: WorkspaceManager,
    ) -> WorkspaceService:
        if workspace_id is None:
            return manager.create()
        return manager.get(workspace_id)

    @provide
    def message_service(self) -> MessageService:
        return InMemoryMessageService()

    @provide
    def llm_request_factory(
        self,
        message_service: MessageService,
    ) -> LLMRequestFactory:
        return AggregatingLLMRequestFactory(message_service)

    @provide
    def history_service(
        self,
        workspace: WorkspaceService,
    ) -> HistoryService:
        return JsonLinesHistoryService(workspace)

    @provide
    def agent_chain(
        self,
        config: AppConfig,
        prompt_providers: list[PromptProvider],
        message_service: MessageService,
        tools_service: ToolsService,
        history_service: HistoryService,
        llm_request_factory: LLMRequestFactory,
    ) -> StreamLoop[AgentContext, None, AgentEvent]:
        builder = StreamChainBuilder[AgentContext, None, AgentEvent]()
        builder.use(IterationCounterMiddleware)
        builder.use(
            lambda inner: HistoryReplayMiddleware(
                inner, history_service, message_service
            )
        )
        builder.use(lambda inner: SystemPromptMiddleware(inner, prompt_providers))
        builder.use(
            lambda inner: UserPromptMiddleware(inner, prompt_providers, message_service)
        )
        builder.use(lambda inner: ToolsDefinitionMiddleware(inner, tools_service))
        builder.use(
            lambda inner: ToolExecutionMiddleware(inner, tools_service, message_service)
        )
        builder.use(LoggingLLMMiddleware)
        builder.use(lambda inner: StupidRetryLLMMiddleware(inner, max_retries=3))
        builder.use(
            lambda inner: AssistantMessagePersistenceMiddleware(inner, message_service)
        )

        chain = builder.terminal(OpenAIMiddleware(config.llm, llm_request_factory))

        stop = StopOnFinished().or_(StopOnMaxIterations())

        return StreamLoop(chain, stop)

    @provide
    def agent_sink(
        self,
        history: HistoryService,
    ) -> Stream[AgentContext, AgentEvent, None]:
        return StreamPipeline(
            [
                ConsoleSink(),
                HistorySink(history),
            ]
        )

    @provide
    def agent(
        self,
        source: StreamLoop[AgentContext, None, AgentEvent],
        sink: Stream[AgentContext, AgentEvent, None],
    ) -> Agent:
        return Agent(
            source,
            sink,
        )
