"""Dishka-провайдеры приложения."""

from __future__ import annotations

from collections.abc import Iterator

from dishka import Provider, Scope, from_context, provide

from boba.adapters.aggregating_llm_request_factory import AggregatingLLMRequestFactory
from boba.adapters.console_sink import ConsoleSink
from boba.adapters.fs_workspace import (
    FsSystemWorkspaceManager,
    FsTmpWorkspaceManager,
    FsUserWorkspaceManager,
)
from boba.adapters.jsonl_messages import JsonLinesMessageService
from boba.adapters.openai_completion import (
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
    MetricsRawLLMObserver,
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
from boba.domain.agent.llm_request_factory import LLMRequestFactory
from boba.domain.agent.meat import (
    Agent,
    AgentContext,
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    AssistantMessagePersistenceMiddleware,
    IterationCounterMiddleware,
    RepeatedFormatFailureGuardMiddleware,
    RepeatedToolCallGuardMiddleware,
    SamplingMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
    StrictJsonContentToolCallMiddleware,
    SystemPromptMiddleware,
    ToolExecutionMiddleware,
    ToolsDefinitionMiddleware,
    UserPromptMiddleware,
)
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentConfig, LLMRequestDefaults
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
    SystemWorkspaceManager,
    SystemWorkspaceService,
    TmpWorkspaceManager,
    TmpWorkspaceService,
    UserWorkspaceManager,
    UserWorkspaceService,
    WorkspaceId,
)


class AppProvider(Provider):
    """Singleton-сервисы: конфигурация."""

    scope = Scope.APP

    def __init__(
        self,
        app_config: AppConfig,
        agent_config: AgentConfig,
        llm_defaults: LLMRequestDefaults,
    ) -> None:
        super().__init__()
        self._app_config = app_config
        self._agent_config = agent_config
        self._llm_defaults = llm_defaults

    @provide
    def config(self) -> AppConfig:
        return self._app_config

    @provide
    def user_workspace_manager(self, config: AppConfig) -> UserWorkspaceManager:
        return FsUserWorkspaceManager(
            config.workspaces.root(),
            config.workspaces.user_subdir,
        )

    @provide
    def system_workspace_manager(self, config: AppConfig) -> SystemWorkspaceManager:
        return FsSystemWorkspaceManager(
            config.workspaces.root(),
            config.workspaces.system_subdir,
        )

    @provide
    def tmp_workspace_manager(self, config: AppConfig) -> TmpWorkspaceManager:
        return FsTmpWorkspaceManager(
            config.workspaces.root(),
            config.workspaces.tmp_subdir,
        )

    @provide
    def agent_config(self) -> AgentConfig:
        return self._agent_config

    @provide
    def llm_defaults(self) -> LLMRequestDefaults:
        return self._llm_defaults

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
                    "Формат ответа.\n"
                    "\n"
                    "У тебя два режима вывода — выбирай ровно один на "
                    "каждый ответ:\n"
                    "\n"
                    "1. Вызов инструмента. Заполняй поле tool_calls в "
                    "ответе API. Поле content оставляй пустым. НЕ пиши "
                    "вызов инструмента как JSON внутри content — это "
                    "ошибка, инструмент не будет выполнен.\n"
                    "\n"
                    "2. Ответ пользователю. Пиши связный прозаический "
                    "текст на русском языке. Без фигурных скобок, без "
                    "ключей в кавычках, без JSON-объектов. "
                    "Просто человеческая речь.\n"
                    "\n"
                    "Пример правильного ответа пользователю:\n"
                    "  В workspace три файла: raw_content.md, "
                    "history.jsonl, raw_messages.md. Первый хранит "
                    "читаемый стрим ответов модели, второй — журнал "
                    "событий, третий — сырой дамп запросов и чанков.\n"
                ),
                kind=PromptKind.SYSTEM,
            ),
            UserQueryProvider(),
        ]


class RequestProvider(Provider):
    """Per-request сервисы."""

    scope = Scope.REQUEST

    workspace_id = from_context(provides=WorkspaceId, scope=Scope.REQUEST)

    @provide
    def user_workspace(
        self,
        workspace_id: WorkspaceId,
        manager: UserWorkspaceManager,
    ) -> UserWorkspaceService:
        svc = manager.get_or_create(workspace_id)
        assert isinstance(svc, UserWorkspaceService)
        return svc

    @provide
    def system_workspace(
        self,
        workspace_id: WorkspaceId,
        manager: SystemWorkspaceManager,
    ) -> SystemWorkspaceService:
        svc = manager.get_or_create(workspace_id)
        assert isinstance(svc, SystemWorkspaceService)
        return svc

    @provide
    def tmp_workspace(
        self,
        workspace_id: WorkspaceId,
        manager: TmpWorkspaceManager,
    ) -> Iterator[TmpWorkspaceService]:
        """Tmp workspace с чисткой на выходе из scope.

        Generator-паттерн dishka: всё, что между ``yield`` и ``finally``,
        выполнится при закрытии request scope — даже если внутри упадёт
        исключение. Ошибка cleanup'а пробрасывается наружу: потеря tmp-
        директории — сигнал, который пользователь должен увидеть.
        """
        svc = manager.get_or_create(workspace_id)
        assert isinstance(svc, TmpWorkspaceService)
        try:
            yield svc
        finally:
            manager.delete(workspace_id)

    @provide
    def tool_factory(
        self,
        user_workspace: UserWorkspaceService,
    ) -> ToolFactory:
        """Агрегатор источников инструментов, собираемый на запрос.

        Per-request, потому что file-tools получают per-request сервис
        workspace'а. Плагины/MCP-источники подключать сюда же рядом с
        builtin-пачкой.

        Builtin file-tools ходят только в :class:`UserWorkspaceService` —
        системный/tmp пользователю писать нельзя.
        """
        tools = [
            ReadFileTool(user_workspace),
            EditFileTool(user_workspace),
            DeleteFileTool(user_workspace),
            LsTool(user_workspace),
            TreeTool(user_workspace),
        ]

        factory = ToolFactory()
        factory.register(
            StaticToolSource(
                source_id=ToolSourceId("builtin.files"),
                priority=10,
                tools=tools,
            )
        )
        return factory

    @provide
    def tools_service(self, factory: ToolFactory) -> ToolsService:
        """Сервис инструментов поверх :class:`ToolFactory`.

        Per-request вслед за фабрикой. Диспатч ``ToolCall → Tool`` по
        ``tool_id`` встроен в :meth:`ToolsService.execute`.
        """
        service = ToolsService(factory)
        service.rebuild_catalog()
        return service

    @provide
    def message_service(
        self,
        workspace: SystemWorkspaceService,
    ) -> MessageService:
        """Persistent-реализация сервиса сообщений.

        При создании инстанса читает ``messages.jsonl`` из system-workspace
        и наполняет in-memory список — восстановление диалога между
        запусками живёт здесь, а не в отдельном replay-middleware. На
        ``add`` одновременно дописывает строку JSON в файл и пушит в список.
        """
        return JsonLinesMessageService(workspace)

    @provide
    def llm_request_factory(
        self,
        message_service: MessageService,
    ) -> LLMRequestFactory:
        return AggregatingLLMRequestFactory(message_service)

    @provide
    def raw_llm_observer(self, workspace: SystemWorkspaceService) -> RawLLMObserver:
        """Комбинированный наблюдатель, пишет два файла внутри workspace и
        логирует сводку в консоль:

        - ``raw_messages.md`` — полный JSON-дамп kwargs и каждого
          ChatCompletionChunk (для дебаг-разбора протокола);
        - ``raw_content.md`` — читаемый текстовый стрим: заголовок
          Request (kwargs JSON) и Response с склеенным ``delta.content``
          (чтобы быстро видеть, что модель реально сказала);
        - :class:`MetricsRawLLMObserver` — одна строка ``LLM done ...`` в
          logger по завершении запроса: считает по сырым chunk-ам, поэтому
          цифры не искажаются middleware, переупаковывающими content в
          tool-calls.
        """
        return CompositeRawLLMObserver(
            [
                FileRawLLMObserver(workspace),
                FileContentObserver(workspace),
                MetricsRawLLMObserver(),
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
        llm_defaults: LLMRequestDefaults,
        prompt_providers: list[PromptProvider],
        message_service: MessageService,
        tools_service: ToolsService,
        llm_request_factory: LLMRequestFactory,
        raw_observer: RawLLMObserver,
        error_router: AgentErrorRouter,
    ) -> StreamSourceLoop[AgentContext, AgentEvent]:
        builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
        builder.use(IterationCounterMiddleware)
        builder.use(
            lambda inner: RepeatedFormatFailureGuardMiddleware(
                inner,
                error_router,
                agent_config.max_consecutive_format_failures,
            )
        )
        builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
        builder.use(lambda inner: SystemPromptMiddleware(inner, prompt_providers))
        builder.use(
            lambda inner: UserPromptMiddleware(inner, prompt_providers, message_service)
        )
        builder.use(lambda inner: ToolsDefinitionMiddleware(inner, tools_service))
        builder.use(lambda inner: SamplingMiddleware(inner, llm_defaults))
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
    def agent_sink(self) -> StreamSink[AgentContext, AgentEvent]:
        return StreamSinkPipeline([ConsoleSink()])

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
