"""Dishka-провайдеры приложения."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from dishka import Provider, Scope, from_context, provide

from boba.adapters.fs_workspace import FsWorkspaceManager
from boba.adapters.in_memory_messages import InMemoryMessageService
from boba.adapters.openai_completion import OpenAICompletionService
from boba.domain.agent.loop import AgentLoop
from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig
from boba.adapters.openai_completion import (
    LoggingLLMMiddleware,
    StupedRetryLLMMiddleware,
)
from boba.domain.core.messages import MessageService
from boba.domain.core.promt import PromptId, SystemPromptService, UserPromptService
from boba.adapters.prompt_providers import (
    EnvironmentPromptProvider,
    GitPromptProvider,
    StaticPromptProvider,
)
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentContext
from boba.domain.agent.stages import GenerateStage, SystemMessageStage, UserMessageStage
from boba.domain.core.stream import Pipeline
from boba.domain.core.workspace import WorkspaceManager, WorkspaceService
from boba.domain.llm.llm import LLMCompletionService


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
    def llm_source(self, config: AppConfig) -> LLMCompletionService:
        llm: LLMCompletionService = OpenAICompletionService(config.llm)
        llm = StupedRetryLLMMiddleware(llm, max_retries=3)
        llm = LoggingLLMMiddleware(llm)
        return llm

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
        return UserPromptService()


class RequestProvider(Provider):
    """Per-request: workspace service, message service."""

    scope = Scope.REQUEST

    workspace_id = from_context(provides=UUID | None, scope=Scope.REQUEST)

    @provide
    def workspace_service(
        self,
        workspace_id: UUID | None,
        manager: WorkspaceManager,
    ) -> WorkspaceService:
        return manager.get_or_create(workspace_id)

    @provide
    def message_service(self) -> MessageService:
        return InMemoryMessageService()

    @provide
    def agent_pipeline(
        self,
        system_prompt_service: SystemPromptService,
        user_prompt_service: UserPromptService,
        message_service: MessageService,
        llm: LLMCompletionService,
    ) -> Pipeline[AgentContext, AgentEvent]:
        return Pipeline(
            [
                SystemMessageStage(system_prompt_service, message_service),
                UserMessageStage(user_prompt_service, message_service),
                GenerateStage(llm, message_service),
            ]
        )

    @provide
    def agent_loop(
        self,
        agent_config: AgentConfig,
        pipeline: Pipeline[AgentContext, AgentEvent],
    ) -> AgentLoop:
        return AgentLoop(
            config=agent_config,
            pipeline=pipeline,
        )
