"""Dishka-провайдеры приложения."""

from __future__ import annotations

from pathlib import Path

from dishka import Provider, Scope, from_context, provide

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
    StaticPromptProvider,
    UserQueryProvider,
)
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.meat import (
    Agent,
    AgentContext,
    IterationCounterMiddleware,
    StopOnFinished,
    StopOnMaxIterations,
    SystemMessageMiddleware,
    UserMessageMiddleware,
)
from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig
from boba.domain.core.history import HistoryService
from boba.domain.core.messages import MessageService
from boba.domain.core.patterns import Stream, StreamLoop, StreamPipeline
from boba.domain.core.promt import PromptId, SystemPromptService, UserPromptService
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
    def system_prompt_service(self) -> SystemPromptService:
        svc = SystemPromptService()
        svc.register(
            StaticPromptProvider(
                PromptId("identity"),
                priority=0,
                content="Ты — ассистент Boba. Отвечай кратко и по делу.",
            )
        )
        svc.register(EnvironmentPromptProvider())
        svc.register(GitPromptProvider())
        return svc

    @provide
    def user_prompt_service(self) -> UserPromptService:
        svc = UserPromptService()
        svc.register(UserQueryProvider())
        return svc

    @provide
    def tool_factory(self) -> ToolFactory:
        """Агрегатор источников инструментов.

        Сюда регистрируются ``ToolSource``-ы (builtin-пачка, MCP-клиент,
        plugin-директория). При ``build(None)`` отдаёт плоский список Tool.
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
    def history_service(
        self,
        workspace: WorkspaceService,
    ) -> HistoryService:
        return JsonLinesHistoryService(workspace)

    @provide
    def agent_chain(
        self,
        config: AppConfig,
        system_prompt_service: SystemPromptService,
        user_prompt_service: UserPromptService,
        message_service: MessageService,
        tools_service: ToolsService,
    ) -> StreamLoop[AgentContext, None, AgentEvent]:
        chain = OpenAIMiddleware(config.llm, message_service, tools_service)
        chain = StupidRetryLLMMiddleware(chain, max_retries=3)
        chain = LoggingLLMMiddleware(chain)
        chain = UserMessageMiddleware(chain, user_prompt_service, message_service)
        chain = SystemMessageMiddleware(chain, system_prompt_service, message_service)
        chain = IterationCounterMiddleware(chain)

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
