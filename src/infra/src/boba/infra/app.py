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
from boba.adapters.raw_llm_observer import (
    CompositeRawLLMObserver,
    FileContentObserver,
    FileRawLLMObserver,
    RawLLMObserver,
)
from boba.adapters.tool_providers import StaticToolSource
from boba.adapters.tools import (
    DeleteFileTool,
    EditFileTool,
    LsTool,
    ReadFileTool,
    TreeTool,
)
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.history import HistoryService
from boba.domain.agent.llm_request_factory import LLMRequestFactory
from boba.domain.agent.meat import (
    Agent,
    AgentContext,
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    AssistantMessagePersistenceMiddleware,
    HistoryReplayMiddleware,
    IterationCounterMiddleware,
    RepeatedToolCallGuardMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
    StrictJsonContentToolCallMiddleware,
    SystemPromptMiddleware,
    ToolExecutionMiddleware,
    ToolsDefinitionMiddleware,
    UserPromptMiddleware,
)
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentConfig
from boba.domain.agent.prompt import PromptId, PromptKind
from boba.domain.config import AppConfig
from boba.domain.core.patterns import (
    StreamSink,
    StreamSinkPipeline,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.domain.core.tools import (
    ToolFactory,
    ToolSourceId,
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

    def __init__(self, app_config: AppConfig, agent_config: AgentConfig) -> None:
        super().__init__()
        self._app_config = app_config
        self._agent_config = agent_config

    @provide
    def config(self) -> AppConfig:
        return self._app_config

    @provide
    def workspace_manager(self, config: AppConfig) -> WorkspaceManager:
        return FsWorkspaceManager(Path(config.workspace_base_dir))

    @provide
    def agent_config(self) -> AgentConfig:
        return self._agent_config

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
            StaticPromptProvider(
                PromptId("output_format"),
                priority=90,
                content=(
                    "Правила вывода:\n"
                    "- Для вызова инструмента используй механизм tool_calls API\n"
                    "- Для ответа пользователю пиши обычный текст на русском. "
                    "Запрещено использовать JSON.\n"
                ),
                kind=PromptKind.SYSTEM,
            ),
            UserQueryProvider(),
        ]


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
    def tool_factory(self, workspace: WorkspaceService) -> ToolFactory:
        """Агрегатор источников инструментов, собираемый на запрос.

        Per-request, потому что workspace-bound tools получают текущий
        :class:`WorkspaceService` через конструктор — ``Tool.execute`` не
        принимает ctx. Плагины/MCP-источники подключать сюда же рядом с
        builtin-пачкой.
        """
        factory = ToolFactory()
        factory.register(
            StaticToolSource(
                source_id=ToolSourceId("builtin.files"),
                priority=10,
                tools=[
                    ReadFileTool(workspace),
                    EditFileTool(workspace),
                    DeleteFileTool(workspace),
                    LsTool(workspace),
                    TreeTool(workspace),
                ],
            )
        )
        return factory

    @provide
    def tools_service(self, factory: ToolFactory) -> ToolsService:
        """Сервис инструментов поверх :class:`ToolFactory`.

        Per-request вслед за фабрикой. Маршрутизация ``ToolCall → Tool``
        через :class:`ExecutorDispatcher` внутри сервиса.
        """
        service = ToolsService(factory)
        service.rebuild_catalog()
        return service

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
    def raw_llm_observer(self, workspace: WorkspaceService) -> RawLLMObserver:
        """Комбинированный наблюдатель, пишет два файла внутри workspace:

        - ``raw_messages.md`` — полный JSON-дамп kwargs и каждого
          ChatCompletionChunk (для дебаг-разбора протокола);
        - ``raw_content.md`` — читаемый текстовый стрим: заголовок
          Request (kwargs JSON) и Response с склеенным ``delta.content``
          (чтобы быстро видеть, что модель реально сказала).
        """
        return CompositeRawLLMObserver(
            [
                FileRawLLMObserver(workspace),
                FileContentObserver(workspace),
            ]
        )

    @provide
    def agent_error_router(
        self, message_service: MessageService
    ) -> AgentErrorRouter:
        return AgentErrorRouter(message_service)

    @provide
    def agent_chain(  # noqa: PLR0913
        self,
        config: AppConfig,
        agent_config: AgentConfig,
        prompt_providers: list[PromptProvider],
        message_service: MessageService,
        tools_service: ToolsService,
        history_service: HistoryService,
        llm_request_factory: LLMRequestFactory,
        raw_observer: RawLLMObserver,
        error_router: AgentErrorRouter,
    ) -> StreamSourceLoop[AgentContext, AgentEvent]:
        builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
        builder.use(IterationCounterMiddleware)
        builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
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
            lambda inner: ToolExecutionMiddleware(
                inner, tools_service, message_service, error_router
            )
        )
        builder.use(
            lambda inner: RepeatedToolCallGuardMiddleware(
                inner,
                error_router,
                agent_config.max_consecutive_tool_calls,
            )
        )
        builder.use(LoggingLLMMiddleware)
        builder.use(lambda inner: StupidRetryLLMMiddleware(inner, max_retries=3))
        builder.use(
            lambda inner: AssistantMessagePersistenceMiddleware(inner, message_service)
        )
        builder.use(StrictJsonContentToolCallMiddleware)

        chain = builder.terminal(
            OpenAIMiddleware(config.llm, llm_request_factory, raw_observer)
        )

        stop = StopOnFinished().or_(StopOnAnyFailure())

        return StreamSourceLoop(chain, stop)

    @provide
    def agent_sink(
        self,
        history: HistoryService,
    ) -> StreamSink[AgentContext, AgentEvent]:
        return StreamSinkPipeline(
            [
                ConsoleSink(),
                HistorySink(history),
            ]
        )

    @provide
    def agent(
        self,
        source: StreamSourceLoop[AgentContext, AgentEvent],
        sink: StreamSink[AgentContext, AgentEvent],
    ) -> Agent:
        return Agent(
            source,
            sink,
        )
